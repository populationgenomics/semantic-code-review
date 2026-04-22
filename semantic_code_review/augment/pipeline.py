"""LLM augmentation pipeline.

High-level entry: `augment_run_dir` takes a fetched run directory and
produces augmented.diff + augmented.scr.json. Uses Claude with tool use
over the head worktree.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from ..cache.store import CacheStore
from ..format.emit import emit_augmented_diff
from ..format.parse import parse_augmented_diff
from ..format.sidecar import dump_sidecar
from .hunks import (
    apply_hunk_annotations, overview_to_prompt_json, run_hunk_pass,
)
from .overview import apply_overview_to_diff, run_overview_pass
from .prompts import PROMPT_VERSION
from .runner import AnthropicClient, ClaudeClient
from .schemas import AugmentedDiff, PRInfo
from .tools import RepoTools


log = logging.getLogger(__name__)


async def augment_run_dir(
    run_dir: Path,
    *,
    model: str = "claude-opus-4-7",
    concurrency: int = 8,
    client: ClaudeClient | None = None,
    cache: CacheStore | None = None,
    only_files: list[str] | None = None,
    max_hunks: int | None = None,
    skip_overview: bool = False,
    skip_context: bool = False,
) -> Path:
    """Augment a fetch run directory. Returns the augmented.diff path."""
    if client is None:
        client = AnthropicClient()
    # cache=None means "no disk caching"; callers pass a CacheStore to enable.

    raw_diff_path = run_dir / "raw.diff"
    meta_path = run_dir / "meta.json"
    augmented_path = run_dir / "augmented.diff"
    sidecar_path = run_dir / "augmented.scr.json"

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    raw = raw_diff_path.read_text(encoding="utf-8")
    diff = parse_augmented_diff(raw)
    diff.pr = PRInfo(
        pr_url=meta.get("url", ""),
        base_sha=meta.get("baseRefOid", ""),
        head_sha=meta.get("headRefOid", ""),
        model=model,
    )

    if only_files:
        diff.files = [f for f in diff.files if f.path in only_files]

    # --- Overview pass -----------------------------------------------------
    if not skip_overview:
        log.info("overview pass for %d files", len(diff.files))
        ov = await run_overview_pass(client, diff=diff, meta=meta, model=model, cache=cache)
        apply_overview_to_diff(diff, ov)

    # --- Per-hunk pass -----------------------------------------------------
    repo_tools = RepoTools(
        head_worktree=run_dir / "head",
        repo_git=run_dir / "repo.git",
        base_sha=diff.pr.base_sha,
        head_sha=diff.pr.head_sha,
    ) if not skip_context else None

    overview_json = overview_to_prompt_json(diff)

    sem = asyncio.Semaphore(concurrency)
    tasks: list[asyncio.Task] = []
    hunks_seen = 0
    for fp in diff.files:
        file_summary = (fp.summary or "").strip()
        for h in fp.hunks:
            if max_hunks is not None and hunks_seen >= max_hunks:
                break
            hunks_seen += 1
            tasks.append(asyncio.create_task(
                _augment_one_hunk(sem, client, fp, h, overview_json, file_summary,
                                  repo_tools, model, cache)
            ))
        if max_hunks is not None and hunks_seen >= max_hunks:
            break

    await asyncio.gather(*tasks)

    # --- Emit --------------------------------------------------------------
    augmented_text = emit_augmented_diff(diff)
    augmented_path.write_text(augmented_text, encoding="utf-8")
    dump_sidecar(diff, sidecar_path)
    log.info("wrote %s (%d bytes) + sidecar", augmented_path.name, len(augmented_text))
    return augmented_path


async def _augment_one_hunk(
    sem: asyncio.Semaphore,
    client: ClaudeClient,
    fp: Any,
    h: Any,
    overview_json: str,
    file_summary: str,
    repo_tools: RepoTools | None,
    model: str,
    cache: CacheStore | None,
) -> None:
    async with sem:
        try:
            if repo_tools is None:
                # Skip-context mode: call with dummy RepoTools that rejects calls.
                from pathlib import Path as _P
                repo_tools = RepoTools(
                    head_worktree=_P("/dev/null"), repo_git=_P("/dev/null"),
                    base_sha="", head_sha="",
                )
            submit = await run_hunk_pass(
                client, fp=fp, hunk=h,
                overview_json=overview_json, file_summary=file_summary,
                repo_tools=repo_tools, model=model, cache=cache,
            )
            apply_hunk_annotations(h, submit)
        except Exception as e:  # noqa: BLE001
            log.warning("hunk %s @ %s failed: %s", fp.path, h.header, e)


__all__ = ["augment_run_dir"]
