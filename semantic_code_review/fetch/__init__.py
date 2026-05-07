"""PR fetch stage: gh + sparse worktrees."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .gh import (
    GhFetchError, PRRef, fetch_pr_diff, fetch_pr_meta, parse_pr_url,
    preflight_gh,
)
from .worktree import init_worktrees, run_dir_name


@dataclass
class FetchResult:
    run_dir: Path
    ref: PRRef
    meta: dict
    base_sha: str
    head_sha: str
    base_worktree: Path
    head_worktree: Path
    raw_diff_path: Path


def fetch(pr_url: str, runs_root: Path) -> FetchResult:
    """Fetch PR metadata, diff, and worktrees into the runs root.

    Returns paths to the artefacts. Idempotent: re-running with the same
    head SHA does not re-download.
    """
    ref = parse_pr_url(pr_url)
    meta = fetch_pr_meta(ref)
    base_sha = meta["baseRefOid"]
    head_sha = meta["headRefOid"]

    run_dir = runs_root / run_dir_name(ref, head_sha)
    run_dir.mkdir(parents=True, exist_ok=True)

    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    raw_diff_path = run_dir / "raw.diff"
    if not raw_diff_path.exists():
        diff = fetch_pr_diff(ref)
        raw_diff_path.write_text(diff, encoding="utf-8")

    files_txt = run_dir / "files.txt"
    paths = [f["path"] for f in meta.get("files", [])]
    files_txt.write_text("\n".join(paths) + "\n", encoding="utf-8")

    base_wt, head_wt = init_worktrees(run_dir, ref, base_sha, head_sha)

    return FetchResult(
        run_dir=run_dir,
        ref=ref,
        meta=meta,
        base_sha=base_sha,
        head_sha=head_sha,
        base_worktree=base_wt,
        head_worktree=head_wt,
        raw_diff_path=raw_diff_path,
    )


__all__ = [
    "FetchResult", "GhFetchError", "PRRef",
    "fetch", "parse_pr_url", "preflight_gh",
]
