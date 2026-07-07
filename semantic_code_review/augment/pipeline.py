"""LLM augmentation pipeline.

High-level entry: `augment_run_dir` takes a fetched run directory and
produces augmented.diff + augmented.scr.json. Uses Claude with tool use
over the head worktree.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..cache.store import CacheStore
from ..format.emit import emit_augmented_diff
from ..format.parse import parse_raw_diff
from ..format.sidecar import dump_sidecar
from ..viewer.build_json import file_fold_spans
from ..viewer.hunk_layout import build_hunk_viewer_block
from . import source_cache
from .agents import Client
from .hunks import (
    build_hunk_annotations,
    overview_to_prompt_json,
    run_hunk_pass,
)
from .overview import apply_overview_to_diff, run_overview_pass
from .progress import ProgressMeter
from .schemas import (
    AnnotatedDiff,
    AnnotatedFile,
    AnnotatedHunk,
    FileAnnotations,
    FileRole,
    HunkAnnotations,
    Overview,
    PRInfo,
    lift_file,
)
from .tools import RepoTools

# Callable signature for streaming progress events. Wired up to the
# review server's SSE channel by `serve_review`; unset elsewhere
# (CLI-only augment, tests). Calls are best-effort — pipeline must not
# fail if the consumer raises.
OnEvent = Callable[[str, dict[str, Any]], None]


def _safe_emit(on_event: OnEvent | None, event_type: str, payload: dict[str, Any]) -> None:
    if on_event is None:
        return
    try:
        on_event(event_type, payload)
    except Exception:
        log.exception("on_event consumer raised for %s; continuing", event_type)


# Paths we do not send to the LLM — lock files, vendored bundles, binary
# formats. The hunks still appear in the viewer; they just lack annotations.
DEFAULT_SKIP_GLOBS: tuple[str, ...] = (
    "*.lock",
    "*.min.js",
    "*.min.css",
    "*.map",
    "*.snap",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "Pipfile.lock",
    "poetry.lock",
    "uv.lock",
    "go.sum",
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.gif",
    "*.svg",
    "*.ico",
    "*.woff",
    "*.woff2",
    "*.ttf",
    "*.otf",
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
    skip_globs: tuple[str, ...] = (),
    skip_overview: bool = False,
    skip_context: bool = False,
    extra_review_prompt: str | None = None,
    show_progress: bool = True,
    on_event: OnEvent | None = None,
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
        if _should_skip(pfile.path, skip_globs):
            ann = FileAnnotations(role=FileRole.GENERATED, summary="Generated / lock file — not analysed.")
            skipped_files.add(pfile.path)
        else:
            ann = FileAnnotations()
        diff_files.append(lift_file(pfile, ann=ann))
    diff = AnnotatedDiff(version=parsed.version, pr=pr, files=diff_files)

    if skipped_files:
        log.info("skipping %d generated file(s): %s", len(skipped_files), ", ".join(sorted(skipped_files)))

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

    # One read/parse memo for the whole run — the seed's base/head parse
    # and every per-hunk tool call share it (ADR 0003 Slice 1).
    parse_cache = source_cache.SourceCache()

    # Deterministic structural symbol delta (ADR 0001 Slice 3). Computed
    # from our own tree-sitter parse of base vs head — independent of
    # `skip_context`, which only gates the LLM's per-hunk tool access, not
    # this in-process seed. Best-effort: a failure leaves the overview
    # unseeded (today's behaviour) rather than aborting the run.
    symbol_delta = None
    try:
        symbol_delta = RepoTools(
            head_worktree=run_dir / "head",
            repo_git=run_dir / "repo.git",
            base_sha=diff.pr.base_sha,
            head_sha=diff.pr.head_sha,
            cache=parse_cache,
        ).compute_symbol_delta()
    except Exception:  # noqa: BLE001 — seed is best-effort
        log.warning("structural symbol seed failed; overview runs unseeded", exc_info=True)

    async with meter:
        # --- Overview pass -------------------------------------------------
        if not skip_overview:
            log.info("overview pass for %d files", len(diff.files))
            meter.start_overview()
            _safe_emit(on_event, "overview-start", {})
            try:
                ov = await run_overview_pass(
                    client,
                    diff=diff,
                    meta=meta,
                    model=model,
                    delta=symbol_delta,
                    cache=cache,
                    trace_dir=trace_dir,
                )
                diff = apply_overview_to_diff(diff, ov)
                meter.finish_overview(ok=True)
                _safe_emit(on_event, "overview", _overview_event_payload(diff))
            except Exception:
                meter.finish_overview(ok=False)
                _safe_emit(on_event, "overview-failed", {})
                raise

        # --- Per-hunk pass -------------------------------------------------
        repo_tools = (
            RepoTools(
                head_worktree=run_dir / "head",
                repo_git=run_dir / "repo.git",
                base_sha=diff.pr.base_sha,
                head_sha=diff.pr.head_sha,
                cache=parse_cache,
            )
            if not skip_context
            else None
        )

        # CLI subprocess backends use this to spawn an MCP server bound
        # to the run's worktree. SDK backends are no-ops; the SDK Agent
        # gets `deps=repo_tools` directly via `Agent.run` in `run_hunk_pass`.
        client.set_repo_tools(repo_tools)

        overview_json = overview_to_prompt_json(diff)

        # Per-file definition spans, parsed once from the worktrees, so the
        # per-hunk SSE re-emits below carry symbol-aware `fold_regions`
        # addresses in lockstep with the full-page build and the viewer's
        # client-side detector. Empty lists where a worktree is absent.
        head_dir = run_dir / "head"
        base_dir = run_dir / "base"
        file_spans: dict[int, tuple[list, list]] = {
            fi: file_fold_spans(
                fp,
                base_dir if base_dir.exists() else None,
                head_dir if head_dir.exists() else None,
            )
            for fi, fp in enumerate(diff.files)
        }

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
                        ord_idx,
                        meter,
                        sem,
                        client,
                        diff,
                        fi,
                        hi,
                        overview_json,
                        repo_tools,
                        model,
                        cache,
                        trace_dir,
                        stats,
                        results,
                        on_event,
                        file_spans.get(fi, ([], [])),
                    )
                )
                for fi, hi, ord_idx in queued
            ]

            log.info("per-hunk pass: %d hunks queued (concurrency=%d)", len(tasks), concurrency)
            await asyncio.gather(*tasks)

        # Merge per-hunk results back into the diff in one pass.
        diff = _merge_hunk_results(diff, results)

        # --- PR-level extra-review pass (opt-in) -------------------------
        # Runs once over the whole diff so the user's prompt can catch
        # cross-file concerns (schema migrations, missing tests, design
        # consistency) that a per-hunk view fundamentally can't see.
        # Best-effort: any failure leaves `diff` unchanged and logs.
        if extra_review_prompt:
            from .extra_review import run_pr_level_extra_review

            diff_before = diff
            diff = await run_pr_level_extra_review(
                client,
                diff=diff,
                overview_json=overview_json,
                diff_text=raw,
                prompt_text=extra_review_prompt,
                model=model,
                cache=cache,
                trace_dir=trace_dir,
            )
            # Re-emit hunk SSE events for hunks whose line_notes grew.
            # The streaming viewer already rendered the per-hunk blocks
            # without extras; this pushes the augmented bodies so the
            # promote-to-comment affordance lights up on the new notes
            # without the user needing to refresh.
            for fi, fp in enumerate(diff.files):
                if fi >= len(diff_before.files):
                    continue
                old_fp = diff_before.files[fi]
                for hi, hunk in enumerate(fp.hunks):
                    if hi >= len(old_fp.hunks):
                        continue
                    if len(hunk.ann.line_notes) == len(old_fp.hunks[hi].ann.line_notes):
                        continue
                    block = build_hunk_viewer_block(
                        hunk,
                        fi,
                        hi,
                        *file_spans.get(fi, ([], [])),
                    )
                    _safe_emit(
                        on_event,
                        "hunk",
                        {
                            "file_idx": fi,
                            "hunk_idx": hi,
                            "ok": True,
                            "block": block,
                        },
                    )

        # --- Emit ----------------------------------------------------------
        augmented_text = emit_augmented_diff(diff)
        augmented_path.write_text(augmented_text, encoding="utf-8")
        dump_sidecar(diff, sidecar_path)
        log.info("wrote %s (%d bytes) + sidecar", augmented_path.name, len(augmented_text))

    # After the meter has finished its final repaint and dropped to a
    # fresh line, emit the human-readable summary to stderr so the
    # one-liner doesn't fight the meter's redraw window.
    backend_tag = "subprocess" if client.is_subprocess_backend else "sdk"
    summary = f"scr augment: backend={backend_tag} model={model} hunks={len(tasks)} ok={stats.ok} failed={stats.failed}"
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
            stats.failed,
            trace_dir,
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
    on_event: OnEvent | None,
    fold_spans: tuple[list, list],
) -> None:
    fp = diff.files[fi]
    hunk = fp.hunks[hi]
    file_summary = (fp.ann.summary or "").strip()
    async with sem:
        # Mark the square live only AFTER acquiring the semaphore so
        # queued-but-unstarted hunks still render as pending dots.
        meter.start_hunk(ord_idx)
        _safe_emit(on_event, "hunk-start", {"file_idx": fi, "hunk_idx": hi})
        try:
            if repo_tools is None:
                rt = RepoTools(
                    head_worktree=Path("/dev/null"),
                    repo_git=Path("/dev/null"),
                    base_sha="",
                    head_sha="",
                )
            else:
                rt = repo_tools
            submit = await run_hunk_pass(
                client,
                fp=fp,
                hunk=hunk,
                overview_json=overview_json,
                file_summary=file_summary,
                repo_tools=rt,
                model=model,
                cache=cache,
                trace_dir=trace_dir,
            )
            ann = build_hunk_annotations(hunk.parsed, submit)
            results[(fi, hi)] = ann
            stats.ok += 1
            meter.finish_hunk(ord_idx, ok=True)
            log.info(
                "hunk %s @ %s: intent=%r smells=%d segs=%d notes=%d",
                fp.path,
                hunk.parsed.header,
                (ann.intent or "")[:80],
                len(ann.smells),
                len(ann.segments),
                len(ann.line_notes),
            )
            block = build_hunk_viewer_block(
                AnnotatedHunk(parsed=hunk.parsed, ann=ann),
                fi,
                hi,
                fold_spans[0],
                fold_spans[1],
            )
            _safe_emit(
                on_event,
                "hunk",
                {
                    "file_idx": fi,
                    "hunk_idx": hi,
                    "ok": True,
                    "block": block,
                },
            )
        except Exception as e:  # noqa: BLE001
            stats.failed += 1
            meter.finish_hunk(ord_idx, ok=False)
            log.warning(
                "hunk %s @ %s failed: %s: %s",
                fp.path,
                hunk.parsed.header,
                type(e).__name__,
                e,
            )
            _safe_emit(
                on_event,
                "hunk",
                {
                    "file_idx": fi,
                    "hunk_idx": hi,
                    "ok": False,
                    "error": f"{type(e).__name__}: {e}",
                },
            )


def _overview_event_payload(diff: AnnotatedDiff) -> dict[str, Any]:
    """Build the `overview` SSE payload from the post-overview diff.

    Carries the PR-level fields the viewer wants to update (summary,
    themes, symbols, callgraph), the semantic groups the sidebar
    filters by, and per-file summaries/symbols that show up in the
    file header. Mirrors the relevant slices of `build_viewer_json`'s
    output so the viewer can patch in place without re-fetching
    `data.json`.
    """
    ov = diff.overview if isinstance(diff.overview, Overview) else None
    path_to_file_idx = {fp.path: i for i, fp in enumerate(diff.files)}
    groups: list[dict[str, Any]] = []
    if ov is not None:
        for gi, g in enumerate(ov.groups):
            hunk_ids: list[str] = []
            for m in g.members:
                fi = path_to_file_idx.get(m.path)
                if fi is None:
                    continue
                hunk_ids.append(f"H{fi}_{m.hunk_index}")
            if not hunk_ids:
                continue
            groups.append(
                {
                    "id": f"G{gi}",
                    "title": g.title,
                    "rationale": g.rationale,
                    "hunk_ids": hunk_ids,
                }
            )
    file_patches = [
        {
            "file_idx": i,
            "path": fp.path,
            "summary": fp.ann.summary,
            "language": fp.ann.lang or "",
            "symbols": (
                fp.ann.symbols.model_dump() if fp.ann.symbols else {"added": [], "modified": [], "removed": []}
            ),
            "status": fp.ann.role.value if fp.ann.role else "modified",
        }
        for i, fp in enumerate(diff.files)
    ]
    return {
        "pr": {
            "summary": ov.summary if ov else "",
            "themes": ov.themes if ov else [],
            "symbols_added": [s.model_dump() for s in (ov.symbols_added if ov else [])],
            "symbols_modified": [s.model_dump() for s in (ov.symbols_modified if ov else [])],
            "symbols_removed": [s.model_dump() for s in (ov.symbols_removed if ov else [])],
            "callgraph_edges": [e.model_dump(by_alias=True) for e in (ov.callgraph_edges if ov else [])],
        },
        "groups": groups,
        "files": file_patches,
    }


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
