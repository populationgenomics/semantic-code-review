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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import git_ops
from ..git_ops import GhError
from ..review.comments import Comment
from .anchor import _PathDiff, apply_path_diff, load_path_diff
from .github import PRRef

log = logging.getLogger(__name__)


def _parse_iso8601(s: str) -> float:
    """GitHub timestamps are ISO 8601 with a trailing 'Z'. Convert to
    a Unix epoch float so it lines up with `time.time()` values used
    by the local comment path.
    """
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
    `line` even on the original revision).
    """
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


_REVIEW_THREAD_QUERY = """
query($owner:String!, $repo:String!, $number:Int!) {
  repository(owner:$owner, name:$repo) {
    pullRequest(number:$number) {
      reviewThreads(first:100) {
        pageInfo { hasNextPage }
        nodes {
          isResolved
          comments(first:100) {
            pageInfo { hasNextPage }
            nodes { id databaseId }
          }
        }
      }
    }
  }
}
"""


@dataclass(frozen=True)
class _ReviewCommentMeta:
    """Per-comment metadata pulled from the GraphQL reviewThreads
    query. Both fields are denormalised from the thread + the comment:
    ``thread_resolved`` is the thread-level flag applied to every
    member, ``node_id`` is the opaque GraphQL id (distinct from the
    REST ``databaseId``) that mutations need when referencing this
    comment as a reply parent.
    """
    thread_resolved: bool
    node_id: str


def fetch_review_thread_metadata(ref: PRRef) -> dict[int, _ReviewCommentMeta]:
    """Map each review-comment ``databaseId`` to its thread state +
    node id.

    The REST comments endpoint we already hit exposes neither thread
    membership / ``isResolved`` nor the GraphQL ``node_id`` we need
    for reply mutations later — both fall out of this one GraphQL
    call for the whole PR (cheap; one round-trip).

    Pagination is capped at 100 threads / 100 comments per thread —
    a ``hasNextPage=true`` response logs a warning and the remainder
    is missing from the map (downstream just defaults to "unknown",
    which is safer than dropping the comment). Real-world PRs
    comfortably fit; the cap is a deliberate v1 simplification.
    """
    rc, stdout, stderr = git_ops.gh_capture(
        "api", "graphql",
        "-f", f"query={_REVIEW_THREAD_QUERY}",
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
            "PR %s has >100 review threads; trailing metadata "
            "(resolution, node_id) will default to absent", ref.url,
        )
    out: dict[int, _ReviewCommentMeta] = {}
    for t in threads.get("nodes") or []:
        if not isinstance(t, dict):
            continue
        resolved = bool(t.get("isResolved"))
        comments = (t.get("comments") or {})
        if comments.get("pageInfo", {}).get("hasNextPage"):
            log.warning(
                "review thread in %s has >100 comments; trailing "
                "metadata may be incomplete", ref.url,
            )
        for c in comments.get("nodes") or []:
            if not isinstance(c, dict):
                continue
            dbid = c.get("databaseId")
            node_id = c.get("id")
            if isinstance(dbid, int) and isinstance(node_id, str):
                out[dbid] = _ReviewCommentMeta(
                    thread_resolved=resolved, node_id=node_id,
                )
    return out


# Back-compat alias: the resolution-only return value used by the
# original caller signature. Drop once nothing reads it.
def fetch_review_thread_resolution(ref: PRRef) -> dict[int, bool]:
    """Resolution flag per ``databaseId``. Thin wrapper over
    :func:`fetch_review_thread_metadata` kept so existing imports keep
    working — new code should use the richer function.
    """
    return {dbid: meta.thread_resolved for dbid, meta in fetch_review_thread_metadata(ref).items()}


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

    # Decorate with thread metadata (resolution flag + GraphQL node id).
    # Best-effort: an error here leaves comments looking unresolved with
    # no node_id, both of which the downstream paths tolerate.
    try:
        meta_map = fetch_review_thread_metadata(ref)
    except GhError as e:
        log.warning("could not fetch review-thread metadata for %s: %s", ref.url, e)
        meta_map = {}
    if meta_map:
        for c in out:
            # Comment ids look like "gh-<databaseId>" — strip the prefix
            # to look up the metadata map.
            if c.id.startswith("gh-"):
                try:
                    dbid = int(c.id[3:])
                except ValueError:
                    continue
                meta = meta_map.get(dbid)
                if meta is None:
                    continue
                if meta.thread_resolved:
                    c.thread_resolved = True
                c.node_id = meta.node_id
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


def decorate_with_head_anchors(
    repo_git: Path, head_sha: str, comments: list[Comment],
) -> None:
    """Propagate every ingested side=new comment's anchor through to
    ``head_sha`` and stamp ``head_line`` + ``anchor_status`` on each.

    Mutates the comments in place. side=old comments are pinned on the
    PR's base (which doesn't move for a non-rebased PR) so we skip them.
    Session-local comments are already at head and skip too. The diff
    for each ``(commit_id, path)`` is loaded once and reused across
    every comment that shares the pair — one PR push typically leaves
    a fistful of comments on the same file at the same commit.
    """
    diff_cache: dict[tuple[str, str], _PathDiff] = {}
    for c in comments:
        if c.source != "github" or c.side != "new":
            continue
        if not c.commit_id or c.commit_id == head_sha:
            c.head_line = c.line
            c.anchor_status = "anchored"
            continue
        key = (c.commit_id, c.file)
        diff = diff_cache.get(key)
        if diff is None:
            diff = load_path_diff(repo_git, c.commit_id, head_sha, c.file)
            diff_cache[key] = diff
        result = apply_path_diff(diff, c.line)
        c.head_line = result.head_line
        c.anchor_status = result.status


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


def materialize_pr_comments(
    run_dir: Path, ref: PRRef, head_sha: str | None = None,
) -> int:
    """Fetch + persist PR review comments into the run directory.

    Returns the number of comments written. Best-effort: GhError is
    logged and treated as "no comments" rather than failing the run.
    No-op if `comments.json` already exists (a prior fetch, or
    session-local comments from a previous review) so we don't clobber
    in-flight reviewer state on a re-materialise.

    Comment-anchor commits are shallow-fetched into ``run_dir/repo.git``
    so the propagator can diff each comment's commit_id against
    ``head_sha``. Passing ``head_sha`` enables anchor propagation; if
    omitted, comments land with no ``head_line`` / ``anchor_status``
    (used by callers that don't yet care about movement tracking).
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
        if head_sha is not None:
            decorate_with_head_anchors(repo_git, head_sha, comments)
    write_comments_file(target, comments)
    return len(comments)


__all__ = [
    "decorate_with_head_anchors",
    "fetch_comment_commits",
    "fetch_pr_review_comments",
    "fetch_review_thread_metadata",
    "fetch_review_thread_resolution",
    "materialize_pr_comments",
    "write_comments_file",
]
