"""GraphQL-backed PR review posting.

GitHub's REST ``POST /pulls/N/reviews`` is bulk-only and refuses to
create a review when a pending one exists for the same user — the bulk
endpoint always creates a *new* review, and there's no REST operation
to append to an existing one. The GraphQL surface fills that gap:
query the user's pending review (if any), create one if absent, mutate
it incrementally with ``addPullRequestReviewThread`` and
``addPullRequestReviewComment``, then submit with
``submitPullRequestReview``.

This module composes those mutations into one entry point —
:func:`post_review_via_graphql` — that the CLI calls in place of the
old REST path.

All requests go through ``gh api graphql`` so auth + host config
piggy-back on ``gh auth status``; same surface as the rest of the
project's GitHub I/O.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Iterable

from .. import git_ops
from .github import GhError, PostResult, PostedComment, comments_to_github

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# gh-api graphql helper
# ---------------------------------------------------------------------------


def _compact_query(query: str) -> str:
    """Collapse a multi-line GraphQL document to one line for log output."""
    return " ".join(query.split())


def _loggable_vars(variables: dict[str, Any], *, limit: int = 300) -> dict[str, Any]:
    """Render variables for a diagnostic log line, truncating long string
    values so a multi-KB comment body doesn't bloat the record (we still
    want the path/line/side anchors, which are short)."""
    out: dict[str, Any] = {}
    for k, v in variables.items():
        if isinstance(v, str) and len(v) > limit:
            out[k] = f"{v[:limit]}… (+{len(v) - limit} chars)"
        else:
            out[k] = v
    return out


def _gh_graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    """Run a GraphQL request via ``gh api graphql``. Returns the parsed
    ``data`` envelope; raises :class:`GhError` on any failure.

    ``variables`` distinguishes ``int`` (sent with ``-F`` so gh emits
    a JSON number) from everything else (sent with ``-f`` as a string).
    GraphQL string + ID + enum inputs all accept the string form.

    Every failure path logs the full gh output, the (compacted) query,
    and the variables at ERROR before raising. The raised ``GhError``
    stays terse for the UI; the log carries the diagnostics. In ``scr
    pr`` these records land in ``<run>/trace/augment.log`` (the package
    file handler installed by the augment pass persists for the process,
    so post-time failures are captured there too).
    """
    args: list[str] = ["api", "graphql", "-f", f"query={query}"]
    for k, v in variables.items():
        if isinstance(v, bool):
            # Coerce bool → "true"/"false" via -F so gh keeps the JSON
            # type. (We don't currently use boolean vars but the
            # branch is here so future additions don't surprise.)
            args.extend(["-F", f"{k}={'true' if v else 'false'}"])
        elif isinstance(v, int):
            args.extend(["-F", f"{k}={v}"])
        else:
            args.extend(["-f", f"{k}={v}"])
    rc, stdout, stderr = git_ops.gh_capture(*args)
    if rc != 0:
        detail = stderr.strip() or stdout.strip() or f"exit {rc}"
        # gh exits non-zero on GraphQL-level errors too (bad line anchor,
        # un-threadable reply, etc.), echoing them to stderr — so this
        # branch is where most real posting failures surface.
        log.error(
            "gh api graphql failed (exit %s)\n  query: %s\n  variables: %s\n"
            "  stderr: %s\n  stdout: %s",
            rc, _compact_query(query), _loggable_vars(variables),
            stderr.strip(), stdout.strip(),
        )
        raise GhError(f"gh api graphql failed: {detail}")
    try:
        body = json.loads(stdout)
    except ValueError as e:
        log.error(
            "gh api graphql returned unparseable JSON: %s\n  query: %s\n  stdout: %s",
            e, _compact_query(query), stdout[:2000],
        )
        raise GhError(f"gh api graphql: unparseable JSON: {e}") from e
    if not isinstance(body, dict):
        log.error(
            "gh api graphql: expected object, got %s\n  query: %s\n  stdout: %s",
            type(body).__name__, _compact_query(query), stdout[:2000],
        )
        raise GhError(f"gh api graphql: expected object, got {type(body).__name__}")
    if body.get("errors"):
        log.error(
            "gh api graphql returned errors\n  query: %s\n  variables: %s\n  errors: %s",
            _compact_query(query), _loggable_vars(variables), body["errors"],
        )
        raise GhError(f"gh api graphql: {body['errors']}")
    data = body.get("data")
    if not isinstance(data, dict):
        log.error(
            "gh api graphql: response missing 'data' envelope\n  query: %s\n  stdout: %s",
            _compact_query(query), stdout[:2000],
        )
        raise GhError("gh api graphql: response missing 'data' envelope")
    return data


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


@dataclass(frozen=True)
class PrReviewState:
    """Snapshot of what we need to know before posting.

    ``pr_node_id`` is the GraphQL id of the PullRequest object — every
    mutation that creates a review takes this. ``pending_review_id`` is
    the id of the viewer's existing pending review if one exists, else
    None: when present we append to it instead of creating a new one,
    which is the whole reason we can't just use the REST bulk endpoint.
    """
    pr_node_id: str
    viewer_login: str
    pending_review_id: str | None


def query_pr_review_state(repo: str, number: int) -> PrReviewState:
    """Resolve the PR's node id + the viewer's pending review id."""
    owner, _, name = repo.partition("/")
    if not owner or not name:
        raise GhError(f"invalid repo {repo!r}: expected 'owner/name'")
    data = _gh_graphql(_PR_STATE_QUERY, {
        "owner": owner, "repo": name, "number": int(number),
    })
    viewer_login = ((data.get("viewer") or {}).get("login")) or ""
    pr = (data.get("repository") or {}).get("pullRequest") or {}
    pr_id = pr.get("id")
    if not pr_id:
        raise GhError(f"PR {repo}#{number} not found via GraphQL")
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
# Mutations
# ---------------------------------------------------------------------------


_CREATE_PENDING_REVIEW = """
mutation($pr: ID!, $body: String) {
  addPullRequestReview(input: {pullRequestId: $pr, body: $body}) {
    pullRequestReview { id }
  }
}
"""


def create_pending_review(pr_node_id: str, *, body: str = "") -> str:
    data = _gh_graphql(_CREATE_PENDING_REVIEW, {
        "pr": pr_node_id, "body": body,
    })
    rid = (
        ((data.get("addPullRequestReview") or {}).get("pullRequestReview") or {})
        .get("id")
    )
    if not rid:
        raise GhError("addPullRequestReview returned no review id")
    return str(rid)


_ADD_REVIEW_THREAD = """
mutation($rid: ID!, $path: String!, $line: Int!, $side: DiffSide!, $body: String!) {
  addPullRequestReviewThread(input: {
    pullRequestReviewId: $rid,
    path: $path,
    line: $line,
    side: $side,
    body: $body
  }) { thread { id } }
}
"""


def add_review_thread(
    review_id: str, path: str, line: int, side: str, body: str,
) -> str:
    """Append a new line-anchored thread to a pending review."""
    data = _gh_graphql(_ADD_REVIEW_THREAD, {
        "rid": review_id, "path": path,
        "line": int(line), "side": side, "body": body,
    })
    tid = (
        ((data.get("addPullRequestReviewThread") or {}).get("thread") or {})
        .get("id")
    )
    if not tid:
        raise GhError("addPullRequestReviewThread returned no thread id")
    return str(tid)


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
    review_id: str, in_reply_to_node_id: str, body: str,
) -> str:
    """Append a reply to an existing comment, under a pending review."""
    data = _gh_graphql(_ADD_REVIEW_COMMENT_REPLY, {
        "rid": review_id, "reply_to": in_reply_to_node_id, "body": body,
    })
    cid = (
        ((data.get("addPullRequestReviewComment") or {}).get("comment") or {})
        .get("id")
    )
    if not cid:
        raise GhError("addPullRequestReviewComment returned no comment id")
    return str(cid)


_SUBMIT_REVIEW = """
mutation($rid: ID!, $event: PullRequestReviewEvent!, $body: String) {
  submitPullRequestReview(input: {
    pullRequestReviewId: $rid,
    event: $event,
    body: $body
  }) { pullRequestReview { id databaseId url } }
}
"""


def submit_review(
    review_id: str, *, event: str = "COMMENT", body: str = "",
) -> dict[str, Any]:
    """Submit a pending review with the given event verdict."""
    data = _gh_graphql(_SUBMIT_REVIEW, {
        "rid": review_id, "event": event, "body": body,
    })
    r = (data.get("submitPullRequestReview") or {}).get("pullRequestReview") or {}
    if not r:
        raise GhError("submitPullRequestReview returned no review")
    return r


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------


def post_review_via_graphql(
    repo: str,
    number: int,
    comments: Iterable[Any],
    *,
    event: str = "COMMENT",
    body: str = "",
) -> PostResult:
    """Submit a review composed from ``comments``, using GraphQL.

    Accepts either raw viewer Comments (mapped + filtered via
    :func:`comments_to_github`) or already-mapped ``PostedComment``
    instances. Caller flow is typically: map once, count
    threads/replies for the confirmation prompt, then hand the same
    list here.

    Posting model: detect the viewer's existing pending review (or
    create one), then for each comment fire the matching mutation —
    ``addPullRequestReviewThread`` for new threads,
    ``addPullRequestReviewComment`` for replies — and finally
    ``submitPullRequestReview`` with the given event.
    """
    if all(isinstance(c, PostedComment) for c in comments):
        posted = list(comments)
    else:
        posted = comments_to_github(comments)
    if not posted:
        raise GhError("no postable comments after mapping (all entries malformed?)")

    state = query_pr_review_state(repo, number)
    review_id = state.pending_review_id
    reused_pending = review_id is not None
    if review_id is None:
        review_id = create_pending_review(state.pr_node_id, body=body)
    if reused_pending and body:
        # Reusing a pending review: we can't easily set its body
        # (PUT-style mutation exists but is rarely useful here).
        # Log so the operator knows the body string went unused.
        log.info(
            "reusing existing pending review %s; --body text not applied",
            review_id,
        )

    total = len(posted)
    log.info(
        "posting review to %s#%s: %d comment(s), review %s (%s)",
        repo, number, total, review_id,
        "reused pending" if reused_pending else "newly created",
    )
    for i, c in enumerate(posted, start=1):
        try:
            if c.is_reply:
                assert c.in_reply_to_node_id is not None  # narrow for type-checker
                add_review_comment_reply(review_id, c.in_reply_to_node_id, c.body)
            else:
                assert c.path is not None and c.line is not None and c.side is not None
                add_review_thread(review_id, c.path, c.line, c.side, c.body)
        except GhError:
            # _gh_graphql already logged the gh output + variables; add the
            # position so "added 1..i-1, failed on i/total" is explicit —
            # the i-1 successes are left as an unsubmitted draft review.
            anchor = (
                f"reply→{c.in_reply_to_node_id}" if c.is_reply
                else f"{c.path}:{c.line} ({c.side})"
            )
            log.error(
                "posting aborted on comment %d/%d (%s); %d already added to "
                "draft review %s (left unsubmitted)",
                i, total, anchor, i - 1, review_id,
            )
            raise

    submitted = submit_review(review_id, event=event, body="" if reused_pending else body)
    return PostResult(
        review_id=int(submitted.get("databaseId") or 0),
        review_url=str(submitted.get("url") or ""),
        posted=len(posted),
    )


__all__ = [
    "PrReviewState",
    "add_review_comment_reply",
    "add_review_thread",
    "create_pending_review",
    "post_review_via_graphql",
    "query_pr_review_state",
    "submit_review",
]
