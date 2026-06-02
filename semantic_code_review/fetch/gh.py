"""PR URL parsing and `gh`-based PR metadata/diff retrieval.

Subprocess invocations live in :mod:`semantic_code_review.git_ops`; this
module owns the wire-format models (`PRRef`, `_PR_FIELDS`) and the
error-message translation specific to PR fetch.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .. import git_ops
from ..git_ops import GhError, GhMissingError


_PR_URL_RE = re.compile(r"https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)/?")


@dataclass(frozen=True)
class PRRef:
    owner: str
    repo: str
    number: int

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}/pull/{self.number}"

    @property
    def clone_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}.git"


def parse_pr_url(url: str) -> PRRef:
    m = _PR_URL_RE.match(url.strip())
    if not m:
        raise ValueError(f"not a GitHub PR URL: {url!r}")
    return PRRef(owner=m.group(1), repo=m.group(2), number=int(m.group(3)))


_PR_FIELDS = [
    "title",
    "body",
    "author",
    "baseRefName",
    "baseRefOid",
    "headRefName",
    "headRefOid",
    "labels",
    "url",
    "additions",
    "deletions",
    "changedFiles",
    "files",
    "number",
]


# Public alias kept for callers that catch the PR-fetch failure mode by
# name. `git_ops.GhMissingError` (preflight failures) is a subclass of
# `GhError`, so a single `except GhFetchError` covers all gh-related
# environment errors as before.
GhFetchError = GhError


def preflight_gh() -> str:
    """Verify `gh` is installed and recent enough; return the binary path.

    Run once at the top of any command that calls a gh subprocess so a
    missing-gh / too-old-gh diagnosis surfaces before we spend time
    fetching, parsing, or contacting any backend.
    """
    return git_ops.preflight_gh()


def fetch_pr_meta(ref: PRRef) -> dict:
    rc, stdout, stderr = git_ops.gh_capture(
        "pr", "view", str(ref.number), "--repo", ref.slug,
        "--json", ",".join(_PR_FIELDS),
    )
    if rc != 0:
        # preflight_gh is responsible for asserting a minimum gh
        # version; anything that fails here is a per-call failure
        # (auth, rate-limit, permissions), not an environment problem.
        raise GhFetchError(f"gh pr view failed: {stderr.strip()}")
    return json.loads(stdout)


def fetch_pr_diff(ref: PRRef) -> str:
    rc, stdout, stderr = git_ops.gh_capture(
        "pr", "diff", str(ref.number), "--repo", ref.slug,
    )
    if rc != 0:
        raise GhFetchError(f"gh pr diff failed: {stderr.strip()}")
    return stdout


def fetch_pr_files(ref: PRRef) -> list[str]:
    """Return the list of changed file paths (post-image)."""
    rc, stdout, stderr = git_ops.gh_capture(
        "pr", "view", str(ref.number), "--repo", ref.slug,
        "--json", "files",
    )
    if rc != 0:
        raise GhFetchError(f"gh pr view --json files failed: {stderr.strip()}")
    data = json.loads(stdout)
    return [f["path"] for f in data.get("files", [])]


__all__ = [
    "GhFetchError", "GhMissingError",
    "PRRef", "parse_pr_url", "preflight_gh",
    "fetch_pr_meta", "fetch_pr_diff", "fetch_pr_files",
]
