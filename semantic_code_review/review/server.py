"""Ephemeral localhost HTTP server in front of one [[review-session]].

Spawned by ``scr review``; dies when the viewer POSTs /exit or when the
idle timeout elapses. No external deps — stdlib ``http.server``.

Transport only: a route decodes the request, calls the session, and
turns what comes back into a response. The session's refusals arrive as
``ScrError``s carrying the status and body to answer with, so the
error-to-status map is the one in :meth:`_Handler._dispatch` rather than
one per handler.

The server also publishes Server-Sent Events on ``GET /events`` so the
viewer can react to back-channel updates (the augmentation pass
completing, a console turn streaming). Each connected client gets a
blocking queue; ``publish()`` fans events out and never blocks the
caller — including when the caller is the session.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import queue
import re
import socket
import sys
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

from .. import errors
from .comments import CommentStore
from .session import PostCallable, ReviewSession, ServerTasks

log = logging.getLogger(__name__)


# --- Static asset resolution --------------------------------------------
# Normally every asset is read from the packaged assets/ directory: the
# viewer.js bundle ships as package_data (built into the wheel by
# release.yml), so wheel and plugin installs both serve it from there. A
# SCR_VIEWER_BUILD_DIR override (set by the test harness, which bundles a
# fresh viewer.js out-of-tree) wins for viewer.js only; everything else
# (CSS, vendor, index.html) is always read from the in-tree assets/.

ASSETS_DIR = Path(__file__).resolve().parent.parent / "viewer" / "assets"


def _resolve_asset(rel: str) -> Path:
    """Locate a static asset, honouring SCR_VIEWER_BUILD_DIR for
    build artefacts.

    The build dir only ever holds the bundled viewer.js (esbuild
    output); everything else — index.html, CSS, vendored highlight
    bundle — is always read from the in-tree assets/ directory.
    The trailing path-traversal guard keeps `_serve_static`'s
    whitelist from being the only line of defence.
    """
    if ".." in Path(rel).parts:
        raise FileNotFoundError(f"refused path-traversal asset: {rel!r}")
    build_dir = os.environ.get("SCR_VIEWER_BUILD_DIR")
    if build_dir and rel == "viewer.js":
        p = Path(build_dir) / rel
        if p.exists():
            return p
    p = ASSETS_DIR / rel
    if p.exists():
        return p
    raise FileNotFoundError(f"asset not found: {rel} (looked in {ASSETS_DIR}{', ' + build_dir if build_dir else ''})")


# Sentinel pushed onto subscriber queues to ask the handler thread to
# release the connection at shutdown (so the daemon thread doesn't pin
# the process). Distinct from any real event payload.
_CLOSE = object()


#: `POST /explainer/section/<id>`. A path prefix rather than an exact
#: match because the section id is in the path — it is passed straight
#: to the session, which 404s on anything the document does not know.
_EXPLAINER_SECTION_PREFIX = "/explainer/section/"


def _parse_last_event_id(raw: str | None) -> int:
    """Coerce the `Last-Event-ID` header to an int, treating anything
    non-numeric (or missing) as 0 — i.e. "give me everything".
    """
    if not raw:
        return 0
    try:
        return int(raw.strip())
    except (TypeError, ValueError):
        return 0


def _ctx_publish(
    ctx: ServerContext,
    event_type: str,
    payload: dict[str, Any],
    *,
    buffer: bool = True,
) -> None:
    """Broadcast an event to subscribers, optionally retaining it for
    `Last-Event-ID` replay.

    Shared by `ReviewServer.publish` and the session so a route-driven
    result can fan out to other tabs without round-tripping through the
    ReviewServer instance.

    With ``buffer=False`` the frame is fanned out live but neither
    retained nor assigned an event id (no ``id:`` line) — it never
    advances a reconnecting client's `Last-Event-ID` cursor and is
    never replayed. The console stream (Slice 2) publishes this way so
    a mid-turn reload starts the conversation fresh.
    """
    with ctx.state_lock:
        if buffer:
            ev = _BufferedEvent(
                id=ctx.next_event_id,
                event_type=event_type,
                payload=payload,
            )
            ctx.next_event_id += 1
            ctx.buffer.append(ev)
            if len(ctx.buffer) > _BUFFER_CAP:
                del ctx.buffer[: len(ctx.buffer) - _BUFFER_CAP]
        else:
            ev = _BufferedEvent(id=None, event_type=event_type, payload=payload)
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
    """One SSE frame. ``id`` is None for unbuffered live-only frames
    (the console stream), which carry no ``id:`` line and so never
    advance a client's `Last-Event-ID` reconnect cursor.
    """

    id: int | None
    event_type: str
    payload: dict[str, Any]


@dataclass
class ServerContext:
    """What a request handler needs that isn't the [[review-session]].

    The session holds the review; this holds the transport's own state —
    the idle clock, the shutdown latch, and the SSE fan-out. Subscribers
    and `buffer` are mutated by both publishing threads and
    request-handling threads; the single ``state_lock`` covers both so a
    reconnecting client sees a consistent snapshot (replay-then-subscribe
    is atomic w.r.t. concurrent publishes).
    """

    session: ReviewSession
    done_event: threading.Event
    last_activity: float = 0.0
    subscribers: list[queue.Queue] = field(default_factory=list)
    buffer: list[_BufferedEvent] = field(default_factory=list)
    next_event_id: int = 1
    state_lock: threading.Lock = field(default_factory=threading.Lock)


class _Handler(BaseHTTPRequestHandler):
    server_version = "scr-review/1"

    #: Bound by ``ReviewServer.start`` onto a per-server subclass, which
    #: is how one context reaches every handler instance the
    #: ThreadingHTTPServer constructs.
    ctx: ClassVar[ServerContext]

    def log_message(self, format: str, *args: Any) -> None:
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

    def _dispatch(self, op: Callable[[], dict[str, Any]], *, ok: int = 200) -> None:
        """Run one session operation and answer with it.

        The single error-to-status map: a `ScrError` is an outcome and
        names its own status and body; anything else is a bug and gets a
        500 naming its type. The session has already logged it with the
        request's own context, so this doesn't log again.
        """
        try:
            payload = op()
        except errors.ScrError as e:
            self._json(e.status, e.body())
        except Exception as e:  # noqa: BLE001 — every failure owes the client a response
            self._json(500, {"error": f"{type(e).__name__}: {e}"})
        else:
            self._json(ok, payload)

    def _body(self) -> dict[str, Any] | None:
        """The decoded request body, or None after answering 400."""
        try:
            return self._read_json()
        except ValueError as e:
            self._json(400, {"error": f"invalid json: {e}"})
            return None

    # --- routes ---------------------------------------------------------

    def do_GET(self) -> None:
        self._touch()
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._serve_asset("index.html", "text/html; charset=utf-8")
            return
        if path.startswith("/static/"):
            self._serve_static(path[len("/static/") :])
            return
        if path == "/data.json":
            self._json(200, self.ctx.session.data_json())
            return
        if path == "/file-text":
            self._handle_file_text()
            return
        if path == "/comments":
            self._json(200, {"comments": [c.model_dump() for c in self.ctx.session.store.all()]})
            return
        if path == "/explainer":
            self._dispatch(self.ctx.session.get_explainer)
            return
        if path == "/post-config":
            self._dispatch(self.ctx.session.post_config)
            return
        if path == "/post-preview":
            self._dispatch(lambda: {"comments": self.ctx.session.post_preview()})
            return
        if path == "/events":
            self._stream_events()
            return
        self._json(404, {"error": "not found"})

    #: Whitelist of asset basenames that may be served via /static/.
    #: Keeps the route from doubling as a generic file-read primitive
    #: even though _resolve_static guards against path traversal too.
    _STATIC_ASSETS: ClassVar[dict[str, str]] = {
        "viewer.css": "text/css; charset=utf-8",
        "viewer.js": "application/javascript; charset=utf-8",
        "vendor/highlight.min.js": "application/javascript; charset=utf-8",
        "vendor/github.min.css": "text/css; charset=utf-8",
        "vendor/github-dark.min.css": "text/css; charset=utf-8",
        # Lazy-loaded by the review console the first time an answer
        # completes a mermaid fence; never bundled into viewer.js.
        "vendor/mermaid.min.js": "application/javascript; charset=utf-8",
        # Lazy-loaded by rendered-markdown mode the first time a flipped
        # `.md` contains math; never bundled. katex.min.css pulls its
        # woff2 fonts by the relative `fonts/` path — served by the
        # pattern branch in _serve_static, not this exact whitelist.
        "vendor/katex.min.js": "application/javascript; charset=utf-8",
        "vendor/katex.min.css": "text/css; charset=utf-8",
    }

    #: KaTeX's stylesheet requests fonts by name from `vendor/fonts/`; the
    #: set is fixed and their basenames well-formed, so a tight pattern
    #: serves them without 20 near-identical whitelist entries. The
    #: `_resolve_asset` traversal guard still applies.
    _KATEX_FONT_RE: ClassVar[re.Pattern[str]] = re.compile(r"vendor/fonts/KaTeX_[A-Za-z0-9]+-[A-Za-z]+\.woff2")

    def _serve_static(self, rel: str) -> None:
        ctype = self._STATIC_ASSETS.get(rel)
        if ctype is None and self._KATEX_FONT_RE.fullmatch(rel):
            ctype = "font/woff2"
        if ctype is None:
            self._json(404, {"error": "not found"})
            return
        self._serve_asset(rel, ctype)

    def _serve_asset(self, rel: str, ctype: str) -> None:
        try:
            path = _resolve_asset(rel)
        except FileNotFoundError as e:
            self._json(500, {"error": str(e)})
            return
        try:
            body = path.read_bytes()
        except OSError as e:
            self._json(500, {"error": str(e)})
            return
        self._text(200, ctype, body)

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
            replay = [ev for ev in self.ctx.buffer if ev.id is not None and ev.id > last_id]
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
                with contextlib.suppress(ValueError):
                    self.ctx.subscribers.remove(q)

    def _write_event(self, ev: _BufferedEvent) -> bool:
        body = json.dumps(ev.payload, ensure_ascii=False)
        # Unbuffered (console) frames carry no id, so they don't move the
        # browser's Last-Event-ID cursor — a reload won't try to replay
        # them (they aren't in the buffer anyway).
        id_line = "" if ev.id is None else f"id: {ev.id}\n"
        frame = f"{id_line}event: {ev.event_type}\ndata: {body}\n\n".encode()
        try:
            self.wfile.write(frame)
            self.wfile.flush()
        except OSError:
            return False
        return True

    def do_POST(self) -> None:
        self._touch()
        path = self.path.split("?", 1)[0]
        if path == "/comments":
            self._handle_upsert_comment()
            return
        if path == "/exit":
            # Respond BEFORE signalling shutdown so the caller's fetch resolves.
            self._json(200, {"ok": True, "count": len(self.ctx.session.store.all())})
            # Defer the event set slightly so the response flushes.
            threading.Thread(target=self._signal_done, daemon=True).start()
            return
        if path == "/fold-summary":
            payload = self._body()
            if payload is not None:
                self._dispatch(lambda: self.ctx.session.fold_summary(payload))
            return
        if path == "/post-review":
            payload = self._body()
            if payload is not None:
                self._dispatch(lambda: self.ctx.session.post(payload))
            return
        if path == "/console/ask":
            payload = self._body()
            if payload is not None:
                self._dispatch(
                    lambda: {"ok": True, "console_id": self.ctx.session.console_turn(payload)},
                    ok=202,
                )
            return
        if path == "/explainer/skeleton":
            self._dispatch(self.ctx.session.explainer_skeleton)
            return
        if path.startswith(_EXPLAINER_SECTION_PREFIX):
            section_id = path[len(_EXPLAINER_SECTION_PREFIX) :]
            self._dispatch(lambda: self.ctx.session.explainer_section(section_id))
            return
        if path == "/console/cancel":
            self.ctx.session.console_cancel()
            self._json(200, {"ok": True})
            return
        if path == "/console/reset":
            self.ctx.session.console_reset()
            self._json(200, {"ok": True})
            return
        self._json(404, {"error": "not found"})

    def do_DELETE(self) -> None:
        self._touch()
        path = self.path.split("?", 1)[0]
        if path.startswith("/comments/"):
            comment_id = path[len("/comments/") :]
            try:
                existed = self.ctx.session.store.delete(comment_id)
            except errors.ScrError as e:
                self._json(e.status, e.body())
                return
            self._json(200 if existed else 404, {"ok": existed})
            return
        self._json(404, {"error": "not found"})

    def _handle_upsert_comment(self) -> None:
        """Add or edit one reviewer comment.

        Not on `_dispatch`: a payload pydantic rejects is the client's
        fault, so the fallback here is 400 rather than the 500 a session
        operation's unexpected failure earns.
        """
        payload = self._body()
        if payload is None:
            return
        try:
            c = self.ctx.session.store.upsert(payload)
        except errors.ScrError as e:
            self._json(e.status, e.body())
            return
        except Exception as e:  # noqa: BLE001 — pydantic throws many kinds
            self._json(400, {"error": str(e)})
            return
        self._json(200, c.model_dump())

    def _handle_file_text(self) -> None:
        """Serve one changed file's full base+head source.

        Query: ``?file_idx=N`` (index into ``viewer_json.files``). The
        index is a URL component, so parsing it is the transport's job;
        whether it addresses a file is the session's.
        """
        query = urllib.parse.urlparse(self.path).query
        raw = (urllib.parse.parse_qs(query).get("file_idx") or [""])[0]
        try:
            file_idx = int(raw)
        except (TypeError, ValueError):
            self._json(400, {"error": "file_idx must be an integer"})
            return
        self._dispatch(lambda: self.ctx.session.file_text(file_idx))

    def _signal_done(self) -> None:
        time.sleep(0.05)
        self.ctx.done_event.set()


def _is_client_hangup(exc: BaseException | None) -> bool:
    """True when ``exc`` is — or was raised from — a client hanging up."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, ConnectionResetError | BrokenPipeError):
            return True
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return False


class _ReviewHTTPServer(ThreadingHTTPServer):
    """A ThreadingHTTPServer that stays quiet when a client hangs up.

    A browser dropping a connection mid-request — Chrome freezes
    background tabs and tears their sockets down — reaches
    ``handle_error`` as a ConnectionResetError / BrokenPipeError, which
    stdlib prints as a full traceback on stderr. Routine here, and next
    to the review's own output it reads as a crash. Every other
    exception keeps the traceback: those are ours.
    """

    def handle_error(
        self,
        request: socket.socket | tuple[bytes, socket.socket],
        client_address: str | tuple[str, int],
    ) -> None:
        exc = sys.exception()
        if _is_client_hangup(exc):
            log.debug("client %s disconnected mid-request: %r", client_address, exc)
            return
        super().handle_error(request, client_address)


class ReviewServer:
    """Thin wrapper over :class:`ThreadingHTTPServer`.

    Owns the socket, the SSE fan-out and the idle clock; the review
    itself lives on :attr:`session`, which is also what callers reach
    for to attach tasks or read the post result back.

    Usage:

    >>> srv = ReviewServer(run_dir=..., viewer_json=...)
    >>> srv.start()
    >>> url = srv.url()
    >>> srv.wait_until_done(timeout=3600)
    >>> srv.stop()
    """

    def __init__(
        self,
        *,
        run_dir: Path,
        viewer_json: dict[str, Any],
        host: str = "127.0.0.1",
        port: int = 0,
        post_callback: PostCallable | None = None,
        post_meta: dict[str, Any] | None = None,
        debug: bool = False,
        explainer: bool = False,
    ) -> None:
        self.run_dir = run_dir
        self.done_event = threading.Event()

        def publish(event_type: str, payload: dict[str, Any], *, buffer: bool = True) -> None:
            # Reads self.ctx at call time: the context needs the session
            # to exist, and the session needs somewhere to publish.
            _ctx_publish(self.ctx, event_type, payload, buffer=buffer)

        self.session = ReviewSession(
            run_dir=run_dir,
            viewer_json=viewer_json,
            store=CommentStore(run_dir / "comments.json"),
            publish=publish,
            debug=debug,
            explainer_enabled=explainer,
            post_callback=post_callback,
            post_meta=post_meta,
        )
        self.ctx = ServerContext(
            session=self.session,
            done_event=self.done_event,
            last_activity=time.time(),
        )
        self._host = host
        self._port = port
        self._httpd: _ReviewHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        ctx = self.ctx

        class _Bound(_Handler):
            pass

        _Bound.ctx = ctx

        self._httpd = _ReviewHTTPServer((self._host, self._port), _Bound)
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

    def attach(self, tasks: ServerTasks, viewer_json: dict[str, Any]) -> None:
        """Hand the augmented diff and its tasks to the session — see
        :meth:`ReviewSession.attach`.
        """
        self.session.attach(tasks, viewer_json)

    def wait_until_done(
        self,
        *,
        timeout: float = 3600,
        idle_poll: float = 5.0,
        on_poll: Callable[[], None] | None = None,
    ) -> bool:
        """Block until /exit fires or the server sits idle for ``timeout``
        seconds. Returns True on clean exit, False on the idle timeout.

        Idle means two things at once: no request has been handled
        (``ctx.last_activity``, set by every route) and no viewer is
        holding an SSE stream open. An open tab is attention, so a
        reviewer reading for an hour without clicking anything is never
        cut off; a closed tab — or one Chrome froze until its socket
        dropped — starts the countdown.
        """
        # The later of the last handled request and the last poll that
        # saw a viewer. Carrying the observation forward is what starts
        # the countdown when a tab drops, rather than at whenever that
        # tab last made a request.
        last_seen = self.ctx.last_activity
        while not self.done_event.is_set():
            if self._connected_viewers():
                last_seen = time.time()
            last_seen = max(last_seen, self.ctx.last_activity)
            idle = time.time() - last_seen
            if idle >= timeout:
                return False
            if self.done_event.wait(timeout=min(idle_poll, timeout - idle)):
                return True
            if on_poll is not None:
                on_poll()
        return True

    def _connected_viewers(self) -> int:
        """How many viewers hold an open SSE stream. Read under
        ``state_lock``, which the handler threads take to (un)register.
        """
        with self.ctx.state_lock:
            return len(self.ctx.subscribers)

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
