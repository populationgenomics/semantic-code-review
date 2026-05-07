"""LLM augmentation pipeline.

High-level entry: `augment_run_dir` takes a fetched run directory and
produces augmented.diff + augmented.scr.json. Uses Claude with tool use
over the head worktree.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import fnmatch

from ..cache.store import CacheStore
from ..format.emit import emit_augmented_diff
from ..format.parse import parse_augmented_diff
from ..format.sidecar import dump_sidecar
from .agents import Client
from .hunks import (
    apply_hunk_annotations, overview_to_prompt_json, run_hunk_pass,
)
from .overview import apply_overview_to_diff, run_overview_pass
from .progress import ProgressMeter
from .prompts import PROMPT_VERSION
from .schemas import AugmentedDiff, FileRole, PRInfo
from .tools import RepoTools


# Paths we do not send to the LLM — lock files, vendored bundles, binary
# formats. The hunks still appear in the viewer; they just lack annotations.
DEFAULT_SKIP_GLOBS: tuple[str, ...] = (
    "*.lock",
    "*.min.js",
    "*.min.css",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "Pipfile.lock",
    "poetry.lock",
    "uv.lock",
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.svg", "*.ico",
    "*.woff", "*.woff2", "*.ttf", "*.otf",
    "*.pdf",
)


def _should_skip(path: str, extra_globs: tuple[str, ...] = ()) -> bool:
    globs = DEFAULT_SKIP_GLOBS + tuple(extra_globs)
    name = path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(path, g) or fnmatch.fnmatch(name, g) for g in globs)


log = logging.getLogger(__name__)


async def augment_run_dir(
    run_dir: Path,
    *,
    model: str = "claude-opus-4-7",
    concurrency: int = 8,
    client: Client | None = None,
    cache: CacheStore | None = None,
    only_files: list[str] | None = None,
    max_hunks: int | None = None,
    skip_overview: bool = False,
    skip_context: bool = False,
    show_progress: bool = True,
) -> Path:
    """Augment a fetch run directory. Returns the augmented.diff path."""
    if client is None:
        # Default to the Anthropic SDK path via pydantic-ai. Callers that
        # need a different backend (CLI, Gemini, tests) construct the
        # backend explicitly via `_select_client` or a stub.
        client = Client(model=f"anthropic:{model}")
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

    # Mark generated files; their hunks stay in the diff but don't go to the LLM.
    skipped_files = set()
    for fp in diff.files:
        if _should_skip(fp.path):
            fp.role = FileRole.GENERATED
            fp.summary = "Generated / lock file — not analysed."
            skipped_files.add(fp.path)
    if skipped_files:
        log.info("skipping %d generated file(s): %s",
                 len(skipped_files), ", ".join(sorted(skipped_files)))

    trace_dir = run_dir / "trace"
    trace_dir.mkdir(parents=True, exist_ok=True)
    _attach_file_log(trace_dir / "augment.log")

    # Enumerate hunks ahead of dispatch so we know the total up front
    # (the progress meter wants this; the dispatch loop wants ordinal
    # indices to attribute start/finish events to the right square).
    queued: list[tuple[Any, Any, int]] = []
    for fp in diff.files:
        if fp.path in skipped_files:
            continue
        for h in fp.hunks:
            if max_hunks is not None and len(queued) >= max_hunks:
                break
            queued.append((fp, h, len(queued)))
        if max_hunks is not None and len(queued) >= max_hunks:
            break

    # show_progress=False → meter is a no-op even on a truecolor TTY
    # (caller is in --verbose mode, where the redraw line would fight
    # the log stream). show_progress=True → meter still gates on TTY +
    # truecolor advertising before drawing anything.
    meter = ProgressMeter(
        total=len(queued),
        enabled=None if show_progress else False,
    )

    async with meter:
        # --- Overview pass -------------------------------------------------
        if not skip_overview:
            log.info("overview pass for %d files", len(diff.files))
            meter.start_overview()
            try:
                ov = await run_overview_pass(
                    client, diff=diff, meta=meta, model=model,
                    cache=cache, trace_dir=trace_dir,
                )
                apply_overview_to_diff(diff, ov)
                meter.finish_overview(ok=True)
            except Exception:
                meter.finish_overview(ok=False)
                raise

        # --- Per-hunk pass -------------------------------------------------
        repo_tools = RepoTools(
            head_worktree=run_dir / "head",
            repo_git=run_dir / "repo.git",
            base_sha=diff.pr.base_sha,
            head_sha=diff.pr.head_sha,
        ) if not skip_context else None

        # CLI subprocess backends use this to spawn an MCP server bound
        # to the run's worktree. SDK backends are no-ops; the SDK Agent
        # gets `deps=repo_tools` directly via `Agent.run` in `run_hunk_pass`.
        client.set_repo_tools(repo_tools)

        overview_json = overview_to_prompt_json(diff)

        # Subprocess clients allocate temp config files at first use;
        # `aclosing` calls `client.aclose()` on exit so /tmp doesn't
        # accumulate them across runs. SDKBackend's aclose is a no-op
        # so this is uniform across backends.
        async with contextlib.aclosing(client):
            sem = asyncio.Semaphore(concurrency)
            stats = _HunkStats()
            tasks = [
                asyncio.create_task(
                    _augment_one_hunk(
                        idx, meter, sem, client, fp, h, overview_json,
                        (fp.summary or "").strip(), repo_tools, model,
                        cache, trace_dir, stats,
                    )
                )
                for fp, h, idx in queued
            ]

            log.info("per-hunk pass: %d hunks queued (concurrency=%d)",
                     len(tasks), concurrency)
            await asyncio.gather(*tasks)

        # --- Emit ----------------------------------------------------------
        augmented_text = emit_augmented_diff(diff)
        augmented_path.write_text(augmented_text, encoding="utf-8")
        dump_sidecar(diff, sidecar_path)
        log.info("wrote %s (%d bytes) + sidecar",
                 augmented_path.name, len(augmented_text))

    # After the meter has finished its final repaint and dropped to a
    # fresh line, emit the human-readable summary to stderr so the
    # one-liner doesn't fight the meter's redraw window.
    backend_tag = "subprocess" if client.is_subprocess_backend else "sdk"
    summary = (
        f"scr augment: backend={backend_tag} model={model} hunks={len(tasks)} "
        f"ok={stats.ok} failed={stats.failed}"
    )
    log.info(summary)
    import sys as _sys
    _sys.stderr.write(summary + "\n")
    _sys.stderr.flush()

    if stats.failed and stats.ok == 0:
        log.error(
            "augmentation produced ZERO annotations: all %d hunks failed. "
            "See per-hunk warnings and trace files under %s. "
            "Common cause in --backend=claude-cli: `claude -p` not logged in or "
            "refused to emit structured JSON within --max-turns.",
            stats.failed, trace_dir,
        )
    return augmented_path


@dataclass
class _HunkStats:
    ok: int = 0
    failed: int = 0


async def _augment_one_hunk(
    idx: int,
    meter: ProgressMeter,
    sem: asyncio.Semaphore,
    client: Client,
    fp: Any,
    h: Any,
    overview_json: str,
    file_summary: str,
    repo_tools: RepoTools | None,
    model: str,
    cache: CacheStore | None,
    trace_dir: Path,
    stats: _HunkStats,
) -> None:
    async with sem:
        # Mark the square live only AFTER acquiring the semaphore so
        # queued-but-unstarted hunks still render as pending dots.
        meter.start_hunk(idx)
        try:
            if repo_tools is None:
                repo_tools = RepoTools(
                    head_worktree=Path("/dev/null"), repo_git=Path("/dev/null"),
                    base_sha="", head_sha="",
                )
            submit = await run_hunk_pass(
                client, fp=fp, hunk=h,
                overview_json=overview_json, file_summary=file_summary,
                repo_tools=repo_tools, model=model, cache=cache,
                trace_dir=trace_dir,
            )
            apply_hunk_annotations(h, submit)
            stats.ok += 1
            meter.finish_hunk(idx, ok=True)
            log.info("hunk %s @ %s: intent=%r smells=%d segs=%d",
                     fp.path, h.header, (h.intent or "")[:80], len(h.smells), len(h.segments))
        except Exception as e:  # noqa: BLE001
            stats.failed += 1
            meter.finish_hunk(idx, ok=False)
            log.warning(
                "hunk %s @ %s failed: %s: %s",
                fp.path, h.header, type(e).__name__, e,
            )


def _attach_file_log(path: Path) -> None:
    """Route `semantic_code_review.*` INFO+ log records to `path`."""
    root = logging.getLogger("semantic_code_review")
    # Idempotent: replace any previous FileHandler for a different run.
    for existing in list(root.handlers):
        if isinstance(existing, logging.FileHandler):
            root.removeHandler(existing)
    handler = logging.FileHandler(path, mode="w", encoding="utf-8")
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    if root.level == 0 or root.level > logging.INFO:
        root.setLevel(logging.INFO)


__all__ = ["augment_run_dir"]
