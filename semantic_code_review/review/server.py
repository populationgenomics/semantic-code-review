"""Ephemeral localhost HTTP server coordinating reviewer comments.

Spawned by ``scr review``; dies when the viewer POSTs /exit or when the
idle timeout elapses. No external deps — stdlib ``http.server``.

The server also publishes Server-Sent Events on ``GET /events`` so the
viewer can react to back-channel updates (today: the augmentation pass
completing). Each connected client gets a blocking queue; ``publish()``
fans events out and never blocks the caller.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
import time
from collections.abc import Awaitable
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


def _parse_last_event_id(raw: str | None) -> int:
    """Coerce the `Last-Event-ID` header to an int, treating anything
    non-numeric (or missing) as 0 — i.e. "give me everything"."""
    if not raw:
        return 0
    try:
        return int(raw.strip())
    except (TypeError, ValueError):
        return 0


def _parse_hunk_id(hunk_id: str) -> tuple[int, int]:
    """`"H{fi}_{hi}"` → (fi, hi). Raises ValueError on malformed input."""
    if not hunk_id.startswith("H") or "_" not in hunk_id:
        raise ValueError(f"malformed hunk_id {hunk_id!r}")
    try:
        fi_str, hi_str = hunk_id[1:].split("_", 1)
        return int(fi_str), int(hi_str)
    except ValueError as e:
        raise ValueError(f"malformed hunk_id {hunk_id!r}") from e


def _update_viewer_json_fold(
    viewer_json: dict[str, Any], fi: int, hi: int,
    new_start: int, new_count: int, summary: str,
) -> None:
    """Patch the matching fold_regions[i].summary in the viewer JSON
    so a fresh `/data.json` fetch sees the result."""
    files = viewer_json.get("files") or []
    if fi >= len(files):
        return
    hunks = files[fi].get("hunks") or []
    if hi >= len(hunks):
        return
    new_end = new_start + new_count - 1
    for reg in hunks[hi].get("fold_regions") or []:
        if reg.get("new_start") == new_start and reg.get("new_end") == new_end:
            reg["summary"] = summary
            return


def _ctx_publish(ctx: ServerContext, event_type: str, payload: dict[str, Any]) -> None:
    """Append an event to the buffer + broadcast to subscribers.

    Shared by `ReviewServer.publish` and route handlers so the
    /fold-summary route can fan out to other tabs without round-
    tripping through the ReviewServer instance.
    """
    with ctx.state_lock:
        ev = _BufferedEvent(
            id=ctx.next_event_id, event_type=event_type, payload=payload,
        )
        ctx.next_event_id += 1
        ctx.buffer.append(ev)
        if len(ctx.buffer) > _BUFFER_CAP:
            del ctx.buffer[: len(ctx.buffer) - _BUFFER_CAP]
        subs = list(ctx.subscribers)
    for q in subs:
        q.put(ev)


#: Upper bound on the replay buffer. A 200-hunk PR emits ~200 hunk
#: events plus overview/done; even at a few KB per event the buffer
#: stays well under a megabyte. Anything beyond this cap drops the
#: oldest entries, which is fine — the buffer is a reconnect safety
#: net, not durable history.
_BUFFER_CAP = 2000


@dataclass
class _BufferedEvent:
    """One SSE frame, retained for `Last-Event-ID` replay on reconnect."""

    id: int
    event_type: str
    payload: dict[str, Any]


#: Signature of the on-demand fold summariser closure. The server
#: resolves hunk_id → file/hunk from the persisted sidecar and passes
#: (AnnotatedFile, AnnotatedHunk, overview_json, new_start, new_count).
#: The closure encapsulates model selection, cache, trace dir, etc;
#: the server stays diff-source-agnostic. Stored as ``Any`` here to
#: avoid pulling the augment-side schemas into the stdlib-only server
#: module — the actual signature is in `review/runner.py`.
FoldSummariser = Callable[..., Awaitable[str]]


@dataclass
class ServerContext:
    run_dir: Path
    store: CommentStore
    html_path: Path
    viewer_json: dict[str, Any]
    done_event: threading.Event
    last_activity: float = 0.0
    # Optional, wired by serve_review when an LLM backend is available.
    # The /fold-summary route returns 409 until this is set (the augment
    # pass has to finish first so the sidecar exists).
    fold_summariser: FoldSummariser | None = None
    # SSE fan-out + replay buffer. Subscribers and `buffer` are mutated
    # by both publishing threads and request-handling threads; the
    # single ``state_lock`` covers both so a reconnecting client sees a
    # consistent snapshot (replay-then-subscribe is atomic w.r.t.
    # concurrent publishes).
    subscribers: list[queue.Queue] = field(default_factory=list)
    buffer: list[_BufferedEvent] = field(default_factory=list)
    next_event_id: int = 1
    state_lock: threading.Lock = field(default_factory=threading.Lock)


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
        server signals shutdown via the close sentinel.

        Honours the EventSource ``Last-Event-ID`` reconnect header so a
        page refresh / dropped connection picks up where it left off —
        every buffered event newer than the supplied id is replayed
        before the live stream resumes.
        """
        last_id = _parse_last_event_id(self.headers.get("Last-Event-ID"))
        q: queue.Queue = queue.Queue()
        # Snapshot the replay slice and register the subscriber under a
        # single lock acquisition so a publish racing this connect can't
        # land an event that's neither in the replay nor in the queue.
        with self.ctx.state_lock:
            replay = [ev for ev in self.ctx.buffer if ev.id > last_id]
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
            for ev in replay:
                if not self._write_event(ev):
                    return
            while True:
                item = q.get()
                if item is _CLOSE:
                    return
                ev = item  # _BufferedEvent
                if not self._write_event(ev):
                    return
        finally:
            with self.ctx.state_lock:
                try:
                    self.ctx.subscribers.remove(q)
                except ValueError:
                    pass

    def _write_event(self, ev: "_BufferedEvent") -> bool:
        body = json.dumps(ev.payload, ensure_ascii=False)
        frame = f"id: {ev.id}\nevent: {ev.event_type}\ndata: {body}\n\n".encode("utf-8")
        try:
            self.wfile.write(frame)
            self.wfile.flush()
        except OSError:
            return False
        return True

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
        if path == "/fold-summary":
            try:
                payload = self._read_json()
            except ValueError as e:
                self._json(400, {"error": f"invalid json: {e}"})
                return
            self._handle_fold_summary(payload)
            return
        self._json(404, {"error": "not found"})

    def _handle_fold_summary(self, payload: dict[str, Any]) -> None:
        """Resolve hunk_id → (file, hunk), call the summariser, persist
        the result, push a `fold-summary` SSE event, and return the
        summary in the HTTP response.
        """
        if self.ctx.fold_summariser is None:
            self._json(409, {"error": "augmentation still in progress"})
            return
        hunk_id = str(payload.get("hunk_id", "")).strip()
        try:
            new_start = int(payload.get("new_start"))
            new_count = int(payload.get("new_count"))
        except (TypeError, ValueError):
            self._json(400, {"error": "new_start and new_count must be integers"})
            return
        try:
            fi, hi = _parse_hunk_id(hunk_id)
        except ValueError as e:
            self._json(400, {"error": str(e)})
            return

        sidecar = self.ctx.run_dir / "augmented.scr.json"
        if not sidecar.exists():
            self._json(409, {"error": "augmented.scr.json missing — augment not complete"})
            return

        # Local imports keep stdlib-only modules importable even when
        # the augment / format machinery isn't installed (tests that
        # exercise the server in isolation).
        from ..format.emit import emit_augmented_diff
        from ..format.sidecar import dump_sidecar, load_sidecar

        diff = load_sidecar(sidecar)
        if fi >= len(diff.files) or hi >= len(diff.files[fi].hunks):
            self._json(404, {"error": f"hunk {hunk_id!r} not in diff"})
            return

        fp = diff.files[fi]
        hunk = diff.files[fi].hunks[hi]
        # Pass the overview as a JSON string mirroring the per-hunk
        # pass's prompt block (which the agent caches against).
        from ..augment.hunks import overview_to_prompt_json

        overview_json = overview_to_prompt_json(diff)

        try:
            summary = asyncio.run(
                self.ctx.fold_summariser(fp, hunk, overview_json, new_start, new_count)
            )
        except Exception as e:  # noqa: BLE001
            log.exception("fold-summary failed for %s %s+%d", hunk_id, new_start, new_count)
            self._json(500, {"error": f"{type(e).__name__}: {e}"})
            return

        # Persist: append or replace the matching FoldDescription on
        # the hunk's annotations, then write the sidecar + the
        # augmented.diff so a manual reload also sees the new summary.
        from ..augment.schemas import FoldDescription

        new_folds = [
            fd for fd in hunk.ann.fold_descriptions
            if not (fd.new_start == new_start and fd.new_count == new_count)
        ]
        new_folds.append(
            FoldDescription(new_start=new_start, new_count=new_count, summary=summary)
        )
        updated_ann = hunk.ann.model_copy(update={"fold_descriptions": new_folds})
        updated_hunk = hunk.model_copy(update={"ann": updated_ann})
        updated_hunks = list(fp.hunks)
        updated_hunks[hi] = updated_hunk
        updated_file = fp.model_copy(update={"hunks": updated_hunks})
        updated_files = list(diff.files)
        updated_files[fi] = updated_file
        updated_diff = diff.model_copy(update={"files": updated_files})

        dump_sidecar(updated_diff, sidecar)
        (self.ctx.run_dir / "augmented.diff").write_text(
            emit_augmented_diff(updated_diff), encoding="utf-8",
        )

        # Update the viewer_json fold_regions[].summary so a reload
        # via /data.json also picks it up, then broadcast the patch
        # to any other connected tabs viewing the same review.
        _update_viewer_json_fold(
            self.ctx.viewer_json, fi, hi, new_start, new_count, summary,
        )
        _ctx_publish(self.ctx, "fold-summary", {
            "hunk_id": hunk_id, "new_start": new_start,
            "new_count": new_count, "summary": summary,
        })
        # Caller's RPC response carries the summary so the requesting
        # tab doesn't need to wait for its own SSE event to round-trip.
        self._json(200, {
            "hunk_id": hunk_id, "new_start": new_start,
            "new_count": new_count, "summary": summary,
        })

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
        """Broadcast an SSE event to every connected /events client and
        append it to the replay buffer.

        Caller-thread-safe and non-blocking — each subscriber has its
        own queue. Disconnected clients drain themselves at the
        handler-thread side; the snapshot taken inside the lock is just
        to avoid holding it while putting onto queues.
        """
        _ctx_publish(self.ctx, event_type, payload)

    def set_fold_summariser(self, summariser: FoldSummariser | None) -> None:
        """Install (or clear) the on-demand fold-summary closure.

        serve_review calls this once the augmentation pass has finished
        and the sidecar is on disk. Before then, /fold-summary returns
        409 because the diff can't be resolved.
        """
        self.ctx.fold_summariser = summariser

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
        with self.ctx.state_lock:
            subs = list(self.ctx.subscribers)
        for q in subs:
            q.put(_CLOSE)
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
