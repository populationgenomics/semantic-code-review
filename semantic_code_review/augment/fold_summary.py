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
from pydantic_ai import Agent, CachePoint
from pydantic_ai.messages import UserContent
from pydantic_ai.models import Model
from pydantic_ai.output import ToolOutput

from ..cache.store import CacheStore
from .agents import Client
from .schemas import AnnotatedDiff, FoldDescription
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


# Anthropic prompt-caching settings applied to every fold-summary call.
# The cacheable prefix is the system prompt + the overview JSON + the
# file summary; folds within the same file (and across files within the
# PR) reuse it. Settings are no-ops on non-Anthropic backends.
_FOLD_CACHE_SETTINGS: dict[str, Any] = {
    "anthropic_cache_instructions": True,
}


def _format_fold_prompt(
    *,
    overview_json: str,
    file_path: str,
    file_summary: str,
    context: FoldContext,
    body: str,
    right_range: tuple[int, int] | None,
    left_range: tuple[int, int] | None,
) -> list[UserContent]:
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
    # CachePoint markers between sections so Anthropic caches the
    # overview prefix (cross-file) and the overview+file_summary
    # prefix (within-file) across fold-summary calls. Other providers
    # silently filter the markers out.
    return [
        f"# PR overview\n{overview_json}",
        CachePoint(),
        f"# File summary\n{file_summary}",
        CachePoint(),
        region_text,
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

    agent = make_fold_summary_agent(client.model)
    async with agent.iter(
        user_content,
        model_settings=_FOLD_CACHE_SETTINGS,
    ) as agent_run:
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


class FoldSummaryNotReady(RuntimeError):
    """The run dir doesn't yet hold an `augmented.scr.json`.

    Maps to HTTP 409 at the review-server boundary — the augmentation
    pass is still in flight or was skipped entirely.
    """


class FoldSummaryFileIndexError(LookupError):
    """`file_idx` from the request doesn't address a file in the diff.

    Maps to HTTP 404 at the review-server boundary.
    """


async def apply_fold_summary_to_run(
    client: Client,
    *,
    run_dir: Path,
    file_idx: int,
    context: FoldContext,
    right_range: tuple[int, int] | None,
    left_range: tuple[int, int] | None,
    model: str,
    cache: CacheStore | None = None,
    trace_dir: Path | None = None,
) -> dict[str, Any]:
    """End-to-end fold-summary: resolve, call LLM, persist, return payload.

    Loads the sidecar, looks up `file_idx`, calls :func:`summarise_fold`
    against the resolved file, writes the new `FoldDescription` back to
    `augmented.scr.json` + `augmented.diff`, and returns the broadcast
    payload (the dict the review server fans out as an SSE event and
    sends back to the requesting tab).

    Raises :class:`FoldSummaryNotReady` if the sidecar isn't on disk
    and :class:`FoldSummaryFileIndexError` if `file_idx` is out of
    range. Other exceptions (LLM failures, write errors) propagate.
    """
    sidecar = run_dir / "augmented.scr.json"
    if not sidecar.exists():
        raise FoldSummaryNotReady(
            "augmented.scr.json missing — augment not complete"
        )

    # Lazy: keeps the augment-side format machinery off the import
    # path for callers that only want :func:`summarise_fold`.
    from ..format.emit import emit_augmented_diff
    from ..format.sidecar import dump_sidecar, load_sidecar
    from .hunks import overview_to_prompt_json

    diff = load_sidecar(sidecar)
    if not (0 <= file_idx < len(diff.files)):
        raise FoldSummaryFileIndexError(
            f"file_idx {file_idx} not in diff"
        )

    fp = diff.files[file_idx]
    summary = await summarise_fold(
        client,
        run_dir=run_dir,
        file_path=fp.path,
        file_summary=(fp.ann.summary or "").strip(),
        overview_json=overview_to_prompt_json(diff),
        context=context,
        right_range=right_range,
        left_range=left_range,
        model=model,
        cache=cache,
        trace_dir=trace_dir,
    )

    rs, re_ = right_range or (0, 0)
    ls, le = left_range or (0, 0)

    # Persist iff there's a hunk to stash the description on; see the
    # comment on _attach_fold_summary for why the file's first hunk is
    # the chosen home.
    if fp.hunks:
        updated_diff = _attach_fold_summary(
            diff, file_idx=file_idx, context=context,
            right=(rs, re_), left=(ls, le), summary=summary,
        )
        dump_sidecar(updated_diff, sidecar)
        (run_dir / "augmented.diff").write_text(
            emit_augmented_diff(updated_diff), encoding="utf-8",
        )

    return {
        "file_idx": file_idx, "context": context,
        "right_start": rs, "right_end": re_,
        "left_start": ls, "left_end": le,
        "summary": summary,
    }


def _attach_fold_summary(
    diff: AnnotatedDiff,
    *,
    file_idx: int,
    context: FoldContext,
    right: tuple[int, int],
    left: tuple[int, int],
    summary: str,
) -> AnnotatedDiff:
    """Return `diff` with the matching `FoldDescription` replaced or
    appended on the addressed file's first hunk's annotations.

    Fold descriptions live at the hunk level for legacy reasons; for
    v2 (file-level) addressing they describe content addressed at the
    *file* level, so we stash them on the file's first hunk (chosen as
    a stable home) until the schema migrates `fold_descriptions` up to
    `AnnotatedFile`.
    """
    fp = diff.files[file_idx]
    rs, re_ = right
    ls, le = left
    hunk = fp.hunks[0]
    new_folds = [
        fd for fd in hunk.ann.fold_descriptions
        if not (
            fd.context == context
            and fd.right_start == rs and fd.right_end == re_
            and fd.left_start == ls and fd.left_end == le
        )
    ]
    new_folds.append(FoldDescription(
        context=context,
        right_start=rs, right_end=re_,
        left_start=ls, left_end=le,
        summary=summary,
    ))
    updated_ann = hunk.ann.model_copy(update={"fold_descriptions": new_folds})
    updated_hunk = hunk.model_copy(update={"ann": updated_ann})
    updated_hunks = list(fp.hunks)
    updated_hunks[0] = updated_hunk
    updated_file = fp.model_copy(update={"hunks": updated_hunks})
    updated_files = list(diff.files)
    updated_files[file_idx] = updated_file
    return diff.model_copy(update={"files": updated_files})


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
