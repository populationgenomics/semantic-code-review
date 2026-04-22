"""Set up base/ and head/ git worktrees under a run directory.

A single bare-ish clone at `repo.git/` serves both worktrees; we fetch
just the two SHAs we need (`--depth 1`) rather than pulling history.
GitHub supports fetching arbitrary reachable SHAs.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .gh import PRRef


def init_worktrees(run_dir: Path, ref: PRRef, base_sha: str, head_sha: str) -> tuple[Path, Path]:
    """Create `<run_dir>/base` and `<run_dir>/head` and return their paths."""
    repo_git = run_dir / "repo.git"
    base = run_dir / "base"
    head = run_dir / "head"

    if base.exists() and head.exists() and repo_git.exists():
        return base, head

    repo_git.mkdir(parents=True, exist_ok=True)
    _git(repo_git.parent, "init", str(repo_git))
    _git(repo_git, "remote", "add", "origin", ref.clone_url)
    _git(repo_git, "fetch", "--depth", "1", "origin", base_sha, head_sha)

    if not base.exists():
        _git(repo_git, "worktree", "add", "--detach", str(base), base_sha)
    if not head.exists():
        _git(repo_git, "worktree", "add", "--detach", str(head), head_sha)

    return base, head


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd,
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def run_dir_name(ref: PRRef, head_sha: str) -> str:
    short = head_sha[:8]
    return f"{ref.owner}-{ref.repo}-pr{ref.number}-{short}"
