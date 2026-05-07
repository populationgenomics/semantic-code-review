"""Single surface for `git` and `gh` subprocess calls.

All other modules call into this one rather than building their own
`subprocess.run(["git", ...])` invocations. Two reasons:

1. Errors normalise to typed `GitError` / `GhError` exceptions; callers
   catch a class instead of inspecting `CompletedProcess.returncode`.
2. There's exactly one place to mock when testing.

Two layers:

* Generic escape hatches `git()`, `gh()`, `git_capture()`, `gh_capture()`
  for one-offs.
* Named helpers (`rev_parse`, `merge_base`, `worktree_add`, `gh_pr_view`,
  ...) for the common patterns. Add new helpers when a third call site
  shows up — until then, the escape hatch is fine.

Wire-format models (PR URL parsing, JSON field lists) stay with their
domain modules; this file only owns the subprocess invocations.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path


class GitError(RuntimeError):
    """A `git` invocation exited non-zero."""


class GhError(RuntimeError):
    """A `gh` invocation exited non-zero."""


class GhMissingError(GhError):
    """`gh` is not on PATH or its version is below the required minimum.

    Distinct from `GhError` so callers can tell missing-tool /
    too-old-tool apart from a normal API failure (auth, rate-limit, etc.)
    when they want to print an install/upgrade hint.
    """


_GH_VERSION_RE = re.compile(r"gh version (\d+)\.(\d+)\.(\d+)")
# baseRefOid / headRefOid landed in gh 2.21 (Jan 2023). Older releases
# reject them at flag-parse time, so we refuse to start.
_MIN_GH_VERSION_TUPLE = (2, 21, 0)
MIN_GH_VERSION = "2.21"


# ---------------------------------------------------------------------------
# Generic git/gh runners
# ---------------------------------------------------------------------------


def git(cwd: Path | None, *args: str) -> str:
    """Run ``git <args>``. Return stdout. Raise :class:`GitError` on
    non-zero exit. ``cwd=None`` runs in the current process's cwd."""
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def git_capture(cwd: Path | None, *args: str) -> tuple[int, str, str]:
    """Run ``git <args>``; return ``(returncode, stdout, stderr)``.

    For callers that need to distinguish "no matches" (rc=1 in git grep)
    from a real failure, or that translate specific stderr messages
    into domain errors.
    """
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def gh(*args: str, input: str | None = None) -> str:
    """Run ``gh <args>``. Return stdout. Raise :class:`GhError` on
    non-zero exit. ``input`` is forwarded as stdin."""
    proc = subprocess.run(
        ["gh", *args],
        input=input, capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        msg = (proc.stderr.strip() or proc.stdout.strip())
        raise GhError(f"gh {' '.join(args)} failed: {msg}")
    return proc.stdout


def gh_capture(*args: str, input: str | None = None) -> tuple[int, str, str]:
    """Run ``gh <args>``; return ``(returncode, stdout, stderr)``.

    Used by callers that translate specific stderr messages into
    domain errors (e.g. fetch_pr_meta's "Unknown JSON field" → upgrade
    hint).
    """
    proc = subprocess.run(
        ["gh", *args],
        input=input, capture_output=True, text=True, check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


# ---------------------------------------------------------------------------
# Higher-level git helpers
# ---------------------------------------------------------------------------


def rev_parse(cwd: Path, ref: str) -> str:
    """Resolve ``ref`` to a commit SHA against the repo at ``cwd``."""
    return git(cwd, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()


def merge_base(cwd: Path, a: str, b: str) -> str:
    return git(cwd, "merge-base", a, b).strip()


def diff(cwd: Path, *args: str) -> str:
    return git(cwd, "diff", *args)


def status_porcelain(cwd: Path) -> str:
    return git(cwd, "status", "--porcelain")


def common_dir() -> str:
    """Return the resolved ``--git-common-dir`` for the current cwd.

    Used by `paths.default_runs_root` to fingerprint the repo. Worktrees
    of the same repo share a common dir; different repos differ.
    """
    return git(None, "rev-parse", "--git-common-dir").strip()


def show(repo_git: Path, sha: str, path: str) -> str:
    """``git show <sha>:<path>``. ``repo_git`` is the .git dir cwd."""
    return git(repo_git, "show", f"{sha}:{path}")


def log_oneline(repo_git: Path, path: str, limit: int) -> str:
    """``git log -n<limit> --oneline -- <path>``."""
    return git(repo_git, "log", f"-n{limit}", "--oneline", "--", path)


def grep(cwd: Path, pattern: str, path_glob: str | None, max_hits: int) -> str:
    """Tracked-files grep via ``git grep``. Treats rc=1 (no matches) as
    success and returns an empty string. Other non-zero exits raise."""
    args = ["grep", "-n", "-I", "--max-count", str(max_hits), "-e", pattern]
    if path_glob:
        args += ["--", path_glob]
    rc, stdout, stderr = git_capture(cwd, *args)
    if rc not in (0, 1):
        raise GitError(f"git grep failed: {stderr.strip()}")
    return stdout


def init_dir(target: Path) -> None:
    """``git init <target>`` — create an empty repo at ``target``.

    Runs from ``target.parent`` so a relative ``target`` argument can't
    accidentally nest under whatever cwd happens to be set.
    """
    git(target.parent, "init", str(target))


def remote_add(cwd: Path, name: str, url: str) -> None:
    git(cwd, "remote", "add", name, url)


def fetch_depth1(cwd: Path, *refs: str, remote: str = "origin") -> None:
    """Shallow-fetch one or more refs/SHAs. GitHub allows fetching
    arbitrary reachable SHAs; we don't pull history."""
    git(cwd, "fetch", "--depth", "1", remote, *refs)


def worktree_add(repo_git: Path, path: Path, sha: str) -> None:
    """``git worktree add --detach <path> <sha>`` from ``repo_git``."""
    git(repo_git, "worktree", "add", "--detach", str(path), sha)


# ---------------------------------------------------------------------------
# gh helpers
# ---------------------------------------------------------------------------


def gh_path() -> str:
    """Resolve the ``gh`` binary on PATH or raise :class:`GhMissingError`."""
    path = shutil.which("gh")
    if not path:
        raise GhMissingError(
            "`gh` (GitHub CLI) not found on PATH. Install it from "
            "https://cli.github.com/ or via your package manager."
        )
    return path


def preflight_gh() -> str:
    """Verify ``gh`` is installed and recent enough; return the binary path.

    Raises :class:`GhMissingError` for both missing-tool and too-old
    cases so a single catch handler covers all gh-environment errors.
    Unparseable ``--version`` output does not block: a real failure
    later will surface its own error.
    """
    path = gh_path()
    rc, stdout, stderr = gh_capture("--version")
    if rc != 0:
        msg = (stderr or stdout or "").strip()
        raise GhError(f"`gh --version` failed: {msg}")
    m = _GH_VERSION_RE.search(stdout)
    if not m:
        return path
    ver = tuple(int(x) for x in m.groups())
    if ver < _MIN_GH_VERSION_TUPLE:
        version_str = ".".join(str(x) for x in ver)
        raise GhMissingError(
            f"gh {version_str} is too old for scr (need >= "
            f"{MIN_GH_VERSION}, released Jan 2023). Upgrade gh — "
            f"`brew upgrade gh` on macOS, or see "
            f"https://cli.github.com/ for other platforms."
        )
    return path


def gh_pr_view(repo: str, number: int, fields: list[str]) -> tuple[int, str, str]:
    """``gh pr view <n> --repo <r> --json <fields>``."""
    return gh_capture(
        "pr", "view", str(number), "--repo", repo,
        "--json", ",".join(fields),
    )


def gh_pr_diff(repo: str, number: int) -> tuple[int, str, str]:
    return gh_capture("pr", "diff", str(number), "--repo", repo)


def gh_pr_list(
    repo: str, search: str, fields: list[str], limit: int = 100,
) -> tuple[int, str, str]:
    return gh_capture(
        "pr", "list", "--repo", repo,
        "--search", search,
        "--json", ",".join(fields),
        "--limit", str(limit),
    )


def gh_api_post(api_path: str, payload: dict) -> tuple[int, str, str]:
    """POST a JSON payload to ``gh api <api_path>`` via stdin."""
    return gh_capture(
        "api", "-X", "POST", api_path, "--input", "-",
        input=json.dumps(payload),
    )
