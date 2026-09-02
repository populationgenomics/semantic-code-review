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
from . import config, mcp_http_host, skip, source_cache, usage
from .agents import Client
from .hunks import (
    build_hunk_annotations,
    format_removed_symbols,
    overview_to_prompt_json,
    run_batch_pass,
    run_hunk_pass,
    split_batch_annotations,
)
from .overview import apply_overview_to_diff, run_overview_pass
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

#: Exception types that mean a defect in scr rather than a model or
#: transport failure. The per-hunk handlers below degrade rather than
#: abort, which is right for a model that returned nothing usable and
#: wrong for a bug: a missing keyword argument once surfaced as 33
#: "failed" hunks and a run that still exited 0. These propagate.
_BUG_ERRORS = (TypeError, AttributeError, NameError, ImportError)


def _safe_emit(on_event: OnEvent | None, event_type: str, payload: dict[str, Any]) -> None:
    if on_event is None:
        return
    try:
        on_event(event_type, payload)
    except Exception:
        log.exception("on_event consumer raised for %s; continuing", event_type)


log = logging.getLogger(__name__)


async def augment_run_dir(
    run_dir: Path,
    cfg: config.AugmentConfig,
    *,
    client: Client | None,
    cache: CacheStore | None,
    on_event: OnEvent | None = None,
    batch_size: int = 1,
) -> Path:
    """Augment a fetch run directory. Returns the augmented.diff path.

    Args:
        run_dir: A populated [[run-directory]] — `raw.diff` + `meta.json`.
        cfg: The pass's settings (model, concurrency, skip globs, extra
            review prompt).
        client: LLM backend. None falls back to the Anthropic SDK path.
        cache: Disk cache for pass results, or None for no caching.
        on_event: Streaming progress consumer, wired to the review
            server's SSE channel by `serve_review`.
        batch_size: >1 annotates up to that many hunks of one file per
            call. Batching exists because on the claude-cli backend
            nothing in the user prompt is cached, so a file's summary and
            outline are re-paid on every one of its hunks; a batch sends
            them once. Hunks a batch fails to answer for fall back to one
            call each, so a fully-failed batch costs 1 + N calls — the
            wasted batch carried every hunk body — and a partial one
            1 + |missing|. Off by default; see ADR 0005.

    Raises:
        ValueError: If `batch_size` is below 1.
    """
    if batch_size < 1:
        raise ValueError(f"batch_size must be >= 1, got {batch_size}")
    model = cfg.model
    concurrency = cfg.concurrency
    skip_globs = cfg.skip_globs
    extra_review_prompt = cfg.extra_review_prompt
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
    # Lift to AnnotatedDiff with empty annotations. Skipped (lock / binary)
    # files get their FileAnnotations pre-populated so that downstream
    # passes leave them alone and the viewer renders the right label.
    skipped_files: set[str] = set()
    diff_files: list[AnnotatedFile] = []
    for pfile in parsed.files:
        if skip.should_skip(pfile.path, skip_globs):
            ann = FileAnnotations(role=FileRole.GENERATED, summary=skip.SKIP_SUMMARY)
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

    # Flat work list of the hunks to annotate, skipped files excluded.
    # Both dispatch paths and the per-file outline / removed-symbol
    # seeds below iterate it.
    queued: list[tuple[int, int]] = []  # (file_idx, hunk_idx)
    for fi, fp in enumerate(diff.files):
        if fp.path in skipped_files:
            continue
        queued += [(fi, hi) for hi in range(len(fp.hunks))]

    # One read/parse memo for the whole run — the seed's base/head parse
    # and every per-hunk tool call share it (ADR 0003 Slice 1).
    parse_cache = source_cache.SourceCache()

    # Deterministic structural symbol delta (ADR 0001 Slice 3). Computed
    # from our own tree-sitter parse of base vs head rather than through
    # the LLM's tool access. Best-effort: a failure leaves the overview
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

    # --- Overview pass -------------------------------------------------
    log.info("overview pass for %d files", len(diff.files))
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
        _safe_emit(on_event, "overview", _overview_event_payload(diff))
    except Exception:
        _safe_emit(on_event, "overview-failed", {})
        raise

    # --- Per-hunk pass -------------------------------------------------
    repo_tools = RepoTools(
        head_worktree=run_dir / "head",
        repo_git=run_dir / "repo.git",
        base_sha=diff.pr.base_sha,
        head_sha=diff.pr.head_sha,
        cache=parse_cache,
    )

    # CLI subprocess backends reach the tools through one warm HTTP MCP
    # server for the whole run (ADR 0003 Slice 3) — started here, torn
    # down with the client below — instead of cold-starting a stdio child
    # per hunk. SDK backends get `deps=repo_tools` directly via `Agent.run`.
    mcp_host: mcp_http_host.McpHttpHost | None = None
    if client.is_subprocess_backend:
        mcp_host = mcp_http_host.McpHttpHost(repo_tools)
        mcp_host.start()
        client.set_mcp_endpoint(mcp_host.mcp_config())

    overview_json = overview_to_prompt_json(diff, include_symbols=False)

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
    async with contextlib.AsyncExitStack() as run_stack:
        # One scope for both tool-using passes (per-hunk + extra-review):
        # aclose() drops the client's temp MCP config, stop() shuts the
        # host down — exception-safe, so neither leaks on a failed run.
        await run_stack.enter_async_context(contextlib.aclosing(client))
        if mcp_host is not None:
            run_stack.callback(mcp_host.stop)

        sem = asyncio.Semaphore(concurrency)
        stats = _HunkStats()
        results: dict[tuple[int, int], HunkAnnotations] = {}
        # One tree-sitter outline per file, shared by all its hunks:
        # it is constant across them and rides in the cached per-file
        # prompt prefix. Empty for unsupported languages and for files
        # with no head side (pure deletions).
        file_outlines: dict[int, str] = {}
        for fi, _hi in queued:
            if fi not in file_outlines:
                file_outlines[fi] = repo_tools.outline_seed(diff.files[fi].path)
        # Symbols this change deletes, per file. Every tool searches the
        # head worktree, so without this a hunk that removes code sends
        # the model hunting for symbols that are gone — it cannot tell an
        # empty result from a bad query, and rephrases instead of
        # concluding. The delta is already computed for the overview seed.
        removed_by_file: dict[int, str] = {}
        if symbol_delta is not None:
            for fi, _hi in queued:
                if fi not in removed_by_file:
                    removed_by_file[fi] = format_removed_symbols(
                        symbol_delta,
                        path=diff.files[fi].path,
                        base_sha=diff.pr.base_sha,
                    )
        # batch_size <= 1 keeps the original one-call-per-hunk path
        # rather than routing through a batch of one: the batched form
        # has its own wire format and system prompt, so "batching off"
        # has to mean the untouched pass, not a degenerate batch.
        if batch_size > 1:
            tasks = [
                asyncio.create_task(
                    _augment_one_batch(
                        batch,
                        sem,
                        client,
                        diff,
                        overview_json,
                        repo_tools,
                        model,
                        cache,
                        trace_dir,
                        stats,
                        results,
                        on_event,
                        file_spans,
                        file_outlines.get(batch[0][0], ""),
                        removed_by_file.get(batch[0][0], ""),
                    )
                )
                for batch in _plan_batches(queued, batch_size)
            ]
        else:
            tasks = [
                asyncio.create_task(
                    _augment_one_hunk(
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
                        file_outlines.get(fi, ""),
                        removed_by_file.get(fi, ""),
                    )
                )
                for fi, hi in queued
            ]

        log.info(
            "per-hunk pass: %d hunks in %d call(s) (batch_size=%d, concurrency=%d)",
            len(queued),
            len(tasks),
            max(1, batch_size),
            concurrency,
        )
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

    backend_tag = "subprocess" if client.is_subprocess_backend else "sdk"
    summary = (
        f"scr: augment backend={backend_tag} model={model} hunks={len(queued)} ok={stats.ok} failed={stats.failed}"
    )
    usage_summary = usage.write_usage_summary(run_dir)
    if usage_summary is not None:
        summary += " " + usage.format_summary_line(usage_summary)
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


def _plan_batches(
    queued: list[tuple[int, int]],
    batch_size: int,
) -> list[list[tuple[int, int]]]:
    """Group queued hunks into per-file batches of at most `batch_size`.

    Batches never span files: the whole point is that a file's summary and
    outline are constant across the batch and can therefore live in the
    system prompt, which is the region the CLI backend caches. A batch
    spanning two files would have to push both outlines back into the user
    prompt, where nothing is cached.

    `batch_size <= 1` yields one hunk per batch, i.e. the unbatched pass.
    """
    by_file: dict[int, list[tuple[int, int]]] = {}
    for entry in queued:
        by_file.setdefault(entry[0], []).append(entry)
    size = max(1, batch_size)
    batches: list[list[tuple[int, int]]] = []
    for entries in by_file.values():
        batches += [entries[i : i + size] for i in range(0, len(entries), size)]
    return batches


async def _augment_one_batch(
    batch: list[tuple[int, int]],
    sem: asyncio.Semaphore,
    client: Client,
    diff: AnnotatedDiff,
    overview_json: str,
    repo_tools: RepoTools,
    model: str,
    cache: CacheStore | None,
    trace_dir: Path,
    stats: _HunkStats,
    results: dict[tuple[int, int], HunkAnnotations],
    on_event: OnEvent | None,
    file_spans: dict[int, tuple[list, list]],
    file_outline: str,
    removed_symbols: str,
) -> None:
    """Annotate one file's batch in a single call; retry the remainder singly.

    Anything the batch doesn't answer for — a hunk it skipped, or every
    hunk if the call itself failed — falls back to the one-hunk-per-call
    path. The fallback runs *after* this batch's semaphore slot is
    released, since `_augment_one_hunk` acquires the same semaphore and
    would otherwise deadlock against a saturated pool.
    """
    fi = batch[0][0]
    fp = diff.files[fi]
    file_summary = (fp.ann.summary or "").strip()
    hunks = [(hi, fp.hunks[hi]) for _fi, hi in batch]
    fallback: list[tuple[int, int]] = []

    async with sem:
        for _fi, hi in batch:
            _safe_emit(on_event, "hunk-start", {"file_idx": fi, "hunk_idx": hi})
        try:
            submit = await run_batch_pass(
                client,
                fp=fp,
                hunks=hunks,
                overview_json=overview_json,
                file_summary=file_summary,
                repo_tools=repo_tools,
                model=model,
                file_outline=file_outline,
                removed_symbols=removed_symbols,
                cache=cache,
                trace_dir=trace_dir,
            )
        except _BUG_ERRORS:
            raise
        except Exception as e:  # noqa: BLE001 — a failed batch degrades to single calls
            log.warning(
                "batch %s [%s] failed: %s: %s — retrying its hunks individually",
                fp.path,
                ", ".join(str(hi) for hi, _ in hunks),
                type(e).__name__,
                e,
            )
            fallback = list(batch)
        else:
            by_index, missing = split_batch_annotations(submit, [hi for hi, _ in hunks])
            # Applying a returned annotation can raise on a malformed
            # payload. Guard each hunk separately: uncontained, one bad
            # entry escapes `asyncio.gather` and loses the whole run,
            # where the unbatched path would have lost one hunk.
            unusable: list[int] = []
            for hi, hunk in hunks:
                if hi not in by_index:
                    continue
                spans = file_spans.get(fi, ([], []))
                try:
                    ann = build_hunk_annotations(hunk.parsed, by_index[hi])
                    block = build_hunk_viewer_block(
                        AnnotatedHunk(parsed=hunk.parsed, ann=ann), fi, hi, spans[0], spans[1]
                    )
                except _BUG_ERRORS:
                    raise
                except Exception as e:  # noqa: BLE001 — one hunk degrades to a single call
                    log.warning(
                        "batched hunk %s @ %s unusable: %s: %s — retrying singly",
                        fp.path,
                        hunk.parsed.header,
                        type(e).__name__,
                        e,
                    )
                    unusable.append(hi)
                    continue
                results[(fi, hi)] = ann
                stats.ok += 1
                log.info(
                    "hunk %s @ %s (batched): intent=%r smells=%d segs=%d notes=%d",
                    fp.path,
                    hunk.parsed.header,
                    (ann.intent or "")[:80],
                    len(ann.smells),
                    len(ann.segments),
                    len(ann.line_notes),
                )
                _safe_emit(
                    on_event,
                    "hunk",
                    {"file_idx": fi, "hunk_idx": hi, "ok": True, "block": block},
                )
            fallback = [entry for entry in batch if entry[1] in missing or entry[1] in unusable]

    for _fi, hi in fallback:
        await _augment_one_hunk(
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
            file_outline,
            removed_symbols,
        )


async def _augment_one_hunk(
    sem: asyncio.Semaphore,
    client: Client,
    diff: AnnotatedDiff,
    fi: int,
    hi: int,
    overview_json: str,
    repo_tools: RepoTools,
    model: str,
    cache: CacheStore | None,
    trace_dir: Path,
    stats: _HunkStats,
    results: dict[tuple[int, int], HunkAnnotations],
    on_event: OnEvent | None,
    fold_spans: tuple[list, list],
    file_outline: str,
    removed_symbols: str,
) -> None:
    fp = diff.files[fi]
    hunk = fp.hunks[hi]
    file_summary = (fp.ann.summary or "").strip()
    async with sem:
        # Announced only AFTER acquiring the semaphore, so queued-but-
        # unstarted hunks stay pending in the viewer rather than all
        # lighting up as in-flight at dispatch.
        _safe_emit(on_event, "hunk-start", {"file_idx": fi, "hunk_idx": hi})
        try:
            submit = await run_hunk_pass(
                client,
                fp=fp,
                hunk=hunk,
                overview_json=overview_json,
                file_summary=file_summary,
                repo_tools=repo_tools,
                model=model,
                file_outline=file_outline,
                removed_symbols=removed_symbols,
                cache=cache,
                trace_dir=trace_dir,
            )
            ann = build_hunk_annotations(hunk.parsed, submit)
            results[(fi, hi)] = ann
            stats.ok += 1
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
        except _BUG_ERRORS:
            raise
        except Exception as e:  # noqa: BLE001 — a failed hunk costs one annotation
            stats.failed += 1
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
