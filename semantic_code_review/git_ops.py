"""Single surface for `git` and `gh` subprocess calls.

All other modules call into this one rather than building their own
`subprocess.run(["git", ...])` invocations. Two reasons:

1. Errors normalise to typed `GitError` / `GhError` exceptions; callers
   catch a class instead of inspecting `CompletedProcess.returncode`.
2. There's exactly one place to mock when testing.

Two layers:

* Generic escape hatches `git()`, `gh()`, `git_capture()`, `gh_capture()`
  for one-offs. These are the canonical mock points — even when a
  caller could write its own ``subprocess.run``, it shouldn't.
* Named helpers (`rev_parse`, `grep`, `worktree_add`, `preflight_gh`,
  ...) for invocation patterns with hidden ceremony or invariants
  worth a name (tag-peeling, rc=1-means-no-match, cwd-safety, etc.).
  Pure pass-throughs that just rename arguments don't earn a helper —
  callers reach for the runner directly.

Wire-format models (PR URL parsing, JSON field lists) stay with their
domain modules; this file only owns the subprocess invocations.
"""

from __future__ import annotations

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


# ---------------------------------------------------------------------------
# Generic git/gh runners — the canonical mock points
# ---------------------------------------------------------------------------


def git(cwd: Path | None, *args: str) -> str:
    """Run ``git <args>``. Return stdout. Raise :class:`GitError` on
    non-zero exit. ``cwd=None`` runs in the current process's cwd.
    """
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
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
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def gh(*args: str, input: str | None = None) -> str:
    """Run ``gh <args>``. Return stdout. Raise :class:`GhError` on
    non-zero exit. ``input`` is forwarded as stdin.
    """
    proc = subprocess.run(
        ["gh", *args],
        input=input,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        msg = proc.stderr.strip() or proc.stdout.strip()
        raise GhError(f"gh {' '.join(args)} failed: {msg}")
    return proc.stdout


def gh_capture(*args: str, input: str | None = None) -> tuple[int, str, str]:
    """Run ``gh <args>``; return ``(returncode, stdout, stderr)``.

    Used by callers that translate specific stderr messages into
    domain errors (e.g. fetch_pr_meta).
    """
    proc = subprocess.run(
        ["gh", *args],
        input=input,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


# ---------------------------------------------------------------------------
# Named helpers — invocation patterns with hidden ceremony
# ---------------------------------------------------------------------------


def rev_parse(cwd: Path, ref: str) -> str:
    """Resolve ``ref`` to a commit SHA against the repo at ``cwd``.

    ``--verify`` + ``^{commit}`` peels tags and rejects non-commit
    objects — both invariants callers shouldn't have to remember.
    """
    return git(cwd, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()


def grep(cwd: Path, pattern: str, path_glob: str | None, max_hits: int) -> str:
    """Tracked-files grep via ``git grep``. Treats rc=1 (no matches) as
    success and returns an empty string. Other non-zero exits raise.
    """
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


def fetch_depth1(cwd: Path, *refs: str, remote: str = "origin") -> None:
    """Shallow-fetch one or more refs/SHAs. GitHub allows fetching
    arbitrary reachable SHAs; we don't pull history.
    """
    git(cwd, "fetch", "--depth", "1", remote, *refs)


def try_fetch_depth1(cwd: Path, refs: list[str], remote: str = "origin") -> set[str]:
    """Best-effort variant of :func:`fetch_depth1` that returns the SHAs
    successfully fetched.

    A single 404 (e.g. a force-pushed commit older than GitHub's 90-day
    grace window) would otherwise sink the whole fetch — for the comment
    anchor-propagation path we want to keep going and just mark the
    affected comments orphaned. We do one batch fetch first (fast path);
    if it fails we fall back to fetching SHAs one at a time and skip
    the ones that 404, so a single bad commit can't poison the rest.
    """
    if not refs:
        return set()
    try:
        fetch_depth1(cwd, *refs, remote=remote)
        return set(refs)
    except subprocess.CalledProcessError:
        pass
    out: set[str] = set()
    for ref in refs:
        try:
            fetch_depth1(cwd, ref, remote=remote)
            out.add(ref)
        except subprocess.CalledProcessError:
            continue
    return out


def worktree_add(repo_git: Path, path: Path, sha: str) -> None:
    """``git worktree add --detach <path> <sha>`` from ``repo_git``."""
    git(repo_git, "worktree", "add", "--detach", str(path), sha)


def diff_name_only(repo_git: Path, base: str, head: str) -> list[str]:
    """Paths changed between two commits, current names, blanks dropped.

    ``git diff --name-only <base> <head>`` — added, modified and renamed
    files report their head name; deleted files their (gone) base name.
    """
    out = git(repo_git, "diff", "--name-only", base, head)
    return [line for line in out.splitlines() if line]


# ---------------------------------------------------------------------------
# gh preflight
# ---------------------------------------------------------------------------

# baseRefOid / headRefOid landed in gh 2.21 (Jan 2023). Older releases
# reject them at flag-parse time, so we refuse to start.
_MIN_GH_VERSION_TUPLE = (2, 21, 0)
_MIN_GH_VERSION_STR = ".".join(str(x) for x in _MIN_GH_VERSION_TUPLE[:2])
_GH_VERSION_RE = re.compile(r"gh version (\d+)\.(\d+)\.(\d+)")


def preflight_gh() -> str:
    """Verify ``gh`` is installed and recent enough; return the binary path.

    Raises :class:`GhMissingError` for both missing-tool and too-old
    cases so a single catch handler covers all gh-environment errors.
    Unparseable ``--version`` output does not block: a real failure
    later will surface its own error.

    Callers invoke this exactly once per gh-using command. The version
    requirement is asserted here and nowhere else — downstream callers
    trust that any preflighted ``gh`` can serve the requests they
    make.
    """
    path = shutil.which("gh")
    if not path:
        raise GhMissingError(
            "`gh` (GitHub CLI) not found on PATH. Install it from https://cli.github.com/ or via your package manager."
        )
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
            f"{_MIN_GH_VERSION_STR}, released Jan 2023). Upgrade gh — "
            f"`brew upgrade gh` on macOS, or see "
            f"https://cli.github.com/ for other platforms."
        )
    return path
