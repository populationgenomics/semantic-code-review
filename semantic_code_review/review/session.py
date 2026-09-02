"""One review session: the state a review holds and what can be done to it.

A review is a [[run-directory]] plus the things a reviewer can ask of it
— summarise a fold, write the change-explainer document, hold a console
turn, post the comments. `ReviewSession` owns that state and those
operations. `review/server.py` is the HTTP transport in front of it and
holds no session state beyond its own SSE fan-out, which the session
publishes *through* rather than owns.

Two conventions the routes rely on:

- **The session owns the request body.** An operation given a decoded
  payload validates it and refuses a malformed one with a
  `ReviewSessionError` carrying the status and message the route
  answers with — the wire format is part of what the session promises.
  Values lifted out of a URL (a path segment, a query parameter) are
  parsed by the transport and arrive typed.
- **Every refusal is a `ScrError`.** The route reads `status` and
  `body()` off it, so there is one error-to-status map rather than one
  per handler. Anything else propagating out is a bug and answers 500.

The LLM-backed operations are unavailable until `attach()` hands over
the `ServerTasks` bundle; until then they refuse with 409, which is the
state the viewer polls against.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import pathlib
import threading
from collections.abc import Callable, Coroutine
from typing import Any, Protocol

from .. import errors
from . import comments

log = logging.getLogger(__name__)


#: Signature of the augment pass `serve_review` runs while the page is
#: live. The second argument is the publisher bound to the review
#: server's SSE channel; pass it through to `augment_run_dir(on_event=)`
#: so the pipeline can stream overview / per-hunk events to the page.
AugmentCallable = Callable[
    [pathlib.Path, Callable[[str, dict], None]],
    Coroutine[Any, Any, None],
]


#: Signature of the on-demand fold-summary callable. The closure
#: resolves the sidecar, calls the LLM against the addressed file,
#: persists the new `FoldDescription`, and returns the broadcast payload
#: (the dict the session fans out as an SSE event and hands back to the
#: requesting tab). Wired only when an LLM backend is available;
#: `--no-augment` reviews leave it `None` and the route 409s.
FoldSummaryCallable = Callable[
    # (file_idx, context, right_range, left_range, qualified_name, kind)
    [
        int,
        str,
        "tuple[int, int] | None",
        "tuple[int, int] | None",
        "str | None",
        "str | None",
    ],
    Coroutine[Any, Any, dict],
]


#: Signature of the change-explainer skeleton generator: no arguments,
#: awaited to the document as a jsonable dict. The closure owns model
#: selection, cache, run dir and persistence, so the session stays
#: diff-source-agnostic.
ExplainerCallable = Callable[[], Coroutine[Any, Any, dict]]


#: Signature of the per-section prose generator, wired alongside the
#: skeleton one. Takes a section id and returns the whole document — a
#: prose call may write more than one section, and a section write is a
#: document write either way.
ExplainerSectionCallable = Callable[[str], Coroutine[Any, Any, dict]]


#: Signature of the streaming console turn driver. Called as
#: `(question, history, on_delta, on_tool, cancel, selection)` and
#: awaited to `(answer_text, new_history)`: `history` is the opaque
#: continuation token from the prior turn (None on the first) — pydantic
#: `message_history` for SDK backends, a `claude -p` session id for CLI
#: subprocess backends. `on_delta` / `on_tool` are sync callbacks the
#: driver invokes as text and tool activity stream, and `cancel` is a
#: `threading.Event` it polls between chunks (raising `ConsoleCancelled`
#: when set). The token is held verbatim on the session and threaded
#: back in on the next turn; nothing here inspects it.
ConsoleCallable = Callable[
    [
        str,
        "list | None",
        "Callable[[str], None]",
        "Callable[[str], None]",
        "threading.Event",
        "dict[str, Any] | None",
    ],
    Coroutine[Any, Any, "tuple[str, list]"],
]


class PostOutcome(Protocol):
    """What the session reads off a completed post.

    Structural so the review layer's wire shaping doesn't bind to
    `review/github.py`'s `PostResult`; read-only members so a frozen
    dataclass satisfies it.
    """

    @property
    def review_id(self) -> int: ...

    @property
    def review_url(self) -> str: ...

    @property
    def posted(self) -> int: ...


#: Signature of the post callback the caller supplies when the viewer is
#: to handle confirm-and-post in-browser. Takes the comment ids the
#: reviewer selected in the confirmation modal; posts them and reports
#: what landed.
PostCallable = Callable[[list[str]], PostOutcome]


class EventPublisher(Protocol):
    """The SSE fan-out, as the session uses it.

    `buffer=False` fans a frame out live without retaining it for
    `Last-Event-ID` replay — how the console stream publishes, so a
    mid-turn reload starts the conversation fresh.
    """

    def __call__(self, event_type: str, payload: dict[str, Any], *, buffer: bool = True) -> None: ...


@dataclasses.dataclass(frozen=True)
class ServerTasks:
    """The optional closures one review installs on its session.

    Every field is None on a `--no-augment` run: each one needs either an
    LLM backend or the augment sidecar, and often both. One bundle serves
    both entry points, so a generator added here reaches `scr review` and
    `scr pr` together.
    """

    augment: AugmentCallable | None = None
    fold_summary: FoldSummaryCallable | None = None
    console: ConsoleCallable | None = None
    explainer: ExplainerCallable | None = None
    explainer_section: ExplainerSectionCallable | None = None
    bind_debug_sink: Callable[[Callable[[dict], None]], None] | None = None


class ReviewSessionError(errors.ScrError):
    """A refusal the session states in the terms the route answers with.

    The refusals that aren't a pass failing: a malformed request, a
    feature that is off for this review, a pass already in flight,
    an index that addresses nothing.
    """

    def __init__(self, status: int, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self._extra = extra

    def body(self) -> dict[str, Any]:
        return {"error": str(self), **self._extra}


class _ExclusiveSlot:
    """A one-holder-at-a-time claim on a shared resource.

    `take()` reports whether the caller got it; the holder `release()`s.
    The two users release at different moments — an explainer pass on
    the way out of the request that took it, a console turn on the
    background thread it started — so this is a pair of calls rather
    than a context manager.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._held = False

    def take(self) -> bool:
        with self._lock:
            if self._held:
                return False
            self._held = True
            return True

    def release(self) -> None:
        with self._lock:
            self._held = False

    @property
    def held(self) -> bool:
        with self._lock:
            return self._held


@dataclasses.dataclass(frozen=True)
class FoldAddress:
    """Where a [[fold-region]] is, as the viewer addresses one.

    `(file_idx, context, right, left)`, with the range for a side the
    context doesn't cover left as None. The same address identifies the
    region in the request, in the `fold_regions` block of the viewer
    JSON, and in the broadcast payload — matching it in all three is
    what `matches` and `as_payload` are for.
    """

    file_idx: int
    context: str
    right: tuple[int, int] | None
    left: tuple[int, int] | None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> FoldAddress:
        """Parse a `/fold-summary` request body.

        Wire format (slice 1 of fold-anywhere):
            { file_idx: int,
              context: "right" | "left" | "both",
              right_start?, right_end?,    # iff context != "left"
              left_start?,  left_end?      # iff context != "right"
            }
        Line numbers are 1-indexed into head/<path> (right) and
        base/<path> (left).

        Raises:
            ReviewSessionError: 400, naming the field at fault.
        """
        context = str(payload.get("context", ""))
        if context not in ("right", "left", "both"):
            raise ReviewSessionError(400, "context must be 'right', 'left', or 'both'")
        try:
            # A missing/None file_idx raises TypeError, caught just below.
            file_idx = int(payload.get("file_idx"))  # pyright: ignore[reportArgumentType]
        except (TypeError, ValueError):
            raise ReviewSessionError(400, "file_idx must be an integer") from None
        right = _range_from_payload(payload, "right") if context != "left" else None
        left = _range_from_payload(payload, "left") if context != "right" else None
        if context in ("right", "both") and right is None:
            raise ReviewSessionError(400, "right_start/right_end required")
        if context in ("left", "both") and left is None:
            raise ReviewSessionError(400, "left_start/left_end required")
        return cls(file_idx=file_idx, context=context, right=right, left=left)

    def matches(self, region: dict[str, Any]) -> bool:
        """True when `region` (a viewer-JSON `fold_regions` entry) is this
        one. An absent bound reads as 0 on both sides of the comparison.
        """
        rs, re_ = self.right or (0, 0)
        ls, le = self.left or (0, 0)
        return (
            region.get("context") == self.context
            and (region.get("right_start") or 0) == rs
            and (region.get("right_end") or 0) == re_
            and (region.get("left_start") or 0) == ls
            and (region.get("left_end") or 0) == le
        )

    def as_payload(self, *, summary: str) -> dict[str, Any]:
        """The broadcast shape: this address flattened, plus the summary."""
        rs, re_ = self.right or (0, 0)
        ls, le = self.left or (0, 0)
        return {
            "file_idx": self.file_idx,
            "context": self.context,
            "right_start": rs,
            "right_end": re_,
            "left_start": ls,
            "left_end": le,
            "summary": summary,
        }


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


#: What a fold in a generated / lock / binary file gets instead of a
#: model call. Those files are excluded from the LLM passes; expanding a
#: fold inside one must not be the way a lock file reaches the model.
_GENERATED_FOLD_SUMMARY = "Generated / lock file — not summarised."


#: Upper bound on a single `file_text` side. Rendered markdown mode
#: (ADR 0004) fetches full base+head text on demand; a pathologically
#: large doc returns null for the offending side so the client falls
#: back to the text diff rather than shipping megabytes to the browser.
_FILE_TEXT_CAP_BYTES = 2_000_000


def _read_worktree_file(worktree: pathlib.Path, rel: str) -> str | None:
    """Read a file from a base/head worktree; None when absent, too
    large, or unreadable.

    `rel` originates in the diff's own file list but is echoed here into
    a filesystem read, so the path-traversal guard stays even though the
    input is already trusted.
    """
    if not rel or ".." in pathlib.Path(rel).parts:
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


class ReviewSession:
    """The state of one review and the operations over it.

    Holds the run directory, the viewer JSON served as `/data.json`, the
    comment store, the tasks once attached, and the guards that keep one
    console turn and one explainer pass in flight at a time. Everything
    reaching an LLM runs `asyncio.run` on the calling thread, except the
    console turn, which streams from a worker thread of its own.
    """

    def __init__(
        self,
        *,
        run_dir: pathlib.Path,
        viewer_json: dict[str, Any],
        store: comments.CommentStore,
        publish: EventPublisher,
        debug: bool = False,
        explainer_enabled: bool = False,
        post_callback: PostCallable | None = None,
        post_meta: dict[str, Any] | None = None,
    ) -> None:
        self.run_dir = run_dir
        self.store = store
        #: Known at construction, not at attach time: the viewer decides
        #: whether to mount the overview-mode button on its first
        #: /data.json, well before augmentation has finished.
        self.explainer_enabled = explainer_enabled
        self._viewer_json = viewer_json
        self._publish = publish
        self._debug = debug
        self._post_callback = post_callback
        self._post_meta = post_meta
        self._tasks = ServerTasks()
        self._lock = threading.Lock()
        self._console_slot = _ExclusiveSlot()
        self._explainer_slot = _ExclusiveSlot()
        # The ephemeral, in-memory conversation continuation — never
        # persisted, dropped on reset, excluded from the SSE replay
        # buffer (a reload starts fresh). Threaded opaquely through the
        # asker; the session never reads into it.
        self._console_history: Any = None
        self._console_cancel: threading.Event | None = None
        self._posted_result: PostOutcome | None = None

    # --- lifecycle ------------------------------------------------------

    def attach(self, tasks: ServerTasks, viewer_json: dict[str, Any]) -> None:
        """Take delivery of the augmented diff: swap in the viewer JSON
        built from it and unlock the tasks it makes possible.

        One call because it is one event. The fold summariser, the
        console and both explainer generators all need the sidecar on
        disk, and a tab that opens mid-pass must see them refuse rather
        than resolve half a diff — so they arrive together, after it
        lands, along with the JSON read back off it.
        """
        self.set_viewer_json(viewer_json)
        self._tasks = tasks

    def set_viewer_json(self, viewer_json: dict[str, Any]) -> None:
        """Replace the payload `/data.json` serves."""
        self._viewer_json = viewer_json

    def data_json(self) -> dict[str, Any]:
        """The `/data.json` payload: the viewer JSON plus the two runtime
        flags the viewer needs before any pass has run.

        Merged at read time because `set_viewer_json` swaps the diff
        payload wholesale.
        """
        return {**self._viewer_json, "debug": self._debug, "explainer": self.explainer_enabled}

    @property
    def posted_result(self) -> PostOutcome | None:
        """The most recent successful post, or None if none happened —
        the reviewer cancelled, closed the tab, or had nothing postable.
        """
        with self._lock:
            return self._posted_result

    # --- fold summaries -------------------------------------------------

    def fold_summary(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Summarise one fold region; patch the viewer JSON, fan it out.

        Returns the broadcast payload, which also goes back to the
        requesting tab so it doesn't wait on its own SSE round-trip.

        Raises:
            ReviewSessionError: 409 before the summariser is attached,
                400 on a malformed address.
            FoldSummaryNotReady: 409 — the sidecar went missing.
            FoldSummaryFileIndexError: 404 — `file_idx` addresses no file.
        """
        task = self._tasks.fold_summary
        if task is None:
            raise ReviewSessionError(409, "augmentation still in progress")
        address = FoldAddress.from_payload(payload)

        if self._file_status(address.file_idx) in ("generated", "binary"):
            return self._land_fold_summary(address, address.as_payload(summary=_GENERATED_FOLD_SUMMARY))

        # Seed the prompt with the symbol the region snapped to. The
        # server-computed regions carry the definition's qualified_name /
        # kind (null for indentation-fallback ones); a client-only region
        # over expanded context matches nothing and leaves it unseeded.
        region = self._find_fold_region(address)
        qualified_name = region.get("qualified_name") if region is not None else None
        kind = region.get("kind") if region is not None else None

        try:
            result = asyncio.run(
                task(address.file_idx, address.context, address.right, address.left, qualified_name, kind)
            )
        except errors.ScrError:
            raise
        except Exception:
            log.exception("fold-summary failed for %r", address)
            raise
        return self._land_fold_summary(address, result)

    def _land_fold_summary(self, address: FoldAddress, result: dict[str, Any]) -> dict[str, Any]:
        """Patch the summary into the viewer JSON so a fresh `/data.json`
        sees it, fan the result out, and hand it back.
        """
        region = self._find_fold_region(address)
        if region is not None:
            region["summary"] = result.get("summary", "")
        self._publish("fold-summary", result)
        return result

    def _find_fold_region(self, address: FoldAddress) -> dict[str, Any] | None:
        """The addressed region in the viewer JSON, or None.

        Regions are addressed at the file level but still hang off
        individual hunks in the per-hunk `fold_regions` block, so the
        search walks every hunk in the file.
        """
        file = self._file_block(address.file_idx)
        if file is None:
            return None
        for hunk in file.get("hunks") or []:
            for region in hunk.get("fold_regions") or []:
                if address.matches(region):
                    return region
        return None

    def _file_block(self, file_idx: int) -> dict[str, Any] | None:
        files = self._viewer_json.get("files") or []
        if file_idx < 0 or file_idx >= len(files):
            return None
        return files[file_idx]

    def _file_status(self, file_idx: int) -> str | None:
        file = self._file_block(file_idx)
        return None if file is None else file.get("status")

    # --- rendered markdown mode (full file text) ------------------------

    def file_text(self, file_idx: int) -> dict[str, Any]:
        """The full base+head source of one changed file.

        Lazy backing for rendered markdown mode (ADR 0004) — fetched only
        when a `.md` file is flipped, so `ViewerData` carries no eager
        full-text payload. Reads from the `base/` / `head/` worktrees
        already materialised in the run dir.

        Returns `{file_idx, path, base, head}` where `base` / `head` are
        the full file text or None when that side has no content (added
        file → base None; deleted file → head None; over the size cap →
        that side None so the client falls back to the text diff).

        Raises:
            ReviewSessionError: 404 — `file_idx` addresses no file.
        """
        file = self._file_block(file_idx)
        if file is None:
            raise ReviewSessionError(404, f"file_idx {file_idx} out of range")
        path = file.get("path") or ""
        # A renamed file's pre-image lives at its old path in base/.
        base_rel = file.get("old_path") or path
        return {
            "file_idx": file_idx,
            "path": path,
            "base": _read_worktree_file(self.run_dir / "base", base_rel),
            "head": _read_worktree_file(self.run_dir / "head", path),
        }

    # --- change explainer (ADR 0007) ------------------------------------

    def get_explainer(self) -> dict[str, Any]:
        """The run's persisted document.

        Raises:
            ReviewSessionError: 409 when the feature is off for this
                review, 404 when no document has been generated — the
                press-the-button state, not an error, since generation is
                reviewer-initiated and a document nobody asked for costs
                nothing precisely because it does not exist.
            ExplainerCorrupt: 500 — `explainer.json` is unreadable.
        """
        self._require_explainer_enabled()
        try:
            document = self._load_document()
        except errors.ScrError as e:
            log.warning("explainer.json is unreadable: %s", e)
            raise
        if document is None:
            raise ReviewSessionError(404, "no explainer document for this diff")
        return document

    def explainer_skeleton(self) -> dict[str, Any]:
        """Generate the document skeleton, persist it, fan it out.

        Idempotent against an existing document: a second press (or a
        second tab) gets what is already on disk rather than paying for
        the call again.

        Raises:
            ReviewSessionError: 409 when the feature is off, not yet
                attached, or another pass holds the slot.
            ExplainerNotReady: 409 — no sidecar to seed the skeleton.
        """
        task = self._tasks.explainer
        if task is None:
            raise self._explainer_unavailable()
        from ..augment import explainer_schema

        try:
            existing = self._load_document()
        except explainer_schema.ExplainerCorrupt:
            # A torn or hand-edited document must not wedge the button:
            # regenerating overwrites it, which is the intended repair.
            log.warning("explainer.json is unreadable — regenerating", exc_info=True)
            existing = None
        if existing is not None:
            return existing
        return self._run_explainer_pass(task, what="explainer skeleton")

    def explainer_section(self, section_id: str) -> dict[str, Any]:
        """Write the prose for the call that owns a section; fan it out.

        The id addresses a section; what runs is the pass that writes it,
        and a pass may write more than one (ADR 0007 addendum —
        Intuition and Code are merged). POSTing either of a merged pair
        runs the same call and lands both, which is why the result is the
        whole document rather than the section.

        Raises:
            ReviewSessionError: 409 when the feature is off, not yet
                attached, or another pass holds the slot.
            SectionNotFound: 404. SectionNotReady: 409 with the counts.
            SectionFailed: 500, with the `failed` document already fanned
                out so every tab sees the retryable state.
        """
        task = self._tasks.explainer_section
        if task is None:
            raise self._explainer_unavailable()
        # Not a status mapping: a failed pass persists its sections
        # `failed` and carries the document, which is ours to broadcast.
        from ..augment.explainer_section import SectionFailed

        try:
            return self._run_explainer_pass(lambda: task(section_id), what=f"explainer section {section_id}")
        except SectionFailed as e:
            self._publish("explainer", e.document)
            raise

    def _run_explainer_pass(
        self,
        start: Callable[[], Coroutine[Any, Any, dict]],
        *,
        what: str,
    ) -> dict[str, Any]:
        """Run one explainer call under the single-pass slot and fan the
        resulting document out.

        One pass at a time, skeleton and per-section alike: a second
        request gets 409 rather than a duplicate spend, and with no
        interleaving there is no race between two read-modify-writes of
        `explainer.json`.
        """
        if not self._explainer_slot.take():
            # `retry`: another pass holds the slot, which clears on its
            # own. Distinct from the readiness 409 (which carries
            # `total`) and from a real failure — a caller that treats
            # this as terminal makes the reviewer press again for a
            # condition that resolves itself.
            raise ReviewSessionError(409, "an explainer pass is already running", retry=True)
        try:
            payload = asyncio.run(start())
        except errors.ScrError:
            raise
        except Exception:
            log.exception("%s failed", what)
            raise
        finally:
            self._explainer_slot.release()
        self._publish("explainer", payload)
        return payload

    def _explainer_unavailable(self) -> ReviewSessionError:
        """The 409 for an explainer route with no generator attached.

        Two states, one of which clears itself: the feature is off for
        this review (permanent), or the tasks are not attached yet
        because augmentation has not finished (transient, so `retry` —
        the caller re-queues rather than latching the section to `failed`
        for a condition that resolves).
        """
        if not self.explainer_enabled:
            return ReviewSessionError(409, "the change explainer is disabled for this review")
        return ReviewSessionError(409, "augmentation still in progress", retry=True)

    def _require_explainer_enabled(self) -> None:
        if not self.explainer_enabled:
            raise ReviewSessionError(409, "the change explainer is disabled for this review")

    def _load_document(self) -> dict[str, Any] | None:
        """The run's persisted document, or None when there isn't one.

        Local import: the explainer schema is pydantic, and a
        `--no-augment` review should never pay for it.
        """
        from ..augment import explainer_schema

        pr = self._viewer_json.get("pr") or {}
        # The document is invalidated wholesale when this pair moves, so
        # it is the identity a persisted one is checked against on load.
        document = explainer_schema.load_explainer(
            self.run_dir,
            base_sha=str(pr.get("base_sha", "")),
            head_sha=str(pr.get("head_sha", "")),
        )
        return None if document is None else document.model_dump(mode="json")

    # --- console (free-form Q&A) ----------------------------------------

    def console_turn(self, payload: dict[str, Any]) -> str:
        """Start one streaming console turn; return its console id.

        Wire format: `{ "question": str, "console_id"?: str,
        "selection"?: object }`. The turn runs on a background worker
        that streams `console-delta` / `console-tool` frames and a
        terminal `console-done` / `console-error`, each tagged with the
        console id so other tabs ignore streams that aren't theirs. The
        caller gets no answer — the client drives the transcript off the
        stream.

        Raises:
            ReviewSessionError: 409 before the asker is attached or while
                a turn is in flight, 400 on an empty question.
        """
        asker = self._tasks.console
        if asker is None:
            raise ReviewSessionError(
                409,
                "review console not ready yet — it becomes available once "
                "analysis finishes (and is disabled for --no-augment runs)",
            )
        question = str(payload.get("question", "")).strip()
        if not question:
            raise ReviewSessionError(400, "question must be a non-empty string")
        console_id = str(payload.get("console_id", "")).strip()
        # The reviewer's pinned selection (Slice 4), if any. Passed
        # through opaquely — the asker folds it into the turn's user
        # message; non-dict payloads are ignored downstream.
        selection = payload.get("selection")
        if not isinstance(selection, dict):
            selection = None

        # Claiming the slot and installing this turn's cancel flag are one
        # step: a /console/cancel landing between them would otherwise
        # trip the previous turn's flag and leave this one uncancellable.
        cancel = threading.Event()
        with self._lock:
            if not self._console_slot.take():
                raise ReviewSessionError(409, "a console turn is already in flight")
            self._console_cancel = cancel
            history = self._console_history
        threading.Thread(
            target=self._run_console_turn,
            args=(asker, question, history, console_id, cancel, selection),
            daemon=True,
        ).start()
        return console_id

    def console_cancel(self) -> None:
        """Flip the in-flight turn's cancel flag — Stop / Esc in the viewer.

        Best-effort and idempotent: no in-flight turn means there's
        nothing to cancel. The worker observes the flag between chunks
        and finishes with a cancelled `console-done`.
        """
        with self._lock:
            cancel = self._console_cancel
        if cancel is not None:
            cancel.set()

    def console_reset(self) -> None:
        """Drop the in-memory conversation — `Esc` in the viewer.

        The history is ephemeral by design (ADR 0002); clearing it just
        nulls the field so the next turn re-seeds from scratch. Also
        trips any in-flight turn's cancel flag, so a reset mid-stream
        doesn't leave a worker writing history back over the cleared
        conversation.
        """
        with self._lock:
            cancel = self._console_cancel
            self._console_history = None
        if cancel is not None:
            cancel.set()

    @property
    def console_history(self) -> Any:
        with self._lock:
            return self._console_history

    @property
    def console_busy(self) -> bool:
        return self._console_slot.held

    @property
    def explainer_busy(self) -> bool:
        return self._explainer_slot.held

    def _run_console_turn(
        self,
        asker: ConsoleCallable,
        question: str,
        history: Any,
        console_id: str,
        cancel: threading.Event,
        selection: dict[str, Any] | None,
    ) -> None:
        """Drive one streaming turn on its own thread and event loop.

        Every frame is unbuffered — a reload starts the console fresh.
        The conversation history is advanced only on clean completion: a
        cancelled turn is discarded.
        """

        def on_delta(chunk: str) -> None:
            self._publish("console-delta", {"console_id": console_id, "text": chunk}, buffer=False)

        def on_tool(label: str) -> None:
            self._publish("console-tool", {"console_id": console_id, "label": label}, buffer=False)

        # Cancellation is the one outcome that isn't an error, so it is
        # the one class this has to recognise by name.
        from ..augment.console import ConsoleCancelled

        try:
            answer, new_history = asyncio.run(asker(question, history, on_delta, on_tool, cancel, selection))
        except ConsoleCancelled:
            # Partial turn abandoned: history stays as it was, the
            # frontend keeps whatever streamed, and the conversation
            # remains usable.
            self._publish("console-done", {"console_id": console_id, "cancelled": True}, buffer=False)
        except errors.ScrError as e:
            # A refusal states itself; only a bug needs its type named.
            self._publish("console-error", {"console_id": console_id, "error": str(e)}, buffer=False)
        except Exception as e:
            log.exception("console turn failed for question=%r", question[:120])
            self._publish(
                "console-error",
                {"console_id": console_id, "error": f"{type(e).__name__}: {e}"},
                buffer=False,
            )
        else:
            with self._lock:
                self._console_history = new_history
            self._publish("console-done", {"console_id": console_id, "answer": answer}, buffer=False)
        finally:
            with self._lock:
                self._console_cancel = None
            self._console_slot.release()

    # --- post (confirm-and-post modal) ----------------------------------

    def post_config(self) -> dict[str, Any]:
        """Whether this review is configured for posting.

        The viewer fetches this once on boot. When `posting` is true, the
        Done button opens the confirmation modal instead of exiting
        directly. The other fields are display-only metadata (modal
        header: "Posting N comments to <repo>#<number> at <head_sha>").
        """
        if self._post_meta is None:
            return {"posting": False}
        return {"posting": True, **self._post_meta}

    def post_preview(self) -> list[dict[str, Any]]:
        """The comments that would be posted, as the modal renders them.

        Computed on demand because the comment store mutates throughout
        the session — a preview taken at startup would be stale by Done.
        Each row carries the id (for the selection round-trip),
        file/side/line (for context), the body, and `is_reply`.

        Raises:
            ReviewSessionError: 409 — this review isn't posting.
        """
        if self._post_callback is None:
            raise self._not_posting()
        # Local import keeps the GitHub mapping types off the import
        # graph of a review that never posts.
        from .github import comments_to_github

        all_comments = self.store.all()
        by_id = {c.id: c for c in all_comments}
        rows: list[dict[str, Any]] = []
        for posted in comments_to_github(all_comments):
            src = by_id.get(posted.source_id or "")
            if src is None:
                continue
            rows.append(
                {
                    "id": src.id,
                    "file": src.file,
                    "side": src.side,
                    "line": src.line,
                    "body": posted.body,
                    "is_reply": posted.is_reply,
                }
            )
        return rows

    def post(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Post the selected comments; keep the result, broadcast it.

        Wire format: `{ "comment_ids": ["id1", "id2", ...] }`. The
        callback filters the store down to those ids, maps to the wire
        shape, and posts. The result is kept on the session so the CLI
        can hand it back after `wait_until_done` returns, and fanned out
        as a `posted` event for other open tabs.

        Does not end the session: the modal stays open showing the
        result so the reviewer can click through to the GitHub URL, and
        ends the session explicitly with its Close button.

        Raises:
            ReviewSessionError: 409 — this review isn't posting; 400 on a
                malformed selection.
        """
        if self._post_callback is None:
            raise self._not_posting()
        ids = payload.get("comment_ids")
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise ReviewSessionError(400, "comment_ids must be a list of strings")

        try:
            result = self._post_callback(ids)
        except errors.ScrError:
            raise
        except Exception:
            log.exception("post callback raised")
            raise

        response = {
            "posted": result.posted,
            "review_url": result.review_url,
            "review_id": result.review_id,
        }
        with self._lock:
            self._posted_result = result
        self._publish("posted", response)
        return response

    def _not_posting(self) -> ReviewSessionError:
        return ReviewSessionError(409, "this server isn't configured for posting")
