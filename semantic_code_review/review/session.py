"""One review session: the state a review holds and what can be done to it.

A review is a [[run-directory]] plus the things a reviewer can ask of it
— summarise a fold, write the change-explainer document, hold a console
turn, send a comment to the counterpart, submit the review. `ReviewSession`
owns that state and those operations, including the stream to Claude: a
Send wakes the `--wait` blocked in `wait_for_batch`, which hands the
oldest pending [[batch]] over and marks it delivered; while one is
blocked the session is [[listening]]. With GitHub as the [[counterpart]]
a Send is delivered into the [[pending-review]] at once through the
`ReviewSink` the session was given; a refusal leaves the comment unsent
for the next retry, and [[submit]] publishes what the review holds.
`review/server.py` is the HTTP transport in front of it and holds no
session state beyond its own SSE fan-out, which the session publishes
*through* rather than owns.

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
import time
from collections.abc import Callable, Coroutine
from typing import Any, Literal, Protocol

from .. import errors, paths
from . import comments, github_graphql, pending_review

log = logging.getLogger(__name__)


#: Who a review's comments are for — the [[counterpart]]. Claude in
#: review mode (`scr review`), GitHub in PR mode (`scr pr`). One
#: lifecycle serves both; the viewer mounts the listening indicator or
#: Submit by it.
Counterpart = Literal["claude", "github"]


#: Signature of the augment pass `serve_review` runs while the page is
#: live. The second argument is the publisher bound to the review
#: server's SSE channel; pass it through to `augment_run_dir(on_event=)`
#: so the pipeline can stream overview / per-hunk events to the page.
AugmentCallable = Callable[
    [paths.RunDir, Callable[[str, dict], None]],
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


def _read_worktree_file(worktree: pathlib.Path, rel: str) -> str | None:
    """Read a file from a base/head worktree; None when absent or unreadable.

    No size cap: region expansion discloses the whole file on demand (ADR
    0008), so a line's reachability cannot depend on the file's size.

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
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        log.warning("file-text: %s unreadable: %s", path, e)
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
        run_dir: paths.RunDir,
        viewer_json: dict[str, Any],
        store: comments.CommentStore,
        publish: EventPublisher,
        counterpart: Counterpart,
        github: pending_review.ReviewSink | None = None,
        debug: bool = False,
        explainer_enabled: bool = False,
    ) -> None:
        if (counterpart == "github") != (github is not None):
            raise ValueError("a GitHub counterpart needs a review sink, and only it takes one")
        self.run_dir = run_dir
        self.store = store
        self.counterpart = counterpart
        self._github = github
        # One flush to GitHub at a time: two requests delivering the same
        # sent comment would add it twice.
        self._github_lock = threading.Lock()
        self._submitted_url: str | None = None
        self._unanchored = 0
        #: Known at construction, not at attach time: the viewer decides
        #: whether to mount the overview-mode button on its first
        #: /data.json, well before augmentation has finished.
        self.explainer_enabled = explainer_enabled
        self._viewer_json = viewer_json
        self._publish = publish
        self._debug = debug
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
        # The stream to Claude: a Send notifies here; `wait_for_batch`
        # blocks on it. Guards the listener count and the closed flag too.
        self._batch_cond = threading.Condition()
        self._listeners = 0
        self._closed = False

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
        """The `/data.json` payload: the viewer JSON plus what the viewer
        needs before any pass has run — the two runtime flags, and the
        run id its per-tab view state is keyed by.

        Merged at read time because `set_viewer_json` swaps the diff
        payload wholesale; the run id is the session's, not the diff's,
        so the pending and the augmented payloads carry the same one.
        """
        data = {
            **self._viewer_json,
            "run_id": self.run_dir.slug,
            "debug": self._debug,
            "explainer": self.explainer_enabled,
            "counterpart": self.counterpart,
            "listening": self.listening,
        }
        if self._github is not None:
            data["pending_review"] = self.pending_review_state()
        return data

    def close(self) -> None:
        """The session is ending: every `wait_for_batch` returns `ended`."""
        with self._batch_cond:
            self._closed = True
            self._batch_cond.notify_all()

    # --- the stream to Claude -------------------------------------------

    @property
    def listening(self) -> bool:
        """Whether a `--wait` is attached — Claude is [[listening]]."""
        with self._batch_cond:
            return self._listeners > 0

    def wait_for_batch(self, *, timeout: float) -> dict[str, Any]:
        """Block until a [[batch]] is pending, `timeout` seconds pass, or
        the session closes; deliver the oldest pending batch if there is one.

        Returns one of:
            `{status: "batch", batch_no, run_id, entries: [{comment, state,
            excerpt}]}` — the batch, whose comments are now delivered;
            `{status: "nothing-yet"}`; `{status: "ended"}`.

        While blocked the caller counts as a listener: the viewer's
        indicator flips with the first attach and the last detach, and the
        server's idle clock does not run.

        Raises:
            ReviewSessionError: 409 — the counterpart is GitHub; there is
                no stream, and a batch taken here would mark comments
                delivered that GitHub never saw.
        """
        if self._github is not None:
            raise ReviewSessionError(409, "this review's counterpart is GitHub; there is no stream to wait on")
        deadline = time.monotonic() + timeout
        with self._batch_cond:
            self._listeners += 1
            if self._listeners == 1:
                self._publish("listening", {"listening": True})
            try:
                while True:
                    if self._closed:
                        return {"status": "ended"}
                    pending = self.store.pending_batches()
                    if pending:
                        # Delivered under the condition so two waiters
                        # cannot both take the same batch.
                        batch = self.store.deliver(pending[0][0])
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return {"status": "nothing-yet"}
                    self._batch_cond.wait(remaining)
            finally:
                self._listeners -= 1
                if self._listeners == 0:
                    self._publish("listening", {"listening": False})
        for entry in batch.entries:
            if entry.state == "withdrawn":
                self._publish("comment-deleted", {"id": entry.comment.id})
        for c in self.store.all():
            if c.batch_no == batch.batch_no and c.delivery == "delivered":
                self._publish_comment(c)
        return {
            "status": "batch",
            "batch_no": batch.batch_no,
            "run_id": self.run_dir.slug,
            "entries": [
                {
                    "comment": e.comment.model_dump(),
                    "state": e.state,
                    "excerpt": None if e.state == "withdrawn" else self._excerpt_for(e.comment),
                }
                for e in batch.entries
            ],
        }

    def _excerpt_for(self, c: comments.Comment) -> str | None:
        """The anchored code, two lines either side, from the side's
        worktree: `head/` for the new side, `base/` (at the file's old
        path) for the old.
        """
        if c.side == "old":
            old_path = c.file
            for file in self._viewer_json.get("files") or []:
                if file.get("path") == c.file and file.get("old_path"):
                    old_path = str(file["old_path"])
                    break
            text = _read_worktree_file(self.run_dir.base, old_path)
        else:
            text = _read_worktree_file(self.run_dir.head, c.file)
        return comments.excerpt(text, c.line)

    # --- reviewer comments ----------------------------------------------
    # The store owns the lifecycle transitions; the session decodes what
    # arrived, calls one store method, and fans the changed comments out
    # as `comment` / `comment-deleted` frames so every tab follows. What
    # a Send then does depends on the counterpart: wake the `--wait`, or
    # deliver into the pending review now.

    def upsert_comment(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Add or edit a reviewer comment, or take a counterpart's reply.

        A payload with `source: "claude"` is a reply from Claude
        (`scr comment reply`): anchored on its parent, read-only to the
        reviewer, outside the lifecycle. Anything else is the reviewer's
        and lands as a local draft whatever source it claims.

        With GitHub as the counterpart an edit to a delivered comment is
        sent again at once: the pending comment is editable in place, so
        there is no re-Send for the reviewer to make. A refused update
        leaves it unsent like any other delivery.

        Raises:
            ReadOnlyCommentError: 403 — editing an ingested or claude comment.
            CommentStateError: 409 — editing a withdrawn tombstone; a
                reply with no parent or body.
            CommentNotFound: 404 — a reply to a comment the store lacks.
            ReviewSessionError: 400 — the payload is not a comment.
        """
        if payload.get("source") == "claude":
            c = self.store.add_reply(payload, source="claude", author=comments.CLAUDE_AUTHOR)
        else:
            try:
                c = self.store.upsert(payload)
            except errors.ScrError:
                raise
            except Exception as e:  # pydantic throws many kinds
                raise ReviewSessionError(400, str(e)) from e
        if self._github is not None and c.is_draft and c.deliveries > 0:
            _, c = self.store.send(c.id)
            self._publish_comment(c)
            self._flush_to_github()
            return self.store.get(c.id).model_dump()
        self._publish_comment(c)
        return c.model_dump()

    def delete_comment(self, comment_id: str) -> dict[str, Any]:
        """Delete a reviewer comment. A delivered one becomes a withdrawn
        tombstone — its own batch, or a deletion in the pending review;
        the response says which happened.

        Raises:
            CommentNotFound: 404. ReadOnlyCommentError: 403.
        """
        tombstone = self.store.delete(comment_id)
        self._publish("comment-deleted", {"id": comment_id})
        if tombstone is not None:
            self._counterpart_notified()
        return {"ok": True, "withdrawn": tombstone is not None}

    def send_comment(self, comment_id: str) -> dict[str, Any]:
        """Send one draft as its own batch.

        Raises:
            CommentNotFound: 404. ReadOnlyCommentError: 403.
            CommentStateError: 409 — not a draft.
        """
        batch_no, sent = self.store.send(comment_id)
        self._publish_comment(sent)
        return {"batch_no": batch_no, "comment_ids": [sent.id], **self._counterpart_notified()}

    def send_all(self) -> dict[str, Any]:
        """Send every draft as one batch. With no drafts nothing is sent
        and `batch_no` is null.
        """
        batch_no, sent = self.store.send_all()
        for c in sent:
            self._publish_comment(c)
        extra = self._counterpart_notified() if sent else {}
        return {"batch_no": batch_no, "comment_ids": [c.id for c in sent], **extra}

    def set_thread_resolved(self, comment_id: str, resolved: bool) -> dict[str, Any]:
        """Resolve or reopen the thread holding `comment_id`.

        With GitHub as the counterpart an upstream thread is flipped on
        GitHub first, as GitHub's own UI does, and recorded only once that
        lands; a thread still in the pending review cannot be resolved
        there and is refused.

        Raises:
            CommentNotFound: 404. ReadOnlyCommentError: 403 — an
                ingested thread's resolution lives on GitHub (review mode).
            ReviewSessionError: 409 — PR mode: a pending thread, or an
                upstream thread whose id the run never recorded.
            GitHubRefused: 502 — GitHub refused; nothing changed locally.
        """
        if self._github is not None:
            root = self.store.thread_root(comment_id)
            if root.source != "github":
                raise ReviewSessionError(409, "a thread in the pending review cannot be resolved until it is submitted")
            if not root.thread_id:
                raise ReviewSessionError(409, f"thread {root.id} has no GitHub thread id recorded")
            self._github.set_thread_resolved(root.thread_id, resolved)
            changed = self.store.set_thread_resolved(comment_id, resolved, upstream=True)
        else:
            changed = self.store.set_thread_resolved(comment_id, resolved)
        for c in changed:
            self._publish_comment(c)
        return {"ok": True, "resolved": resolved, "comment_ids": [c.id for c in changed]}

    def _publish_comment(self, c: comments.Comment) -> None:
        self._publish("comment", c.model_dump())

    def _counterpart_notified(self) -> dict[str, Any]:
        """Something is sent: wake any `--wait`, or flush to the pending
        review. Returns what the Send response carries beyond the ids —
        the pending review's state, in PR mode.
        """
        if self._github is not None:
            return {"pending_review": self._flush_to_github()}
        with self._batch_cond:
            self._batch_cond.notify_all()
        return {}

    # --- the pending review (PR mode) --------------------------------------

    def resume_pending_review(self) -> None:
        """Take over the reviewer's pending review on the PR, if one
        exists: its comments the store lacks are adopted as delivered,
        so nothing is submitted sight-unseen. Best-effort at start: a
        lookup GitHub refuses is logged, and the first Send looks again.
        """
        if self._github is None:
            raise ReviewSessionError(409, "this review's counterpart is Claude")
        try:
            resumed = self._github.resume(self.store.node_index())
        except github_graphql.GitHubRefused as e:
            log.warning("could not look for a pending review: %s", e)
            return
        if resumed is None:
            return
        for c in resumed.comments:
            self.store.adopt(c)
        for cid in resumed.reclaimed:
            # A comment this run already holds as the reviewer's needs
            # nothing; one the ingest recorded as upstream is theirs.
            if self.store.get(cid).source == "github":
                self.store.reclaim(cid)
        self._unanchored = resumed.unanchored
        log.info(
            "resumed the pending review: %d comment(s) adopted, %d without a line",
            len(resumed.comments),
            resumed.unanchored,
        )

    def retry_deliveries(self) -> dict[str, Any]:
        """Deliver every unsent comment again — the viewer's interval, and
        a gesture after a refusal. Returns the pending review's state.

        Raises:
            ReviewSessionError: 409 — the counterpart is Claude.
        """
        if self._github is None:
            raise ReviewSessionError(409, "this review's counterpart is Claude; nothing is retried")
        return self._flush_to_github()

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Publish the pending review with a verdict.

        Wire format: `{ "event": "COMMENT" | "APPROVE" | "REQUEST_CHANGES",
        "body"?: str }`. Every comment the review holds becomes an
        upstream comment (read-only) and is fanned out; drafts stay the
        reviewer's. The session goes on: the tab closing ends it.

        Raises:
            ReviewSessionError: 409 — the counterpart is Claude; 400 — a
                malformed payload; 409 with `unsent` — a comment GitHub
                does not hold as it stands, named, so nothing is published
                that the reviewer believes is in it.
            GitHubRefused: 502 — GitHub refused the submit.
        """
        if self._github is None:
            raise ReviewSessionError(409, "this review's counterpart is Claude; there is nothing to submit")
        event = payload.get("event")
        if event not in github_graphql.REVIEW_EVENTS:
            raise ReviewSessionError(400, f"event must be one of {', '.join(github_graphql.REVIEW_EVENTS)}")
        body = payload.get("body", "")
        if not isinstance(body, str):
            raise ReviewSessionError(400, "body must be a string")
        with self._github_lock:
            unsent = self._unsent()
            if unsent:
                n = len(unsent)
                raise ReviewSessionError(
                    409,
                    f"{n} comment{'s are' if n != 1 else ' is'} unsent; GitHub does not hold the review as shown",
                    unsent=unsent,
                )
            submitted = self._github.submit(event, body)
            changed = self.store.mark_submitted()
            self._submitted_url = submitted.url
        for c in changed:
            self._publish_comment(c)
        state = self.pending_review_state()
        self._publish("pending-review", state)
        return {"review_url": submitted.url, "event": event, "submitted": len(changed)}

    def pending_review_state(self) -> dict[str, Any]:
        """What the viewer shows of the pending review: the comments GitHub
        does not hold as they stand (each with why), the URL of the review
        once submitted, and how many pending comments have no line in
        this diff and so are not shown.
        """
        return {
            "unsent": self._unsent(),
            "submitted_url": self._submitted_url,
            "unanchored": self._unanchored,
        }

    def _unsent(self) -> list[dict[str, Any]]:
        return [
            {
                "id": c.id,
                "file": c.file,
                "side": c.side,
                "line": c.line,
                "body": c.body,
                "deleted": c.withdrawn,
                "error": c.send_error,
            }
            for c in self.store.undelivered()
        ]

    def _flush_to_github(self) -> dict[str, Any]:
        """Deliver every sent comment into the pending review, one at a
        time; a refusal marks that comment unsent and the rest go on.
        Fans out each comment's new state and then the review's.
        """
        assert self._github is not None
        with self._github_lock:
            for c in self.store.undelivered():
                by_id = {k.id: k for k in self.store.all()}
                try:
                    delivered = self._github.deliver(c, by_id)
                except github_graphql.GitHubRefused as e:
                    log.warning("GitHub refused comment %s: %s", c.id, e)
                    failed = self.store.mark_send_failed(c.id, str(e))
                    if not failed.withdrawn:
                        self._publish_comment(failed)
                    continue
                landed = self.store.mark_delivered(c.id, node_id=delivered.node_id, thread_id=delivered.thread_id)
                if landed is not None:
                    self._publish_comment(landed)
            state = self.pending_review_state()
        self._publish("pending-review", state)
        return state

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

        # Seed the prompt with the definition the region is. Regions carry
        # the definition's qualified_name / kind (null for an indentation
        # stanza); an address naming no region leaves the prompt unseeded.
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
        """The addressed region in the file's `fold_regions`, or None."""
        file = self._file_block(address.file_idx)
        if file is None:
            return None
        for region in file.get("fold_regions") or []:
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

    # --- full file text ---------------------------------------------------

    def file_text(self, file_idx: int) -> dict[str, Any]:
        """The full base+head source of one changed file.

        Lazy backing for rendered markdown mode (ADR 0004) and for region
        expansion in the text diff (ADR 0008) — fetched the first time
        either asks, so `ViewerData` carries no full-text payload. Reads
        from the `base/` / `head/` worktrees already materialised in the
        run dir.

        Returns `{file_idx, path, base, head}` where `base` / `head` are
        the full file text or None when that side has no file (added
        file → base None; deleted file → head None).

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
            "base": _read_worktree_file(self.run_dir.base, base_rel),
            "head": _read_worktree_file(self.run_dir.head, path),
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
