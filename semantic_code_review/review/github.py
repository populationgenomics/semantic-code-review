"""GitHub PR-review helpers for the `scr pr` command.

Three responsibilities:

1. Enumerate open PRs in a repo where the current `gh` user has been
   requested as a reviewer (so the no-number invocation can either
   auto-select a single match or present a picker).
2. Run a numbered stdin picker when there's more than one match.
3. Post the inline comments collected by the viewer back to GitHub
   as a single `COMMENT`-event review.

All GitHub I/O goes through the `gh` CLI subprocess so we don't take
on a Python GitHub-client dependency. Auth and host config piggy-back
on whatever `gh auth status` reports.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Iterable


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
    path: str
    line: int
    side: str    # "LEFT" or "RIGHT"
    body: str


@dataclass(frozen=True)
class PostResult:
    """Outcome of posting a review. `review_url` is GitHub's permalink
    for the new review object so the caller can offer "view on
    github.com"."""
    review_id: int
    review_url: str
    posted: int


class GhError(RuntimeError):
    """Any non-zero exit from a `gh` subprocess we can't handle."""


def require_gh() -> str:
    """Resolve the `gh` binary path or raise GhError."""
    path = shutil.which("gh")
    if not path:
        raise GhError(
            "`gh` (GitHub CLI) not found on PATH. Install it from "
            "https://cli.github.com/ or via your package manager."
        )
    return path


# ---------------------------------------------------------------------------
# PR resolution
# ---------------------------------------------------------------------------

def list_review_requested_prs(repo: str) -> list[OpenPR]:
    """Open PRs in `repo` where the gh user is a requested reviewer.

    `repo` is the `owner/name` form. Uses the GitHub search qualifier
    `review-requested:@me` which is exactly the filter we want and
    keeps the auth/host story inside `gh`.
    """
    gh = require_gh()
    cmd = [
        gh, "pr", "list",
        "--repo", repo,
        "--search", "is:open review-requested:@me",
        "--json", "number,title,author,headRefName,baseRefName,updatedAt,url",
        "--limit", "100",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise GhError(f"`gh pr list` failed: {proc.stderr.strip() or proc.stdout.strip()}")
    raw = json.loads(proc.stdout or "[]")
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


def comments_to_github(comments: Iterable[Any]) -> list[PostedComment]:
    """Map the viewer's `Comment` objects (or dicts) into the shape
    GitHub's review-comments API accepts. We use the line+side form,
    which is the recommended modern variant.
    """
    out: list[PostedComment] = []
    for c in comments:
        path = getattr(c, "file", None) if not isinstance(c, dict) else c.get("file")
        side = getattr(c, "side", None) if not isinstance(c, dict) else c.get("side")
        line = getattr(c, "line", None) if not isinstance(c, dict) else c.get("line")
        body = getattr(c, "body", None) if not isinstance(c, dict) else c.get("body")
        if not path or side is None or line is None or not body:
            # Dropped: a malformed entry shouldn't sink the whole post.
            continue
        out.append(PostedComment(
            path=str(path),
            line=int(line),
            side=map_side(str(side)),
            body=str(body),
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
    """
    gh = require_gh()
    posted = comments_to_github(comments)
    if not posted:
        raise GhError("no postable comments after mapping (all entries malformed?)")
    payload = {
        "commit_id": head_sha,
        "event": event,
        "body": body,
        "comments": [
            {"path": c.path, "line": c.line, "side": c.side, "body": c.body}
            for c in posted
        ],
    }
    cmd = [
        gh, "api", "-X", "POST",
        f"repos/{repo}/pulls/{number}/reviews",
        "--input", "-",
    ]
    proc = subprocess.run(
        cmd,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        # gh's stderr is usually informative; pass it through verbatim.
        raise GhError(
            f"`gh api` POST review failed (exit {proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    response = json.loads(proc.stdout or "{}")
    return PostResult(
        review_id=int(response.get("id", 0)),
        review_url=str(response.get("html_url", "")),
        posted=len(posted),
    )
