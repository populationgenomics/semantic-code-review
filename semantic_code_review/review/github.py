"""GitHub PR-review helpers for the `scr pr` command.

Three responsibilities:

1. Enumerate open PRs in a repo where the current `gh` user has been
   requested as a reviewer (so the no-number invocation can either
   auto-select a single match or present a picker).
2. Run a numbered stdin picker when there's more than one match.
3. Map a [[reviewer-comment]] to the shape GitHub's review mutations
   take (`PostedComment`): an anchored thread, or a reply to an
   upstream comment by its node id.

All GitHub I/O goes through `git_ops`'s `gh` wrappers; auth and host
config piggy-back on whatever `gh auth status` reports. Callers
preflight `gh` once at the CLI boundary (`fetch.preflight_gh`). The
mutations themselves live in `github_graphql.py`.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, TextIO

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
        return f"#{self.number} {title}\n        {self.author or '?'} · {head} → {base}" + (
            f" · updated {self.updated_at}" if self.updated_at else ""
        )


@dataclass(frozen=True)
class PostedComment:
    """One inline review comment, ready to feed into a GraphQL mutation.

    Either *anchored* — ``path`` + ``line`` + ``side`` set, ``in_reply_to_node_id``
    is None — for a new thread (becomes addPullRequestReviewThread), or a
    *reply* — ``in_reply_to_node_id`` set, anchor fields None — to an
    existing upstream comment (becomes addPullRequestReviewComment with
    inReplyTo set). The GraphQL post pipeline dispatches per shape.

    ``source_id`` carries the source :class:`Comment.id`, so what GitHub
    answers can be written back onto the comment it came from.
    """

    body: str
    path: str | None = None
    line: int | None = None
    side: str | None = None  # "LEFT" or "RIGHT" for anchored comments.
    in_reply_to_node_id: str | None = None
    source_id: str | None = None

    @property
    def is_reply(self) -> bool:
        return self.in_reply_to_node_id is not None


@dataclass(frozen=True)
class PostResult:
    """Outcome of posting a review. `review_url` is GitHub's permalink
    for the new review object so the caller can offer "view on
    github.com".
    """

    review_id: int
    review_url: str
    posted: int
    #: `source_id -> upstream node id` for each comment that landed.
    #: The caller marks these posted in the store; without it they stay
    #: `source="local"` and the next post sends them again, duplicating
    #: them into a second review.
    posted_node_ids: dict[str, str] = dataclasses.field(default_factory=dict)


# Public alias kept so callers that catch posting failures by name
# don't need to import from git_ops.
GhError = git_ops.GhError


# ---------------------------------------------------------------------------
# PR resolution
# ---------------------------------------------------------------------------

_LIST_FIELDS = [
    "number",
    "title",
    "author",
    "headRefName",
    "baseRefName",
    "updatedAt",
    "url",
]


def list_review_requested_prs(repo: str) -> list[OpenPR]:
    """Open PRs in `repo` where the gh user is a requested reviewer.

    `repo` is the `owner/name` form. Uses the GitHub search qualifier
    `review-requested:@me` which is exactly the filter we want and
    keeps the auth/host story inside `gh`.
    """
    rc, stdout, stderr = git_ops.gh_capture(
        "pr",
        "list",
        "--repo",
        repo,
        "--search",
        "is:open review-requested:@me",
        "--json",
        ",".join(_LIST_FIELDS),
        "--limit",
        "100",
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
    out: TextIO = sys.stderr,
    in_: TextIO = sys.stdin,
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


def _comment_get(c: Any, key: str) -> Any:
    return c.get(key) if isinstance(c, dict) else getattr(c, key, None)


class UnpostableComment(ValueError):
    """A comment GitHub cannot take as it stands: no body, no anchor, or
    a reply whose parent has no upstream node id to thread under.
    """


def comment_to_github(c: Any, by_id: Mapping[str, Any]) -> PostedComment:
    """Map one reviewer comment (a `Comment` or its dict) to the shape
    the GraphQL mutations take.

    `by_id` holds every comment in the store, so a reply can find its
    parent's ``node_id``. A reply becomes an ``in_reply_to_node_id``
    entry; anything else an anchored one, with the viewer's `old`/`new`
    side mapped to GitHub's `LEFT`/`RIGHT`.

    Raises:
        UnpostableComment: an empty body; an anchored comment missing
            its file, side or line; a reply whose parent is unknown or
            carries no ``node_id`` (a draft that has not reached GitHub).
    """
    body = _comment_get(c, "body")
    if not body:
        raise UnpostableComment("comment has no body")
    source_id = str(_comment_get(c, "id") or "") or None
    parent_id = _comment_get(c, "in_reply_to_id")
    if parent_id:
        parent = by_id.get(str(parent_id))
        if parent is None:
            raise UnpostableComment(f"reply parent {parent_id!r} is not in the comment set")
        parent_node = _comment_get(parent, "node_id")
        if not parent_node:
            raise UnpostableComment(f"reply parent {parent_id!r} has not reached GitHub yet")
        return PostedComment(body=str(body), in_reply_to_node_id=str(parent_node), source_id=source_id)
    path = _comment_get(c, "file")
    side = _comment_get(c, "side")
    line = _comment_get(c, "line")
    if not path or side is None or line is None:
        raise UnpostableComment("comment has no file/side/line anchor")
    return PostedComment(body=str(body), path=str(path), line=int(line), side=map_side(str(side)), source_id=source_id)


def comments_to_github(comments: Iterable[Any]) -> list[PostedComment]:
    """Map every local comment in `comments` with :func:`comment_to_github`.

    Pass *all* comments (local + ingested) so reply lookups can find
    their parent's ``node_id``. Non-local comments drop out — they are
    already on GitHub. A local comment the mapper refuses is skipped
    with a warning; a caller that needs the refusal maps one at a time.
    """
    all_list = list(comments)
    by_id: dict[str, Any] = {}
    for c in all_list:
        cid = _comment_get(c, "id")
        if isinstance(cid, str):
            by_id[cid] = c

    out: list[PostedComment] = []
    for c in all_list:
        if (_comment_get(c, "source") or "local") != "local":
            continue
        try:
            out.append(comment_to_github(c, by_id))
        except UnpostableComment as e:
            log.warning("skipping comment %r: %s", _comment_get(c, "id"), e)
    return out
