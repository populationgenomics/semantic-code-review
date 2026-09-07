"""The reviewer's [[pending-review]] on one PR, as the review session
drives it in PR mode (ADR 0009).

`PendingReview` composes the GraphQL primitives in `github_graphql.py`
into the operations the session performs on the [[counterpart]]: find
and take over an existing pending review (`resume`), deliver one
[[reviewer-comment]] into it as its state dictates (`deliver` — a new
thread, a reply, an edit in place, or a deletion), publish it with a
verdict (`submit`), and resolve or unresolve an upstream thread. The
pending review is created on the first delivery, never at start, so a
reviewer who opens a PR and leaves writes nothing.

Anchors are resolved against the run's `raw.diff` before a thread is
added (`anchors.resolve`): GitHub silently creates nothing for a line
outside the diff.

Every GitHub refusal is a `GitHubRefused` (502) for the session to
answer with; the session, not this module, decides what the refusal
does to the comment's lifecycle.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import logging
from collections.abc import Mapping
from typing import Protocol

from .. import paths
from ..fetch import github as fetch_github
from ..fetch import github_comments
from . import anchors, comments, github, github_graphql

log = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class Delivered:
    """What GitHub gave a delivered comment: the ids later mutations
    address it by. Both None for a deletion — nothing remains to address.
    """

    node_id: str | None
    thread_id: str | None


@dataclasses.dataclass(frozen=True)
class Resumed:
    """What an existing pending review yielded on resume: the comments the
    store lacked, as the viewer can show them; the ids of comments the
    store already held as upstream ones that are in fact still pending
    (`reclaimed`); and how many pending comments have no line to anchor
    on (file-level) and so are not shown.
    """

    comments: list[comments.Comment]
    reclaimed: list[str]
    unanchored: int


class ReviewSink(Protocol):
    """What the session needs of the GitHub side — `PendingReview`, or a
    fake in tests. Every method raises `GitHubRefused` when GitHub does.
    """

    def resume(self, node_index: Mapping[str, str]) -> Resumed | None: ...

    def deliver(self, c: comments.Comment, by_id: Mapping[str, comments.Comment]) -> Delivered: ...

    def submit(self, event: str, body: str) -> github_graphql.SubmittedReview: ...

    def set_thread_resolved(self, thread_id: str, resolved: bool) -> None: ...


class PendingReview:
    """The pending review on `repo#number`, backed by the run directory's
    diff and worktrees for anchoring.
    """

    def __init__(self, repo: str, number: int, run_dir: paths.RunDir, *, head_sha: str) -> None:
        self.repo = repo
        self.number = number
        self.run_dir = run_dir
        self.head_sha = head_sha
        self._pr_node_id: str | None = None
        self._review_id: str | None = None
        self._ranges: dict[tuple[str, str], list[tuple[int, int]]] | None = None

    @property
    def review_id(self) -> str | None:
        """The pending review's node id once known; None before the first
        delivery and after a submit.
        """
        return self._review_id

    # --- resume ------------------------------------------------------------

    def resume(self, node_index: Mapping[str, str]) -> Resumed | None:
        """Take over the viewer's pending review on the PR, if there is one.

        `node_index` maps the node ids the store already knows to their
        comment ids: a pending comment already recorded is not returned
        again (its id is in `reclaimed` instead, for the store to make
        the reviewer's own if it had it as upstream), and a pending reply
        is threaded under the store's copy of its parent. Returns None
        when the viewer has no pending review.

        Raises:
            GitHubRefused: the lookup failed.
        """
        state = github_graphql.query_pr_review_state(self.repo, self.number)
        self._pr_node_id = state.pr_node_id
        self._review_id = state.pending_review_id
        if self._review_id is None:
            return None
        pending = github_graphql.list_pending_review_comments(self._review_id)
        return self._to_comments(pending, node_index)

    def _to_comments(self, pending: list[github_graphql.PendingComment], node_index: Mapping[str, str]) -> Resumed:
        """Map pending comments to delivered local comments, in GitHub's
        order so a parent precedes its reply. Anchors are propagated to
        the run's head as an ingested comment's are.
        """
        ids: dict[str, str] = dict(node_index)
        out: list[comments.Comment] = []
        reclaimed: list[str] = []
        unanchored = 0
        for p in pending:
            if p.node_id in node_index:
                reclaimed.append(node_index[p.node_id])
                continue
            if p.line is None:
                unanchored += 1
                log.warning("pending comment %s on %s has no line; not shown", p.node_id, p.path)
                continue
            cid = f"gh-{p.database_id}"
            ids[p.node_id] = cid
            parent_id = ids.get(p.in_reply_to_node_id) if p.in_reply_to_node_id else None
            if p.in_reply_to_node_id and parent_id is None:
                log.warning("pending reply %s: parent %s unknown; shown on its own", p.node_id, p.in_reply_to_node_id)
            out.append(
                comments.Comment(
                    id=cid,
                    file=p.path,
                    side="old" if p.side == "LEFT" else "new",
                    line=p.line,
                    body=p.body,
                    created_at=_epoch(p.created_at),
                    updated_at=_epoch(p.updated_at),
                    source="local",
                    in_reply_to_id=parent_id,
                    commit_id=p.commit_oid,
                    node_id=p.node_id,
                    delivery="delivered",
                    deliveries=1,
                )
            )
        repo_git = self.run_dir.repo_git
        if out and repo_git.exists():
            github_comments.fetch_comment_commits(repo_git, out)
            github_comments.decorate_with_head_anchors(repo_git, self.head_sha, out)
        return Resumed(comments=out, reclaimed=reclaimed, unanchored=unanchored)

    # --- delivery ----------------------------------------------------------

    def deliver(self, c: comments.Comment, by_id: Mapping[str, comments.Comment]) -> Delivered:
        """Put one sent comment's state onto GitHub.

        A withdrawn tombstone deletes its pending comment; a comment GitHub
        already holds (`node_id` set) has its body replaced; a reply is
        added under its parent's node id; anything else opens a thread at
        its anchor, moved into the diff if need be. The pending review is
        created here on the first addition.

        Raises:
            GitHubRefused: GitHub refused, or the comment cannot be mapped
                (a reply whose parent has not reached GitHub).
            ValueError: a tombstone with no `node_id` — nothing upstream
                to delete; a store invariant, not a GitHub state.
        """
        if c.withdrawn:
            if c.node_id is None:
                raise ValueError(f"withdrawn comment {c.id} has no node_id")
            github_graphql.delete_review_comment(c.node_id)
            return Delivered(node_id=None, thread_id=None)
        try:
            posted = github.comment_to_github(c, by_id)
        except github.UnpostableComment as e:
            raise github_graphql.GitHubRefused(str(e)) from e
        if c.node_id is not None:
            github_graphql.update_review_comment(c.node_id, self._body_for(posted))
            return Delivered(node_id=c.node_id, thread_id=c.thread_id)
        review_id = self._ensure_review()
        if posted.is_reply:
            assert posted.in_reply_to_node_id is not None
            node_id = github_graphql.add_review_comment_reply(review_id, posted.in_reply_to_node_id, posted.body)
            parent = by_id.get(c.in_reply_to_id or "")
            return Delivered(node_id=node_id, thread_id=parent.thread_id if parent is not None else None)
        anchor = self._anchor_for(posted)
        thread = github_graphql.add_review_thread(
            review_id, anchor.path, anchor.line, anchor.side, anchors.with_note(posted.body, anchor.note)
        )
        return Delivered(node_id=thread.comment_id, thread_id=thread.thread_id)

    def _anchor_for(self, posted: github.PostedComment) -> anchors.Anchor:
        if posted.path is None or posted.line is None:
            raise ValueError("an anchored comment has a path and a line")
        a = anchors.resolve(posted.path, posted.line, posted.side or "RIGHT", self._postable_ranges())
        if a.note:
            log.warning("comment on %s:%s — %s", posted.path, posted.line, a.note)
        return a

    def _body_for(self, posted: github.PostedComment) -> str:
        """The body as GitHub should hold it: with the relocation note an
        anchored comment carries, so an edit keeps it.
        """
        if posted.is_reply or posted.path is None or posted.line is None:
            return posted.body
        return anchors.with_note(posted.body, self._anchor_for(posted).note)

    def _postable_ranges(self) -> dict[tuple[str, str], list[tuple[int, int]]]:
        if self._ranges is None:
            self._ranges = anchors.postable_ranges(self.run_dir.raw_diff.read_text(encoding="utf-8"))
        return self._ranges

    def _ensure_review(self) -> str:
        """The pending review's id, looking one up or creating one."""
        if self._review_id is None:
            if self._pr_node_id is None:
                state = github_graphql.query_pr_review_state(self.repo, self.number)
                self._pr_node_id = state.pr_node_id
                self._review_id = state.pending_review_id
            if self._review_id is None:
                self._review_id = github_graphql.create_pending_review(self._pr_node_id)
        return self._review_id

    # --- submit ------------------------------------------------------------

    def submit(self, event: str, body: str) -> github_graphql.SubmittedReview:
        """Publish the pending review — created empty first when there is
        none, which is how Approve with no comments is the LGTM. The id
        is forgotten: the next delivery opens a new pending review.

        Raises:
            GitHubRefused: GitHub refused.
            ValueError: `event` is not a review event.
        """
        review_id = self._ensure_review()
        submitted = github_graphql.submit_review(review_id, event=event, body=body)
        self._review_id = None
        return submitted

    # --- threads -----------------------------------------------------------

    def set_thread_resolved(self, thread_id: str, resolved: bool) -> None:
        github_graphql.set_thread_resolved(thread_id, resolved)


def _epoch(iso: str) -> float:
    """GitHub's ISO 8601 timestamp as a Unix epoch; 0.0 when absent."""
    if not iso:
        return 0.0
    return datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def for_run(run_dir: paths.RunDir) -> PendingReview:
    """The pending review for a PR run, from its `meta.json` (`url` and
    `headRefOid`).

    Raises:
        ValueError: the run is not a GitHub PR run — no `url` naming a PR,
            or no head SHA.
    """
    meta = json.loads(run_dir.meta.read_text(encoding="utf-8"))
    url = str(meta.get("url") or "")
    head_sha = str(meta.get("headRefOid") or "")
    if not url:
        raise ValueError(f"{run_dir.path} is not a PR run: meta.json has no url")
    if not head_sha:
        raise ValueError(f"{run_dir.path}: meta.json has no headRefOid")
    ref = fetch_github.parse_pr_url(url)
    return PendingReview(ref.slug, ref.number, run_dir, head_sha=head_sha)


__all__ = ["Delivered", "PendingReview", "Resumed", "ReviewSink", "for_run"]
