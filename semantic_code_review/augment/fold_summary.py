"""On-demand fold-region summariser.

The per-hunk LLM pass used to ask the model for a one-liner per indent
fold region. Most folds are never collapsed in a review, so we now
defer: the review server fires this code path the first time the
reviewer closes a region, the result is cached, and the augmented
sidecar is updated so subsequent loads are free.

The summariser is structurally similar to `run_hunk_pass` but cheaper:
- No repo tools — the region body is self-contained.
- Tiny structured output (just a `summary` field).
- Cache key includes the region range so different regions of the same
  hunk don't collide.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.output import ToolOutput

from ..cache.store import CacheStore
from ..viewer.hunk_layout import build_rows, compute_fold_regions
from .agents import Client
from .schemas import AnnotatedFile, AnnotatedHunk
from .trace_adapter import (
    submit_args_from_result, write_partial_trace, write_pydantic_ai_trace,
)


log = logging.getLogger(__name__)


FOLD_SYSTEM = (
    "You are describing one COLLAPSED fold region inside a diff hunk. A reviewer "
    "is looking at the hunk with this region's body hidden; your sentence is the "
    "ONLY thing they see in place of it.\n\n"
    "Rules:\n"
    "- One sentence. <= 25 words.\n"
    "- Present tense. Lowercase. No trailing period optional.\n"
    "- Describe EFFECT, not structure or control flow.\n"
    "- If the folded block REPLACES existing code, describe the CHANGE the block "
    "  introduces, not just the new steps.\n"
    "- Tone: explanatory, not evaluative.\n\n"
    "Good: 'convert every top-level message in every descriptor to a json schema'.\n"
    "Good: 'fall back to inline generation when include_all is set'.\n"
    "Good: 'forward page/size kwargs to list_users so pagination reaches the handler'.\n"
    "Bad: 'iterate every file descriptor in the set' (structure, not effect).\n"
    "Bad: 'if / elif / else' (control flow).\n"
    "Bad: 'call self._convert_message_to_schema' (names a call, not what is achieved)."
)


class FoldSummarySubmission(BaseModel):
    """Wire format for the `submit_fold_summary` tool. Single field, kept
    structured so trace + cache plumbing matches the other passes."""

    summary: str = Field(description="One sentence describing the folded region's effect.")


def make_fold_summary_agent(model: str | Model) -> Agent[None, FoldSummarySubmission]:
    return Agent(
        model=model,
        output_type=ToolOutput(FoldSummarySubmission, name="submit_fold_summary"),
        instructions=FOLD_SYSTEM,
    )


def extract_region_body(hunk: AnnotatedHunk, new_start: int, new_count: int) -> str:
    """Return the raw +/-/space-prefixed lines that fall inside the
    requested post-image range, joined by newlines. Falls back to the
    full hunk body when the range doesn't match a computed region.
    """
    rows = build_rows(hunk.parsed)
    regions = compute_fold_regions(rows)
    new_end = new_start + new_count - 1
    matched = next(
        (
            r for r in regions
            if r.new_start == new_start and r.new_end == new_end
        ),
        None,
    )
    if matched is None:
        # Unknown region — return the whole hunk body so the model has
        # something coherent to summarise. The caller already knows the
        # range was suspicious; this keeps it useful as a fallback.
        return hunk.parsed.body
    lines = []
    for row in rows[matched.header_idx : matched.body_end_idx + 1]:
        if row.kind == "ins":
            lines.append("+" + row.new_text)
        elif row.kind == "del":
            lines.append("-" + row.old_text)
        elif row.kind == "pair":
            lines.append("-" + row.old_text)
            lines.append("+" + row.new_text)
        else:  # ctx
            lines.append(" " + row.new_text)
    return "\n".join(lines)


def _format_fold_prompt(
    *,
    overview_json: str,
    fp: AnnotatedFile,
    hunk: AnnotatedHunk,
    new_start: int,
    new_count: int,
) -> list[dict[str, Any]]:
    region_body = extract_region_body(hunk, new_start, new_count)
    file_summary = (fp.ann.summary or "").strip()
    region_text = (
        f"# File\npath: {fp.path}\nlang: {fp.ann.lang or ''}\n\n"
        f"# Hunk\n{hunk.parsed.header}\n\n"
        f"# Folded region (post-image lines +{new_start}..+{new_start + new_count - 1})\n"
        f"{region_body}\n\n"
        "Summarise the folded region."
    )
    return [
        {"type": "text", "text": f"# PR overview\n{overview_json}",
         "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": f"# File summary\n{file_summary}",
         "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": region_text},
    ]


async def summarise_fold(
    client: Client,
    *,
    fp: AnnotatedFile,
    hunk: AnnotatedHunk,
    overview_json: str,
    new_start: int,
    new_count: int,
    model: str,
    cache: CacheStore | None = None,
    trace_dir: Path | None = None,
) -> str:
    """Return a one-sentence summary for the fold region at (new_start, new_count).

    Cached by (file path, hunk body, region). Trace file (if `trace_dir`
    is given) lands at `trace_dir/fold-<file>-<hunk>-<range>.json` so
    failures are diagnosable alongside the per-hunk traces.
    """
    user_content = _format_fold_prompt(
        overview_json=overview_json, fp=fp, hunk=hunk,
        new_start=new_start, new_count=new_count,
    )
    trace_path = _trace_path(trace_dir, fp.path, hunk, new_start, new_count)

    if cache is not None:
        key = cache.key(
            "fold-summary",
            model,
            FOLD_SYSTEM,
            overview_json,
            fp.path,
            hunk.parsed.header,
            hunk.parsed.body,
            str(new_start),
            str(new_count),
        )
        entry = cache.get(key)
        if entry is not None:
            if trace_path is not None:
                trace_path.parent.mkdir(parents=True, exist_ok=True)
                trace_path.write_text(
                    json.dumps(
                        {"cache_hit": True, "pass": "fold-summary",
                         "response": entry.get("response")},
                        indent=2, ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
            return str(entry["response"].get("summary", "")).strip()

    user_text = "\n\n".join(b["text"] for b in user_content)
    agent = make_fold_summary_agent(client.model)
    async with agent.iter(user_text) as agent_run:
        try:
            async for _ in agent_run:
                pass
        except BaseException as exc:
            if trace_path is not None:
                write_partial_trace(
                    list(agent_run.all_messages()),
                    trace_path=trace_path,
                    model=str(client.model),
                    system=FOLD_SYSTEM,
                    tool_names=[],
                    submit_tool="submit_fold_summary",
                    error=exc,
                )
            raise
        run_result = agent_run.result
    submit_args = submit_args_from_result(run_result)
    if trace_path is not None:
        write_pydantic_ai_trace(
            run_result,
            trace_path=trace_path,
            model=str(client.model),
            system=FOLD_SYSTEM,
            tool_names=[],
            submit_tool="submit_fold_summary",
        )
    usage = run_result.usage()
    tokens_in = usage.input_tokens or 0
    tokens_out = usage.output_tokens or 0
    if cache is not None:
        cache.put(
            key,
            request={
                "file": fp.path, "header": hunk.parsed.header,
                "new_start": new_start, "new_count": new_count,
            },
            response=submit_args,
            tokens_in=tokens_in, tokens_out=tokens_out,
        )
    return str(submit_args.get("summary", "")).strip()


def _trace_path(
    trace_dir: Path | None, file_path: str, hunk: AnnotatedHunk,
    new_start: int, new_count: int,
) -> Path | None:
    if trace_dir is None:
        return None
    safe_file = file_path.replace("/", "_")
    safe_hunk = (
        hunk.parsed.header.replace(" ", "_").replace("@", "")
        .replace(",", "_").replace("+", "p").replace("-", "m")
    )
    return trace_dir / f"fold-{safe_file}-{safe_hunk[:40]}-p{new_start}_{new_count}.json"
