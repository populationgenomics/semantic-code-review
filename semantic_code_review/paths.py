"""Filesystem-path helpers shared across the CLI and runner.

Kept tiny so it can be imported eagerly without dragging in heavy
dependencies (anthropic SDK, jinja, etc.).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from . import git_ops


def write_private_file(path: Path, text: str) -> None:
    """Write `text` to `path` as an owner-only (0600) file.

    Config and credential files (`config.toml`, `.env`) must never be
    group- or world-readable. New files are created 0600 from the start
    — via `O_CREAT` with an explicit mode rather than write-then-chmod —
    so there is no window where a freshly written secret is readable by
    others; an existing file's mode is tightened too. The parent
    directory is created if missing but its mode is left alone: the
    parent may be a shared location (a repo root, for `.env`) that must
    not be narrowed. Use `ensure_private_dir` for scr's own config dirs.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(path, 0o600)


def ensure_private_dir(path: Path) -> Path:
    """Create `path` as an owner-only (0700) directory and return it.

    For scr's own config directories (`~/.config/scr`, `<repo>/.scr`),
    whose contents may include credentials. Never call this on a shared
    parent such as a repo root.
    """
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def default_config_path() -> Path:
    """Path to the user-level scr config.

    `~/.config/scr/config.toml`, or `$XDG_CONFIG_HOME/scr/config.toml`
    when set. The file is optional — its absence is the same as an
    empty config.
    """
    config_root = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "scr"
    return config_root / "config.toml"


def find_repo_config_path(start: Path | None = None) -> Path | None:
    """Walk up from `start` (cwd by default) looking for `.scr/config.toml`.

    Stops at the filesystem root or the first match. Returns None when
    no per-repo override exists. The walk is bounded by the parent
    chain — symlinks are followed by `Path.resolve()`.
    """
    here = (start or Path.cwd()).resolve()
    for d in (here, *here.parents):
        candidate = d / ".scr" / "config.toml"
        if candidate.is_file():
            return candidate
    return None


def default_runs_root() -> Path:
    """Resolve the per-repo run-artefacts root for the current cwd.

    `~/.cache/scr/runs/<fingerprint>/` (XDG-aware), where the
    fingerprint is a sha256 of the resolved `git rev-parse
    --git-common-dir`. Worktrees of the same repo share a fingerprint;
    different repos get different ones. Falls back to a hash of cwd
    when not in a git repo (e.g. `scr fetch` outside a checkout).

    Lives outside the repo on purpose: a `.scr/` at repo root is a
    deploy-tool footgun (gcloud, docker, tar), and its worktrees can
    contain a full git history that no one ever wants uploaded.
    """
    cache_root = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")) / "scr" / "runs"
    try:
        identity = str(Path(git_ops.git(None, "rev-parse", "--git-common-dir").strip()).resolve())
    except (git_ops.GitError, FileNotFoundError):
        identity = str(Path.cwd().resolve())
    fp = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return cache_root / fp
