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
from ..format.parse import parse_raw_diff
from ..format.sidecar import dump_sidecar
from .agents import Client
from .hunks import (
    apply_hunk_annotations, build_hunk_annotations, overview_to_prompt_json,
    run_hunk_pass,
)
from .overview import apply_overview_to_diff, run_overview_pass
from .progress import ProgressMeter
from .prompts import PROMPT_VERSION
from .schemas import (
    AnnotatedDiff, AnnotatedFile, AnnotatedHunk, FileAnnotations, FileRole,
    HunkAnnotations, PRInfo, ParsedDiff, lift_file,
)
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
    parsed = parse_raw_diff(raw)
    pr = PRInfo(
        pr_url=meta.get("url", ""),
        base_sha=meta.get("baseRefOid", ""),
        head_sha=meta.get("headRefOid", ""),
        model=model,
    )
    parsed_files = parsed.files
    if only_files:
        parsed_files = [f for f in parsed_files if f.path in only_files]

    # Lift to AnnotatedDiff with empty annotations. Skipped (lock / binary)
    # files get their FileAnnotations pre-populated so that downstream
    # passes leave them alone and the viewer renders the right label.
    skipped_files: set[str] = set()
    diff_files: list[AnnotatedFile] = []
    for pfile in parsed_files:
        if _should_skip(pfile.path):
            ann = FileAnnotations(role=FileRole.GENERATED, summary="Generated / lock file — not analysed.")
            skipped_files.add(pfile.path)
        else:
            ann = FileAnnotations()
        diff_files.append(lift_file(pfile, ann=ann))
    diff = AnnotatedDiff(version=parsed.version, pr=pr, files=diff_files)

    if skipped_files:
        log.info("skipping %d generated file(s): %s",
                 len(skipped_files), ", ".join(sorted(skipped_files)))

    trace_dir = run_dir / "trace"
    trace_dir.mkdir(parents=True, exist_ok=True)
    _attach_file_log(trace_dir / "augment.log")

    # Enumerate hunks ahead of dispatch so we know the total up front
    # (the progress meter wants this; the dispatch loop wants ordinal
    # indices to attribute start/finish events to the right square).
    queued: list[tuple[int, int, int]] = []  # (file_idx, hunk_idx, ordinal)
    for fi, fp in enumerate(diff.files):
        if fp.path in skipped_files:
            continue
        for hi in range(len(fp.hunks)):
            if max_hunks is not None and len(queued) >= max_hunks:
                break
            queued.append((fi, hi, len(queued)))
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
                diff = apply_overview_to_diff(diff, ov)
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
            results: dict[tuple[int, int], HunkAnnotations] = {}
            tasks = [
                asyncio.create_task(
                    _augment_one_hunk(
                        ord_idx, meter, sem, client, diff, fi, hi,
                        overview_json, repo_tools, model, cache,
                        trace_dir, stats, results,
                    )
                )
                for fi, hi, ord_idx in queued
            ]

            log.info("per-hunk pass: %d hunks queued (concurrency=%d)",
                     len(tasks), concurrency)
            await asyncio.gather(*tasks)

        # Merge per-hunk results back into the diff in one pass.
        diff = _merge_hunk_results(diff, results)

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
    ord_idx: int,
    meter: ProgressMeter,
    sem: asyncio.Semaphore,
    client: Client,
    diff: AnnotatedDiff,
    fi: int,
    hi: int,
    overview_json: str,
    repo_tools: RepoTools | None,
    model: str,
    cache: CacheStore | None,
    trace_dir: Path,
    stats: _HunkStats,
    results: dict[tuple[int, int], HunkAnnotations],
) -> None:
    fp = diff.files[fi]
    hunk = fp.hunks[hi]
    file_summary = (fp.ann.summary or "").strip()
    async with sem:
        # Mark the square live only AFTER acquiring the semaphore so
        # queued-but-unstarted hunks still render as pending dots.
        meter.start_hunk(ord_idx)
        try:
            if repo_tools is None:
                rt = RepoTools(
                    head_worktree=Path("/dev/null"), repo_git=Path("/dev/null"),
                    base_sha="", head_sha="",
                )
            else:
                rt = repo_tools
            submit = await run_hunk_pass(
                client, fp=fp, hunk=hunk,
                overview_json=overview_json, file_summary=file_summary,
                repo_tools=rt, model=model, cache=cache,
                trace_dir=trace_dir,
            )
            ann = build_hunk_annotations(hunk.parsed, submit)
            results[(fi, hi)] = ann
            stats.ok += 1
            meter.finish_hunk(ord_idx, ok=True)
            log.info("hunk %s @ %s: intent=%r smells=%d segs=%d",
                     fp.path, hunk.parsed.header,
                     (ann.intent or "")[:80], len(ann.smells), len(ann.segments))
        except Exception as e:  # noqa: BLE001
            stats.failed += 1
            meter.finish_hunk(ord_idx, ok=False)
            log.warning(
                "hunk %s @ %s failed: %s: %s",
                fp.path, hunk.parsed.header, type(e).__name__, e,
            )


def _merge_hunk_results(
    diff: AnnotatedDiff,
    results: dict[tuple[int, int], HunkAnnotations],
) -> AnnotatedDiff:
    if not results:
        return diff
    new_files: list[AnnotatedFile] = []
    for fi, fp in enumerate(diff.files):
        new_hunks: list[AnnotatedHunk] = []
        for hi, h in enumerate(fp.hunks):
            ann = results.get((fi, hi))
            if ann is None:
                new_hunks.append(h)
            else:
                new_hunks.append(h.model_copy(update={"ann": ann}))
        new_files.append(fp.model_copy(update={"hunks": new_hunks}))
    return diff.model_copy(update={"files": new_files})


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
