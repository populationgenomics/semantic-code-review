"""Shared shape for the two run-directory sources.

`RunSpec` is the data shape both source pipelines (local-diff,
GitHub-PR) hand to the materialise step. `materialize_run_metadata`
writes the on-disk artefacts that every consumer downstream of the
[[run-directory]] convention assumes:

  <run_dir>/raw.diff
  <run_dir>/files.txt
  <run_dir>/meta.json
  <run_dir>/spec.md          # iff spec_md_text is set

Worktree setup (`base/` and `head/`) lives per source — the mechanics
diverge enough (fresh bare clone + remote fetch for GitHub, vs.
`worktree add` against an existing repo for local) that unifying
them would mean branching inside this function on flags from the
spec. Per-source worktree functions live in `fetch/github.py` and
`fetch/local.py` next to their resolve step.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RunSpec:
    """The shared cross-source shape — what `materialize_run_metadata`
    writes to disk. Source-specific resolved bookkeeping (worktree
    strategy, PRRef, etc.) lives on per-source wrapper structs
    (`LocalResolved`, `GithubResolved`) that compose a `RunSpec`.
    """

    slug: str
    raw_diff: str
    base_sha: str
    head_sha: str
    files: list[str]
    meta: dict[str, Any]  # PR-shaped; written verbatim to meta.json
    spec_md_text: str | None = None


def materialize_run_metadata(spec: RunSpec, runs_root: Path) -> Path:
    """Write the shared run-directory artefacts; return the run-dir path.

    Idempotent w.r.t. its own outputs: existing files are overwritten
    each call. Worktree setup is the caller's job (per-source).
    """
    run_dir = runs_root / spec.slug
    run_dir.mkdir(parents=True, exist_ok=True)

    (run_dir / "raw.diff").write_text(spec.raw_diff, encoding="utf-8")
    (run_dir / "files.txt").write_text(
        "\n".join(spec.files) + ("\n" if spec.files else ""),
        encoding="utf-8",
    )
    (run_dir / "meta.json").write_text(
        json.dumps(spec.meta, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    if spec.spec_md_text is not None:
        (run_dir / "spec.md").write_text(spec.spec_md_text, encoding="utf-8")

    return run_dir


__all__ = ["RunSpec", "materialize_run_metadata"]
