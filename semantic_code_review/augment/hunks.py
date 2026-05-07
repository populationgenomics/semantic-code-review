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

from ..augment.schemas import (
    AugmentedDiff, FilePatch, FoldDescription, Hunk, LineNote, Ref, Segment, Smell,
)
from ..cache.store import CacheStore
from ..viewer.rows import build_rows, compute_fold_regions
from .agents import Client, make_hunk_agent
from .prompts import HUNK_SYSTEM, PROMPT_VERSION
from .tools import TOOL_FUNCTIONS, RepoTools
from .trace_adapter import submit_args_from_result, write_pydantic_ai_trace


def format_hunk_prompt(fp: FilePatch, hunk: Hunk, overview_json: str, file_summary: str) -> list[dict[str, Any]]:
    """Assemble the user content blocks for one hunk call.

    Three blocks: overview (cached), file summary (cached), hunk-specific
    (not cached). The hunk-specific block also lists any indent fold
    regions that contain changed lines — the LLM is expected to return a
    one-line description per region.
    """
    rows = build_rows(hunk)
    regions = compute_fold_regions(rows)
    changed_regions = [
        r for r in regions
        if r.has_changes and r.new_start is not None and r.new_end is not None
    ]

    fold_block = ""
    if changed_regions:
        bullet_lines = [
            f"  +{r.new_start}..+{r.new_end}" for r in changed_regions
        ]
        fold_block = (
            "\n# Indent fold regions (post-image, contain changes)\n"
            + "\n".join(bullet_lines)
            + "\nReturn a one-liner for each in `fold_descriptions`."
        )

    hunk_text = (
        f"# File\npath: {fp.path}\n"
        f"lang: {fp.lang or ''}\n\n"
        f"# Hunk\n{hunk.header}\n{hunk.body}"
        f"{fold_block}"
    )
    return [
        {"type": "text", "text": f"# PR overview\n{overview_json}",
         "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": f"# File summary\n{file_summary}",
         "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": hunk_text},
    ]


async def run_hunk_pass(
    client: Client,
    *,
    fp: FilePatch,
    hunk: Hunk,
    overview_json: str,
    file_summary: str,
    repo_tools: RepoTools,
    model: str,
    cache: CacheStore | None = None,
    trace_dir: Path | None = None,
) -> dict[str, Any]:
    user_content = format_hunk_prompt(fp, hunk, overview_json, file_summary)

    trace_path = None
    if trace_dir is not None:
        safe_file = fp.path.replace("/", "_")
        safe_hunk = hunk.header.replace(" ", "_").replace("@", "").replace(",", "_").replace("+", "p").replace("-", "m")
        trace_path = trace_dir / f"hunk-{safe_file}-{safe_hunk[:40]}.json"

    if cache is not None:
        key = cache.key(
            "hunk",
            model,
            HUNK_SYSTEM,
            overview_json,
            file_summary,
            fp.path,
            hunk.header,
            hunk.body,
        )
        entry = cache.get(key)
        if entry is not None:
            if trace_path is not None:
                trace_path.parent.mkdir(parents=True, exist_ok=True)
                trace_path.write_text(
                    json.dumps(
                        {"cache_hit": True, "pass": "hunk", "response": entry.get("response")},
                        indent=2, ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
            return entry["response"]

    # Concatenate the three cache-segmented blocks into a single user
    # prompt — pydantic-ai's message format doesn't surface Anthropic
    # prompt-caching breakpoints. Provider-side caching is a v0.13
    # follow-up; correctness comes first.
    user_text = "\n\n".join(b["text"] for b in user_content)
    agent = make_hunk_agent(client.model)
    run_result = await agent.run(user_text, deps=repo_tools)
    submit_args = submit_args_from_result(run_result)
    if trace_path is not None:
        write_pydantic_ai_trace(
            run_result,
            trace_path=trace_path,
            model=str(client.model),
            system=HUNK_SYSTEM,
            tool_names=[fn.__name__ for fn in TOOL_FUNCTIONS],
            submit_tool="submit_annotations",
        )
    usage = run_result.usage()
    tokens_in = usage.input_tokens or 0
    tokens_out = usage.output_tokens or 0

    if cache is not None:
        cache.put(
            key,
            request={"file": fp.path, "header": hunk.header, "body_len": len(hunk.body)},
            response=submit_args,
            tokens_in=tokens_in, tokens_out=tokens_out,
        )
    return submit_args


def apply_hunk_annotations(hunk: Hunk, submit_args: dict[str, Any]) -> None:
    """Fold a submit_annotations payload into a Hunk in place.

    Drops segments that fall outside the hunk's post-image range or
    overlap a previously-kept segment — the LLM occasionally emits
    pre-image line numbers or off-by-a-few ranges.
    """
    hunk.intent = submit_args.get("intent", "") or ""
    hunk.context = submit_args.get("context", "") or ""
    hunk.confidence = submit_args.get("confidence")
    hunk.smells = [_smell(s) for s in submit_args.get("smells") or []]
    hunk.refs = [Ref(**_ref(r)) for r in submit_args.get("refs") or []]
    hunk.line_notes = [LineNote(**ln) for ln in submit_args.get("line_notes") or []
                       if _line_in_hunk(int(ln["line"]), hunk)]

    hunk.segments = []
    hunk_end = hunk.new_start + hunk.new_count - 1
    last_end = hunk.new_start - 1  # so a segment starting at new_start is allowed
    for seg in submit_args.get("segments") or []:
        try:
            start = int(seg["new_start"])
            count = int(seg["new_count"])
        except (KeyError, TypeError, ValueError):
            log.warning("hunk %s: malformed segment %r — dropped", hunk.header, seg)
            continue
        end = start + count - 1
        if count <= 0 or start < hunk.new_start or end > hunk_end:
            log.warning(
                "hunk %s: segment +%d..+%d outside range +%d..+%d — dropped",
                hunk.header, start, end, hunk.new_start, hunk_end,
            )
            continue
        if start <= last_end:
            log.warning(
                "hunk %s: segment +%d..+%d overlaps previous (ends +%d) — dropped",
                hunk.header, start, end, last_end,
            )
            continue
        hunk.segments.append(
            Segment(
                new_start=start, new_count=count,
                intent=seg.get("intent", "") or "",
                smells=[_smell(s) for s in seg.get("smells") or []],
                context=seg.get("context", "") or "",
                refs=[Ref(**_ref(r)) for r in seg.get("refs") or []],
            )
        )
        last_end = end

    hunk.fold_descriptions = []
    for fd in submit_args.get("fold_descriptions") or []:
        try:
            start = int(fd["new_start"])
            count = int(fd["new_count"])
        except (KeyError, TypeError, ValueError):
            log.warning("hunk %s: malformed fold_description %r — dropped", hunk.header, fd)
            continue
        end = start + count - 1
        if count <= 0 or start < hunk.new_start or end > hunk_end:
            log.warning(
                "hunk %s: fold +%d..+%d outside range — dropped",
                hunk.header, start, end,
            )
            continue
        summary = (fd.get("summary") or "").strip()
        if not summary:
            continue
        hunk.fold_descriptions.append(
            FoldDescription(new_start=start, new_count=count, summary=summary)
        )


def _line_in_hunk(line: int, hunk: Hunk) -> bool:
    return hunk.new_start <= line <= hunk.new_start + hunk.new_count - 1


def _smell(d: dict[str, Any]) -> Smell:
    return Smell(tag=d.get("tag", ""), note=d.get("note", "") or "")


def _ref(d: dict[str, Any]) -> dict[str, Any]:
    return {"path": d["path"], "line": int(d["line"]), "reason": d.get("reason", "") or ""}


def overview_to_prompt_json(diff: AugmentedDiff) -> str:
    """Serialize the overview into a compact JSON string for the hunk prompt."""
    if diff.overview is None:
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
