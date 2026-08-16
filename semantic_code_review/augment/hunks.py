"""Per-hunk pass: intent + segments + smells + context + refs.

Fused into a single call per hunk for v1. The system prompt frames the
job as comprehension-first; smells are secondary.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic_ai import CachePoint
from pydantic_ai.messages import UserContent

from ..augment.schemas import (
    AnnotatedDiff,
    AnnotatedFile,
    AnnotatedHunk,
    FoldDescription,
    HunkAnnotations,
    LineNote,
    Overview,
    ParsedHunk,
    Ref,
    Segment,
    Smell,
)
from ..cache.store import CacheStore
from ..format import linenos
from ..structural import ChangedSymbol
from .agents import Client, make_batch_agent, make_hunk_agent
from .pass_ import PassMeta, run_pass
from .prompts import HUNK_BATCH_SYSTEM, HUNK_SYSTEM
from .tools import TOOL_FUNCTIONS, RepoTools

log = logging.getLogger(__name__)

_HUNK = PassMeta(
    name="hunk",
    submit_tool="submit_annotations",
    tool_names=tuple(fn.__name__ for fn in TOOL_FUNCTIONS),
)

# Same submit tool and pass name as the single-hunk form: a batch is a
# different grouping of the same work, and `usage.json` should account for
# both under `hunk` rather than splitting the pass in two.
_HUNK_BATCH = PassMeta(
    name="hunk",
    submit_tool="submit_annotations",
    tool_names=tuple(fn.__name__ for fn in TOOL_FUNCTIONS),
)


# Anthropic prompt-caching settings applied to every per-hunk call.
#
# The cacheable prefix on this pass is `[system prompt] + [tool defs] +
# [overview] + [file summary]`. The first two stay byte-identical for
# every hunk in the run, so caching them buys cross-hunk reuse on a
# multi-hunk PR. The CachePoint markers in `format_hunk_prompt` then
# split the user prompt so within-file (overview + summary cached) and
# within-PR (overview cached) prefixes are reused too.
#
# `anthropic_cache_messages` is the one that matters on a tool-using
# pass, and its absence was costing more than the other two save. The
# agent loop appends [tool_use][tool_result] per turn *after* every
# fixed breakpoint, so without a rolling breakpoint the whole growing
# conversation is re-sent as fresh input every turn: measured on a
# 51-turn hunk, cache_read sat flat at 7,076 while fresh input climbed
# 7,740 -> 46,698 and cache_write stayed at 0 throughout. Cost per turn
# grows linearly and the loop's total grows quadratically. With the
# rolling breakpoint each turn writes only its own delta and reads the
# accumulated prefix at a tenth of the price.
#
# AnthropicModelSettings keys are silently ignored by non-Anthropic
# backends (TypedDict total=False), so this is safe to apply
# unconditionally — Google + the CLI drivers see them as no-ops.
_HUNK_CACHE_SETTINGS: dict[str, Any] = {
    "anthropic_cache_instructions": True,  # system prompt block
    "anthropic_cache_tool_definitions": True,  # tools/<RepoTools>
    "anthropic_cache_messages": True,  # rolling breakpoint on the latest turn
}

# Adaptive thinking, with the summary returned rather than omitted.
# Leaving `thinking` unset means the model does not think at all — so
# every SDK-backend hunk had been annotated with reasoning off, while the
# CLI backend got Claude Code's own. `display="summarized"` is what makes
# a trace show *why* a pass investigated the way it did, otherwise only
# reconstructible from the tool-call sequence.
#
# Applied only alongside native structured output: Anthropic rejects
# thinking combined with the forced tool choice `ToolOutput` produces, so
# a model that cannot do native output must not be asked to think either.
_THINKING_SETTINGS: dict[str, Any] = {
    "anthropic_thinking": {"type": "adaptive", "display": "summarized"},
}


def _hunk_model_settings(client: Client) -> dict[str, Any]:
    """Model settings for a per-hunk call against `client`."""
    if not client.native_output:
        return _HUNK_CACHE_SETTINGS
    return {**_HUNK_CACHE_SETTINGS, **_THINKING_SETTINGS}


def format_removed_symbols(removed: Sequence[ChangedSymbol]) -> str:
    """Render the symbols this change deletes from a file, base-side.

    Every tool we expose searches the head worktree, so a symbol the
    change removes returns nothing from `grep`, `read_file`, or the
    outline seed. The model cannot tell "empty because it was deleted"
    from "empty because my pattern was wrong", and rephrases: one
    observed hunk spent 50 tool calls — zero exact repeats, four distinct
    paths — hunting for three symbols this commit had removed. Naming
    them, with their base-side spans, answers that question up front.
    """
    if not removed:
        return ""
    lines = [
        "# Removed by this change (base side — these are NOT in the head worktree, "
        "so `grep`/`read_file` will not find them; use `read_file_at` with the base SHA)"
    ]
    for sym in removed:
        label = sym.signature or f"{sym.kind} {sym.qualified_name}"
        lines.append(f"{label}  (base {sym.range.start_line}-{sym.range.end_line})")
    return "\n".join(lines)


def format_hunk_prompt(
    fp: AnnotatedFile,
    hunk: AnnotatedHunk,
    overview_json: str,
    file_summary: str,
    file_outline: str = "",
    removed_symbols: str = "",
) -> list[UserContent]:
    """Assemble the user-prompt blocks for one hunk call.

    Returns a `UserContent` list with `CachePoint` markers between the
    cacheable prefix sections (overview, file summary + outline) and the
    per-hunk text. pydantic-ai's Anthropic adapter translates each
    `CachePoint` into a `cache_control: ephemeral` annotation on the
    preceding text block; non-supporting providers filter the markers
    out and concatenate the text blocks. Fold-region summaries are not
    produced here — the review server fires a focused call on first
    fold-close; see :mod:`semantic_code_review.augment.fold_summary`.

    `file_outline` sits inside the per-file cached prefix — it is
    constant across the file's hunks. It is omitted when empty rather
    than emitted as an empty section: an unsupported language genuinely
    has no outline, and a header-only section reads as "there is nothing
    here".
    """
    numbered = linenos.number_for_prompt(f"{hunk.parsed.header}\n{hunk.parsed.body}")
    file_block = f"# File summary\n{file_summary}"
    if file_outline:
        file_block += f"\n\n# File outline (deterministic — tree-sitter, head side)\n{file_outline}"
    if removed_symbols:
        file_block += f"\n\n{removed_symbols}"
    hunk_text = f"# File\npath: {fp.path}\nlang: {fp.ann.lang or ''}\n\n# Hunk\n{numbered}"
    return [
        f"# PR overview\n{overview_json}",
        CachePoint(),
        file_block,
        CachePoint(),
        hunk_text,
    ]


_UNSAFE_IN_FILENAME = re.compile(r"[^A-Za-z0-9._-]")


def _hunk_trace_path(
    trace_dir: Path | None,
    fp: AnnotatedFile,
    hunk: AnnotatedHunk,
) -> Path | None:
    """Trace path for one hunk, as a flat file directly under `trace_dir`.

    A `@@` header carries git's trailing section text, which routinely holds
    a path (``@@ -74,10 +74,10 @@ See `commands/review.md` ...``). Any
    separator surviving into the name makes the trace a nested file — the
    writer `mkdir -p`s, so it lands in a stray directory instead of failing —
    and it then hides from a flat scan of the trace dir. Whitelist the
    filename-safe characters rather than blacklisting the separators seen
    so far.
    """
    if trace_dir is None:
        return None
    safe_file = _UNSAFE_IN_FILENAME.sub("_", fp.path)
    header = hunk.parsed.header.replace("+", "p").replace("-", "m")
    safe_hunk = _UNSAFE_IN_FILENAME.sub("_", header)
    return trace_dir / f"hunk-{safe_file}-{safe_hunk[:40]}.json"


async def run_hunk_pass(
    client: Client,
    *,
    fp: AnnotatedFile,
    hunk: AnnotatedHunk,
    overview_json: str,
    file_summary: str,
    repo_tools: RepoTools,
    model: str,
    file_outline: str = "",
    removed_symbols: str = "",
    cache: CacheStore | None = None,
    trace_dir: Path | None = None,
) -> dict[str, Any]:
    payload = await run_pass(
        _HUNK,
        client=client,
        agent=make_hunk_agent(client.model, native_output=client.native_output),
        user_content=format_hunk_prompt(fp, hunk, overview_json, file_summary, file_outline, removed_symbols),
        system=HUNK_SYSTEM,
        model=model,
        cache_inputs=(
            overview_json,
            file_summary,
            fp.path,
            hunk.parsed.header,
            hunk.parsed.body,
            file_outline,
            removed_symbols,
        ),
        deps=repo_tools,
        model_settings=_hunk_model_settings(client),
        cache=cache,
        trace_path=_hunk_trace_path(trace_dir, fp, hunk),
        cache_request={
            "file": fp.path,
            "header": hunk.parsed.header,
            "body_len": len(hunk.parsed.body),
        },
    )
    assert payload is not None  # `_HUNK.swallow_errors` is false
    return payload


def format_batch_system(overview_json: str) -> str:
    """Assemble the system text for one batched call: RUN-invariant only.

    The system prompt caches because it is byte-identical across every
    call in the run — one entry, written once, read by all. Per-file
    content does not belong here even though it is constant within a
    batch: it makes the prompt per-file, which turns one shared entry
    into one entry per file that nothing reads back. Measured on commit
    7a232f9, putting the file summary and outline here took billed input
    from 243k to 517k. Batch-invariant is not the test; run-invariant is.
    """
    return f"{HUNK_BATCH_SYSTEM}\n\n# PR overview\n{overview_json}"


def format_batch_prompt(
    fp: AnnotatedFile,
    hunks: Sequence[tuple[int, AnnotatedHunk]],
    file_summary: str,
    file_outline: str = "",
    removed_symbols: str = "",
) -> list[UserContent]:
    """User-prompt blocks for a batched call: file context, then each hunk.

    The per-file context sits here rather than in the system prompt: it is
    constant within the batch but not across the run, so in the system
    prompt it would un-share the one cached entry. Here it is uncached but
    sent once per batch instead of once per hunk, which is the saving
    batching is actually for.

    The `# Hunk <index>` label is the address the model echoes back as
    `hunk_index`, so it must be the hunk's position within its file — the
    same numbering the overview pass cites in `groups[].members`.
    """
    file_block = f"# File\npath: {fp.path}\nlang: {fp.ann.lang or ''}"
    if file_summary:
        file_block += f"\n\n# File summary\n{file_summary}"
    if file_outline:
        file_block += f"\n\n# File outline (deterministic — tree-sitter, head side)\n{file_outline}"
    if removed_symbols:
        file_block += f"\n\n{removed_symbols}"
    blocks = [file_block]
    for index, hunk in hunks:
        numbered = linenos.number_for_prompt(f"{hunk.parsed.header}\n{hunk.parsed.body}")
        blocks.append(f"# Hunk {index}\n{numbered}")
    return ["\n\n".join(blocks)]


def _batch_trace_path(trace_dir: Path | None, fp: AnnotatedFile, indices: Sequence[int]) -> Path | None:
    if trace_dir is None:
        return None
    safe_file = _UNSAFE_IN_FILENAME.sub("_", fp.path)
    span = f"{indices[0]}_{indices[-1]}" if indices else "empty"
    return trace_dir / f"hunk-batch-{safe_file}-{span}.json"


async def run_batch_pass(
    client: Client,
    *,
    fp: AnnotatedFile,
    hunks: Sequence[tuple[int, AnnotatedHunk]],
    overview_json: str,
    file_summary: str,
    repo_tools: RepoTools,
    model: str,
    file_outline: str = "",
    removed_symbols: str = "",
    cache: CacheStore | None = None,
    trace_dir: Path | None = None,
) -> dict[str, Any]:
    """Annotate several hunks of one file in a single call.

    Returns the raw `BatchAnnotations`-shaped payload; the caller splits
    it with `split_batch_annotations` and re-requests whatever is missing.
    """
    system = format_batch_system(overview_json)
    indices = [i for i, _ in hunks]
    payload = await run_pass(
        _HUNK_BATCH,
        client=client,
        agent=make_batch_agent(client.model, system, native_output=client.native_output),
        user_content=format_batch_prompt(fp, hunks, file_summary, file_outline, removed_symbols),
        system=system,
        model=model,
        cache_inputs=(
            system,
            fp.path,
            file_summary,
            file_outline,
            removed_symbols,
            *(h.parsed.header for _, h in hunks),
            *(h.parsed.body for _, h in hunks),
        ),
        deps=repo_tools,
        model_settings=_hunk_model_settings(client),
        cache=cache,
        trace_path=_batch_trace_path(trace_dir, fp, indices),
        cache_request={"file": fp.path, "hunk_indices": indices},
    )
    assert payload is not None  # `_HUNK_BATCH.swallow_errors` is false
    return payload


def split_batch_annotations(
    submit_args: dict[str, Any],
    hunk_indices: Sequence[int],
) -> tuple[dict[int, dict[str, Any]], list[int]]:
    """Split a batched payload into per-hunk payloads keyed by hunk index.

    Args:
        submit_args: The raw `BatchAnnotations`-shaped payload.
        hunk_indices: The hunk indices the prompt actually asked about.

    Returns:
        `(by_index, missing)` — the payload for each hunk the model
        answered for, and the indices it didn't. A batch that answers for
        an index it wasn't asked about, or answers twice for the same
        one, has misread the prompt: the stray entry is dropped and the
        first answer wins, since a second entry for a hunk carries no way
        to tell which is meant. `missing` is what the caller must
        re-request individually — silently returning a partial batch
        would drop annotations the reviewer is expecting.
    """
    asked = list(hunk_indices)
    by_index: dict[int, dict[str, Any]] = {}
    for entry in submit_args.get("annotations") or []:
        try:
            idx = int(entry["hunk_index"])
        except (KeyError, TypeError, ValueError):
            log.warning("batch: entry with missing/malformed hunk_index — dropped: %r", entry)
            continue
        if idx not in asked:
            log.warning("batch: entry for hunk_index %d which was not in this batch — dropped", idx)
            continue
        if idx in by_index:
            log.warning("batch: duplicate entry for hunk_index %d — keeping the first", idx)
            continue
        by_index[idx] = entry
    missing = [i for i in asked if i not in by_index]
    if missing:
        log.warning("batch: no annotation returned for hunk_index %s — will retry individually", missing)
    return by_index, missing


def build_hunk_annotations(parsed: ParsedHunk, submit_args: dict[str, Any]) -> HunkAnnotations:
    """Validate a submit_annotations payload against `parsed` and return
    a `HunkAnnotations` record.

    Drops segments/fold_descriptions outside the hunk's post-image range
    or overlapping a previously-kept segment — the LLM occasionally emits
    pre-image line numbers or off-by-a-few ranges.
    """
    hunk_end = parsed.new_start + parsed.new_count - 1

    segments: list[Segment] = []
    last_end = parsed.new_start - 1
    for seg in submit_args.get("segments") or []:
        try:
            start = int(seg["new_start"])
            count = int(seg["new_count"])
        except (KeyError, TypeError, ValueError):
            log.warning("hunk %s: malformed segment %r — dropped", parsed.header, seg)
            continue
        end = start + count - 1
        if count <= 0 or start < parsed.new_start or end > hunk_end:
            log.warning(
                "hunk %s: segment +%d..+%d outside range +%d..+%d — dropped",
                parsed.header,
                start,
                end,
                parsed.new_start,
                hunk_end,
            )
            continue
        if start <= last_end:
            log.warning(
                "hunk %s: segment +%d..+%d overlaps previous (ends +%d) — dropped",
                parsed.header,
                start,
                end,
                last_end,
            )
            continue
        segments.append(
            Segment(
                new_start=start,
                new_count=count,
                intent=seg.get("intent", "") or "",
                smells=[_smell(s) for s in seg.get("smells") or []],
                context=seg.get("context", "") or "",
                refs=[Ref(**_ref(r)) for r in seg.get("refs") or []],
            )
        )
        last_end = end

    fold_descriptions: list[FoldDescription] = []
    for fd in submit_args.get("fold_descriptions") or []:
        try:
            start = int(fd["new_start"])
            count = int(fd["new_count"])
        except (KeyError, TypeError, ValueError):
            log.warning("hunk %s: malformed fold_description %r — dropped", parsed.header, fd)
            continue
        end = start + count - 1
        if count <= 0 or start < parsed.new_start or end > hunk_end:
            log.warning(
                "hunk %s: fold +%d..+%d outside range — dropped",
                parsed.header,
                start,
                end,
            )
            continue
        summary = (fd.get("summary") or "").strip()
        if not summary:
            continue
        fold_descriptions.append(FoldDescription(right_start=start, right_end=end, summary=summary))

    line_notes = [
        LineNote(**ln) for ln in submit_args.get("line_notes") or [] if _line_in_hunk(int(ln["line"]), parsed)
    ]

    return HunkAnnotations(
        intent=submit_args.get("intent", "") or "",
        context=submit_args.get("context", "") or "",
        confidence=submit_args.get("confidence"),
        smells=[_smell(s) for s in submit_args.get("smells") or []],
        refs=[Ref(**_ref(r)) for r in submit_args.get("refs") or []],
        line_notes=line_notes,
        segments=segments,
        fold_descriptions=fold_descriptions,
    )


def apply_hunk_annotations(hunk: AnnotatedHunk, submit_args: dict[str, Any]) -> AnnotatedHunk:
    """Return a new AnnotatedHunk with `ann` set from `submit_args`."""
    return hunk.model_copy(update={"ann": build_hunk_annotations(hunk.parsed, submit_args)})


def _line_in_hunk(line: int, parsed: ParsedHunk) -> bool:
    return parsed.new_start <= line <= parsed.new_start + parsed.new_count - 1


def _smell(d: dict[str, Any]) -> Smell:
    return Smell(tag=d.get("tag", ""), note=d.get("note", "") or "")


def _ref(d: dict[str, Any]) -> dict[str, Any]:
    return {"path": d["path"], "line": int(d["line"]), "reason": d.get("reason", "") or ""}


def overview_to_prompt_json(diff: AnnotatedDiff) -> str:
    """Serialize the overview into a compact JSON string for the hunk prompt."""
    if not isinstance(diff.overview, Overview):
        return "{}"
    payload = {
        "summary": diff.overview.summary,
        "symbols_added": [s.model_dump() for s in diff.overview.symbols_added],
        "symbols_modified": [s.model_dump() for s in diff.overview.symbols_modified],
        "symbols_removed": [s.model_dump() for s in diff.overview.symbols_removed],
        "callgraph_edges": [e.model_dump(by_alias=True) for e in diff.overview.callgraph_edges],
        "themes": list(diff.overview.themes),
    }
    return json.dumps(payload, ensure_ascii=False)
