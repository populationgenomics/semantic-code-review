"""Filesystem-path helpers shared across the CLI and runner.

Kept tiny so it can be imported eagerly without dragging in heavy
dependencies (anthropic SDK, jinja, etc.).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from . import git_ops


def default_config_path() -> Path:
    """Path to the user-level scr config.

    `~/.config/scr/config.toml`, or `$XDG_CONFIG_HOME/scr/config.toml`
    when set. The file is optional — its absence is the same as an
    empty config.
    """
    config_root = Path(
        os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    ) / "scr"
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
    cache_root = Path(
        os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    ) / "scr" / "runs"
    try:
        identity = str(Path(git_ops.common_dir()).resolve())
    except (git_ops.GitError, FileNotFoundError):
        identity = str(Path.cwd().resolve())
    fp = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return cache_root / fp
