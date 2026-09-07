"""The GitHub side of PR mode: the [[pending-review]] over GraphQL.

Every operation the review session performs against GitHub is one
function here, each a single ``gh api graphql`` call: find the viewer's
pending review, create one, add a thread or a reply to it, update or
delete one of its comments, list what it holds (to resume), submit it
with a verdict, and resolve or unresolve a review thread. Composition —
which comment goes where, what is retried — lives in
:mod:`semantic_code_review.review.pending_review`.

Every failure is a :class:`GitHubRefused`: a ``ScrError`` the session
answers with (502 — the counterpart, not the request, is at fault) and a
``git_ops.GhError`` for callers that catch ``gh`` failures by that name.
The raised message stays terse for the viewer; the full ``gh`` output,
query and variables go to the log at ERROR.

GraphQL rather than REST because REST's ``POST /pulls/N/reviews`` is
bulk-only and has no append-to-pending-review operation, and because
REST cannot resolve a review thread at all.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from collections.abc import Iterable
from typing import Any

from .. import errors, git_ops
from . import anchors, github

log = logging.getLogger(__name__)


class GitHubRefused(errors.ScrError, git_ops.GhError):
    """A ``gh api graphql`` call failed or GitHub returned errors.

    502: the review server relayed a request its counterpart refused.
    """

    status = 502


#: The verdicts `submitPullRequestReview` accepts.
REVIEW_EVENTS = ("COMMENT", "APPROVE", "REQUEST_CHANGES")


# ---------------------------------------------------------------------------
# gh-api graphql helper
# ---------------------------------------------------------------------------


def _compact_query(query: str) -> str:
    """Collapse a multi-line GraphQL document to one line for log output."""
    return " ".join(query.split())


def _loggable_vars(variables: dict[str, Any], *, limit: int = 300) -> dict[str, Any]:
    """Render variables for a diagnostic log line, truncating long string
    values so a multi-KB comment body doesn't bloat the record (we still
    want the path/line/side anchors, which are short).
    """
    out: dict[str, Any] = {}
    for k, v in variables.items():
        if isinstance(v, str) and len(v) > limit:
            out[k] = f"{v[:limit]}… (+{len(v) - limit} chars)"
        else:
            out[k] = v
    return out


def _gh_graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    """Run a GraphQL request via ``gh api graphql``. Returns the parsed
    ``data`` envelope.

    ``variables`` distinguishes ``bool`` and ``int`` (sent with ``-F`` so
    gh emits a JSON literal) from everything else (sent with ``-f`` as a
    string). GraphQL string + ID + enum inputs all accept the string form.

    Raises:
        GitHubRefused: gh exited non-zero (which it does on GraphQL-level
            errors too — a bad line anchor, an un-threadable reply), the
            output was not JSON, or the envelope carried ``errors``.
    """
    args: list[str] = ["api", "graphql", "-f", f"query={query}"]
    for k, v in variables.items():
        if isinstance(v, bool):
            args.extend(["-F", f"{k}={'true' if v else 'false'}"])
        elif isinstance(v, int):
            args.extend(["-F", f"{k}={v}"])
        else:
            args.extend(["-f", f"{k}={v}"])
    rc, stdout, stderr = git_ops.gh_capture(*args)
    if rc != 0:
        detail = stderr.strip() or stdout.strip() or f"exit {rc}"
        log.error(
            "gh api graphql failed (exit %s)\n  query: %s\n  variables: %s\n  stderr: %s\n  stdout: %s",
            rc,
            _compact_query(query),
            _loggable_vars(variables),
            stderr.strip(),
            stdout.strip(),
        )
        raise GitHubRefused(f"gh api graphql failed: {detail}")
    try:
        body = json.loads(stdout)
    except ValueError as e:
        log.error(
            "gh api graphql returned unparseable JSON: %s\n  query: %s\n  stdout: %s",
            e,
            _compact_query(query),
            stdout[:2000],
        )
        raise GitHubRefused(f"gh api graphql: unparseable JSON: {e}") from e
    if not isinstance(body, dict):
        log.error(
            "gh api graphql: expected object, got %s\n  query: %s\n  stdout: %s",
            type(body).__name__,
            _compact_query(query),
            stdout[:2000],
        )
        raise GitHubRefused(f"gh api graphql: expected object, got {type(body).__name__}")
    if body.get("errors"):
        log.error(
            "gh api graphql returned errors\n  query: %s\n  variables: %s\n  errors: %s",
            _compact_query(query),
            _loggable_vars(variables),
            body["errors"],
        )
        raise GitHubRefused(f"GitHub: {_error_messages(body['errors'])}")
    data = body.get("data")
    if not isinstance(data, dict):
        log.error(
            "gh api graphql: response missing 'data' envelope\n  query: %s\n  stdout: %s",
            _compact_query(query),
            stdout[:2000],
        )
        raise GitHubRefused("gh api graphql: response missing 'data' envelope")
    return data


def _error_messages(raw_errors: object) -> str:
    """The `message` of each GraphQL error, joined — what a reviewer can
    act on; the full structure is in the log.
    """
    if not isinstance(raw_errors, list):
        return str(raw_errors)
    return "; ".join(str(e.get("message") or e) if isinstance(e, dict) else str(e) for e in raw_errors)


# ---------------------------------------------------------------------------
# State query
# ---------------------------------------------------------------------------


_PR_STATE_QUERY = """
query($owner: String!, $repo: String!, $number: Int!) {
  viewer { login }
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      id
      reviews(first: 50, states: [PENDING]) {
        nodes { id author { login } }
      }
    }
  }
}
"""


@dataclasses.dataclass(frozen=True)
class PrReviewState:
    """What one look at the PR says before anything is written.

    ``pr_node_id`` is the GraphQL id of the PullRequest object — the
    mutation that creates a review takes this. ``pending_review_id`` is
    the viewer's existing [[pending-review]], else None: GitHub allows one
    per user per PR, so an existing one is resumed rather than created.
    """

    pr_node_id: str
    viewer_login: str
    pending_review_id: str | None


def query_pr_review_state(repo: str, number: int) -> PrReviewState:
    """Resolve the PR's node id and the viewer's pending review id.

    Raises:
        GitHubRefused: the call failed, `repo` is not `owner/name`, or
            the PR does not exist.
    """
    owner, _, name = repo.partition("/")
    if not owner or not name:
        raise GitHubRefused(f"invalid repo {repo!r}: expected 'owner/name'")
    data = _gh_graphql(_PR_STATE_QUERY, {"owner": owner, "repo": name, "number": int(number)})
    viewer_login = ((data.get("viewer") or {}).get("login")) or ""
    pr = (data.get("repository") or {}).get("pullRequest") or {}
    pr_id = pr.get("id")
    if not pr_id:
        raise GitHubRefused(f"PR {repo}#{number} not found via GraphQL")
    reviews = (pr.get("reviews") or {}).get("nodes") or []
    pending_id: str | None = None
    for r in reviews:
        if not isinstance(r, dict):
            continue
        author = (r.get("author") or {}).get("login") or ""
        if author and viewer_login and author == viewer_login:
            pending_id = r.get("id")
            break
    return PrReviewState(
        pr_node_id=str(pr_id),
        viewer_login=str(viewer_login),
        pending_review_id=str(pending_id) if pending_id else None,
    )


# ---------------------------------------------------------------------------
# The pending review's comments (resume)
# ---------------------------------------------------------------------------


_PENDING_REVIEW_COMMENTS = """
query($rid: ID!) {
  node(id: $rid) {
    ... on PullRequestReview {
      comments(first: 100) {
        pageInfo { hasNextPage }
        nodes {
          id databaseId path line originalLine diffHunk body createdAt updatedAt
          replyTo { id }
          commit { oid }
        }
      }
    }
  }
}
"""


@dataclasses.dataclass(frozen=True)
class PendingComment:
    """One comment as the [[pending-review]] holds it.

    ``line`` is None for a file-level comment. ``side`` is GitHub's
    ``LEFT`` / ``RIGHT``, read off the last line of ``diffHunk`` — the
    comment object carries no side of its own, and the hunk GitHub
    returns ends on the commented line.
    """

    node_id: str
    database_id: int
    path: str
    line: int | None
    side: str
    body: str
    created_at: str
    updated_at: str
    in_reply_to_node_id: str | None
    commit_oid: str | None


def _side_from_hunk(diff_hunk: str) -> str:
    """LEFT when the commented line is a deletion, else RIGHT. A context
    line commented on the old side reads as RIGHT: the hunk cannot tell
    the two apart.
    """
    lines = diff_hunk.splitlines()
    return "LEFT" if lines and lines[-1].startswith("-") else "RIGHT"


def list_pending_review_comments(review_id: str) -> list[PendingComment]:
    """Every comment the pending review holds, in GitHub's order.

    Raises:
        GitHubRefused: the call failed, the id is not a review, or a
            comment lacks the fields the viewer anchors by.
    """
    data = _gh_graphql(_PENDING_REVIEW_COMMENTS, {"rid": review_id})
    node = data.get("node")
    if not isinstance(node, dict) or "comments" not in node:
        raise GitHubRefused(f"{review_id} is not a pull request review")
    connection = node.get("comments") or {}
    if (connection.get("pageInfo") or {}).get("hasNextPage"):
        log.warning("pending review %s holds more than 100 comments; only the first 100 are resumed", review_id)
    out: list[PendingComment] = []
    for raw in connection.get("nodes") or []:
        if not isinstance(raw, dict):
            continue
        node_id = raw.get("id")
        database_id = raw.get("databaseId")
        path = raw.get("path")
        if not isinstance(node_id, str) or not isinstance(database_id, int) or not isinstance(path, str):
            raise GitHubRefused(f"pending review comment missing id/databaseId/path: {raw!r}")
        line = raw.get("line")
        if line is None:
            line = raw.get("originalLine")
        reply_to = raw.get("replyTo") or {}
        commit = raw.get("commit") or {}
        out.append(
            PendingComment(
                node_id=node_id,
                database_id=database_id,
                path=path,
                line=int(line) if isinstance(line, int) and line > 0 else None,
                side=_side_from_hunk(str(raw.get("diffHunk") or "")),
                body=str(raw.get("body") or ""),
                created_at=str(raw.get("createdAt") or ""),
                updated_at=str(raw.get("updatedAt") or raw.get("createdAt") or ""),
                in_reply_to_node_id=str(reply_to["id"]) if reply_to.get("id") else None,
                commit_oid=str(commit["oid"]) if commit.get("oid") else None,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------


_CREATE_PENDING_REVIEW = """
mutation($pr: ID!) {
  addPullRequestReview(input: {pullRequestId: $pr}) {
    pullRequestReview { id }
  }
}
"""


def create_pending_review(pr_node_id: str) -> str:
    """Open a pending review on the PR; returns its node id.

    Raises:
        GitHubRefused: the call failed — including because the viewer
            already has a pending review on this PR.
    """
    data = _gh_graphql(_CREATE_PENDING_REVIEW, {"pr": pr_node_id})
    rid = ((data.get("addPullRequestReview") or {}).get("pullRequestReview") or {}).get("id")
    if not rid:
        raise GitHubRefused("addPullRequestReview returned no review id")
    return str(rid)


_ADD_REVIEW_THREAD = """
mutation($rid: ID!, $path: String!, $line: Int!, $side: DiffSide!, $body: String!) {
  addPullRequestReviewThread(input: {
    pullRequestReviewId: $rid,
    path: $path,
    line: $line,
    side: $side,
    body: $body
  }) { thread { id comments(first: 1) { nodes { id } } } }
}
"""


_ADD_REVIEW_THREAD_FILE = """
mutation($rid: ID!, $path: String!, $body: String!) {
  addPullRequestReviewThread(input: {
    pullRequestReviewId: $rid,
    path: $path,
    body: $body,
    subjectType: FILE
  }) { thread { id comments(first: 1) { nodes { id } } } }
}
"""


@dataclasses.dataclass(frozen=True)
class NewThread:
    """What adding a thread yields: the thread's id (what resolve and
    unresolve address) and its first comment's id (what update, delete
    and reply address).
    """

    thread_id: str
    comment_id: str


def add_review_thread(
    review_id: str,
    path: str,
    line: int | None,
    side: str | None,
    body: str,
) -> NewThread:
    """Append a new thread to a pending review.

    `line is None` posts a file-level thread. The two forms are
    mutually exclusive: passing a line alongside `subjectType: FILE`
    returns a null thread with no error, as does any line outside a
    diff hunk — which is why callers resolve anchors up front
    (`anchors.resolve`) rather than discovering it here.

    Raises:
        GitHubRefused: the call failed, or GitHub created no thread.
    """
    if line is None:
        data = _gh_graphql(_ADD_REVIEW_THREAD_FILE, {"rid": review_id, "path": path, "body": body})
    else:
        data = _gh_graphql(
            _ADD_REVIEW_THREAD,
            {"rid": review_id, "path": path, "line": int(line), "side": side or "RIGHT", "body": body},
        )
    thread = (data.get("addPullRequestReviewThread") or {}).get("thread") or {}
    tid = thread.get("id")
    nodes = (thread.get("comments") or {}).get("nodes") or []
    cid = nodes[0].get("id") if nodes and isinstance(nodes[0], dict) else None
    if not tid or not cid:
        raise GitHubRefused(f"GitHub created no thread for {path}:{line} — the line is outside the diff")
    return NewThread(thread_id=str(tid), comment_id=str(cid))


_ADD_REVIEW_COMMENT_REPLY = """
mutation($rid: ID!, $reply_to: ID!, $body: String!) {
  addPullRequestReviewComment(input: {
    pullRequestReviewId: $rid,
    inReplyTo: $reply_to,
    body: $body
  }) { comment { id } }
}
"""


def add_review_comment_reply(
    review_id: str,
    in_reply_to_node_id: str,
    body: str,
) -> str:
    """Append a reply to an existing comment, under a pending review;
    returns the new comment's node id.

    Raises:
        GitHubRefused: the call failed, or GitHub created no comment.
    """
    data = _gh_graphql(
        _ADD_REVIEW_COMMENT_REPLY,
        {"rid": review_id, "reply_to": in_reply_to_node_id, "body": body},
    )
    cid = ((data.get("addPullRequestReviewComment") or {}).get("comment") or {}).get("id")
    if not cid:
        raise GitHubRefused("addPullRequestReviewComment returned no comment id")
    return str(cid)


_UPDATE_REVIEW_COMMENT = """
mutation($cid: ID!, $body: String!) {
  updatePullRequestReviewComment(input: {pullRequestReviewCommentId: $cid, body: $body}) {
    pullRequestReviewComment { id }
  }
}
"""


def update_review_comment(comment_id: str, body: str) -> None:
    """Replace a review comment's body in place.

    Raises:
        GitHubRefused: the call failed, or GitHub reported no comment.
    """
    data = _gh_graphql(_UPDATE_REVIEW_COMMENT, {"cid": comment_id, "body": body})
    updated = ((data.get("updatePullRequestReviewComment") or {}).get("pullRequestReviewComment") or {}).get("id")
    if not updated:
        raise GitHubRefused("updatePullRequestReviewComment returned no comment")


_DELETE_REVIEW_COMMENT = """
mutation($cid: ID!) {
  deletePullRequestReviewComment(input: {id: $cid}) {
    pullRequestReview { id }
  }
}
"""


def delete_review_comment(comment_id: str) -> None:
    """Delete a review comment.

    Raises:
        GitHubRefused: the call failed.
    """
    data = _gh_graphql(_DELETE_REVIEW_COMMENT, {"cid": comment_id})
    if "deletePullRequestReviewComment" not in data:
        raise GitHubRefused("deletePullRequestReviewComment returned nothing")


_SUBMIT_REVIEW = """
mutation($rid: ID!, $event: PullRequestReviewEvent!, $body: String) {
  submitPullRequestReview(input: {
    pullRequestReviewId: $rid,
    event: $event,
    body: $body
  }) { pullRequestReview { id databaseId url } }
}
"""


@dataclasses.dataclass(frozen=True)
class SubmittedReview:
    """The review as GitHub reports it once published."""

    node_id: str
    database_id: int
    url: str


def submit_review(review_id: str, *, event: str, body: str = "") -> SubmittedReview:
    """Publish a pending review with a verdict.

    Raises:
        ValueError: `event` is not one of `REVIEW_EVENTS`.
        GitHubRefused: the call failed, or GitHub reported no review.
    """
    if event not in REVIEW_EVENTS:
        raise ValueError(f"event must be one of {REVIEW_EVENTS}, not {event!r}")
    data = _gh_graphql(_SUBMIT_REVIEW, {"rid": review_id, "event": event, "body": body})
    r = (data.get("submitPullRequestReview") or {}).get("pullRequestReview") or {}
    if not r.get("id"):
        raise GitHubRefused("submitPullRequestReview returned no review")
    return SubmittedReview(node_id=str(r["id"]), database_id=int(r.get("databaseId") or 0), url=str(r.get("url") or ""))


_RESOLVE_THREAD = """
mutation($tid: ID!) {
  resolveReviewThread(input: {threadId: $tid}) { thread { id isResolved } }
}
"""

_UNRESOLVE_THREAD = """
mutation($tid: ID!) {
  unresolveReviewThread(input: {threadId: $tid}) { thread { id isResolved } }
}
"""


def set_thread_resolved(thread_id: str, resolved: bool) -> None:
    """Resolve or unresolve a review thread on GitHub.

    Raises:
        GitHubRefused: the call failed, or the thread did not end in
            the requested state.
    """
    op = "resolveReviewThread" if resolved else "unresolveReviewThread"
    data = _gh_graphql(_RESOLVE_THREAD if resolved else _UNRESOLVE_THREAD, {"tid": thread_id})
    thread = (data.get(op) or {}).get("thread") or {}
    if thread.get("isResolved") is not resolved:
        raise GitHubRefused(f"{op} did not change thread {thread_id}")


# ---------------------------------------------------------------------------
# One-shot posting — what `scr pr --yes` and the modal call. Goes with them.
# ---------------------------------------------------------------------------


def post_review_via_graphql(
    repo: str,
    number: int,
    comments: Iterable[Any],
    *,
    event: str = "COMMENT",
    body: str = "",
    diff_text: str | None = None,
) -> github.PostResult:
    """Submit one review composed from ``comments``: find or create the
    pending review, add every comment, submit with ``event``.
    """
    posted = list(comments) if all(isinstance(c, github.PostedComment) for c in comments) else None
    if posted is None:
        posted = github.comments_to_github(comments)
    if not posted:
        raise GitHubRefused("no postable comments after mapping (all entries malformed?)")
    if diff_text is not None:
        ranges = anchors.postable_ranges(diff_text)
        resolved: list[github.PostedComment] = []
        for c in posted:
            if c.is_reply or c.path is None or c.line is None:
                resolved.append(c)
                continue
            a = anchors.resolve(c.path, c.line, c.side or "RIGHT", ranges)
            resolved.append(dataclasses.replace(c, line=a.line, side=a.side, body=anchors.with_note(c.body, a.note)))
        posted = resolved
    state = query_pr_review_state(repo, number)
    review_id = state.pending_review_id or create_pending_review(state.pr_node_id)
    node_ids: dict[str, str] = {}
    for c in posted:
        if c.is_reply:
            assert c.in_reply_to_node_id is not None
            nid = add_review_comment_reply(review_id, c.in_reply_to_node_id, c.body)
        else:
            if c.path is None:
                raise GitHubRefused(f"comment has neither a reply target nor a path: {c.body[:60]!r}")
            nid = add_review_thread(review_id, c.path, c.line, c.side, c.body).comment_id
        if c.source_id:
            node_ids[c.source_id] = nid
    submitted = submit_review(review_id, event=event, body=body)
    return github.PostResult(
        review_id=submitted.database_id,
        review_url=submitted.url,
        posted=len(posted),
        posted_node_ids=node_ids,
    )


__all__ = [
    "REVIEW_EVENTS",
    "GitHubRefused",
    "NewThread",
    "PendingComment",
    "PrReviewState",
    "SubmittedReview",
    "add_review_comment_reply",
    "add_review_thread",
    "create_pending_review",
    "delete_review_comment",
    "list_pending_review_comments",
    "query_pr_review_state",
    "set_thread_resolved",
    "submit_review",
    "update_review_comment",
]
