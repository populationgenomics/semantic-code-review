"""Additional whole-PR review pass driven by a user-supplied prompt.

The main per-hunk pass is comprehension-first — it answers "what does
this change do?" and only secondarily flags concerns. Teams often
want an *additional* pass with a different brief: bug-hunting,
security review, style checks, schema-migration audits. Folding
those instructions into HUNK_SYSTEM would dilute the comprehension
role and force every observation to fit the closed `SMELL_TAGS`
vocabulary, and a per-hunk view fundamentally can't see cross-file
concerns like "added a new persistence format with no schema version"
or "added logic but no tests".

So the extra pass runs *once per PR*, after the overview + per-hunk
passes have completed. The model sees the user's prompt as the
system message, the PR overview JSON, and the raw unified diff. It
returns a flat list of ``(file, line, body)`` line-anchored notes;
the pipeline buckets each one into the matching hunk's ``line_notes``
so the viewer renders them alongside main-pass annotations. The
reviewer can promote any of them to a PR comment via the existing
"Add as comment" affordance.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from pydantic_ai import Agent, CachePoint
from pydantic_ai.messages import UserContent
from pydantic_ai.models import Model
from pydantic_ai.output import ToolOutput

from ..cache.store import CacheStore
from ..format import linenos
from .agents import Client
from .pass_ import PassMeta, run_pass
from .schemas import AnnotatedDiff, LineNote

log = logging.getLogger(__name__)


# Two breakpoints — instructions (the user prompt) and after the
# overview JSON — so a re-run of the same PR with the same prompt
# but a different diff still reads the prefix from cache.
_EXTRA_CACHE_SETTINGS: dict[str, Any] = {
    "anthropic_cache_instructions": True,
}


# Best-effort pass: a failure here returns the original diff unchanged
# rather than poisoning the main-pass output. `swallow_errors=True`
# makes `run_pass` log + return None on exception; the caller then
# short-circuits to the unmodified diff.
_EXTRA_REVIEW = PassMeta(
    name="extra-review-pr",
    submit_tool="submit_extra_notes",
    swallow_errors=True,
)


class ExtraReviewNote(BaseModel):
    """One line-anchored observation produced by the PR-level extra-review pass."""

    file: str = Field(description="Repository-relative path of the file the note applies to.")
    line: int = Field(description="Post-image (new-side) line number this note applies to.")
    body: str = Field(
        description=(
            "The reviewer-facing note. Plain text or GitHub-flavored Markdown — "
            "the body becomes the PR comment verbatim if the reviewer promotes it."
        ),
    )


class ExtraReviewSubmission(BaseModel):
    notes: list[ExtraReviewNote] = Field(
        default_factory=list,
        description=(
            "Line-anchored observations across the whole diff. Each note's "
            "(file, line) must point at a post-image line that exists inside "
            "one of the diff's hunks. Emit only what the prompt actually "
            "finds; an empty list is fine."
        ),
    )


def make_extra_review_agent(
    model: str | Model,
    system_prompt: str,
) -> Agent[None, ExtraReviewSubmission]:
    """Agent for the PR-level extra-review pass.

    The user-supplied prompt becomes the system message. Output is
    constrained via ``ToolOutput(ExtraReviewSubmission, name='submit_extra_notes')``;
    no repo tools are registered — the call works from the same
    pre-shaped context (overview + raw diff text) that the rest of
    the pipeline assembles.
    """
    return Agent(
        model=model,
        output_type=ToolOutput(ExtraReviewSubmission, name="submit_extra_notes"),
        instructions=system_prompt,
    )


def _format_pr_level_prompt(
    *,
    overview_json: str,
    diff_text: str,
) -> list[UserContent]:
    # Post-image line numbers are prepended per body line so the model
    # copies a coordinate rather than counting `+` lines (which drifts).
    numbered = linenos.number_for_prompt(diff_text)
    return [
        f"# PR overview\n{overview_json}",
        CachePoint(),
        (
            "# Diff\nEach body line is prefixed with its post-image (new-side) "
            "line number; deleted lines have a blank number column. Use those "
            "numbers for each note's `line`.\n\n"
            f"{numbered}"
        ),
    ]


def _distribute_notes_to_hunks(
    diff: AnnotatedDiff,
    notes: list[ExtraReviewNote],
) -> AnnotatedDiff:
    """Bucket each ``(file, line)`` note into the matching hunk's
    ``line_notes``. Notes whose ``file`` doesn't match any AnnotatedFile,
    or whose ``line`` falls outside every hunk's post-image range, get
    dropped with a warning — the model isn't trusted to stay in bounds.
    Returns a new AnnotatedDiff; the input is not mutated.
    """
    by_path = {fp.path: fp for fp in diff.files}
    list(diff.files)
    # Per-hunk new_notes accumulators keyed by (file_idx, hunk_idx).
    appends: dict[tuple[int, int], list[LineNote]] = {}

    for n in notes:
        body = (n.body or "").strip()
        if not body:
            continue
        fp = by_path.get(n.file)
        if fp is None:
            log.warning(
                "extra-review note for unknown path %r — dropped (body=%r)",
                n.file,
                body[:80],
            )
            continue
        fi = diff.files.index(fp)
        landed = False
        for hi, hunk in enumerate(fp.hunks):
            start = hunk.parsed.new_start
            end = start + hunk.parsed.new_count - 1
            if start <= n.line <= end:
                appends.setdefault((fi, hi), []).append(
                    LineNote(line=n.line, body=body),
                )
                landed = True
                break
        if not landed:
            log.warning(
                "extra-review note %s:%d outside any hunk's range — dropped (body=%r)",
                n.file,
                n.line,
                body[:80],
            )

    if not appends:
        return diff
    # Rebuild only the files that gained notes.
    new_files = list(diff.files)
    files_touched: set[int] = {fi for fi, _ in appends}
    for fi in files_touched:
        fp = new_files[fi]
        new_hunks = list(fp.hunks)
        for (afi, hi), to_add in appends.items():
            if afi != fi:
                continue
            h = new_hunks[hi]
            new_ann = h.ann.model_copy(
                update={"line_notes": list(h.ann.line_notes) + to_add},
            )
            new_hunks[hi] = h.model_copy(update={"ann": new_ann})
        new_files[fi] = fp.model_copy(update={"hunks": new_hunks})
    return diff.model_copy(update={"files": new_files})


async def run_pr_level_extra_review(
    client: Client,
    *,
    diff: AnnotatedDiff,
    overview_json: str,
    diff_text: str,
    prompt_text: str,
    model: str,
    cache: CacheStore | None = None,
    trace_dir: Path | None = None,
) -> AnnotatedDiff:
    """Run the PR-level extra-review call and return a copy of ``diff``
    with the resulting notes folded into the matching hunks'
    ``line_notes``.

    Best-effort: any failure (model error, schema mismatch after
    retries, etc.) leaves ``diff`` unchanged and logs a warning. The
    extra pass is an add-on; the main-pass output is load-bearing
    and must not be poisoned by an extras hiccup.
    """
    payload = await run_pass(
        _EXTRA_REVIEW,
        client=client,
        agent=make_extra_review_agent(client.model, system_prompt=prompt_text),
        user_content=_format_pr_level_prompt(
            overview_json=overview_json,
            diff_text=diff_text,
        ),
        # `prompt_text` is the user-supplied system prompt — it varies
        # per call, so it lives on the run_pass arg surface rather than
        # on PassMeta. It also participates in the cache key (via the
        # `system` parameter) so re-runs with a different prompt miss.
        system=prompt_text,
        model=model,
        cache_inputs=(overview_json, diff_text),
        model_settings=_EXTRA_CACHE_SETTINGS,
        cache=cache,
        trace_path=(trace_dir / "extra-review.json") if trace_dir is not None else None,
        cache_request={"diff_len": len(diff_text)},
    )
    if payload is None:
        return diff

    notes = [ExtraReviewNote.model_validate(n) for n in payload.get("notes") or []]
    log.info("extra-review: %d notes emitted across %d files", len(notes), len({n.file for n in notes}))
    return _distribute_notes_to_hunks(diff, notes)


__all__ = [
    "ExtraReviewNote",
    "ExtraReviewSubmission",
    "make_extra_review_agent",
    "run_pr_level_extra_review",
]
