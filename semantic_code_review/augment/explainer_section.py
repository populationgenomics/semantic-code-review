"""Per-section prose for the change explainer (ADR 0007, slices 2-3).

The skeleton (`explainer.py`) writes the Map and leaves Background,
Intuition and Code `pending`. Each of those is filled by one structured
call from `POST /explainer/section/{id}`, on first open — a section
nobody opens is never paid for.

The passes differ in what they are given, not in what they emit:

- **Intuition** and **Code** are seeded and tool-less: the overview, the
  skeleton's fixed decisions, and — for the files the skeleton assigned
  the section — every hunk under them with its established intent.
  Connective tissue between those intents is the thing forty independent
  per-hunk calls structurally cannot produce.
- **Background** also gets `RepoTools`, under a bounded turn budget,
  because it is the only section asserting facts about code *outside*
  the diff. What it opened is recorded from the tool surface and
  rendered as a citation line, and its answer is cached on `base_sha`
  alone — it describes the system before the change, so it survives head
  movement and prompt-iteration re-runs on the same branch.

A section whose anchored hunks are not all annotated is refused rather
than written: prose built on half the intents reads exactly as fluently
as prose built on all of them. A pass that raises leaves its section
`failed` — retryable, and it must not poison its neighbours — with the
updated document attached so the route can fan the state out.
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.output import ToolOutput

from ..cache.store import CacheStore
from ..viewer.build_json import ViewerIdIndex, viewer_id_index
from . import explainer_schema, mcp_http_host, source_cache
from .agents import Client
from .explainer import ExplainerNotReady, carry_guidance
from .pass_ import PassMeta, run_pass
from .prompts import (
    EXPLAINER_BACKGROUND_GUIDANCE,
    EXPLAINER_SECTION_BRIEFS,
    EXPLAINER_SECTION_GUIDANCE,
)
from .schemas import AnnotatedDiff
from .tools import TOOL_FUNCTIONS, RepoTools

log = logging.getLogger(__name__)


#: Requests Background's agentic loop may make before pydantic-ai cuts
#: it off. Small on purpose: the section wants the two or three files the
#: change lands on, not a survey. The cap and the count actually used go
#: to `trace/`, because an agentic pass whose cost is invisible is the
#: failure mode the cap exists to prevent.
BACKGROUND_TURN_CAP = 12


class SectionNotFound(KeyError):
    """No fillable section with that id in this run's document.

    Maps to HTTP 404. The Map is written by the skeleton and subsections
    are written by their parent's pass, so neither is addressable here.
    """


class SectionNotReady(RuntimeError):
    """The hunks this section is anchored to are not all annotated yet.

    Prose built on half the hunk intents reads exactly as fluently as
    prose built on all of them, which is why this is a refusal rather
    than a caveat in the output. Maps to HTTP 409 with the counts, so
    the viewer can say what it is waiting for.
    """

    def __init__(self, annotated: int, total: int) -> None:
        super().__init__(f"{annotated} of {total} anchored hunks are annotated")
        self.annotated = annotated
        self.total = total


class SectionFailed(RuntimeError):
    """The section's pass raised; the document records it as `failed`.

    Carries the persisted document so the route can fan the `failed`
    state out to every tab rather than leaving the other readers on
    `pending` forever.
    """

    def __init__(self, message: str, document: dict[str, Any]) -> None:
        super().__init__(message)
        self.document = document


# --- Wire format ----------------------------------------------------------


class SubmittedRef(BaseModel):
    """A reference as the model submits it."""

    kind: Literal["file", "hunk"] = Field(description="`file` for an `F<i>` id, `hunk` for an `H<fi>_<hi>` id.")
    id: str = Field(description="The viewer id, verbatim from the `# Files` / `# Anchored code` lists.")


class SubmittedSubsection(BaseModel):
    """One model-chosen subsection. Only the Code section may have them."""

    title: str = Field(description="A short noun phrase. Rendered as a heading and as a sidebar child node.")
    body: str = Field(description="Markdown prose for this subsection.")
    refs: list[SubmittedRef] = Field(
        default_factory=list,
        description="What this subsection is about, in reading order.",
    )


class SubmittedTerm(BaseModel):
    """One entry of a term list."""

    term: str = Field(description="The name as the code spells it.")
    definition: str = Field(description="One or two sentences. Markdown.")


class SubmittedSkipBox(BaseModel):
    """Background's 'you already know this' escape hatch."""

    body: str = Field(description="One sentence naming what the reader would already have to know.")
    target_section_id: str = Field(description="The section to jump to: `intuition` or `code`.")


class ExplainerSectionSubmission(BaseModel):
    """Wire format of `submit_explainer_section`."""

    body: str = Field(description="The section's prose, as markdown.")
    refs: list[SubmittedRef] = Field(
        default_factory=list,
        description="What this section is about, in reading order.",
    )
    subsections: list[SubmittedSubsection] = Field(
        default_factory=list,
        description="Model-chosen parts of the walkthrough. Code only; ignored elsewhere.",
    )
    terms: list[SubmittedTerm] = Field(
        default_factory=list,
        description="Names the reader needs before the prose uses them. Rendered as a definition list.",
    )
    skip_box: SubmittedSkipBox | None = Field(
        default=None,
        description="Background only: lets a reader who knows the system skip its first layer.",
    )
    toy_data: bool = Field(
        default=False,
        description="True when a worked example here uses invented identifiers, counts or values.",
    )


def make_explainer_section_agent(
    model: str | Model,
    system: str,
    *,
    tools: list | None = None,
) -> Agent[RepoTools, ExplainerSectionSubmission]:
    """Agent for one prose section.

    `tools` is empty for the seeded passes — their seed is the only code
    they may speak about — and the recording `RepoTools` surface for
    Background. `deps_type` is declared either way so one agent shape
    serves both.
    """
    return Agent(
        model=model,
        deps_type=RepoTools,
        output_type=ToolOutput(ExplainerSectionSubmission, name="submit_explainer_section"),
        instructions=system,
        tools=tools or [],
    )


# --- Prompt assembly ------------------------------------------------------


def anchored_hunks(diff: AnnotatedDiff, refs: list[explainer_schema.Reference]) -> list[tuple[int, int]]:
    """The `(file_index, hunk_index)` pairs a section's references cover.

    A file reference expands to every hunk in that file — the skeleton
    assigns files, and the section is written about what is in them.
    Order follows the references; duplicates are collapsed. References
    that address nothing in this diff contribute nothing, which cannot
    happen for a persisted document but does for a hand-edited one.
    """
    out: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for ref in refs:
        for pair in _ref_hunks(diff, ref):
            if pair not in seen:
                seen.add(pair)
                out.append(pair)
    return out


def _ref_hunks(diff: AnnotatedDiff, ref: explainer_schema.Reference) -> list[tuple[int, int]]:
    if ref.kind == "file":
        fi = _file_index(ref.id)
        if fi is None or fi >= len(diff.files):
            return []
        return [(fi, hi) for hi in range(len(diff.files[fi].hunks))]
    pair = _hunk_index(ref.id)
    if pair is None or pair[0] >= len(diff.files) or pair[1] >= len(diff.files[pair[0]].hunks):
        return []
    return [pair]


def readiness(diff: AnnotatedDiff, refs: list[explainer_schema.Reference]) -> tuple[int, int]:
    """`(annotated, total)` over the hunks a section is anchored to."""
    pairs = anchored_hunks(diff, refs)
    annotated = sum(1 for fi, hi in pairs if diff.files[fi].hunks[hi].ann.intent.strip())
    return annotated, len(pairs)


def format_section_prompt(
    diff: AnnotatedDiff,
    doc: explainer_schema.ExplainerDocument,
    section: explainer_schema.Section,
    *,
    overview_json: str,
) -> str:
    """Assemble one section call's user text.

    Carries the overview, the skeleton's fixed decisions, the section
    list (so a cross-reference resolves), the whole file id map, and the
    established intent of every hunk this section is anchored to.
    """
    parts = [
        f"# Change overview\n{overview_json}",
        "",
        _format_document_context(doc, section),
        "",
        _format_file_ids(diff),
        "",
        _format_anchored_code(diff, section.refs),
        "",
        f"# Your section: {section.title}\n{EXPLAINER_SECTION_BRIEFS[section.kind]}",
        "",
        f"Write the {section.title} section.",
    ]
    return "\n".join(parts) + "\n"


def _format_document_context(
    doc: explainer_schema.ExplainerDocument,
    section: explainer_schema.Section,
) -> str:
    lines = ["# The document so far"]
    if doc.verdict_note:
        lines.append(f"Shape of the change: {doc.verdict_note}")
    if doc.figure_family:
        lines.append(f"Figure family (fixed, do not revise): {doc.figure_family}")
    if doc.cast:
        lines.append(f"Recurring cast: {', '.join(doc.cast)}")
    lines.append("Sections, in document order:")
    for s in doc.sections:
        marker = "  <- yours" if s.id == section.id else ""
        lines.append(f"  {s.id}  {s.title}  ({s.state}){marker}")
    lines.append("The Map is written and each other section is written by its own call. Do not reproduce them.")
    return "\n".join(lines)


def _format_file_ids(diff: AnnotatedDiff) -> str:
    lines = [
        "# Files",
        "Every file in the change. These ids and the hunk ids below are the only valid references.",
    ]
    for fi, f in enumerate(diff.files):
        role = f.ann.role.value if f.ann.role else "modified"
        lines.append(f"  F{fi}  {f.path}  ({len(f.hunks)} hunks, {role})")
    return "\n".join(lines)


def _format_anchored_code(diff: AnnotatedDiff, refs: list[explainer_schema.Reference]) -> str:
    """The hunks under this section's references, with their intents."""
    pairs = anchored_hunks(diff, refs)
    if not pairs:
        return (
            "# Anchored code\n"
            "The skeleton assigned this section no files. Write from the overview and "
            "the document's decisions alone, and reference nothing you cannot support."
        )
    lines = [
        "# Anchored code",
        "The hunks this section is about, with the intent already established for each.",
    ]
    current = -1
    for fi, hi in pairs:
        f = diff.files[fi]
        if fi != current:
            current = fi
            lines.append(f"  F{fi}  {f.path}")
        lines.append(f"    H{fi}_{hi}  {f.hunks[hi].parsed.header.strip()}")
        lines.append(f"          {f.hunks[hi].ann.intent.strip() or '(not annotated)'}")
    return "\n".join(lines)


def _file_index(file_id: str) -> int | None:
    if not file_id.startswith("F"):
        return None
    try:
        return int(file_id[1:])
    except ValueError:
        return None


def _hunk_index(hunk_id: str) -> tuple[int, int] | None:
    if not hunk_id.startswith("H") or "_" not in hunk_id:
        return None
    left, _, right = hunk_id[1:].partition("_")
    try:
        return int(left), int(right)
    except ValueError:
        return None


# --- Apply ----------------------------------------------------------------


def apply_section_submission(
    doc: explainer_schema.ExplainerDocument,
    section: explainer_schema.Section,
    submission: ExplainerSectionSubmission,
    *,
    ids: ViewerIdIndex,
    sources: list[str] | None = None,
) -> None:
    """Fold a `submit_explainer_section` payload into `section`, in place.

    References the model invented are dropped and counted into the
    document's `dropped_refs`. A submission that narrows nothing leaves
    the skeleton's references standing — that is the section's assigned
    scope, not a missing value. `sources` is the recorded read list for a
    tool-using pass; it comes from the tool surface, never from the
    submission.
    """
    kept, dropped = _validate(submission.refs, ids)
    subsections, sub_dropped = _subsections(section, submission.subsections, ids)

    section.body = submission.body.strip()
    if kept:
        section.refs = kept
    section.subsections = subsections
    section.terms = [
        explainer_schema.Term(term=t.term.strip(), definition=t.definition.strip())
        for t in submission.terms
        if t.term.strip()
    ]
    section.skip_box = _skip_box(doc, section, submission.skip_box)
    section.sources = list(sources) if sources else []
    section.state = "ready"

    doc.dropped_refs += dropped + sub_dropped
    doc.toy_data = doc.toy_data or submission.toy_data


def _skip_box(
    doc: explainer_schema.ExplainerDocument,
    section: explainer_schema.Section,
    submitted: SubmittedSkipBox | None,
) -> explainer_schema.SkipBox | None:
    """The section's skip box, or None when it has no valid one.

    Background only — it is the only section written in two layers — and
    the target has to resolve, because a jump to nowhere is worse than no
    jump offered.
    """
    if submitted is None:
        return None
    if section.kind != "background":
        log.warning("explainer: %s section submitted a skip box — Background only; dropped", section.id)
        return None
    target = submitted.target_section_id.strip()
    if not any(s.id == target for s in doc.sections):
        log.warning("explainer: skip box targets %r, which is not a section here — dropped", target)
        return None
    return explainer_schema.SkipBox(body=submitted.body.strip(), target_section_id=target)


def _validate(
    refs: list[SubmittedRef],
    ids: ViewerIdIndex,
) -> tuple[list[explainer_schema.Reference], int]:
    return explainer_schema.validate_references(
        (explainer_schema.Reference(kind=r.kind, id=r.id) for r in refs),
        file_ids=ids.files,
        hunk_ids=ids.hunks,
    )


def _subsections(
    section: explainer_schema.Section,
    submitted: list[SubmittedSubsection],
    ids: ViewerIdIndex,
) -> tuple[list[explainer_schema.Section], int]:
    """Model-chosen child sections, or `([], 0)` outside Code."""
    if not submitted:
        return [], 0
    if section.kind != "code":
        log.warning(
            "explainer: %s section submitted %d subsections — only Code has them; dropped",
            section.id,
            len(submitted),
        )
        return [], 0
    out: list[explainer_schema.Section] = []
    dropped = 0
    for i, sub in enumerate(submitted, start=1):
        refs, n = _validate(sub.refs, ids)
        dropped += n
        out.append(
            explainer_schema.Section(
                # Minted here, not by the model: an id has to be unique
                # within the document and safe as a DOM id, and a title
                # is neither.
                id=f"{section.id}-{i}",
                kind=section.kind,
                title=sub.title.strip(),
                state="ready",
                body=sub.body.strip(),
                refs=refs,
            )
        )
    return out, dropped


def find_section(
    doc: explainer_schema.ExplainerDocument,
    section_id: str,
) -> explainer_schema.Section:
    """The fillable top-level section with that id.

    Raises:
        SectionNotFound: No such section, or it is the Map — which the
            skeleton writes and this route cannot.
    """
    for s in doc.sections:
        if s.id == section_id and s.kind != "map":
            return s
    raise SectionNotFound(section_id)


# --- Provenance -----------------------------------------------------------

#: Tool arguments that name a file the pass opened. `grep`'s
#: `path_glob` is a filter over a search, not a read, so it is not one
#: of these — a citation line has to list what was actually looked at.
_PATH_ARGS = ("path",)

#: Where the recorded read list rides in the pass payload. Not a field
#: of `ExplainerSectionSubmission` — the model must not be able to write
#: its own citation line — but it is merged into the payload before the
#: cache stores it, so a cached Background keeps its provenance.
#: `model_validate` ignores it.
_SOURCES_KEY = "recorded_sources"


class _ReadRecorder:
    """The paths a tool-using pass opened, in first-read order.

    Fed from whichever transport the backend uses: the pydantic-ai tool
    wrappers on SDK backends, the hosted MCP server's dispatch hook on
    subprocess ones. The reader never sees the model's account of what
    it read — only this.
    """

    def __init__(self) -> None:
        self.paths: list[str] = []

    def record(self, args: dict[str, Any]) -> None:
        for name in _PATH_ARGS:
            value = args.get(name)
            if not isinstance(value, str):
                continue
            path = value.strip()
            if path and path not in self.paths:
                self.paths.append(path)


def _recording_tool_functions(recorder: _ReadRecorder) -> list:
    """`TOOL_FUNCTIONS`, each wrapped to note the path it was handed.

    Wrapped here rather than in `tools.py`: this is the one pass that
    cites its reads, and every other caller of the tool surface should
    be unaffected by that.
    """
    return [_recording_tool(fn, recorder) for fn in TOOL_FUNCTIONS]


def _recording_tool(fn: Any, recorder: _ReadRecorder) -> Any:
    async def wrapper(ctx: Any, **kwargs: Any) -> str:
        recorder.record(kwargs)
        return await fn(ctx, **kwargs)

    # pydantic-ai derives each tool's schema by introspection, so the
    # wrapper has to present the wrapped function's signature verbatim.
    wrapper.__name__ = fn.__name__
    wrapper.__qualname__ = fn.__qualname__
    wrapper.__doc__ = fn.__doc__
    wrapper.__signature__ = fn.__signature__  # type: ignore[attr-defined]
    wrapper.__annotations__ = dict(fn.__annotations__)
    return wrapper


# --- Running --------------------------------------------------------------


async def generate_explainer_section(
    client: Client,
    *,
    run_dir: Path,
    section_id: str,
    model: str,
    cache: CacheStore | None = None,
    trace_dir: Path | None = None,
) -> explainer_schema.ExplainerDocument:
    """Fill one section of the run's document and persist the result.

    Args:
        client: The LLM backend handle. Chooses the guidance carrier.
        run_dir: The run directory; must hold `augmented.scr.json` and a
            document for the run's current `(base_sha, head_sha)`.
        section_id: A fillable section id (`background`, `intuition`,
            `code`).
        model: The user-facing model string, for the cache key.
        cache: Optional response cache.
        trace_dir: Optional `trace/` directory for the call envelope.

    Returns:
        The whole document, with that section `ready`.

    Raises:
        ExplainerNotReady: No sidecar, or no document to fill a section
            of.
        SectionNotFound: No fillable section with that id.
        SectionNotReady: The section's anchored hunks are not all
            annotated.
        SectionFailed: The pass raised. The section is persisted
            `failed`; the document rides on the exception.
    """
    diff, doc = _load(run_dir)
    section = find_section(doc, section_id)
    annotated, total = readiness(diff, section.refs)
    if annotated < total:
        raise SectionNotReady(annotated, total)

    # Lazy: keeps the overview formatter off the import path for callers
    # that only want the prompt assembly or the apply step.
    from .hunks import overview_to_prompt_json

    is_background = section.kind == "background"
    guidance = EXPLAINER_SECTION_GUIDANCE
    if is_background:
        guidance = f"{guidance}\n\n{EXPLAINER_BACKGROUND_GUIDANCE}"
    system_text, user_prefix = carry_guidance(client, guidance)
    user_text = format_section_prompt(
        diff,
        doc,
        section,
        overview_json=overview_to_prompt_json(diff, include_symbols=False),
    )
    if user_prefix:
        user_text = f"{user_prefix}\n\n{user_text}"

    try:
        payload, sources = await _run_section_pass(
            client,
            run_dir=run_dir,
            diff=diff,
            section=section,
            system_text=system_text,
            user_text=user_text,
            model=model,
            cache=cache,
            trace_dir=trace_dir,
        )
    except Exception as e:
        section.state = "failed"
        explainer_schema.save_explainer(run_dir, doc)
        log.exception("explainer section %s failed", section_id)
        raise SectionFailed(f"{type(e).__name__}: {e}", doc.model_dump(mode="json")) from e

    apply_section_submission(
        doc,
        section,
        ExplainerSectionSubmission.model_validate(payload),
        ids=viewer_id_index(diff),
        sources=sources,
    )
    explainer_schema.save_explainer(run_dir, doc)
    return doc


async def _run_section_pass(
    client: Client,
    *,
    run_dir: Path,
    diff: AnnotatedDiff,
    section: explainer_schema.Section,
    system_text: str,
    user_text: str,
    model: str,
    cache: CacheStore | None,
    trace_dir: Path | None,
) -> tuple[dict[str, Any], list[str]]:
    """Drive one section's pass; return `(payload, files_read)`.

    Intuition and Code are one tool-less call keyed on their own prompt.
    Background gets the repo tools, `BACKGROUND_TURN_CAP` requests to
    spend, and a cache key of `base_sha` alone — it describes the system
    before the change, so it is invariant across head movement and across
    prompt-iteration re-runs on the same branch, which is the whole
    reason it is a separate call.
    """
    trace_path = (trace_dir / f"explainer-{section.id}.json") if trace_dir is not None else None
    if section.kind != "background":
        payload = await run_pass(
            PassMeta(name=f"explainer-{section.kind}", submit_tool="submit_explainer_section"),
            client=client,
            agent=make_explainer_section_agent(client.model, system_text),
            user_content=user_text,
            system=system_text,
            model=model,
            cache_inputs=(user_text,),
            cache=cache,
            trace_path=trace_path,
            cache_request={"system": system_text, "user": user_text},
        )
        assert payload is not None  # `swallow_errors` is false
        return payload, []

    recorder = _ReadRecorder()
    repo_tools = RepoTools(
        head_worktree=run_dir / "head",
        repo_git=run_dir / "repo.git",
        base_sha=diff.pr.base_sha,
        head_sha=diff.pr.head_sha,
        cache=source_cache.SourceCache(),
    )
    async with contextlib.AsyncExitStack() as stack:
        if client.is_subprocess_backend:
            # Subprocess backends reach the worktree over MCP rather than
            # pydantic-ai `deps`, so the recorder feeds off the host's
            # dispatch hook instead of the tool wrappers. One host per
            # request, as the console does.
            host = stack.enter_context(
                mcp_http_host.McpHttpHost(repo_tools, on_tool=lambda _name, args: recorder.record(args))
            )
            client.set_mcp_endpoint(host.mcp_config())
            stack.callback(client.set_mcp_endpoint, None)
        payload = await run_pass(
            PassMeta(
                name="explainer-background",
                submit_tool="submit_explainer_section",
                tool_names=tuple(fn.__name__ for fn in TOOL_FUNCTIONS),
            ),
            client=client,
            agent=make_explainer_section_agent(
                client.model,
                system_text,
                tools=_recording_tool_functions(recorder),
            ),
            user_content=user_text,
            system=system_text,
            model=model,
            cache_inputs=(diff.pr.base_sha,),
            deps=repo_tools,
            request_limit=BACKGROUND_TURN_CAP,
            cache=cache,
            trace_path=trace_path,
            cache_request={"system": system_text, "user": user_text},
            # The read list is not the model's to submit, but it has to
            # ride the cache with the prose: a cached Background whose
            # citation line came back empty would claim it read nothing.
            payload_extra=lambda: {_SOURCES_KEY: list(recorder.paths)},
        )
    assert payload is not None  # `swallow_errors` is false
    return payload, [str(p) for p in payload.get(_SOURCES_KEY, [])]


def _load(run_dir: Path) -> tuple[AnnotatedDiff, explainer_schema.ExplainerDocument]:
    """The run's annotated diff and its current document.

    Raises:
        ExplainerNotReady: Either is missing. A section cannot be filled
            before the skeleton has said what the sections are.
    """
    sidecar = run_dir / "augmented.scr.json"
    if not sidecar.exists():
        raise ExplainerNotReady("augmented.scr.json missing — augment not complete")

    # Lazy: keeps the format machinery off the import path for callers
    # that only want the prompt assembly or the apply step.
    from ..format.sidecar import load_sidecar

    diff = load_sidecar(sidecar)
    doc = explainer_schema.load_explainer(run_dir, base_sha=diff.pr.base_sha, head_sha=diff.pr.head_sha)
    if doc is None:
        raise ExplainerNotReady("no explainer document for this diff — generate the skeleton first")
    return diff, doc


__all__ = [
    "BACKGROUND_TURN_CAP",
    "ExplainerSectionSubmission",
    "SectionFailed",
    "SectionNotFound",
    "SectionNotReady",
    "anchored_hunks",
    "apply_section_submission",
    "find_section",
    "format_section_prompt",
    "generate_explainer_section",
    "make_explainer_section_agent",
    "readiness",
]
