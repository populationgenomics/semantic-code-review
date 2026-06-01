"""On-demand fold-region summariser.

The per-hunk LLM pass used to ask the model for a one-liner per indent
fold region. Most folds are never collapsed in a review, so we now
defer: the review server fires this code path the first time the
reviewer closes a region, the result is cached, and the augmented
sidecar is updated so subsequent loads are free.

Address space (slice 1 of "fold anywhere"): a fold region is identified
by a file path + a `context` (right / left / both) + 1-indexed line
ranges into the named worktree file. Pure-context folds use right;
deletion-only folds use left; folds that straddle changed content use
both. The server reads the actual line content from the head/ and/or
base/ worktrees and passes a prepared body string to this module —
the summariser stays narrow and focused on the LLM call + caching.
"""

from __future__ import annotations

import difflib
import json
import logging
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.output import ToolOutput

from ..cache.store import CacheStore
from .agents import Client
from .trace_adapter import (
    submit_args_from_result, write_partial_trace, write_pydantic_ai_trace,
)


log = logging.getLogger(__name__)


FoldContext = Literal["right", "left", "both"]


FOLD_SYSTEM = (
    "You are describing one COLLAPSED fold region for a reviewer who has "
    "the body hidden; your sentence is the ONLY thing they see in place of it.\n\n"
    "The user prompt declares which kind of region you're looking at:\n"
    "  - `right`: the lines exist post-change. Describe what the code DOES.\n"
    "  - `left`:  the lines are being REMOVED. Describe what they did,\n"
    "             phrased so the reviewer understands what was lost.\n"
    "  - `both`:  the fold straddles changed content; you'll see a diff.\n"
    "             Describe the CHANGE the fold introduces.\n\n"
    "Rules:\n"
    "- One sentence. <= 25 words.\n"
    "- Present tense. Lowercase. No trailing period.\n"
    "- Describe EFFECT, not structure or control flow.\n"
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


def extract_fold_body(
    run_dir: Path,
    file_path: str,
    context: FoldContext,
    right_range: tuple[int, int] | None,
    left_range: tuple[int, int] | None,
) -> str:
    """Read the actual line content for a fold region.

    `right_range` / `left_range` are 1-indexed inclusive `(start, end)`.
    For `right` / `left` returns the plain lines joined with newlines.
    For `both` returns a unified-diff-style body so the LLM can see
    what changed.
    """
    head_path = run_dir / "head" / file_path
    base_path = run_dir / "base" / file_path

    if context == "right":
        if right_range is None:
            return ""
        return "\n".join(_slice_lines(head_path, *right_range))
    if context == "left":
        if left_range is None:
            return ""
        return "\n".join(_slice_lines(base_path, *left_range))
    # both — unified diff between the corresponding slices.
    right_lines = _slice_lines(head_path, *right_range) if right_range else []
    left_lines = _slice_lines(base_path, *left_range) if left_range else []
    diff = difflib.unified_diff(
        left_lines, right_lines,
        fromfile=f"base/{file_path}", tofile=f"head/{file_path}",
        lineterm="",
    )
    return "\n".join(diff)


def _slice_lines(path: Path, start: int, end: int) -> list[str]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    # 1-indexed inclusive → Python slice.
    return lines[max(0, start - 1) : end]


def _format_fold_prompt(
    *,
    overview_json: str,
    file_path: str,
    file_summary: str,
    context: FoldContext,
    body: str,
    right_range: tuple[int, int] | None,
    left_range: tuple[int, int] | None,
) -> list[dict[str, Any]]:
    if context == "right":
        rs, re_ = right_range or (0, 0)
        region_label = f"post-image lines head/{file_path}:{rs}..{re_}"
    elif context == "left":
        ls, le = left_range or (0, 0)
        region_label = f"pre-image (deleted) lines base/{file_path}:{ls}..{le}"
    else:
        rs, re_ = right_range or (0, 0)
        ls, le = left_range or (0, 0)
        region_label = (
            f"both sides — head/{file_path}:{rs}..{re_} vs base/{file_path}:{ls}..{le}"
        )
    region_text = (
        f"# File\npath: {file_path}\n\n"
        f"# Folded region — context: {context}; {region_label}\n"
        f"{body}\n\n"
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
    run_dir: Path,
    file_path: str,
    file_summary: str,
    overview_json: str,
    context: FoldContext,
    right_range: tuple[int, int] | None,
    left_range: tuple[int, int] | None,
    model: str,
    cache: CacheStore | None = None,
    trace_dir: Path | None = None,
) -> str:
    """Return a one-sentence summary for a fold region.

    Cached by `(file path, ranges, context, body content hash)`. Trace
    file (if `trace_dir` is given) lands at
    `trace_dir/fold-<file>-<context><range>.json` so failures are
    diagnosable alongside the per-hunk traces.
    """
    body = extract_fold_body(
        run_dir, file_path, context, right_range, left_range,
    )
    user_content = _format_fold_prompt(
        overview_json=overview_json, file_path=file_path,
        file_summary=file_summary, context=context, body=body,
        right_range=right_range, left_range=left_range,
    )
    trace_path = _trace_path(
        trace_dir, file_path, context, right_range, left_range,
    )

    if cache is not None:
        key = cache.key(
            "fold-summary-v2",
            model,
            FOLD_SYSTEM,
            overview_json,
            file_summary,
            file_path,
            context,
            str(right_range or ""),
            str(left_range or ""),
            # Include the body so a re-run after the file content changed
            # is invalidated even when ranges happen to line up.
            body,
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
                "file": file_path, "context": context,
                "right_range": right_range, "left_range": left_range,
            },
            response=submit_args,
            tokens_in=tokens_in, tokens_out=tokens_out,
        )
    return str(submit_args.get("summary", "")).strip()


def _trace_path(
    trace_dir: Path | None, file_path: str, context: FoldContext,
    right_range: tuple[int, int] | None, left_range: tuple[int, int] | None,
) -> Path | None:
    if trace_dir is None:
        return None
    safe_file = file_path.replace("/", "_")
    if context == "right":
        rs, re_ = right_range or (0, 0)
        tag = f"r{rs}_{re_}"
    elif context == "left":
        ls, le = left_range or (0, 0)
        tag = f"l{ls}_{le}"
    else:
        rs, re_ = right_range or (0, 0)
        ls, le = left_range or (0, 0)
        tag = f"b_r{rs}_{re_}_l{ls}_{le}"
    return trace_dir / f"fold-{safe_file}-{tag}.json"
