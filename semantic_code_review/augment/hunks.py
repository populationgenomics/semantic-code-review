"""Per-hunk pass: intent + segments + smells + context + refs.

Fused into a single call per hunk for v1. The system prompt frames the
job as comprehension-first; smells are secondary.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

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
from .agents import Client, make_hunk_agent
from .pass_ import PassMeta, run_pass
from .prompts import HUNK_SYSTEM
from .tools import TOOL_FUNCTIONS, RepoTools

_HUNK = PassMeta(
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
# AnthropicModelSettings keys are silently ignored by non-Anthropic
# backends (TypedDict total=False), so this is safe to apply
# unconditionally — Google + the CLI drivers see them as no-ops.
_HUNK_CACHE_SETTINGS: dict[str, Any] = {
    "anthropic_cache_instructions": True,       # system prompt block
    "anthropic_cache_tool_definitions": True,   # tools/<RepoTools>
}


def format_hunk_prompt(
    fp: AnnotatedFile,
    hunk: AnnotatedHunk,
    overview_json: str,
    file_summary: str,
) -> list[UserContent]:
    """Assemble the user-prompt blocks for one hunk call.

    Returns a `UserContent` list with `CachePoint` markers between the
    cacheable prefix sections (overview, file summary) and the
    per-hunk text. pydantic-ai's Anthropic adapter translates each
    `CachePoint` into a `cache_control: ephemeral` annotation on the
    preceding text block; non-supporting providers filter the markers
    out and concatenate the text blocks. Fold-region summaries are not
    produced here — the review server fires a focused call on first
    fold-close; see :mod:`semantic_code_review.augment.fold_summary`.
    """
    hunk_text = (
        f"# File\npath: {fp.path}\n"
        f"lang: {fp.ann.lang or ''}\n\n"
        f"# Hunk\n{hunk.parsed.header}\n{hunk.parsed.body}"
    )
    return [
        f"# PR overview\n{overview_json}",
        CachePoint(),
        f"# File summary\n{file_summary}",
        CachePoint(),
        hunk_text,
    ]


def _hunk_trace_path(
    trace_dir: Path | None, fp: AnnotatedFile, hunk: AnnotatedHunk,
) -> Path | None:
    if trace_dir is None:
        return None
    safe_file = fp.path.replace("/", "_")
    safe_hunk = (
        hunk.parsed.header
        .replace(" ", "_").replace("@", "").replace(",", "_")
        .replace("+", "p").replace("-", "m")
    )
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
    cache: CacheStore | None = None,
    trace_dir: Path | None = None,
) -> dict[str, Any]:
    payload = await run_pass(
        _HUNK,
        client=client,
        agent=make_hunk_agent(client.model),
        user_content=format_hunk_prompt(fp, hunk, overview_json, file_summary),
        system=HUNK_SYSTEM,
        model=model,
        cache_inputs=(
            overview_json, file_summary,
            fp.path, hunk.parsed.header, hunk.parsed.body,
        ),
        deps=repo_tools,
        model_settings=_HUNK_CACHE_SETTINGS,
        cache=cache,
        trace_path=_hunk_trace_path(trace_dir, fp, hunk),
        cache_request={
            "file": fp.path, "header": hunk.parsed.header,
            "body_len": len(hunk.parsed.body),
        },
    )
    assert payload is not None  # `_HUNK.swallow_errors` is false
    return payload


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
                parsed.header, start, end, parsed.new_start, hunk_end,
            )
            continue
        if start <= last_end:
            log.warning(
                "hunk %s: segment +%d..+%d overlaps previous (ends +%d) — dropped",
                parsed.header, start, end, last_end,
            )
            continue
        segments.append(
            Segment(
                new_start=start, new_count=count,
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
                parsed.header, start, end,
            )
            continue
        summary = (fd.get("summary") or "").strip()
        if not summary:
            continue
        fold_descriptions.append(
            FoldDescription(new_start=start, new_count=count, summary=summary)
        )

    line_notes = [
        LineNote(**ln) for ln in submit_args.get("line_notes") or []
        if _line_in_hunk(int(ln["line"]), parsed)
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
