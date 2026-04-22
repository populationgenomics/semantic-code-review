"""Per-hunk pass: intent + segments + smells + context + refs.

Fused into a single call per hunk for v1. The system prompt frames the
job as comprehension-first; smells are secondary.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..augment.schemas import (
    AugmentedDiff, FilePatch, Hunk, LineNote, Ref, Segment, Smell,
)
from ..cache.store import CacheStore
from .prompts import HUNK_SYSTEM, PROMPT_VERSION, hunk_tools
from .runner import ClaudeClient, run_agentic
from .tools import RepoTools


def format_hunk_prompt(fp: FilePatch, hunk: Hunk, overview_json: str, file_summary: str) -> list[dict[str, Any]]:
    """Assemble the user content blocks for one hunk call.

    Three blocks: overview (cached), file summary (cached), hunk-specific
    (not cached). Caching the first two maximises prompt-cache hit rate
    when hunks from the same file share these prefixes.
    """
    hunk_text = (
        f"# File\npath: {fp.path}\n"
        f"lang: {fp.lang or ''}\n\n"
        f"# Hunk\n{hunk.header}\n{hunk.body}"
    )
    return [
        {"type": "text", "text": f"# PR overview\n{overview_json}",
         "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": f"# File summary\n{file_summary}",
         "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": hunk_text},
    ]


async def run_hunk_pass(
    client: ClaudeClient,
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

    result = await run_agentic(
        client,
        model=model,
        system=HUNK_SYSTEM,
        user_content=user_content,
        tools=hunk_tools(),
        submit_tool_name="submit_annotations",
        repo_tools=repo_tools,
        trace_path=trace_path,
    )

    if cache is not None:
        cache.put(
            key,
            request={"file": fp.path, "header": hunk.header, "body_len": len(hunk.body)},
            response=result.submit_args,
            tokens_in=result.input_tokens, tokens_out=result.output_tokens,
        )
    return result.submit_args


def apply_hunk_annotations(hunk: Hunk, submit_args: dict[str, Any]) -> None:
    """Fold a submit_annotations payload into a Hunk in place."""
    hunk.intent = submit_args.get("intent", "") or ""
    hunk.context = submit_args.get("context", "") or ""
    hunk.confidence = submit_args.get("confidence")
    hunk.smells = [_smell(s) for s in submit_args.get("smells") or []]
    hunk.refs = [Ref(**_ref(r)) for r in submit_args.get("refs") or []]
    hunk.line_notes = [LineNote(**ln) for ln in submit_args.get("line_notes") or []]
    hunk.segments = []
    for seg in submit_args.get("segments") or []:
        hunk.segments.append(
            Segment(
                new_start=int(seg["new_start"]),
                new_count=int(seg["new_count"]),
                intent=seg.get("intent", "") or "",
                smells=[_smell(s) for s in seg.get("smells") or []],
                context=seg.get("context", "") or "",
                refs=[Ref(**_ref(r)) for r in seg.get("refs") or []],
            )
        )


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
