"""GitHub PR-review helpers for the `scr pr` command.

Three responsibilities:

1. Enumerate open PRs in a repo where the current `gh` user has been
   requested as a reviewer (so the no-number invocation can either
   auto-select a single match or present a picker).
2. Run a numbered stdin picker when there's more than one match.
3. Post the inline comments collected by the viewer back to GitHub
   as a single `COMMENT`-event review.

All GitHub I/O goes through `git_ops`'s `gh` wrappers; auth and host
config piggy-back on whatever `gh auth status` reports. Callers
preflight `gh` once at the CLI boundary (`fetch.preflight_gh`).
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from typing import Any, Iterable

from .. import git_ops

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class OpenPR:
    """A single PR row returned by `gh pr list`. Fields mirror the
    `--json` keys we ask for; missing keys come back as empty strings.
    """
    number: int
    title: str
    author: str
    head_ref: str
    base_ref: str
    updated_at: str
    url: str

    def picker_line(self) -> str:
        """Two-line entry the picker prints for this PR."""
        title = self.title.strip() or "(untitled)"
        head = self.head_ref or "?"
        base = self.base_ref or "?"
        return (
            f"#{self.number} {title}\n"
            f"        {self.author or '?'} · {head} → {base}"
            + (f" · updated {self.updated_at}" if self.updated_at else "")
        )


@dataclass(frozen=True)
class PostedComment:
    """One inline review comment in GitHub's expected shape.

    Either *anchored* — ``path`` + ``line`` + ``side`` set, ``in_reply_to``
    is None — for a new thread, or a *reply* — ``in_reply_to`` set,
    anchor fields None — to an existing upstream comment. GitHub's
    `POST /pulls/{n}/reviews` accepts both shapes interchangeably in
    one ``comments`` array.
    """
    body: str
    path: str | None = None
    line: int | None = None
    side: str | None = None  # "LEFT" or "RIGHT" for anchored comments.
    in_reply_to: int | None = None

    @property
    def is_reply(self) -> bool:
        return self.in_reply_to is not None

    def to_payload(self) -> dict[str, Any]:
        if self.in_reply_to is not None:
            return {"in_reply_to": self.in_reply_to, "body": self.body}
        return {
            "path": self.path,
            "line": self.line,
            "side": self.side,
            "body": self.body,
        }


@dataclass(frozen=True)
class PostResult:
    """Outcome of posting a review. `review_url` is GitHub's permalink
    for the new review object so the caller can offer "view on
    github.com"."""
    review_id: int
    review_url: str
    posted: int


# Public alias kept so callers that catch posting failures by name
# don't need to import from git_ops.
GhError = git_ops.GhError


# ---------------------------------------------------------------------------
# PR resolution
# ---------------------------------------------------------------------------

_LIST_FIELDS = [
    "number", "title", "author", "headRefName", "baseRefName", "updatedAt", "url",
]


def list_review_requested_prs(repo: str) -> list[OpenPR]:
    """Open PRs in `repo` where the gh user is a requested reviewer.

    `repo` is the `owner/name` form. Uses the GitHub search qualifier
    `review-requested:@me` which is exactly the filter we want and
    keeps the auth/host story inside `gh`.
    """
    rc, stdout, stderr = git_ops.gh_capture(
        "pr", "list", "--repo", repo,
        "--search", "is:open review-requested:@me",
        "--json", ",".join(_LIST_FIELDS),
        "--limit", "100",
    )
    if rc != 0:
        raise GhError(f"`gh pr list` failed: {stderr.strip() or stdout.strip()}")
    raw = json.loads(stdout or "[]")
    return [_open_pr_from_json(item) for item in raw]


def _open_pr_from_json(item: dict[str, Any]) -> OpenPR:
    author = item.get("author") or {}
    return OpenPR(
        number=int(item["number"]),
        title=str(item.get("title") or ""),
        author=str(author.get("login") or ""),
        head_ref=str(item.get("headRefName") or ""),
        base_ref=str(item.get("baseRefName") or ""),
        updated_at=str(item.get("updatedAt") or ""),
        url=str(item.get("url") or ""),
    )


def pick_pr_interactive(
    repo: str,
    prs: list[OpenPR],
    *,
    out=sys.stderr,
    in_=sys.stdin,
) -> int | None:
    """Numbered stdin picker. Returns the chosen PR's number, or None
    if the user typed `q` / EOF'd out. `out` and `in_` are injectable
    for tests.
    """
    out.write(f"Open PRs awaiting your review in {repo}:\n\n")
    for i, pr in enumerate(prs, start=1):
        out.write(f"  [{i}] {pr.picker_line()}\n")
    out.write(f"\nPick a PR [1-{len(prs)}, q to quit]: ")
    out.flush()
    choice = (in_.readline() or "").strip().lower()
    if not choice or choice == "q":
        return None
    if choice.isdigit():
        n = int(choice)
        if 1 <= n <= len(prs):
            return prs[n - 1].number
    out.write(f"invalid selection: {choice!r}\n")
    out.flush()
    return None


# ---------------------------------------------------------------------------
# Posting comments back to GitHub
# ---------------------------------------------------------------------------

def map_side(viewer_side: str) -> str:
    """Translate the viewer's `old`/`new` side label into GitHub's
    `LEFT`/`RIGHT`. Anything else raises — we don't want silent
    fallbacks here.
    """
    if viewer_side == "old":
        return "LEFT"
    if viewer_side == "new":
        return "RIGHT"
    raise ValueError(f"unknown viewer side: {viewer_side!r}")


def _github_db_id(comment_id: Any) -> int | None:
    """Extract the upstream databaseId from an ingest-side comment id.

    Ingested ids look like ``"gh-3331909762"`` — the integer suffix is
    GitHub's review-comment databaseId. Returns None for ids that
    don't fit (a local-id reply target, garbage, etc.) so the caller
    can drop the comment with a warning rather than crash.
    """
    if not isinstance(comment_id, str) or not comment_id.startswith("gh-"):
        return None
    try:
        return int(comment_id[3:])
    except ValueError:
        return None


def comments_to_github(comments: Iterable[Any]) -> list[PostedComment]:
    """Map the viewer's `Comment` objects (or dicts) into the shape
    GitHub's review-comments API accepts.

    Filters and shapes in one pass:

    - Comments whose ``source`` is anything other than ``"local"``
      drop out. Ingested github comments are already on GitHub — re-
      posting would create duplicates, which is the bug this filter
      exists to prevent.
    - Comments with an ``in_reply_to_id`` pointing at an ingested
      parent (``"gh-<databaseId>"``) become reply entries
      (``in_reply_to`` + ``body``), not new anchored threads.
    - Local replies to other local comments are skipped with a warning
      — the parent doesn't exist on GitHub yet, so a one-shot review
      can't thread to it.
    - Anything missing the basic shape (no body, no path on a non-reply)
      is dropped quietly.
    """
    out: list[PostedComment] = []

    def _get(c: Any, key: str) -> Any:
        return c.get(key) if isinstance(c, dict) else getattr(c, key, None)

    for c in comments:
        source = _get(c, "source") or "local"
        if source != "local":
            continue
        body = _get(c, "body")
        if not body:
            continue
        body_str = str(body)
        parent = _get(c, "in_reply_to_id")
        if parent:
            db_id = _github_db_id(parent)
            if db_id is None:
                log.warning(
                    "skipping local reply with non-github parent %r — "
                    "the bulk-review endpoint can't thread to a "
                    "comment that doesn't exist upstream yet",
                    parent,
                )
                continue
            out.append(PostedComment(body=body_str, in_reply_to=db_id))
            continue
        path = _get(c, "file")
        side = _get(c, "side")
        line = _get(c, "line")
        if not path or side is None or line is None:
            continue
        out.append(PostedComment(
            body=body_str,
            path=str(path),
            line=int(line),
            side=map_side(str(side)),
        ))
    return out


def post_inline_review(
    repo: str,
    number: int,
    head_sha: str,
    comments: Iterable[Any],
    *,
    event: str = "COMMENT",
    body: str = "",
) -> PostResult:
    """POST a single review with all the inline comments grouped under
    it. Always `event = "COMMENT"` for now (verdicts stay on
    github.com — see plan non-goals); the parameter exists so future
    flags can flip it without touching this signature.

    Accepts either raw viewer Comments (mapped + filtered via
    :func:`comments_to_github`) or already-mapped ``PostedComment``
    instances. The caller flow is typically: map first to count
    threads vs replies for the confirmation prompt, then hand the
    same list here.
    """
    if all(isinstance(c, PostedComment) for c in comments):
        posted = list(comments)
    else:
        posted = comments_to_github(comments)
    if not posted:
        raise GhError("no postable comments after mapping (all entries malformed?)")
    payload = {
        "commit_id": head_sha,
        "event": event,
        "body": body,
        "comments": [c.to_payload() for c in posted],
    }
    rc, stdout, stderr = git_ops.gh_capture(
        "api", "-X", "POST",
        f"repos/{repo}/pulls/{number}/reviews",
        "--input", "-",
        input=json.dumps(payload),
    )
    if rc != 0:
        # gh's stderr is usually informative; pass it through verbatim.
        raise GhError(
            f"`gh api` POST review failed (exit {rc}): "
            f"{stderr.strip() or stdout.strip()}"
        )
    response = json.loads(stdout or "{}")
    return PostResult(
        review_id=int(response.get("id", 0)),
        review_url=str(response.get("html_url", "")),
        posted=len(posted),
    )
