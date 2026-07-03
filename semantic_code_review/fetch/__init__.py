"""Sources that produce a [[run-directory]] from some input.

Two sources today, behind a shared shape:

* :mod:`semantic_code_review.fetch.github` — GitHub PR URL → run dir
  (gh subprocess + fresh bare clone).
* :mod:`semantic_code_review.fetch.local` — git ref/range against the
  cwd repo → run dir (existing-repo worktrees, optional working-state
  symlink for head).

Both build a `RunSpec` (the shared shape, see `.run_source`); each
adds per-source worktree mechanics. The high-level entry points
``materialize_github_pr_run`` and ``materialize_local_diff_run``
return a run-directory path for callers that just want one.
"""

from __future__ import annotations

from .github import (
    GhFetchError,
    GhMissingError,
    GithubResolved,
    PRRef,
    materialize_github_pr_run,
    parse_pr_url,
    preflight_gh,
    resolve_github_pr,
    setup_github_worktrees,
)
from .local import (
    EmptyDiff,
    LocalDiffError,
    LocalResolved,
    materialize_local_diff_run,
    resolve_local_diff,
    setup_local_worktrees,
)
from .run_source import RunSpec, materialize_run_metadata

__all__ = [
    # Local-diff source
    "EmptyDiff",
    # GitHub-PR source
    "GhFetchError",
    "GhMissingError",
    "GithubResolved",
    "LocalDiffError",
    "LocalResolved",
    "PRRef",
    # Shared shape
    "RunSpec",
    "materialize_github_pr_run",
    "materialize_local_diff_run",
    "materialize_run_metadata",
    "parse_pr_url",
    "preflight_gh",
    "resolve_github_pr",
    "resolve_local_diff",
    "setup_github_worktrees",
    "setup_local_worktrees",
]
