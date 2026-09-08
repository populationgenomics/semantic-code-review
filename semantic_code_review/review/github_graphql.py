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
from typing import Any

from .. import errors, git_ops

log = logging.getLogger(__name__)


class GitHubRefused(errors.ScrError, git_ops.GhError):
    """A ``gh api graphql`` call failed or GitHub returned errors.

    502: the review server relayed a request its counterpart refused.
    `not_found` says the refusal was GitHub not knowing a node the call
    addressed — a comment or review deleted or published from the web —
    which is a sign the store's view has diverged and is reconciled,
    where a network, auth or rate-limit failure is retried as it stands.
    """

    status = 502

    def __init__(self, message: str, *, not_found: bool = False) -> None:
        super().__init__(message)
        self.not_found = not_found

    def body(self) -> dict[str, Any]:
        return {"error": str(self), "not_found": self.not_found}


def _is_not_found(text: str) -> bool:
    """Whether gh's or GraphQL's error text is GitHub not knowing a node."""
    return "NOT_FOUND" in text or "Could not resolve to a node" in text


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


def _gh_graphql(query: str, variables: dict[str, Any], *, tolerate_not_found: bool = False) -> dict[str, Any]:
    """Run a GraphQL request via ``gh api graphql``. Returns the parsed
    ``data`` envelope.

    ``variables`` distinguishes ``bool`` and ``int`` (sent with ``-F`` so
    gh emits a JSON literal) from everything else (sent with ``-f`` as a
    string). GraphQL string + ID + enum inputs all accept the string form.

    `tolerate_not_found`: a `nodes(ids:)` lookup answers an unknown id
    with a null element *and* a NOT_FOUND error beside the data; with
    this set, errors that are all NOT_FOUND leave the data to the caller.

    gh exits non-zero whenever the envelope carries ``errors`` — including
    the partial-success shape above, where the data is complete and the
    errors are the answer — so the envelope is read before the exit code
    is judged; the exit code alone decides only when there is no
    envelope to read.

    Raises:
        GitHubRefused: gh exited non-zero with no JSON envelope (gh itself
            failed: auth, network, a bad query), the output was not JSON,
            or the envelope carried ``errors`` the caller does not
            tolerate (a bad line anchor, an un-threadable reply).
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
    try:
        body = json.loads(stdout) if stdout.strip() else None
    except ValueError as e:
        if rc != 0:
            body = None
        else:
            log.error(
                "gh api graphql returned unparseable JSON: %s\n  query: %s\n  stdout: %s",
                e,
                _compact_query(query),
                stdout[:2000],
            )
            raise GitHubRefused(f"gh api graphql: unparseable JSON: {e}") from e
    if rc != 0 and not (isinstance(body, dict) and ("data" in body or "errors" in body)):
        detail = stderr.strip() or stdout.strip() or f"exit {rc}"
        log.error(
            "gh api graphql failed (exit %s)\n  query: %s\n  variables: %s\n  stderr: %s\n  stdout: %s",
            rc,
            _compact_query(query),
            _loggable_vars(variables),
            stderr.strip(),
            stdout.strip(),
        )
        raise GitHubRefused(f"gh api graphql failed: {detail}", not_found=_is_not_found(detail))
    if not isinstance(body, dict):
        log.error(
            "gh api graphql: expected object, got %s\n  query: %s\n  stdout: %s",
            type(body).__name__,
            _compact_query(query),
            stdout[:2000],
        )
        raise GitHubRefused(f"gh api graphql: expected object, got {type(body).__name__}")
    if body.get("errors"):
        not_found = _all_not_found(body["errors"])
        if not (tolerate_not_found and not_found and isinstance(body.get("data"), dict)):
            log.error(
                "gh api graphql returned errors\n  query: %s\n  variables: %s\n  errors: %s",
                _compact_query(query),
                _loggable_vars(variables),
                body["errors"],
            )
            raise GitHubRefused(f"GitHub: {_error_messages(body['errors'])}", not_found=not_found)
    data = body.get("data")
    if not isinstance(data, dict):
        log.error(
            "gh api graphql: response missing 'data' envelope\n  query: %s\n  stdout: %s",
            _compact_query(query),
            stdout[:2000],
        )
        raise GitHubRefused("gh api graphql: response missing 'data' envelope")
    return data


def _all_not_found(raw_errors: object) -> bool:
    """Every error is GitHub not knowing a node (`type: NOT_FOUND`)."""
    if not isinstance(raw_errors, list) or not raw_errors:
        return False
    return all(
        isinstance(e, dict) and (e.get("type") == "NOT_FOUND" or _is_not_found(str(e.get("message") or "")))
        for e in raw_errors
    )


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
    return _state_from(data, repo, number)


def _state_from(data: dict[str, Any], repo: str, number: int) -> PrReviewState:
    """The `PrReviewState` in a response carrying `viewer` and the PR's
    pending `reviews`.
    """
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
# Reconciliation: where the comments the store knows stand on GitHub
# ---------------------------------------------------------------------------


_RECONCILE_QUERY = """
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
  nodes(ids: %s) {
    ... on PullRequestReviewComment {
      id
      body
      pullRequestReview { id state url }
    }
  }
}
"""

#: `nodes(ids:)` takes at most this many ids per call.
_NODES_PER_CALL = 100


@dataclasses.dataclass(frozen=True)
class CommentStanding:
    """Where one comment the store knows stands on GitHub: the body it
    holds, the review it is in and that review's state (`PENDING`, or a
    published one — `COMMENTED`, `APPROVED`, `CHANGES_REQUESTED`,
    `DISMISSED`) with its URL.
    """

    review_id: str
    review_state: str
    review_url: str
    body: str = ""

    @property
    def pending(self) -> bool:
        return self.review_state == "PENDING"


@dataclasses.dataclass(frozen=True)
class Reconciliation:
    """One look at GitHub for everything the store believes: the PR's
    state (its node id, the viewer's pending review if any) and, per
    comment node id asked about, its standing — None when GitHub no
    longer knows the node.
    """

    state: PrReviewState
    standing: dict[str, CommentStanding | None]


def query_reconciliation(repo: str, number: int, node_ids: list[str]) -> Reconciliation:
    """Where each of `node_ids` stands, plus the PR's pending-review
    state, in one call per hundred ids. Ids are inlined as literals —
    `gh api graphql` has no clean list variable.

    Raises:
        GitHubRefused: the call failed for a reason other than an
            unknown id, `repo` is not `owner/name`, or the PR is gone.
    """
    owner, _, name = repo.partition("/")
    if not owner or not name:
        raise GitHubRefused(f"invalid repo {repo!r}: expected 'owner/name'")
    standing: dict[str, CommentStanding | None] = {}
    state: PrReviewState | None = None
    chunks = [node_ids[i : i + _NODES_PER_CALL] for i in range(0, len(node_ids), _NODES_PER_CALL)] or [[]]
    for chunk in chunks:
        query = _RECONCILE_QUERY % json.dumps(chunk)
        data = _gh_graphql(query, {"owner": owner, "repo": name, "number": int(number)}, tolerate_not_found=True)
        if state is None:
            state = _state_from(data, repo, number)
        nodes = data.get("nodes") or []
        for node_id, raw in zip(chunk, nodes, strict=False):
            if not isinstance(raw, dict) or raw.get("id") != node_id:
                standing[node_id] = None
                continue
            review = raw.get("pullRequestReview") or {}
            if not review.get("id"):
                standing[node_id] = None
                continue
            standing[node_id] = CommentStanding(
                review_id=str(review["id"]),
                review_state=str(review.get("state") or ""),
                review_url=str(review.get("url") or ""),
                body=str(raw.get("body") or ""),
            )
        for node_id in chunk[len(nodes) :]:
            standing[node_id] = None
    assert state is not None
    return Reconciliation(state=state, standing=standing)


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


__all__ = [
    "REVIEW_EVENTS",
    "CommentStanding",
    "GitHubRefused",
    "NewThread",
    "PendingComment",
    "PrReviewState",
    "Reconciliation",
    "SubmittedReview",
    "add_review_comment_reply",
    "add_review_thread",
    "create_pending_review",
    "delete_review_comment",
    "list_pending_review_comments",
    "query_pr_review_state",
    "query_reconciliation",
    "set_thread_resolved",
    "submit_review",
    "update_review_comment",
]
