"""Per-hunk pass: intent + spans + smells + context + refs.

Fused into a single call per hunk for v1. The system prompt frames the
job as comprehension-first; smells are secondary. Spans are addressed by
boundary ids (`boundaries`), never by line numbers the model computes.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic_ai import CachePoint
from pydantic_ai.messages import UserContent

from .. import structural
from ..augment.schemas import (
    AnnotatedDiff,
    AnnotatedFile,
    AnnotatedHunk,
    AnnotationSpan,
    HunkAnnotations,
    Overview,
    ParsedHunk,
    Ref,
    Smell,
)
from ..cache.store import CacheStore
from ..format import linenos
from . import boundaries
from .agents import Client, make_batch_agent, make_hunk_agent
from .pass_ import PassMeta, run_pass
from .prompts import HUNK_BATCH_SYSTEM, HUNK_SYSTEM
from .tools import TOOL_FUNCTIONS, RepoTools
from .trace_adapter import trace_filename

log = logging.getLogger(__name__)

# Shared by the single-hunk and batched forms: a batch is a different
# grouping of the same work, so `usage.json` accounts for both under
# `hunk` rather than splitting the pass in two.
_HUNK = PassMeta(
    name="hunk",
    submit_tool="submit_annotations",
    tool_names=tuple(fn.__name__ for fn in TOOL_FUNCTIONS),
)


# Anthropic prompt-caching settings applied to every per-hunk call.
#
# The cacheable prefix on this pass is `[tool defs] + [system prompt] +
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
# Anthropic allows 4 breakpoints per request. Over budget, pydantic-ai
# keeps every system and tool one and trims *message* breakpoints
# oldest-first, which silently evicts the overview marker — the only
# prefix shared across files. Tool definitions get no breakpoint of
# their own: Anthropic renders tools -> system -> messages, so caching
# after the tools block caches `[tools]`, a strict prefix of the
# `[tools + system]` that the instructions breakpoint already caches,
# with both constant for the whole run. It can never produce a hit the
# longer entry doesn't, and it was costing the slot that mattered.
_HUNK_CACHE_SETTINGS: dict[str, Any] = {
    "anthropic_cache_instructions": True,  # system prompt block
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


#: Entries rendered per section before truncating. A wholesale file
#: deletion can remove thousands of symbols, and this seed rides in
#: every hunk prompt for the file — uncapped, one measured case reached
#: ~62k tokens per hunk, which is the cost blowup the seed exists to
#: avoid paying in tool calls.
_REMOVED_SEED_MAX = 40


def format_removed_symbols(
    delta: structural.SymbolDelta | None,
    *,
    path: str,
    base_sha: str,
) -> str:
    """Render what this change deletes, for one file's hunk prompt.

    Every tool we expose searches the head worktree, so a symbol the
    change removes returns nothing from `grep`, `read_file`, or the
    outline seed. The model cannot tell "empty because it was deleted"
    from "empty because my pattern was wrong", and rephrases: one
    observed hunk spent 50 tool calls — zero exact repeats, four distinct
    paths — hunting for three symbols this commit had removed.

    Three sections, because three different questions get asked. Symbols
    gone from *this* file, with base-side spans and the base SHA to read
    them at. Symbols gone from *elsewhere in the change*, because a hunk
    that deletes call sites is looking for a definition in another file —
    the case that motivated the seed, and the one a per-file list misses.
    And symbols that only *moved*, which a per-path set-diff reports as
    removed even though they are still in the head worktree; telling the
    model to stop searching for those would earn a confidently wrong
    annotation on an ordinary refactor.
    """
    if delta is None:
        return ""
    added_by_name: dict[str, str] = {sym.qualified_name: sym.path for sym in delta.added}
    here: list[str] = []
    moved: list[str] = []
    elsewhere: list[str] = []
    for sym in delta.removed:
        destination = added_by_name.get(sym.qualified_name)
        if destination is not None and destination != sym.path:
            moved.append(f"{sym.kind} {sym.qualified_name}: {sym.path} -> {destination}")
        elif sym.path == path:
            label = sym.signature or f"{sym.kind} {sym.qualified_name}"
            here.append(f"{label}  (base {sym.range.start_line}-{sym.range.end_line})")
        else:
            elsewhere.append(f"{sym.kind} {sym.qualified_name}  ({sym.path})")

    sections: list[str] = []
    if here:
        sections.append(
            "\n".join(
                [
                    f"# Removed from this file (base side — NOT in the head worktree, so "
                    f"`grep`/`read_file` will not find them; read them with "
                    f"`read_file_at(sha={base_sha!r}, ...)`)",
                    *_capped(here),
                ]
            )
        )
    if elsewhere:
        sections.append("\n".join(["# Removed elsewhere in this change (same caveat)", *_capped(elsewhere)]))
    if moved:
        sections.append(
            "\n".join(
                [
                    "# Moved, not removed — these ARE in the head worktree at the new path",
                    *_capped(moved),
                ]
            )
        )
    return "\n\n".join(sections)


def _capped(entries: list[str]) -> list[str]:
    if len(entries) <= _REMOVED_SEED_MAX:
        return entries
    dropped = len(entries) - _REMOVED_SEED_MAX
    return [*entries[:_REMOVED_SEED_MAX], f"... and {dropped} more (list truncated)"]


def _hunk_block(label: str, hunk: AnnotatedHunk, bounds: boundaries.Boundaries) -> str:
    """One `# Hunk` prompt block: label, boundary summary, numbered body.

    The body's left gutter carries the boundary ids (`b<n>`) beside the
    post-image line numbers; the summary line names the ids' range and
    the definitions the hunk touches as id pairs, so "this whole
    function" is a pair the model copies rather than reads off the
    gutter. A deletion-only hunk has no post-image lines and no
    boundaries, and the block says so.
    """
    parsed = hunk.parsed
    if not bounds.lines:
        summary = "post-image lines: none — this hunk only deletes, so it has no boundaries; emit no `spans`."
    else:
        first, last = bounds.id_for(bounds.lines[0]), bounds.id_for(bounds.lines[-1])
        summary = (
            f"boundaries: {first}..{last} (left gutter); a span is a pair of these ids, or one id for a single line."
        )
        if bounds.structure:
            summary += "\nstructure: " + "; ".join(bounds.structure)
    body = parsed.body[:-1] if parsed.body.endswith("\n") else parsed.body
    # Without the strip, the body's final newline reads as one more context
    # line, numbered `new_start + new_count` — a line the hunk does not have.
    numbered = linenos.number_for_prompt(f"{parsed.header}\n{body}", bounds.gutter())
    return f"{label}\n{summary}\n{numbered}"


def format_hunk_prompt(
    fp: AnnotatedFile,
    hunk: AnnotatedHunk,
    overview_json: str,
    file_summary: str,
    file_outline: str = "",
    removed_symbols: str = "",
    *,
    bounds: boundaries.Boundaries,
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

    `bounds` is the hunk's boundary list; the same object resolves the
    returned spans in `build_hunk_annotations`.
    """
    file_block = f"# File summary\n{file_summary}"
    if file_outline:
        file_block += f"\n\n# File outline (deterministic — tree-sitter, head side)\n{file_outline}"
    if removed_symbols:
        file_block += f"\n\n{removed_symbols}"
    hunk_text = f"# File\npath: {fp.path}\nlang: {fp.ann.lang or ''}\n\n{_hunk_block('# Hunk', hunk, bounds)}"
    return [
        f"# PR overview\n{overview_json}",
        CachePoint(),
        file_block,
        CachePoint(),
        hunk_text,
    ]


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
    header = hunk.parsed.header.replace("+", "p").replace("-", "m")
    return trace_dir / trace_filename("hunk", fp.path, header)


async def run_hunk_pass(
    client: Client,
    *,
    fp: AnnotatedFile,
    hunk: AnnotatedHunk,
    overview_json: str,
    file_summary: str,
    repo_tools: RepoTools,
    model: str,
    bounds: boundaries.Boundaries,
    file_outline: str = "",
    removed_symbols: str = "",
    cache: CacheStore | None = None,
    trace_dir: Path | None = None,
) -> dict[str, Any]:
    """Annotate one hunk. `bounds` is what `build_hunk_annotations` resolves the payload with."""
    payload = await run_pass(
        _HUNK,
        client=client,
        agent=make_hunk_agent(client.model, native_output=client.native_output),
        user_content=format_hunk_prompt(
            fp, hunk, overview_json, file_summary, file_outline, removed_symbols, bounds=bounds
        ),
        system=HUNK_SYSTEM,
        model=model,
        # The boundary list is in the key: a cached payload's ids resolve
        # against the list it was written for, and the list depends on the
        # head file's structure as well as the hunk's text.
        cache_inputs=(
            overview_json,
            file_summary,
            fp.path,
            hunk.parsed.header,
            hunk.parsed.body,
            file_outline,
            removed_symbols,
            _boundary_cache_input(bounds),
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


def _boundary_cache_input(bounds: boundaries.Boundaries) -> str:
    return json.dumps([bounds.first_id, list(bounds.lines), list(bounds.structure)])


def format_batch_prompt(
    fp: AnnotatedFile,
    hunks: Sequence[tuple[int, AnnotatedHunk]],
    file_summary: str,
    file_outline: str = "",
    removed_symbols: str = "",
    *,
    bounds: Mapping[int, boundaries.Boundaries],
) -> list[UserContent]:
    """User-prompt blocks for a batched call: file context, then each hunk.

    The per-file context sits here rather than in the system prompt: it is
    constant within the batch but not across the run, so in the system
    prompt it would un-share the one cached entry. Here it is uncached but
    sent once per batch instead of once per hunk, which is the saving
    batching is actually for.

    The `# Hunk <index>` label is the address the model echoes back as
    `hunk_index`, so it must be the hunk's position within its file — the
    same numbering the overview pass cites in `groups[].members`. `bounds`
    is keyed the same way (`boundaries.for_batch`), numbered continuously
    across the batch so an id names one line in the whole call.
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
        blocks.append(_hunk_block(f"# Hunk {index}", hunk, bounds[index]))
    return ["\n\n".join(blocks)]


def _batch_trace_path(trace_dir: Path | None, fp: AnnotatedFile, indices: Sequence[int]) -> Path | None:
    if trace_dir is None:
        return None
    span = f"{indices[0]}_{indices[-1]}" if indices else "empty"
    return trace_dir / trace_filename("hunk-batch", fp.path, span)


async def run_batch_pass(
    client: Client,
    *,
    fp: AnnotatedFile,
    hunks: Sequence[tuple[int, AnnotatedHunk]],
    overview_json: str,
    file_summary: str,
    repo_tools: RepoTools,
    model: str,
    bounds: Mapping[int, boundaries.Boundaries],
    file_outline: str = "",
    removed_symbols: str = "",
    cache: CacheStore | None = None,
    trace_dir: Path | None = None,
) -> dict[str, Any]:
    """Annotate several hunks of one file in a single call.

    Returns the raw `BatchAnnotations`-shaped payload; the caller splits
    it with `split_batch_annotations`, resolves each entry against
    `bounds[hunk_index]`, and re-requests whatever is missing.
    """
    system = format_batch_system(overview_json)
    indices = [i for i, _ in hunks]
    payload = await run_pass(
        _HUNK,
        client=client,
        agent=make_batch_agent(client.model, system, native_output=client.native_output),
        user_content=format_batch_prompt(fp, hunks, file_summary, file_outline, removed_symbols, bounds=bounds),
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
            *(_boundary_cache_input(bounds[i]) for i in indices),
        ),
        deps=repo_tools,
        model_settings=_hunk_model_settings(client),
        cache=cache,
        trace_path=_batch_trace_path(trace_dir, fp, indices),
        cache_request={"file": fp.path, "hunk_indices": indices},
    )
    assert payload is not None  # `_HUNK.swallow_errors` is false
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


def _resolve_span(
    parsed: ParsedHunk,
    raw: dict[str, Any],
    bounds: boundaries.Boundaries,
) -> AnnotationSpan | None:
    """One submitted span with its boundary ids resolved, or None to drop it.

    A span is dropped when an id is not in the hunk's boundary list — the
    model named a boundary it was never given — or when the pair is
    inverted. Neither is a coordinate the validator could repair: an
    unknown id has no line, and an inverted pair says nothing about which
    end was meant.
    """
    start_id = raw.get("start")
    end_id = raw.get("end") or start_id
    if not isinstance(start_id, str) or not isinstance(end_id, str):
        log.warning("hunk %s: span with malformed boundary ids %r — dropped", parsed.header, raw)
        return None
    start, end = bounds.line_for(start_id), bounds.line_for(end_id)
    if start is None or end is None:
        unknown = start_id if start is None else end_id
        log.warning(
            "hunk %s: span %s..%s names unknown boundary %s — dropped", parsed.header, start_id, end_id, unknown
        )
        return None
    if end < start:
        log.warning(
            "hunk %s: span %s..%s (+%d..+%d) is inverted — dropped", parsed.header, start_id, end_id, start, end
        )
        return None
    return AnnotationSpan(
        start=start,
        end=end,
        intent=raw.get("intent", "") or "",
        smells=[_smell(s) for s in raw.get("smells") or []],
        context=raw.get("context", "") or "",
        refs=[Ref(**_ref(r)) for r in raw.get("refs") or []],
    )


def nest_spans(spans: Sequence[AnnotationSpan], *, header: str = "") -> list[AnnotationSpan]:
    """Order `spans` outermost-first and drop any that partially overlap.

    Two spans either nest or are disjoint; the viewer renders them as a
    tree. A span that starts inside another and ends past it is the model
    labelling incoherently rather than misplacing an edge — it is dropped
    with a warning, keeping the enclosing one. Two spans over the same
    range are both kept: two notes on one line are two observations.
    Sorted by `(start, -end)`, so an enclosing span precedes what it
    encloses; the sort is stable, so same-range spans keep their order.
    """
    kept: list[AnnotationSpan] = []
    open_spans: list[AnnotationSpan] = []  # ancestors of the current position
    for span in sorted(spans, key=lambda s: (s.start, -s.end)):
        while open_spans and open_spans[-1].end < span.start:
            open_spans.pop()
        if open_spans and span.end > open_spans[-1].end:
            log.warning(
                "hunk %s: span +%d..+%d partially overlaps +%d..+%d — dropped",
                header,
                span.start,
                span.end,
                open_spans[-1].start,
                open_spans[-1].end,
            )
            continue
        kept.append(span)
        open_spans.append(span)
    return kept


def build_hunk_annotations(
    parsed: ParsedHunk,
    submit_args: dict[str, Any],
    bounds: boundaries.Boundaries,
) -> HunkAnnotations:
    """Validate a submit_annotations payload against `parsed` and return
    a `HunkAnnotations` record.

    Each span's boundary ids are resolved to post-image lines through
    `bounds` — the list the prompt carried — and the spans are ordered
    outermost-first with partial overlaps dropped (`nest_spans`). Fold
    summaries are not read: they are the file's
    (`FileAnnotations.fold_descriptions`), filled by the fold-summary
    pass, never asked of the model.
    """
    resolved = [
        span for raw in submit_args.get("spans") or [] if (span := _resolve_span(parsed, raw, bounds)) is not None
    ]
    return HunkAnnotations(
        intent=submit_args.get("intent", "") or "",
        spans=nest_spans(resolved, header=parsed.header),
        context=submit_args.get("context", "") or "",
        confidence=submit_args.get("confidence"),
        smells=[_smell(s) for s in submit_args.get("smells") or []],
        refs=[Ref(**_ref(r)) for r in submit_args.get("refs") or []],
    )


def apply_hunk_annotations(
    hunk: AnnotatedHunk,
    submit_args: dict[str, Any],
    bounds: boundaries.Boundaries,
) -> AnnotatedHunk:
    """Return a new AnnotatedHunk with `ann` set from `submit_args`."""
    return hunk.model_copy(update={"ann": build_hunk_annotations(hunk.parsed, submit_args, bounds)})


def _smell(d: dict[str, Any]) -> Smell:
    return Smell(tag=d.get("tag", ""), note=d.get("note", "") or "")


def _ref(d: dict[str, Any]) -> dict[str, Any]:
    return {"path": d["path"], "line": int(d["line"]), "reason": d.get("reason", "") or ""}


def overview_to_prompt_json(diff: AnnotatedDiff) -> str:
    """Serialize the overview into a compact JSON string for a prompt.

    Prose only. The symbol inventory lives in the deterministic
    `SymbolDelta` — reachable through the `changed_symbols` tool, which
    filters by path and carries more per entry than the overview ever
    did — so nothing here restates it.
    """
    if not isinstance(diff.overview, Overview):
        return "{}"
    payload: dict[str, Any] = {
        # The tools that read the pre-change tree (`read_file_at`,
        # `grep_at`) need this and had no way to learn it: measured over
        # one sweep, `read_file_at` errored on 31% of calls and `grep_at`
        # came back empty on 95%, because the model guessed `HEAD~1` — a
        # revision that cannot resolve in a depth-1 fetch. It rides here
        # rather than in the per-hunk block because it is constant for
        # the run, so it stays inside the cached prefix.
        "base_sha": diff.pr.base_sha,
        "summary": diff.overview.summary,
        "themes": list(diff.overview.themes),
        "callgraph_edges": [e.model_dump(by_alias=True) for e in diff.overview.callgraph_edges],
    }
    return json.dumps(payload, ensure_ascii=False)
