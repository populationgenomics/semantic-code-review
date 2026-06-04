"""Additional per-hunk review pass driven by a user-supplied prompt.

The main per-hunk pass is comprehension-first — it answers "what does
this change do?" and only secondarily flags concerns. Teams often want
an *additional* pass with a different brief: "look for bugs, security
issues, X, Y, Z" — the kind of thing the standard Claude Code review
prompt does. Rather than fold those instructions into HUNK_SYSTEM
(where they'd dilute the comprehension role and force every smell to
fit the closed `SMELL_TAGS` vocabulary), the extra pass runs alongside
with the user's prompt as its system message and a small, freeform
output schema.

The output is a list of line-anchored notes that merge into the
hunk's ``line_notes`` field. They render exactly like main-pass
line_notes in the viewer; the reviewer can promote any of them to a
PR comment via the existing "Add as comment" affordance.

Cost: one extra LLM call per hunk. The cacheable prefix (user prompt
+ overview + file_summary) is wired up identically to the main pass,
so within a single PR pass each subsequent hunk reads the prefix
from cache.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from pydantic_ai import Agent, CachePoint
from pydantic_ai.messages import UserContent
from pydantic_ai.models import Model
from pydantic_ai.output import ToolOutput

from ..cache.store import CacheStore
from .agents import Client
from .schemas import AnnotatedFile, AnnotatedHunk, LineNote

log = logging.getLogger(__name__)


_EXTRA_CACHE_SETTINGS: dict[str, Any] = {
    "anthropic_cache_instructions": True,
}


class ExtraReviewNote(BaseModel):
    """One line-anchored observation produced by the extra-review pass."""
    line: int = Field(description="Post-image (new-side) line number this note applies to.")
    body: str = Field(description="The reviewer-facing note. Plain text, one or two sentences.")


class ExtraReviewSubmission(BaseModel):
    notes: list[ExtraReviewNote] = Field(
        default_factory=list,
        description=(
            "Line-anchored observations. Each note's line must fall inside the "
            "hunk's post-image range. Emit only what the prompt actually finds; "
            "an empty list is fine."
        ),
    )


def make_extra_review_agent(
    model: str | Model, system_prompt: str,
) -> Agent[None, ExtraReviewSubmission]:
    """Agent for the per-hunk extra-review pass.

    The user-supplied prompt becomes the system message. Output is
    constrained via ``ToolOutput(ExtraReviewSubmission, name='submit_extra_notes')``;
    no repo tools are registered — the extra pass works from the same
    pre-shaped context (overview + file_summary + hunk text) the main
    pass uses.
    """
    return Agent(
        model=model,
        output_type=ToolOutput(ExtraReviewSubmission, name="submit_extra_notes"),
        instructions=system_prompt,
    )


def _format_extra_prompt(
    fp: AnnotatedFile,
    hunk: AnnotatedHunk,
    overview_json: str,
    file_summary: str,
) -> list[UserContent]:
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


def _filter_to_hunk_range(
    notes: list[ExtraReviewNote], hunk: AnnotatedHunk,
) -> list[LineNote]:
    """Drop notes whose line falls outside the hunk's post-image range.

    The main-pass build step does the same kind of clamp on segments
    and fold_descriptions; the extra pass gets identical treatment so
    a stray line number can't anchor an annotation in a row that
    doesn't belong to this hunk.
    """
    start = hunk.parsed.new_start
    end = start + hunk.parsed.new_count - 1
    out: list[LineNote] = []
    for n in notes:
        if n.line < start or n.line > end:
            log.warning(
                "extra-review note line %d outside hunk %s (+%d..+%d) — dropped",
                n.line, hunk.parsed.header, start, end,
            )
            continue
        body = (n.body or "").strip()
        if not body:
            continue
        out.append(LineNote(line=n.line, body=body))
    return out


async def run_extra_review_pass(
    client: Client,
    *,
    fp: AnnotatedFile,
    hunk: AnnotatedHunk,
    overview_json: str,
    file_summary: str,
    prompt_text: str,
    model: str,
    cache: CacheStore | None = None,
    trace_dir: Path | None = None,
) -> list[LineNote]:
    """Run the extra review pass for one hunk; return validated LineNotes.

    Best-effort: on any failure (model error, schema mismatch left
    after pydantic-ai's retries, etc.) the caller's main-pass output
    stays intact — the extra notes are an add-on, not load-bearing.
    Logs the failure at warning level so it surfaces in --verbose runs.
    """
    if cache is not None:
        key = cache.key(
            "extra-review",
            model,
            prompt_text,
            overview_json,
            file_summary,
            fp.path,
            hunk.parsed.header,
            hunk.parsed.body,
        )
        entry = cache.get(key)
        if entry is not None:
            notes = [ExtraReviewNote.model_validate(n) for n in entry["response"]["notes"]]
            return _filter_to_hunk_range(notes, hunk)

    user_content = _format_extra_prompt(fp, hunk, overview_json, file_summary)
    agent = make_extra_review_agent(client.model, system_prompt=prompt_text)
    async with agent.iter(
        user_content, model_settings=_EXTRA_CACHE_SETTINGS,
    ) as agent_run:
        try:
            async for _ in agent_run:
                pass
        except BaseException as exc:  # noqa: BLE001
            log.warning(
                "extra-review pass failed on %s @ %s: %s: %s",
                fp.path, hunk.parsed.header, type(exc).__name__, exc,
            )
            return []
        result = agent_run.result

    if result is None or result.output is None:
        return []
    submission: ExtraReviewSubmission = result.output
    notes = list(submission.notes)

    if cache is not None:
        usage = result.usage()
        cache.put(
            key,
            request={"file": fp.path, "header": hunk.parsed.header},
            response={"notes": [n.model_dump() for n in notes]},
            tokens_in=usage.input_tokens or 0,
            tokens_out=usage.output_tokens or 0,
        )

    return _filter_to_hunk_range(notes, hunk)


__all__ = [
    "ExtraReviewNote",
    "ExtraReviewSubmission",
    "make_extra_review_agent",
    "run_extra_review_pass",
]
