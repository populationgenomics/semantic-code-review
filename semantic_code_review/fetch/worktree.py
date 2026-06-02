"""Set up base/ and head/ git worktrees under a run directory.

A single bare-ish clone at `repo.git/` serves both worktrees; we fetch
just the two SHAs we need (`--depth 1`) rather than pulling history.
GitHub supports fetching arbitrary reachable SHAs.
"""

from __future__ import annotations

from pathlib import Path

from .. import git_ops
from .gh import PRRef


def init_worktrees(run_dir: Path, ref: PRRef, base_sha: str, head_sha: str) -> tuple[Path, Path]:
    """Create `<run_dir>/base` and `<run_dir>/head` and return their paths."""
    # Use absolute paths so relative-cwd surprises inside git_ops can't
    # accidentally create nested directories under repo.git.
    run_dir = run_dir.resolve()
    repo_git = run_dir / "repo.git"
    base = run_dir / "base"
    head = run_dir / "head"

    if base.exists() and head.exists() and repo_git.exists():
        return base, head

    if not repo_git.exists():
        repo_git.mkdir(parents=True, exist_ok=True)
        git_ops.init_dir(repo_git)
        git_ops.git(repo_git, "remote", "add", "origin", ref.clone_url)
        git_ops.fetch_depth1(repo_git, base_sha, head_sha)

    if not base.exists():
        git_ops.worktree_add(repo_git, base, base_sha)
    if not head.exists():
        git_ops.worktree_add(repo_git, head, head_sha)

    return base, head


def run_dir_name(ref: PRRef, head_sha: str) -> str:
    short = head_sha[:8]
    return f"{ref.owner}-{ref.repo}-pr{ref.number}-{short}"
