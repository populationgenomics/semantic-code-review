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
import contextlib
import json
import logging
import os
import queue
import re
import threading
import time
import urllib.parse
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

from .comments import CommentStore, ReadOnlyCommentError

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


# Upper bound on a single /file-text side. Rendered markdown mode
# (ADR 0004) fetches full base+head text on demand; a pathologically
# large doc returns null for the offending side so the client falls
# back to the text diff rather than shipping megabytes to the browser.
_FILE_TEXT_CAP_BYTES = 2_000_000


def _read_worktree_file(worktree: Path, rel: str) -> str | None:
    """Read a file from a base/head worktree; None when absent, too
    large, or unreadable.

    `rel` originates in the diff's own file list but is echoed here into
    a filesystem read, so the path-traversal guard stays even though the
    input is already trusted.
    """
    if not rel or ".." in Path(rel).parts:
        return None
    path = worktree / rel
    try:
        if not path.is_file():
            return None
        if path.stat().st_size > _FILE_TEXT_CAP_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


# Sentinel pushed onto subscriber queues to ask the handler thread to
# release the connection at shutdown (so the daemon thread doesn't pin
# the process). Distinct from any real event payload.
_CLOSE = object()


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


def _update_viewer_json_fold(
    viewer_json: dict[str, Any],
    payload: dict[str, Any],
) -> None:
    """Patch the matching fold_regions[i].summary in the viewer JSON
    so a fresh `/data.json` fetch sees the result.

    Walks every hunk in the addressed file looking for a region whose
    (context, right_start/end, left_start/end) match — the regions are
    addressed at the file level by the v2 protocol but still attached
    to individual hunks in the per-hunk fold_regions block.
    """
    files = viewer_json.get("files") or []
    fi = payload.get("file_idx")
    if fi is None or fi >= len(files):
        return
    context = payload.get("context")
    rs = payload.get("right_start", 0)
    re_ = payload.get("right_end", 0)
    ls = payload.get("left_start", 0)
    le = payload.get("left_end", 0)
    summary = payload.get("summary", "")
    for hunk in files[fi].get("hunks") or []:
        for reg in hunk.get("fold_regions") or []:
            if (
                reg.get("context") == context
                and (reg.get("right_start") or 0) == rs
                and (reg.get("right_end") or 0) == re_
                and (reg.get("left_start") or 0) == ls
                and (reg.get("left_end") or 0) == le
            ):
                reg["summary"] = summary
                return


def _fold_symbol_from_viewer_json(
    viewer_json: dict[str, Any],
    file_idx: int,
    context: str,
    right_range: tuple[int, int] | None,
    left_range: tuple[int, int] | None,
) -> tuple[str | None, str | None]:
    """Look up the symbol a fold region snapped to, from the viewer JSON.

    The server-computed `fold_regions` carry the definition's
    `qualified_name` / `kind` (or null for indentation-fallback regions);
    the client requests by `(context, ranges)`, which match in lockstep.
    Returns `(None, None)` when no region matches — a client-only region
    over expanded context the server never computed — so the prompt is
    simply left unseeded.
    """
    files = viewer_json.get("files") or []
    if file_idx < 0 or file_idx >= len(files):
        return (None, None)
    rs, re_ = right_range or (0, 0)
    ls, le = left_range or (0, 0)
    for hunk in files[file_idx].get("hunks") or []:
        for reg in hunk.get("fold_regions") or []:
            if (
                reg.get("context") == context
                and (reg.get("right_start") or 0) == rs
                and (reg.get("right_end") or 0) == re_
                and (reg.get("left_start") or 0) == ls
                and (reg.get("left_end") or 0) == le
            ):
                return (reg.get("qualified_name"), reg.get("kind"))
    return (None, None)


def _range_from_payload(payload: dict[str, Any], side: str) -> tuple[int, int] | None:
    """Pull (start, end) for a side out of the request payload, or None
    if the keys aren't both present + parsable.
    """
    try:
        s = int(payload[f"{side}_start"])
        e = int(payload[f"{side}_end"])
    except (KeyError, TypeError, ValueError):
        return None
    return (s, e)


def _ctx_publish(
    ctx: ServerContext,
    event_type: str,
    payload: dict[str, Any],
    *,
    buffer: bool = True,
) -> None:
    """Broadcast an event to subscribers, optionally retaining it for
    `Last-Event-ID` replay.

    Shared by `ReviewServer.publish` and route handlers so the
    /fold-summary route can fan out to other tabs without round-
    tripping through the ReviewServer instance.

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


def _run_console_worker(
    ctx: ServerContext,
    asker: ConsoleAsker,
    question: str,
    history: Any,
    console_id: str,
    cancel: threading.Event,
    selection: Any = None,
) -> None:
    """Drive one streaming console turn on a dedicated background thread.

    Runs the asker (which drives ``Agent.iter``) under its own event
    loop, fanning out ``console-delta`` / ``console-tool`` frames as the
    answer streams, then a terminal ``console-done`` (or
    ``console-error``). Every frame is tagged with ``console_id`` so
    other tabs ignore streams that aren't theirs, and all are unbuffered
    (a reload starts the console fresh). The conversation history is
    advanced only on clean completion — a cancelled turn is discarded.
    """

    def on_delta(chunk: str) -> None:
        _ctx_publish(
            ctx,
            "console-delta",
            {"console_id": console_id, "text": chunk},
            buffer=False,
        )

    def on_tool(label: str) -> None:
        _ctx_publish(
            ctx,
            "console-tool",
            {"console_id": console_id, "label": label},
            buffer=False,
        )

    # Local import keeps the augment-side schemas off this stdlib-only
    # module's import graph (mirrors the /fold-summary route).
    from ..augment.console import ConsoleCancelled, ConsoleNotReady

    try:
        answer, new_history = asyncio.run(asker(question, history, on_delta, on_tool, cancel, selection))
    except ConsoleCancelled:
        # Partial turn abandoned: history stays as it was, the frontend
        # keeps whatever streamed, and the conversation remains usable.
        _ctx_publish(
            ctx,
            "console-done",
            {"console_id": console_id, "cancelled": True},
            buffer=False,
        )
    except ConsoleNotReady as e:
        _ctx_publish(
            ctx,
            "console-error",
            {"console_id": console_id, "error": str(e)},
            buffer=False,
        )
    except Exception as e:
        log.exception("console turn failed for question=%r", question[:120])
        _ctx_publish(
            ctx,
            "console-error",
            {"console_id": console_id, "error": f"{type(e).__name__}: {e}"},
            buffer=False,
        )
    else:
        with ctx.state_lock:
            ctx.console_history = new_history
        _ctx_publish(
            ctx,
            "console-done",
            {"console_id": console_id, "answer": answer},
            buffer=False,
        )
    finally:
        with ctx.state_lock:
            ctx.console_busy = False
            ctx.console_cancel = None


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


#: Signature of the on-demand fold summariser closure. The server
#: resolves hunk_id → file/hunk from the persisted sidecar and passes
#: (AnnotatedFile, AnnotatedHunk, overview_json, new_start, new_count).
#: The closure encapsulates model selection, cache, trace dir, etc;
#: the server stays diff-source-agnostic. Stored as ``Any`` here to
#: avoid pulling the augment-side schemas into the stdlib-only server
#: module — the actual signature is in `review/runner.py`.
FoldSummariser = Callable[..., Coroutine[Any, Any, dict]]


#: Signature of the streaming console turn driver wired by
#: ``serve_review`` once augmentation completes. Called as
#: ``asker(question, history, on_delta, on_tool, cancel)`` and awaited to
#: ``(answer_text, new_history)``: ``history`` is the opaque continuation
#: token from the prior turn (None on the first) — pydantic
#: ``message_history`` for SDK backends, a ``claude -p`` session id for CLI
#: subprocess backends. ``on_delta(str)`` / ``on_tool(str)`` are sync
#: callbacks the driver invokes as text and tool activity stream, and
#: ``cancel`` is a ``threading.Event`` it polls between chunks (raising
#: ``ConsoleCancelled`` when set). The token is held verbatim on
#: ``ServerContext`` and threaded back in on the next turn; the server
#: never inspects it. Stored as ``Any`` to keep the pydantic-ai message
#: types out of this stdlib-only module — the concrete signature lives
#: in ``augment/console.py``.
ConsoleAsker = Callable[..., Coroutine[Any, Any, Any]]


#: Signature of the post callback accepted by ``serve_review`` when the
#: caller wants the viewer to handle confirm-and-post in-browser.
#: Takes the comment IDs the reviewer selected in the confirmation
#: modal; returns the result of posting them (today: GitHub's
#: :class:`PostResult`). Loose annotation here so server.py stays
#: independent of the github post types.
PostCallable = Callable[[list[str]], Any]


@dataclass
class ServerContext:
    run_dir: Path
    store: CommentStore
    viewer_json: dict[str, Any]
    done_event: threading.Event
    last_activity: float = 0.0
    # `--debug` / SCR_DEBUG. When true the /data.json payload carries
    # `debug: true` so the viewer mounts its raw-log drawer, and the CLI
    # backend's per-spawn records are fanned out as `debug-log` SSE frames.
    debug: bool = False
    # Optional, wired by serve_review when an LLM backend is available.
    # The /fold-summary route returns 409 until this is set (the augment
    # pass has to finish first so the sidecar exists).
    fold_summariser: FoldSummariser | None = None
    # Optional, wired by serve_review once augmentation completes and
    # only for SDK backends (ADR 0002 — console). The /console/ask
    # route returns 409 until this is set. `console_history` is the
    # ephemeral, in-memory conversation `message_history` — never
    # persisted, dropped on /console/reset, excluded from the SSE
    # replay buffer (a reload starts fresh). Threaded opaquely through
    # the asker; the server never reads into it.
    console_asker: ConsoleAsker | None = None
    console_history: Any = None
    # In-flight turn coordination (Slice 2). ``console_busy`` enforces
    # one streaming turn per conversation — a second /console/ask while
    # a turn runs gets 409. ``console_cancel`` is the threading.Event the
    # background worker polls between chunks; /console/cancel (and a
    # mid-turn /console/reset) flips it. Both are guarded by
    # ``state_lock``. A threading.Event (not asyncio.Event) so the
    # request thread can flip it while the worker's own event loop polls
    # it — no cross-loop signalling.
    console_busy: bool = False
    console_cancel: threading.Event | None = None
    # Optional, wired by serve_review when the caller wants posting to
    # happen via the in-browser confirmation modal (``scr pr``). When
    # both are None the modal stays absent and Done exits the way it
    # always has. ``post_meta`` is a static dict shown to the viewer
    # on /post-config (repo, number, head_sha) so the modal can label
    # itself; the callback fires on /post-review with the IDs the
    # reviewer selected.
    post_callback: PostCallable | None = None
    post_meta: dict[str, Any] | None = None
    # Result of the most recent successful /post-review, persisted on
    # the context so ``serve_review`` can hand it back to the CLI after
    # ``wait_until_done`` returns. None means "no post happened" (user
    # cancelled / closed the tab / no postable comments).
    posted_result: Any = None
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
            # Stamp the runtime debug flag alongside the diff payload so the
            # viewer knows whether to mount the debug drawer. Merged at serve
            # time because update_viewer_json swaps viewer_json wholesale.
            self._json(200, {**self.ctx.viewer_json, "debug": self.ctx.debug})
            return
        if path == "/file-text":
            self._handle_file_text()
            return
        if path == "/comments":
            self._json(200, {"comments": [c.model_dump() for c in self.ctx.store.all()]})
            return
        if path == "/post-config":
            self._handle_post_config()
            return
        if path == "/post-preview":
            self._handle_post_preview()
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
            try:
                payload = self._read_json()
            except ValueError as e:
                self._json(400, {"error": f"invalid json: {e}"})
                return
            try:
                c = self.ctx.store.upsert(payload)
            except ReadOnlyCommentError as e:
                self._json(403, {"error": str(e)})
                return
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
        if path == "/post-review":
            try:
                payload = self._read_json()
            except ValueError as e:
                self._json(400, {"error": f"invalid json: {e}"})
                return
            self._handle_post_review(payload)
            return
        if path == "/console/ask":
            try:
                payload = self._read_json()
            except ValueError as e:
                self._json(400, {"error": f"invalid json: {e}"})
                return
            self._handle_console_ask(payload)
            return
        if path == "/console/cancel":
            self._handle_console_cancel()
            return
        if path == "/console/reset":
            self._handle_console_reset()
            return
        self._json(404, {"error": "not found"})

    def _handle_fold_summary(self, payload: dict[str, Any]) -> None:
        """Parse + validate the request, dispatch to the wired-in
        fold-summary task, broadcast the result.

        Wire format (slice 1 of fold-anywhere):
            { file_idx: int,
              context: "right" | "left" | "both",
              right_start?, right_end?,    # iff context != "left"
              left_start?,  left_end?      # iff context != "right"
            }
        Line numbers are 1-indexed into head/<path> (right) and
        base/<path> (left). Domain work (sidecar I/O, schema mutation,
        persistence) lives in :func:`apply_fold_summary_to_run`; this
        method is transport.
        """
        if self.ctx.fold_summariser is None:
            self._json(409, {"error": "augmentation still in progress"})
            return
        context = str(payload.get("context", ""))
        if context not in ("right", "left", "both"):
            self._json(400, {"error": "context must be 'right', 'left', or 'both'"})
            return
        try:
            # A missing/None file_idx raises TypeError, caught just below.
            file_idx = int(payload.get("file_idx"))  # pyright: ignore[reportArgumentType]
        except (TypeError, ValueError):
            self._json(400, {"error": "file_idx must be an integer"})
            return

        right_range = _range_from_payload(payload, "right") if context != "left" else None
        left_range = _range_from_payload(payload, "left") if context != "right" else None
        if context in ("right", "both") and right_range is None:
            self._json(400, {"error": "right_start/right_end required"})
            return
        if context in ("left", "both") and left_range is None:
            self._json(400, {"error": "left_start/left_end required"})
            return

        # Generated / lock / binary files are excluded from the LLM passes;
        # honour that here too so expanding a fold in one doesn't sneak a
        # lock file to the model. Return a canned summary, no LLM call.
        files = (self.ctx.viewer_json or {}).get("files") or []
        if 0 <= file_idx < len(files) and files[file_idx].get("status") in ("generated", "binary"):
            rs, re_ = right_range or (0, 0)
            ls, le = left_range or (0, 0)
            skipped = {
                "file_idx": file_idx,
                "context": context,
                "right_start": rs,
                "right_end": re_,
                "left_start": ls,
                "left_end": le,
                "summary": "Generated / lock file — not summarised.",
            }
            _update_viewer_json_fold(self.ctx.viewer_json, skipped)
            _ctx_publish(self.ctx, "fold-summary", skipped)
            self._json(200, skipped)
            return

        # Map typed errors from the apply step to HTTP statuses.
        from ..augment.fold_summary import (
            FoldSummaryFileIndexError,
            FoldSummaryNotReady,
        )

        # Seed the prompt with the symbol the region snapped to (if any),
        # resolved from the server-computed fold_regions in the viewer JSON.
        qualified_name, kind = _fold_symbol_from_viewer_json(
            self.ctx.viewer_json,
            file_idx,
            context,
            right_range,
            left_range,
        )

        try:
            result = asyncio.run(
                self.ctx.fold_summariser(
                    file_idx,
                    context,
                    right_range,
                    left_range,
                    qualified_name,
                    kind,
                )
            )
        except FoldSummaryNotReady as e:
            self._json(409, {"error": str(e)})
            return
        except FoldSummaryFileIndexError as e:
            self._json(404, {"error": str(e)})
            return
        except Exception as e:
            log.exception(
                "fold-summary failed for file_idx=%s context=%s right=%s left=%s",
                file_idx,
                context,
                right_range,
                left_range,
            )
            self._json(500, {"error": f"{type(e).__name__}: {e}"})
            return

        _update_viewer_json_fold(self.ctx.viewer_json, result)
        _ctx_publish(self.ctx, "fold-summary", result)
        # Caller's RPC response carries the summary so the requesting
        # tab doesn't need to wait for its own SSE event to round-trip.
        self._json(200, result)

    # --- console (free-form Q&A) ---------------------------------------

    def _handle_console_ask(self, payload: dict[str, Any]) -> None:
        """Kick off one streaming console turn; reply ``202`` immediately.

        Wire format: ``{ "question": str, "console_id"?: str }``. The
        turn runs on a background worker (:func:`_run_console_worker`),
        which streams ``console-delta`` / ``console-tool`` frames and a
        terminal ``console-done`` / ``console-error`` over the SSE bus,
        each tagged with ``console_id``. The POST itself carries no
        answer — the client drives the transcript off the stream.

        One turn per conversation: a second ask while a turn is in flight
        gets ``409``. The prior history is snapshotted under the lock so
        a concurrent reset/cancel can't tear it out mid-turn.
        """
        if self.ctx.console_asker is None:
            self._json(
                409,
                {
                    "error": (
                        "review console not ready yet — it becomes available once "
                        "analysis finishes (and is disabled for --no-augment runs)"
                    )
                },
            )
            return
        question = str(payload.get("question", "")).strip()
        if not question:
            self._json(400, {"error": "question must be a non-empty string"})
            return
        console_id = str(payload.get("console_id", "")).strip()
        # The reviewer's pinned selection (Slice 4), if any. Passed
        # through opaquely — the asker folds it into the turn's user
        # message; non-dict payloads are ignored downstream.
        selection = payload.get("selection")
        if not isinstance(selection, dict):
            selection = None

        with self.ctx.state_lock:
            if self.ctx.console_busy:
                self._json(409, {"error": "a console turn is already in flight"})
                return
            self.ctx.console_busy = True
            cancel = threading.Event()
            self.ctx.console_cancel = cancel
            history = self.ctx.console_history
            asker = self.ctx.console_asker

        threading.Thread(
            target=_run_console_worker,
            args=(self.ctx, asker, question, history, console_id, cancel, selection),
            daemon=True,
        ).start()
        self._json(202, {"ok": True, "console_id": console_id})

    def _handle_console_cancel(self) -> None:
        """Flip the in-flight turn's cancel flag — Stop / Esc in the viewer.

        Best-effort and idempotent: no in-flight turn means there's
        nothing to cancel, which is still a ``200``. The worker observes
        the flag between chunks and finishes with a cancelled
        ``console-done``.
        """
        with self.ctx.state_lock:
            cancel = self.ctx.console_cancel
        if cancel is not None:
            cancel.set()
        self._json(200, {"ok": True})

    def _handle_console_reset(self) -> None:
        """Drop the in-memory conversation — `Esc` in the viewer.

        The history is ephemeral by design (ADR 0002); clearing it just
        nulls the field so the next turn re-seeds from scratch. Also
        trips any in-flight turn's cancel flag so a reset mid-stream
        doesn't leave a worker writing history back over the cleared
        conversation.
        """
        with self.ctx.state_lock:
            cancel = self.ctx.console_cancel
            self.ctx.console_history = None
        if cancel is not None:
            cancel.set()
        self._json(200, {"ok": True})

    # --- rendered markdown mode (full file text) -----------------------

    def _handle_file_text(self) -> None:
        """Serve the full base+head source of one changed file.

        Query: ``?file_idx=N`` (index into ``viewer_json.files``). Lazy
        backing for rendered markdown mode (ADR 0004) — fetched only when
        a ``.md`` file is flipped to rendered mode, so ``ViewerData``
        carries no eager full-text payload. Reads from the ``base/`` /
        ``head/`` worktrees already materialised in the run dir.

        Returns ``{file_idx, path, base, head}`` where ``base`` / ``head``
        are the full file text or null when that side has no content
        (added file → base null; deleted file → head null; over the size
        cap → that side null so the client falls back to the text diff).
        """
        query = urllib.parse.urlparse(self.path).query
        raw = (urllib.parse.parse_qs(query).get("file_idx") or [""])[0]
        try:
            file_idx = int(raw)
        except (TypeError, ValueError):
            self._json(400, {"error": "file_idx must be an integer"})
            return
        files = (self.ctx.viewer_json or {}).get("files") or []
        if file_idx < 0 or file_idx >= len(files):
            self._json(404, {"error": f"file_idx {file_idx} out of range"})
            return
        fb = files[file_idx]
        path = fb.get("path") or ""
        # A renamed file's pre-image lives at its old path in base/.
        base_rel = fb.get("old_path") or path
        head = _read_worktree_file(self.ctx.run_dir / "head", path)
        base = _read_worktree_file(self.ctx.run_dir / "base", base_rel)
        self._json(200, {"file_idx": file_idx, "path": path, "base": base, "head": head})

    # --- post (confirm-and-post modal) ---------------------------------

    def _handle_post_config(self) -> None:
        """Tell the viewer whether this server is configured for posting.

        The viewer fetches this once on boot. When ``posting`` is true,
        the Done button opens the confirmation modal instead of exiting
        directly. The other fields are display-only metadata (modal
        header: "Posting N comments to <repo>#<number> at <head_sha>").
        """
        if self.ctx.post_meta is None:
            self._json(200, {"posting": False})
            return
        out: dict[str, Any] = {"posting": True}
        out.update(self.ctx.post_meta)
        self._json(200, out)

    def _handle_post_preview(self) -> None:
        """Return the current list of comments-that-would-be-posted.

        Computed on demand because the comment store mutates throughout
        the session — a preview taken at startup would be stale by Done.
        Returns rows the viewer needs to render the modal: id (for
        selection round-trip), file/side/line (for context), body, and
        ``is_reply`` to label the row.
        """
        if self.ctx.post_callback is None:
            self._json(409, {"error": "this server isn't configured for posting"})
            return
        # Local import keeps the GitHub mapping types out of the server
        # module's import-time graph — the route is only used when the
        # caller wired post_callback, which only ``scr pr`` does.
        from .github import comments_to_github

        all_comments = self.ctx.store.all()
        by_id = {c.id: c for c in all_comments}
        rows: list[dict[str, Any]] = []
        for p in comments_to_github(all_comments):
            src = by_id.get(p.source_id or "")
            if src is None:
                continue
            rows.append(
                {
                    "id": src.id,
                    "file": src.file,
                    "side": src.side,
                    "line": src.line,
                    "body": p.body,
                    "is_reply": p.is_reply,
                }
            )
        self._json(200, {"comments": rows})

    def _handle_post_review(self, payload: dict[str, Any]) -> None:
        """Fire the post callback with the selected IDs; persist + broadcast.

        Wire format: ``{ "comment_ids": ["id1", "id2", ...] }``. The
        callback is responsible for filtering the store down to those
        IDs, mapping to the wire shape, and posting. On success the
        result is persisted on ``ctx.posted_result`` so
        :func:`serve_review` can hand it back to the CLI; it's also
        fanned out as an SSE ``posted`` event for other open tabs.

        Does NOT auto-exit. The modal stays open showing the result so
        the reviewer can click through to the GitHub URL; the user
        ends the session explicitly via the modal's Close button
        (which POSTs /exit).
        """
        if self.ctx.post_callback is None:
            self._json(409, {"error": "this server isn't configured for posting"})
            return
        ids = payload.get("comment_ids")
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            self._json(400, {"error": "comment_ids must be a list of strings"})
            return

        try:
            result = self.ctx.post_callback(ids)
        except Exception as e:
            log.exception("post callback raised")
            self._json(500, {"error": f"{type(e).__name__}: {e}"})
            return

        # Marshal the PostResult-ish object into a JSON-safe dict
        # without binding the server to its concrete type.
        response = {
            "posted": int(getattr(result, "posted", 0)),
            "review_url": str(getattr(result, "review_url", "") or ""),
            "review_id": int(getattr(result, "review_id", 0) or 0),
        }
        self.ctx.posted_result = result
        _ctx_publish(self.ctx, "posted", response)
        self._json(200, response)

    def do_DELETE(self) -> None:
        self._touch()
        path = self.path.split("?", 1)[0]
        if path.startswith("/comments/"):
            comment_id = path[len("/comments/") :]
            try:
                existed = self.ctx.store.delete(comment_id)
            except ReadOnlyCommentError as e:
                self._json(403, {"error": str(e)})
                return
            self._json(200 if existed else 404, {"ok": existed})
            return
        self._json(404, {"error": "not found"})

    def _signal_done(self) -> None:
        time.sleep(0.05)
        self.ctx.done_event.set()


class ReviewServer:
    """Thin wrapper over :class:`ThreadingHTTPServer`.

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
    ) -> None:
        self.run_dir = run_dir
        self.store = CommentStore(run_dir / "comments.json")
        self.done_event = threading.Event()
        self.ctx = ServerContext(
            run_dir=run_dir,
            store=self.store,
            viewer_json=viewer_json,
            done_event=self.done_event,
            last_activity=time.time(),
            post_callback=post_callback,
            post_meta=post_meta,
            debug=debug,
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

    def set_console_asker(self, asker: ConsoleAsker | None) -> None:
        """Install (or clear) the console turn driver.

        ``serve_review`` calls this once augmentation completes (SDK
        backends only). Before then, /console/ask returns 409 because
        there's no augmented diff to ground the conversation against.
        """
        self.ctx.console_asker = asker

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
