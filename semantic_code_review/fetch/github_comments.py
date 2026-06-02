"""Fetch GitHub PR review comments into the run directory.

GitHub exposes two kinds of PR comments:

- *Review comments* anchored to (path, side, line) — what GitHub's UI
  shows inline against a hunk. These are what we care about; they
  round-trip cleanly into SCR's [[reviewer-comment]] model.
- *Issue comments* / *review summaries* with no file anchor. v1 skips
  them — they need a different display surface than a gutter pin.

The fetch is best-effort: any failure (auth, rate-limit, schema drift)
is logged and the run continues with no ingested comments. Reviewing
the diff must not block on the comments API.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
from pathlib import Path
from typing import Any

from .. import git_ops
from ..git_ops import GhError
from ..review.comments import Comment
from .github import PRRef

log = logging.getLogger(__name__)


def _parse_iso8601(s: str) -> float:
    """GitHub timestamps are ISO 8601 with a trailing 'Z'. Convert to
    a Unix epoch float so it lines up with `time.time()` values used
    by the local comment path."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return _dt.datetime.fromisoformat(s).timestamp()


def _side_from_github(raw: str | None) -> str | None:
    if raw == "RIGHT":
        return "new"
    if raw == "LEFT":
        return "old"
    return None


def _comment_from_payload(payload: dict[str, Any]) -> Comment | None:
    """Map one GitHub review-comment record to a `Comment`.

    Returns None when the comment is not line-anchored in a way SCR
    can pin (e.g. a multi-line comment that was outdated and lost its
    `line` even on the original revision)."""
    gh_id = payload.get("id")
    path = payload.get("path")
    side = _side_from_github(payload.get("side"))
    if gh_id is None or not path or side is None:
        return None

    # `line` is null on outdated comments; fall back to the original
    # line so the comment still pins where it was first written.
    line = payload.get("line")
    if line is None:
        line = payload.get("original_line")
    if not isinstance(line, int) or line <= 0:
        return None

    body = payload.get("body") or ""
    body_html = payload.get("body_html")  # only present with full+json accept
    user = payload.get("user") or {}
    author = user.get("login")
    avatar = user.get("avatar_url")
    in_reply_to = payload.get("in_reply_to_id")
    commit_id = payload.get("commit_id") or payload.get("original_commit_id")
    html_url = payload.get("html_url")

    created_at_raw = payload.get("created_at")
    updated_at_raw = payload.get("updated_at") or created_at_raw
    try:
        created_at = _parse_iso8601(created_at_raw) if created_at_raw else 0.0
        updated_at = _parse_iso8601(updated_at_raw) if updated_at_raw else created_at
    except ValueError:
        return None

    return Comment(
        id=f"gh-{gh_id}",
        file=path,
        side=side,
        line=line,
        body=body,
        body_html=body_html,
        created_at=created_at,
        updated_at=updated_at,
        source="github",
        author=author,
        author_avatar_url=avatar,
        in_reply_to_id=f"gh-{in_reply_to}" if in_reply_to is not None else None,
        commit_id=commit_id,
        html_url=html_url,
    )


_RESOLUTION_QUERY = """
query($owner:String!, $repo:String!, $number:Int!) {
  repository(owner:$owner, name:$repo) {
    pullRequest(number:$number) {
      reviewThreads(first:100) {
        pageInfo { hasNextPage }
        nodes {
          isResolved
          comments(first:100) {
            pageInfo { hasNextPage }
            nodes { databaseId }
          }
        }
      }
    }
  }
}
"""


def fetch_review_thread_resolution(ref: PRRef) -> dict[int, bool]:
    """Map each review-comment ``databaseId`` to its thread's resolution flag.

    The REST endpoint we use for the comment bodies does not expose
    thread membership or ``isResolved``; the GraphQL ``reviewThreads``
    connection does, and it's cheap (one round-trip for the whole PR).
    We denormalise the thread-level flag onto each member so the viewer
    can decide per-comment whether to start collapsed.

    Pagination is capped at 100 threads / 100 comments per thread — a
    `hasNextPage=true` response logs a warning and the remaining flags
    default to False. Real-world PRs comfortably fit; the cap is a
    deliberate v1 simplification, not a permanent constraint.
    """
    rc, stdout, stderr = git_ops.gh_capture(
        "api", "graphql",
        "-f", f"query={_RESOLUTION_QUERY}",
        "-F", f"owner={ref.owner}",
        "-F", f"repo={ref.repo}",
        "-F", f"number={ref.number}",
    )
    if rc != 0:
        raise GhError(f"gh api graphql failed: {stderr.strip()}")
    try:
        body = json.loads(stdout)
    except ValueError as e:
        raise GhError(f"gh api graphql: unparseable JSON: {e}") from e
    if not isinstance(body, dict):
        raise GhError(f"gh api graphql: expected object, got {type(body).__name__}")
    if body.get("errors"):
        raise GhError(f"gh api graphql: {body['errors']}")

    pr = ((body.get("data") or {}).get("repository") or {}).get("pullRequest") or {}
    threads = (pr.get("reviewThreads") or {})
    if threads.get("pageInfo", {}).get("hasNextPage"):
        log.warning(
            "PR %s has >100 review threads; resolution flags on the "
            "remainder will default to false", ref.url,
        )
    out: dict[int, bool] = {}
    for t in threads.get("nodes") or []:
        if not isinstance(t, dict):
            continue
        resolved = bool(t.get("isResolved"))
        comments = (t.get("comments") or {})
        if comments.get("pageInfo", {}).get("hasNextPage"):
            log.warning(
                "review thread in %s has >100 comments; trailing "
                "resolution flags may be incomplete", ref.url,
            )
        for c in comments.get("nodes") or []:
            dbid = c.get("databaseId") if isinstance(c, dict) else None
            if isinstance(dbid, int):
                out[dbid] = resolved
    return out


def fetch_pr_review_comments(ref: PRRef) -> list[Comment]:
    """Return all review comments on the PR, mapped to `Comment` records.

    Calls `gh api --paginate` with `Accept: application/vnd.github.full+json`
    so each record carries server-rendered `body_html` — saves us shipping
    a markdown parser to the client. Pagination is delegated to gh.

    Thread resolution state is fetched in a second GraphQL call and
    denormalised onto each comment. The two-call approach keeps the
    REST mapping path simple; a future deepening could merge them.

    Raises `GhError` on subprocess / API failures. Callers wrapping a
    user-facing pipeline should catch and degrade to "no comments".
    """
    endpoint = f"repos/{ref.slug}/pulls/{ref.number}/comments"
    rc, stdout, stderr = git_ops.gh_capture(
        "api", endpoint,
        "--paginate",
        "-H", "Accept: application/vnd.github.full+json",
    )
    if rc != 0:
        raise GhError(f"gh api {endpoint} failed: {stderr.strip()}")

    raw = stdout.strip()
    if not raw:
        return []
    # gh --paginate concatenates the per-page arrays into one outer array
    # (it parses each page's JSON and re-emits). We can json.loads the whole
    # response and iterate.
    try:
        records = json.loads(raw)
    except ValueError as e:
        raise GhError(f"gh api {endpoint}: unparseable JSON: {e}") from e
    if not isinstance(records, list):
        raise GhError(f"gh api {endpoint}: expected list, got {type(records).__name__}")

    out: list[Comment] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        c = _comment_from_payload(rec)
        if c is not None:
            out.append(c)

    # Decorate with thread-resolution state. The resolution call is
    # best-effort: an error here leaves every thread looking unresolved,
    # which is a strictly safer default than dropping the comments.
    try:
        resolved_map = fetch_review_thread_resolution(ref)
    except GhError as e:
        log.warning("could not fetch review-thread resolution for %s: %s", ref.url, e)
        resolved_map = {}
    if resolved_map:
        for c in out:
            # Comment ids look like "gh-<databaseId>" — strip the prefix
            # to look up the resolution map.
            if c.id.startswith("gh-"):
                try:
                    dbid = int(c.id[3:])
                except ValueError:
                    continue
                if resolved_map.get(dbid):
                    c.thread_resolved = True
    return out


def write_comments_file(path: Path, comments: list[Comment]) -> None:
    """Seed `comments.json` with ingested comments.

    Same on-disk shape as `CommentStore._flush_locked` — sorted by
    (file, line, created_at) so a re-fetch is order-stable. We write
    directly rather than going through CommentStore so this path
    stays free of the runtime lock + flush machinery; the server only
    instantiates a store once the file exists.
    """
    ordered = sorted(comments, key=lambda c: (c.file, c.line, c.created_at))
    payload = {"comments": [c.model_dump() for c in ordered]}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def fetch_comment_commits(repo_git: Path, comments: list[Comment]) -> set[str]:
    """Shallow-fetch every distinct commit_id referenced by an ingested
    comment, returning the set of SHAs now available in ``repo_git``.

    Anchor propagation needs to diff each comment's commit_id against
    head_sha; only base_sha + head_sha are fetched by the run-dir
    setup, so any comment left on an intermediate (or force-pushed-over)
    commit needs its object pulled in explicitly. Best-effort per SHA:
    a 404 on one commit (force-push >90d ago) leaves the rest fetchable
    and the affected comments are marked orphaned downstream.
    """
    wanted = sorted({
        c.commit_id for c in comments
        if c.commit_id and c.source == "github"
    })
    if not wanted:
        return set()
    return git_ops.try_fetch_depth1(repo_git, wanted)


def materialize_pr_comments(run_dir: Path, ref: PRRef) -> int:
    """Fetch + persist PR review comments into the run directory.

    Returns the number of comments written. Best-effort: GhError is
    logged and treated as "no comments" rather than failing the run.
    No-op if `comments.json` already exists (a prior fetch, or
    session-local comments from a previous review) so we don't clobber
    in-flight reviewer state on a re-materialise.

    Comment-anchor commits are shallow-fetched into ``run_dir/repo.git``
    so the propagator (slice 2) can diff each comment's commit_id
    against head_sha without an extra round-trip per anchor.
    """
    target = run_dir / "comments.json"
    if target.exists():
        return 0
    try:
        comments = fetch_pr_review_comments(ref)
    except GhError as e:
        log.warning("skipping PR comment ingest for %s: %s", ref.url, e)
        return 0
    repo_git = run_dir / "repo.git"
    if repo_git.exists():
        fetch_comment_commits(repo_git, comments)
    write_comments_file(target, comments)
    return len(comments)


__all__ = [
    "fetch_comment_commits",
    "fetch_pr_review_comments",
    "fetch_review_thread_resolution",
    "materialize_pr_comments",
    "write_comments_file",
]
