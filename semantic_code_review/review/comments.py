"""Reviewer comments: model, storage, the lifecycle, markdown formatters.

A [[reviewer-comment]] has a lifecycle towards its [[counterpart]] (ADR
0009): a [[draft]] until the reviewer [[send]]s it, [[sent]] until the
counterpart holds it, then [[delivered]]. `CommentStore` is the single
owner of the transitions; the session calls its methods and the routes
call the session. What one Send gesture delivers to Claude is a
[[batch]], numbered by the store at Send time and handed over whole by
`deliver`; GitHub takes one comment at a time (`mark_delivered`,
`mark_send_failed`), and a [[pending-review]] that already exists is
taken in with `adopt`.

Only `local` comments are in the lifecycle. Ingested (`github`) and
`claude`-authored comments are what the counterpart said, read-only to
the reviewer; their lifecycle fields are inert. `mark_submitted` is the
one transition out of `local`: a comment published in a GitHub review
is an upstream comment from then on.
"""

from __future__ import annotations

import dataclasses
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .. import errors

CommentSource = Literal["local", "github", "claude"]

#: Where a local comment stands towards its counterpart.
Delivery = Literal["draft", "sent", "delivered"]

#: What one delivery of a comment carries.
BatchState = Literal["new", "revised", "withdrawn", "reply"]

#: The author label a `claude` reply carries.
CLAUDE_AUTHOR = "claude"


class ReadOnlyCommentError(errors.ScrError):
    """Raised by CommentStore when the caller tries to mutate a comment
    that wasn't authored in this run (e.g. an ingested PR comment).
    """

    status = 403


class CommentStateError(errors.ScrError):
    """A transition the lifecycle does not allow: sending what is not a
    draft, editing a withdrawn tombstone, replying to nothing.
    """

    status = 409


class CommentNotFound(errors.ScrError):
    status = 404


class Comment(BaseModel):
    id: str
    file: str
    side: str = Field(pattern=r"^(old|new)$")
    line: int
    body: str
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    # Provenance + threading. All optional; absent on session-local comments
    # so the on-disk format stays backwards-compatible with older runs.
    source: CommentSource = "local"
    author: str | None = None
    author_avatar_url: str | None = None
    in_reply_to_id: str | None = None
    # The commit SHA the comment was anchored to upstream. May not match the
    # run's head_sha if upstream advanced after the comment was left — the
    # viewer surfaces the comment at (file, side, line) regardless.
    commit_id: str | None = None
    html_url: str | None = None
    # GitHub-rendered body. When present the viewer prefers it over `body`
    # so we don't ship a markdown parser to the client.
    body_html: str | None = None
    # True when the review thread containing this comment is resolved —
    # on GitHub for an ingested thread, by `scr comment resolve` for a
    # local one. Denormalised onto every member of the thread; the viewer
    # reads it from the root entry to decide whether to collapse the
    # thread by default.
    thread_resolved: bool = False
    # Head-side anchor after diff-based propagation from `commit_id` to
    # the run's `head_sha`. `head_line` is the propagated line number
    # the viewer should pin to; `anchor_status` is one of
    # `anchored | shifted | orphaned | file_gone | commit_unavailable`.
    # Both null on session-local comments (which are always at head).
    head_line: int | None = None
    anchor_status: str | None = None
    # GraphQL node id ("opaque string", distinct from the integer
    # `databaseId` embedded in `id`). What the GraphQL mutations address
    # a comment by: as a reply parent, to update or delete it. Populated
    # on ingest, and on a local comment once the pending review holds it.
    node_id: str | None = None
    # GraphQL id of the review thread holding this comment — what
    # resolve / unresolve address. Populated on ingest and on delivery.
    thread_id: str | None = None
    # Stable id of the LLM annotation this comment was promoted from,
    # if any. Examples: "H0_3:span:42-42", "H0_3:smell:perf". The
    # viewer hides any annotation whose id matches a derived_from on
    # an existing local comment so the source annotation visibly
    # "transitions" into the comment and a re-augment doesn't
    # resurrect it. Null on comments authored from the gutter.
    derived_from: str | None = None
    # Lifecycle towards the counterpart (ADR 0009). Meaningful on local
    # comments only. Absent in a file written before the lifecycle
    # existed, so every old local comment loads as a draft.
    delivery: Delivery = "draft"
    # How many times the counterpart has received this comment; the next
    # delivery after the first is `revised`.
    deliveries: int = 0
    # The batch this comment was last sent in. Kept after delivery.
    batch_no: int | None = None
    # A delivered comment the reviewer deleted: kept as a tombstone until
    # its withdrawal is delivered, then dropped. Never rendered.
    withdrawn: bool = False
    # Why the last delivery to GitHub failed; the comment is *unsent*
    # while set. Cleared by the next Send and by a delivery that lands.
    send_error: str | None = None

    @property
    def is_writable(self) -> bool:
        """True iff this run owns the comment — i.e. the server may
        mutate or delete it. Ingested and claude-authored comments stay
        read-only.
        """
        return self.source == "local"

    @property
    def is_draft(self) -> bool:
        return self.source == "local" and not self.withdrawn and self.delivery == "draft"

    @property
    def is_undelivered(self) -> bool:
        """A local comment the counterpart does not hold as it stands."""
        return self.source == "local" and not self.withdrawn and self.delivery != "delivered"

    def batch_state(self) -> BatchState:
        """What delivering this comment now would carry."""
        if self.withdrawn:
            return "withdrawn"
        if self.deliveries > 0:
            return "revised"
        if self.in_reply_to_id:
            return "reply"
        return "new"


@dataclasses.dataclass(frozen=True)
class BatchEntry:
    """One comment as a [[batch]] carries it: the comment as it stood at
    delivery and the state that delivery had.
    """

    comment: Comment
    state: BatchState


@dataclasses.dataclass(frozen=True)
class Batch:
    """What one Send gesture delivered. `entries` are in store order."""

    batch_no: int
    entries: list[BatchEntry]


class CommentStore:
    """Thread-safe in-memory store with atomic flush to disk.

    Every mutation returns only after the backing file has been written.
    Callers on the CLI side read the file directly when the server has
    exited. The store is the single owner of the lifecycle transitions.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._items: dict[str, Comment] = {}
        # The highest batch number ever assigned, so numbering never
        # restarts even after every comment of a batch is gone.
        self._last_batch_no = 0
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                for d in data.get("comments", []):
                    c = Comment.model_validate(d)
                    self._items[c.id] = c
                self._last_batch_no = int(data.get("last_batch_no", 0))
            except (OSError, ValueError):
                pass

    def upsert(self, payload: dict[str, Any]) -> Comment:
        """Add a reviewer comment as a draft, or edit one.

        An edit replaces the text of a draft or a sent comment in place; an
        edit to a delivered comment returns it to draft, so the viewer
        shows it needs re-sending and its next delivery is `revised`.

        Raises:
            ReadOnlyCommentError: the id names an ingested or claude comment.
            CommentStateError: the id names a withdrawn tombstone.
        """
        with self._lock:
            now = time.time()
            existing = self._items.get(payload.get("id", ""))
            if existing is not None and not existing.is_writable:
                raise ReadOnlyCommentError(f"comment {existing.id} is from {existing.source}; not editable")
            if existing is not None and existing.withdrawn:
                raise CommentStateError(f"comment {existing.id} is withdrawn")
            if existing is None:
                c = Comment.model_validate(payload)
                c.created_at = payload.get("created_at", now)
                c.updated_at = now
                # Ignore any source or lifecycle claim on the wire —
                # newly-authored comments are always local drafts.
                c.source = "local"
                c.delivery = "draft"
                c.deliveries = 0
                c.batch_no = None
                c.withdrawn = False
            else:
                data = existing.model_dump()
                data.update({k: v for k, v in payload.items() if k in {"body", "line", "side", "file"}})
                data["updated_at"] = now
                if existing.delivery == "delivered":
                    data["delivery"] = "draft"
                c = Comment.model_validate(data)
            self._items[c.id] = c
            self._flush_locked()
            return c

    def add_reply(self, payload: dict[str, Any], *, source: CommentSource, author: str) -> Comment:
        """Add a counterpart's reply into a thread: anchored where the
        parent is, read-only to the reviewer, outside the lifecycle.

        Wire format: `{ in_reply_to_id: str, body: str, id?: str }`.

        Raises:
            CommentStateError: no parent id, an empty body, or an id
                already taken.
            CommentNotFound: the parent is not in this store.
        """
        parent_id = str(payload.get("in_reply_to_id") or "")
        body = str(payload.get("body") or "")
        if not parent_id:
            raise CommentStateError("a reply needs in_reply_to_id")
        if not body.strip():
            raise CommentStateError("a reply needs a body")
        with self._lock:
            parent = self._items.get(parent_id)
            if parent is None or parent.withdrawn:
                raise CommentNotFound(f"comment {parent_id} not found")
            now = time.time()
            c = Comment(
                id=str(payload.get("id") or f"{source}-{int(now * 1000):x}"),
                file=parent.file,
                side=parent.side,
                line=parent.line,
                body=body,
                created_at=now,
                updated_at=now,
                source=source,
                author=author,
                in_reply_to_id=parent_id,
                thread_resolved=parent.thread_resolved,
                head_line=parent.head_line,
                anchor_status=parent.anchor_status,
            )
            if c.id in self._items:
                raise CommentStateError(f"comment {c.id} already exists")
            self._items[c.id] = c
            self._flush_locked()
            return c

    # --- the pending review (PR mode) ---------------------------------------

    def undelivered(self) -> list[Comment]:
        """Every sent comment GitHub does not hold as it stands, tombstones
        included, in store order: what a flush to the pending review
        works through, and what Submit is refused over.
        """
        with self._lock:
            return [c for c in self._ordered_locked() if c.delivery == "sent"]

    def mark_delivered(
        self, comment_id: str, *, node_id: str | None = None, thread_id: str | None = None
    ) -> Comment | None:
        """GitHub holds this comment as it stands: record the ids it
        gave, clear any refusal, count the delivery. A tombstone is
        dropped and None returned.

        Raises:
            CommentNotFound: no such comment.
            CommentStateError: not a sent comment.
        """
        with self._lock:
            c = self._items.get(comment_id)
            if c is None:
                raise CommentNotFound(f"comment {comment_id} not found")
            if c.delivery != "sent":
                raise CommentStateError(f"comment {comment_id} is {c.delivery}, not sent")
            if c.withdrawn:
                del self._items[comment_id]
                self._flush_locked()
                return None
            data = c.model_dump()
            data.update({"delivery": "delivered", "deliveries": c.deliveries + 1, "send_error": None})
            if node_id is not None:
                data["node_id"] = node_id
            if thread_id is not None:
                data["thread_id"] = thread_id
            delivered = Comment.model_validate(data)
            self._items[comment_id] = delivered
            self._flush_locked()
            return delivered

    def mark_send_failed(self, comment_id: str, error: str) -> Comment:
        """GitHub refused this comment's delivery: it stays sent, marked
        *unsent* with the reason, for the next retry.

        Raises:
            CommentNotFound: no such comment.
            CommentStateError: not a sent comment.
        """
        with self._lock:
            c = self._items.get(comment_id)
            if c is None:
                raise CommentNotFound(f"comment {comment_id} not found")
            if c.delivery != "sent":
                raise CommentStateError(f"comment {comment_id} is {c.delivery}, not sent")
            data = c.model_dump()
            data.update({"send_error": error, "updated_at": time.time()})
            failed = Comment.model_validate(data)
            self._items[comment_id] = failed
            self._flush_locked()
            return failed

    def adopt(self, c: Comment) -> Comment:
        """Take in a comment an existing pending review already holds: the
        reviewer's own, delivered once, editable and deletable.

        Raises:
            CommentStateError: the id is taken, the comment carries no
                `node_id`, or its source is not local.
        """
        if c.source != "local":
            raise CommentStateError(f"comment {c.id} is from {c.source}; only the reviewer's are adopted")
        if not c.node_id:
            raise CommentStateError(f"comment {c.id} has no node_id; nothing on GitHub to adopt")
        with self._lock:
            if c.id in self._items:
                raise CommentStateError(f"comment {c.id} already exists")
            data = c.model_dump()
            data.update({"delivery": "delivered", "deliveries": 1, "batch_no": None, "withdrawn": False})
            data["send_error"] = None
            adopted = Comment.model_validate(data)
            self._items[adopted.id] = adopted
            self._flush_locked()
            return adopted

    def reclaim(self, comment_id: str) -> Comment:
        """A comment recorded as upstream turns out to be the reviewer's
        own, still in the pending review: make it theirs again — local,
        delivered once, editable.

        Raises:
            CommentNotFound: no such comment.
            CommentStateError: not an upstream comment with a node id.
        """
        with self._lock:
            c = self._items.get(comment_id)
            if c is None or c.withdrawn:
                raise CommentNotFound(f"comment {comment_id} not found")
            if c.source != "github" or not c.node_id:
                raise CommentStateError(f"comment {comment_id} is not an upstream comment with a node id")
            data = c.model_dump()
            data.update({"source": "local", "delivery": "delivered", "deliveries": 1, "send_error": None})
            reclaimed = Comment.model_validate(data)
            self._items[comment_id] = reclaimed
            self._flush_locked()
            return reclaimed

    def node_index(self) -> dict[str, str]:
        """`node_id -> comment id` for every comment GitHub knows."""
        with self._lock:
            return {c.node_id: c.id for c in self._items.values() if c.node_id}

    def get(self, comment_id: str) -> Comment:
        """One comment as it stands.

        Raises:
            CommentNotFound: no such comment, or a withdrawn tombstone.
        """
        with self._lock:
            c = self._items.get(comment_id)
            if c is None or c.withdrawn:
                raise CommentNotFound(f"comment {comment_id} not found")
            return c

    def mark_submitted(self) -> list[Comment]:
        """The pending review was published: every local comment it held
        is an upstream comment now, read-only like every other comment
        that exists on GitHub. Returns the comments changed. Drafts stay
        the reviewer's.
        """
        with self._lock:
            changed: list[Comment] = []
            for c in self._ordered_locked():
                if c.source != "local" or c.withdrawn or c.delivery != "delivered" or not c.node_id:
                    continue
                data = c.model_dump()
                data.update({"source": "github", "updated_at": time.time()})
                updated = Comment.model_validate(data)
                self._items[c.id] = updated
                changed.append(updated)
            if changed:
                self._flush_locked()
            return changed

    def delete(self, comment_id: str) -> Comment | None:
        """Delete a reviewer comment.

        One the counterpart never held is removed outright and the call
        returns None. One it has held stays as a tombstone whose
        withdrawal is its own batch; the tombstone is returned.

        Raises:
            CommentNotFound: no such comment (or already withdrawn).
            ReadOnlyCommentError: not the reviewer's.
        """
        with self._lock:
            existing = self._items.get(comment_id)
            if existing is None or existing.withdrawn:
                raise CommentNotFound(f"comment {comment_id} not found")
            if not existing.is_writable:
                raise ReadOnlyCommentError(f"comment {existing.id} is from {existing.source}; not deletable")
            if existing.deliveries == 0:
                del self._items[comment_id]
                self._flush_locked()
                return None
            data = existing.model_dump()
            data.update(
                {
                    "withdrawn": True,
                    "delivery": "sent",
                    "batch_no": self._next_batch_no_locked(),
                    "send_error": None,
                    "updated_at": time.time(),
                }
            )
            tombstone = Comment.model_validate(data)
            self._items[comment_id] = tombstone
            self._flush_locked()
            return tombstone

    # --- the lifecycle ----------------------------------------------------

    def send(self, comment_id: str) -> tuple[int, Comment]:
        """Send one draft: its own batch.

        Raises:
            CommentNotFound: no such comment.
            ReadOnlyCommentError: not the reviewer's.
            CommentStateError: not a draft.
        """
        with self._lock:
            c = self._items.get(comment_id)
            if c is None or c.withdrawn:
                raise CommentNotFound(f"comment {comment_id} not found")
            if not c.is_writable:
                raise ReadOnlyCommentError(f"comment {comment_id} is from {c.source}; not sendable")
            if c.delivery != "draft":
                raise CommentStateError(f"comment {comment_id} is {c.delivery}, not a draft")
            batch_no = self._next_batch_no_locked()
            sent = self._mark_sent_locked(c, batch_no)
            self._flush_locked()
            return batch_no, sent

    def send_all(self) -> tuple[int | None, list[Comment]]:
        """Send every draft as one batch. No drafts → `(None, [])` and
        nothing changes.
        """
        with self._lock:
            drafts = [c for c in self._ordered_locked() if c.is_draft]
            if not drafts:
                return None, []
            batch_no = self._next_batch_no_locked()
            sent = [self._mark_sent_locked(c, batch_no) for c in drafts]
            self._flush_locked()
            return batch_no, sent

    def pending_batches(self) -> list[tuple[int, list[Comment]]]:
        """Every batch the counterpart has not received, oldest first, with
        its comments (tombstones included) in store order.
        """
        with self._lock:
            return self._pending_batches_locked()

    def deliver(self, batch_no: int) -> Batch:
        """Hand a batch over: snapshot what it carries, then mark every
        comment in it delivered and drop the tombstones.

        Raises:
            CommentNotFound: no undelivered batch has that number.
        """
        with self._lock:
            members = [c for c in self._ordered_locked() if c.delivery == "sent" and c.batch_no == batch_no]
            if not members:
                raise CommentNotFound(f"no pending batch {batch_no}")
            entries = [BatchEntry(comment=c, state=c.batch_state()) for c in members]
            for c in members:
                self._mark_delivered_locked(c)
            self._flush_locked()
            return Batch(batch_no=batch_no, entries=entries)

    def deliver_remaining(self) -> list[Comment]:
        """The end of a review: every undelivered local comment, drafts and
        sent alike, as the final list; all marked delivered. Tombstones
        are dropped without being listed.
        """
        with self._lock:
            remaining = [c for c in self._ordered_locked() if c.is_undelivered]
            tombstones = [c for c in self._items.values() if c.withdrawn]
            for c in remaining + tombstones:
                self._mark_delivered_locked(c)
            if remaining or tombstones:
                self._flush_locked()
            return remaining

    def thread_root(self, comment_id: str) -> Comment:
        """The root of the thread holding `comment_id`.

        Raises:
            CommentNotFound: no such comment.
        """
        with self._lock:
            c = self._items.get(comment_id)
            if c is None or c.withdrawn:
                raise CommentNotFound(f"comment {comment_id} not found")
            return self._root_of_locked(c)

    def set_thread_resolved(self, comment_id: str, resolved: bool, *, upstream: bool = False) -> list[Comment]:
        """Resolve or reopen the thread holding `comment_id`; returns every
        member changed. Denormalised onto each member, as the ingest path
        does, so the viewer keeps reading the root.

        `upstream` says the caller has already flipped the thread on
        GitHub and this records it; without it an ingested thread is
        refused, since its resolution lives there.

        Raises:
            CommentNotFound: no such comment.
            ReadOnlyCommentError: the thread's root is an ingested comment
                and `upstream` is not set.
        """
        with self._lock:
            c = self._items.get(comment_id)
            if c is None or c.withdrawn:
                raise CommentNotFound(f"comment {comment_id} not found")
            root = self._root_of_locked(c)
            if root.source == "github" and not upstream:
                raise ReadOnlyCommentError(f"thread {root.id} is from github; resolve it there")
            changed: list[Comment] = []
            for member in self._ordered_locked():
                if self._root_of_locked(member).id != root.id or member.thread_resolved == resolved:
                    continue
                data = member.model_dump()
                data.update({"thread_resolved": resolved, "updated_at": time.time()})
                updated = Comment.model_validate(data)
                self._items[member.id] = updated
                changed.append(updated)
            if changed:
                self._flush_locked()
            return changed

    def all(self) -> list[Comment]:
        """Every comment the reviewer can see: tombstones excluded."""
        with self._lock:
            return [c for c in self._ordered_locked() if not c.withdrawn]

    @property
    def last_batch_no(self) -> int:
        with self._lock:
            return self._last_batch_no

    # --- internal --------------------------------------------------------

    def _ordered_locked(self) -> list[Comment]:
        return sorted(self._items.values(), key=lambda c: (c.file, c.line, c.created_at))

    def _next_batch_no_locked(self) -> int:
        self._last_batch_no += 1
        return self._last_batch_no

    def _mark_sent_locked(self, c: Comment, batch_no: int) -> Comment:
        data = c.model_dump()
        data.update({"delivery": "sent", "batch_no": batch_no, "send_error": None, "updated_at": time.time()})
        sent = Comment.model_validate(data)
        self._items[c.id] = sent
        return sent

    def _mark_delivered_locked(self, c: Comment) -> None:
        if c.withdrawn:
            del self._items[c.id]
            return
        data = c.model_dump()
        data.update({"delivery": "delivered", "deliveries": c.deliveries + 1})
        self._items[c.id] = Comment.model_validate(data)

    def _pending_batches_locked(self) -> list[tuple[int, list[Comment]]]:
        by_no: dict[int, list[Comment]] = {}
        for c in self._ordered_locked():
            if c.delivery == "sent" and c.batch_no is not None:
                by_no.setdefault(c.batch_no, []).append(c)
        return sorted(by_no.items())

    def _root_of_locked(self, c: Comment) -> Comment:
        seen: set[str] = set()
        cur = c
        while cur.in_reply_to_id and cur.in_reply_to_id in self._items and cur.id not in seen:
            seen.add(cur.id)
            cur = self._items[cur.in_reply_to_id]
        return cur

    def _flush_locked(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "comments": [c.model_dump() for c in self._ordered_locked()],
            "last_batch_no": self._last_batch_no,
        }
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)


def format_markdown(comments: list[Comment], *, run_slug: str = "", heading: str | None = None) -> str:
    """The markdown list of comments the slash command feeds back in.

    `heading` replaces the default "Review comments for <slug>" title.
    """
    if heading is None:
        heading = f"# Review comments for {run_slug}" if run_slug else "# Review comments"
    if not comments:
        return f"{heading}\n\n_No comments left. The reviewer had no concerns._\n"

    out: list[str] = [heading, ""]
    for c in comments:
        header = f"## {c.file}:{c.line} ({c.side})"
        if c.author and c.source != "local":
            header += f" — @{c.author}"
        out.append(header)
        for line in c.body.splitlines() or [""]:
            out.append(f"> {line}" if line else ">")
        out.append("")
    total = len(comments)
    word = "comment" if total == 1 else "comments"
    out.append(f"_{total} {word} total._")
    return "\n".join(out) + "\n"


#: Lines of code shown either side of a comment's anchor in a batch.
EXCERPT_CONTEXT = 2


def excerpt(text: str | None, line: int, *, context: int = EXCERPT_CONTEXT) -> str | None:
    """The anchored line with `context` lines either side, numbered, the
    anchor marked with `>`. None when the file has no such line.
    """
    if text is None:
        return None
    lines = text.splitlines()
    if line < 1 or line > len(lines):
        return None
    lo = max(1, line - context)
    hi = min(len(lines), line + context)
    width = len(str(hi))
    out = []
    for n in range(lo, hi + 1):
        mark = ">" if n == line else " "
        out.append(f"{mark} {n:>{width}} | {lines[n - 1]}")
    return "\n".join(out)


def format_batch_markdown(
    batch_no: int,
    entries: list[dict[str, Any]],
    *,
    run_slug: str,
) -> str:
    """The markdown a `scr review --wait` prints for one [[batch]].

    `entries` are the wire shape `ReviewSession.wait_for_batch` returns:
    `{comment: <Comment dump>, state: <BatchState>, excerpt: str | None}`.
    Carries the run id, the batch number, and per comment its id, state,
    anchor, body and the anchored code.
    """
    out: list[str] = [f"# Batch {batch_no} for {run_slug}", ""]
    for entry in entries:
        c = entry["comment"]
        state = entry["state"]
        header = f"## {c['id']} — {state} — {c['file']}:{c['line']} ({c['side']})"
        if c.get("in_reply_to_id"):
            header += f" — in reply to {c['in_reply_to_id']}"
        out.append(header)
        for line in str(c["body"]).splitlines() or [""]:
            out.append(f"> {line}" if line else ">")
        code = entry.get("excerpt")
        if code:
            out.append("")
            out.append("```")
            out.append(code)
            out.append("```")
        out.append("")
    total = len(entries)
    word = "comment" if total == 1 else "comments"
    out.append(f"_{total} {word} in this batch._")
    return "\n".join(out) + "\n"
