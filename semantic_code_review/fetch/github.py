"""GitHub-PR source: PR URL → `GithubResolved` → run directory.

The full pipeline is three steps:

1. `resolve_github_pr(pr_url)` — gh-side metadata + diff fetch, slug
   computation, packaging into a `RunSpec` carried inside a
   `GithubResolved` wrapper. Per-source extras (the `PRRef` for the
   clone URL) live on the wrapper.
2. `materialize_run_metadata(resolved.spec, runs_root)` — shared
   on-disk write of raw.diff, files.txt, meta.json.
3. `setup_github_worktrees(run_dir, resolved)` — fresh bare clone in
   `run_dir/repo.git/`, shallow fetch of base + head SHAs, and
   `worktree add --detach` for each side.

`materialize_github_pr_run` ties the three together for callers that
just want a run directory.

Wire-format models (PR URL parsing) and the gh-subprocess-side error
translation live here; the generic `git`/`gh` subprocess surface stays
in :mod:`semantic_code_review.git_ops`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .. import git_ops
from ..git_ops import GhError, GhMissingError
from .run_source import RunSpec, materialize_run_metadata

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


# Public alias kept for callers that catch the PR-fetch failure mode by
# name. `git_ops.GhMissingError` (preflight failures) is a subclass of
# `GhError`, so a single `except GhFetchError` covers all gh-related
# environment errors as before.
GhFetchError = GhError


def parse_pr_url(url: str) -> PRRef:
    m = _PR_URL_RE.match(url.strip())
    if not m:
        raise ValueError(f"not a GitHub PR URL: {url!r}")
    return PRRef(owner=m.group(1), repo=m.group(2), number=int(m.group(3)))


def preflight_gh() -> str:
    """Verify `gh` is installed and recent enough; return the binary path.

    Run once at the top of any command that calls a gh subprocess so a
    missing-gh / too-old-gh diagnosis surfaces before we spend time
    fetching, parsing, or contacting any backend.
    """
    return git_ops.preflight_gh()


# ---------------------------------------------------------------------------
# Internal gh helpers
# ---------------------------------------------------------------------------

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


def _fetch_pr_meta(ref: PRRef) -> dict:
    rc, stdout, stderr = git_ops.gh_capture(
        "pr",
        "view",
        str(ref.number),
        "--repo",
        ref.slug,
        "--json",
        ",".join(_PR_FIELDS),
    )
    if rc != 0:
        # preflight_gh is responsible for asserting a minimum gh
        # version; anything that fails here is a per-call failure
        # (auth, rate-limit, permissions), not an environment problem.
        raise GhFetchError(f"gh pr view failed: {stderr.strip()}")
    return json.loads(stdout)


def _fetch_pr_diff(ref: PRRef) -> str:
    rc, stdout, stderr = git_ops.gh_capture(
        "pr",
        "diff",
        str(ref.number),
        "--repo",
        ref.slug,
    )
    if rc != 0:
        raise GhFetchError(f"gh pr diff failed: {stderr.strip()}")
    return stdout


# ---------------------------------------------------------------------------
# Resolve + materialise
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GithubResolved:
    """`RunSpec` + GH-specific extras carried between resolve and the
    per-source worktree setup.
    """

    spec: RunSpec
    ref: PRRef


def resolve_github_pr(pr_url: str) -> GithubResolved:
    """Hit `gh` for metadata + diff; package into a `GithubResolved`.

    Side-effect-free above the gh subprocess calls — does not touch
    the filesystem. Caller-owned: deciding where to write artefacts.
    """
    ref = parse_pr_url(pr_url)
    meta = _fetch_pr_meta(ref)
    base_sha = meta["baseRefOid"]
    head_sha = meta["headRefOid"]
    raw_diff = _fetch_pr_diff(ref)
    files = [f["path"] for f in meta.get("files", [])]
    slug = f"{ref.owner}-{ref.repo}-pr{ref.number}-{head_sha[:8]}"
    spec = RunSpec(
        slug=slug,
        raw_diff=raw_diff,
        base_sha=base_sha,
        head_sha=head_sha,
        files=files,
        meta=meta,
    )
    return GithubResolved(spec=spec, ref=ref)


def setup_github_worktrees(run_dir: Path, resolved: GithubResolved) -> None:
    """Create `<run_dir>/repo.git` (bare-ish), shallow-fetch the two
    SHAs into it, and add detached worktrees at `base/` and `head/`.

    Idempotent: re-running with the same head SHA does not re-fetch
    or re-create worktrees that already exist.
    """
    repo_git = (run_dir / "repo.git").resolve()
    base = (run_dir / "base").resolve()
    head = (run_dir / "head").resolve()

    if base.exists() and head.exists() and repo_git.exists():
        return

    if not repo_git.exists():
        repo_git.mkdir(parents=True, exist_ok=True)
        git_ops.init_dir(repo_git)
        git_ops.git(repo_git, "remote", "add", "origin", resolved.ref.clone_url)
        git_ops.fetch_depth1(repo_git, resolved.spec.base_sha, resolved.spec.head_sha)

    if not base.exists():
        git_ops.worktree_add(repo_git, base, resolved.spec.base_sha)
    if not head.exists():
        git_ops.worktree_add(repo_git, head, resolved.spec.head_sha)


def materialize_github_pr_run(pr_url: str, runs_root: Path) -> Path:
    """High-level: resolve → materialise metadata → set up worktrees.

    Returns the run-directory path. Idempotent: re-running for the
    same head SHA re-resolves but does not re-download artefacts that
    are already on disk.

    Also seeds `comments.json` from the PR's review comments on first
    materialise, so the reviewer sees existing discussion alongside
    the diff. Imported lazily to avoid a cycle: github_comments imports
    PRRef from this module.
    """
    resolved = resolve_github_pr(pr_url)
    run_dir = materialize_run_metadata(resolved.spec, runs_root)
    setup_github_worktrees(run_dir, resolved)
    from .github_comments import materialize_pr_comments

    materialize_pr_comments(run_dir, resolved.ref, head_sha=resolved.spec.head_sha)
    return run_dir


__all__ = [
    "GhFetchError",
    "GhMissingError",
    "GithubResolved",
    "PRRef",
    "materialize_github_pr_run",
    "parse_pr_url",
    "preflight_gh",
    "resolve_github_pr",
    "setup_github_worktrees",
]
