"""Filesystem-path helpers shared across the CLI and runner.

Kept tiny so it can be imported eagerly without dragging in heavy
dependencies (anthropic SDK, jinja, etc.).
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path


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
        r = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, check=True,
        )
        identity = str(Path(r.stdout.strip()).resolve())
    except (subprocess.CalledProcessError, FileNotFoundError):
        identity = str(Path.cwd().resolve())
    fp = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return cache_root / fp
