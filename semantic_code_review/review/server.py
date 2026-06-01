"""Ephemeral localhost HTTP server coordinating reviewer comments.

Spawned by ``scr review``; dies when the viewer POSTs /exit or when the
idle timeout elapses. No external deps — stdlib ``http.server``.

The server also publishes Server-Sent Events on ``GET /events`` so the
viewer can react to back-channel updates (today: the augmentation pass
completing). Each connected client gets a blocking queue; ``publish()``
fans events out and never blocks the caller.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from .comments import CommentStore


log = logging.getLogger(__name__)


# Sentinel pushed onto subscriber queues to ask the handler thread to
# release the connection at shutdown (so the daemon thread doesn't pin
# the process). Distinct from any real event payload.
_CLOSE = object()


@dataclass
class ServerContext:
    run_dir: Path
    store: CommentStore
    html_path: Path
    viewer_json: dict[str, Any]
    done_event: threading.Event
    last_activity: float = 0.0
    # SSE fan-out. Each connected /events client gets a queue;
    # publish() pushes the same payload onto all queues. Mutated only
    # under ``subs_lock`` because handler threads add/remove and the
    # publishing thread broadcasts.
    subscribers: list[queue.Queue] = field(default_factory=list)
    subs_lock: threading.Lock = field(default_factory=threading.Lock)


class _Handler(BaseHTTPRequestHandler):
    server_version = "scr-review/1"

    # mypy/Pyright: BaseHTTPRequestHandler exposes ``server`` as a HTTPServer
    # instance; we stash the context on it at construction time.
    ctx: ServerContext  # type: ignore[assignment]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 — stdlib sig
        log.debug("%s - %s", self.address_string(), format % args)

    # --- dispatch helpers -----------------------------------------------

    def _json(self, code: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _text(self, code: int, content_type: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _touch(self) -> None:
        self.ctx.last_activity = time.time()

    # --- routes ---------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 — stdlib naming
        self._touch()
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            try:
                html = self.ctx.html_path.read_bytes()
            except OSError as e:
                self._json(500, {"error": str(e)})
                return
            self._text(200, "text/html; charset=utf-8", html)
            return
        if path == "/data.json":
            self._json(200, self.ctx.viewer_json)
            return
        if path == "/comments":
            self._json(200, {"comments": [c.model_dump() for c in self.ctx.store.all()]})
            return
        if path == "/events":
            self._stream_events()
            return
        self._json(404, {"error": "not found"})

    def _stream_events(self) -> None:
        """Serve the SSE channel until the client disconnects or the
        server signals shutdown via the close sentinel."""
        q: queue.Queue = queue.Queue()
        with self.ctx.subs_lock:
            self.ctx.subscribers.append(q)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            # Disable proxy buffering in case anyone front-proxies this
            # localhost server (rare but cheap).
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            # Initial comment line tells the browser the connection is
            # open before any real event arrives; some EventSource
            # implementations wait for the first byte before firing
            # ``open``.
            try:
                self.wfile.write(b": ok\n\n")
                self.wfile.flush()
            except OSError:
                return
            while True:
                item = q.get()
                if item is _CLOSE:
                    return
                event_type, payload = item
                body = json.dumps(payload, ensure_ascii=False)
                frame = f"event: {event_type}\ndata: {body}\n\n".encode("utf-8")
                try:
                    self.wfile.write(frame)
                    self.wfile.flush()
                except OSError:
                    # Peer disconnected. Just return; the finally block
                    # unregisters this subscriber.
                    return
        finally:
            with self.ctx.subs_lock:
                try:
                    self.ctx.subscribers.remove(q)
                except ValueError:
                    pass

    def do_POST(self) -> None:  # noqa: N802
        self._touch()
        path = self.path.split("?", 1)[0]
        if path == "/comments":
            try:
                payload = self._read_json()
            except ValueError as e:
                self._json(400, {"error": f"invalid json: {e}"})
                return
            try:
                c = self.ctx.store.upsert(payload)
            except Exception as e:  # noqa: BLE001 — pydantic throws many kinds
                self._json(400, {"error": str(e)})
                return
            self._json(200, c.model_dump())
            return
        if path == "/exit":
            # Respond BEFORE signalling shutdown so the caller's fetch resolves.
            self._json(200, {"ok": True, "count": len(self.ctx.store.all())})
            # Defer the event set slightly so the response flushes.
            threading.Thread(target=self._signal_done, daemon=True).start()
            return
        self._json(404, {"error": "not found"})

    def do_DELETE(self) -> None:  # noqa: N802
        self._touch()
        path = self.path.split("?", 1)[0]
        if path.startswith("/comments/"):
            comment_id = path[len("/comments/"):]
            existed = self.ctx.store.delete(comment_id)
            self._json(200 if existed else 404, {"ok": existed})
            return
        self._json(404, {"error": "not found"})

    def _signal_done(self) -> None:
        time.sleep(0.05)
        self.ctx.done_event.set()


class ReviewServer:
    """Thin wrapper over :class:`ThreadingHTTPServer`.

    Usage:

    >>> srv = ReviewServer(run_dir=..., html_path=..., viewer_json=...)
    >>> srv.start()
    >>> url = srv.url()
    >>> srv.wait_until_done(timeout=3600)
    >>> srv.stop()
    """

    def __init__(
        self,
        *,
        run_dir: Path,
        html_path: Path,
        viewer_json: dict[str, Any],
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        self.run_dir = run_dir
        self.store = CommentStore(run_dir / "comments.json")
        self.done_event = threading.Event()
        self.ctx = ServerContext(
            run_dir=run_dir,
            store=self.store,
            html_path=html_path,
            viewer_json=viewer_json,
            done_event=self.done_event,
            last_activity=time.time(),
        )
        self._host = host
        self._port = port
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        ctx = self.ctx
        handler_cls: type[_Handler]

        class _Bound(_Handler):
            pass
        _Bound.ctx = ctx  # type: ignore[assignment]
        handler_cls = _Bound

        self._httpd = ThreadingHTTPServer((self._host, self._port), handler_cls)
        self._httpd.daemon_threads = True
        self._port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def url(self) -> str:
        return f"http://{self._host}:{self._port}"

    def publish(self, event_type: str, payload: dict[str, Any]) -> None:
        """Broadcast an SSE event to every connected /events client.

        Caller-thread-safe and non-blocking — each subscriber has its
        own queue. Disconnected clients drain themselves at the
        handler-thread side; the snapshot here is just to avoid holding
        the lock while putting onto queues.
        """
        with self.ctx.subs_lock:
            subs = list(self.ctx.subscribers)
        item = (event_type, payload)
        for q in subs:
            q.put(item)

    def update_viewer_json(self, viewer_json: dict[str, Any]) -> None:
        """Replace the JSON returned by ``GET /data.json``.

        Used after the augmentation pass completes so any late
        ``data.json`` fetch (a tab opened post-augment, a manual reload)
        sees the full state.
        """
        self.ctx.viewer_json = viewer_json

    def wait_until_done(
        self,
        *,
        timeout: float = 3600,
        idle_poll: float = 5.0,
        on_poll: Callable[[], None] | None = None,
    ) -> bool:
        """Block until /exit fires or ``timeout`` elapses. Returns True on clean exit."""
        deadline = time.time() + timeout
        while not self.done_event.is_set():
            remaining = max(0.0, deadline - time.time())
            if remaining <= 0:
                return False
            if self.done_event.wait(timeout=min(idle_poll, remaining)):
                return True
            if on_poll is not None:
                on_poll()
        return True

    def stop(self) -> None:
        # Wake any SSE handler threads parked on their queue so they
        # return out of ``_stream_events`` before we tear down the
        # socket — otherwise ``server_close`` can race the still-open
        # connections and the process pins on the daemon threads.
        with self.ctx.subs_lock:
            subs = list(self.ctx.subscribers)
        for q in subs:
            q.put(_CLOSE)
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
