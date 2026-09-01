"""Prose for the change explainer (ADR 0007 and its addendum).

The skeleton (`explainer.py`) writes the Map and leaves Background,
Intuition and Code `pending`. `POST /explainer/section/{id}` fills them,
on the reviewer's press — prose nobody asks for is never paid for.

Sections and calls are separate axes. `explainer_schema.PROSE_PASSES`
maps the three prose sections onto two calls: Background alone, then
Intuition and Code together. A POST names a section; what runs is the
call that owns it, and every section that call writes lands. The route
stays coherent for a caller that does not know two of them are merged —
it addresses a section, and the response is the whole document either
way.

Both passes get `RepoTools`. Background asserts facts about code the
diff does not contain; the walkthrough's job is the connective tissue
between hunks, and the questions that produce it — is this new function
called anywhere, what did this replace, did a removal leave something
behind — are what `references` and `changed_symbols` answer and what
the seed does not contain. What a pass opened is recorded off the tool
surface, never from the model's account of itself, and rendered as a
citation line under the prose that call wrote.

Every pass is seeded with the whole change: every file, and every hunk
with the intent established for it. The skeleton's assignment routes
rather than scopes — it says what a section is about and seeds the
references it starts from, and does not bound what the call can see. A
tool-less skeleton, which sees paths and one-line summaries, must not be
able to decide what a tool-bearing prose call may know.

The tool loop is metered against one budget for the whole document
(`DOCUMENT_TURN_BUDGET`), carried on the document because the passes
are separated in time. A pass that needs six turns gets six; one that
needs none costs nothing; and the document cannot spend a per-section
cap twice over.

A section whose anchored hunks are not all annotated is refused rather
than written: prose built on half the intents reads exactly as fluently
as prose built on all of them. The gate is the routed hunks, not the
whole listing — those are the ones the section's own claims rest on, and
a hunk elsewhere in the change that has no intent yet is listed as
`(not annotated)`. A pass that raises leaves the sections it
was writing `failed` — retryable, and it must not poison its neighbours
— with the updated document attached so the route can fan the state out.
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

from .. import errors
from ..cache.store import CacheStore
from ..viewer.build_json import ViewerIdIndex, viewer_id_index
from . import explainer_schema, mcp_http_host, source_cache
from .agents import Client
from .explainer import ExplainerNotReady, carry_guidance, file_seed_lines, prose_figure_guidance
from .pass_ import PassMeta, run_pass
from .prompts import (
    EXPLAINER_BACKGROUND_GUIDANCE,
    EXPLAINER_SECTION_BRIEFS,
    EXPLAINER_SECTION_GUIDANCE,
    EXPLAINER_TOOL_GUIDANCE,
    format_house_style,
)
from .schemas import AnnotatedDiff
from .tools import TOOL_FUNCTIONS, RepoTools

log = logging.getLogger(__name__)


#: Model requests the document's prose passes may spend between them,
#: across every call, however far apart in time.
#:
#: The unit is the document, not the section, for two reasons. A
#: per-section cap is a cap on nothing: three of them are a document
#: total nobody chose. And the passes do not want equal shares — one
#: lands on a subsystem nobody has to be told about and reads nothing,
#: the next needs six files. A shared total gives each what it needs
#: until the document as a whole has had enough.
#:
#: Sized for Background, which earns the most reading: the system it
#: describes is mostly code the diff does not contain, so its ground
#: comes off the tool surface rather than out of the seed.
#:
#: The spend is tracked on `ExplainerDocument.turns_used`, so it
#: survives a reload, a second tab and a restart, and is scoped exactly
#: the way the document is: a new `(base_sha, head_sha)` is a new
#: document and a fresh budget. A cache hit adds nothing.
DOCUMENT_TURN_BUDGET = 36

#: Remaining budget below which a pass runs with no tools at all rather
#: than with a ceiling it cannot finish under. One request buys a read
#: and nothing else; a loop cut off mid-investigation loses the pass
#: after spending the most on it, and telling a model about tools it has
#: no budget to use makes it hedge. Below this the pass is seeded and
#: tool-less — which is what every prose pass was before the addendum,
#: so it is a known-good shape, not a degraded guess.
MIN_TOOL_TURNS = 2


class SectionNotFound(errors.ScrError, KeyError):
    """No fillable section with that id in this run's document.

    The Map is written by the skeleton and subsections are written by
    their parent's pass, so neither is addressable here.
    """

    status = 404

    def __init__(self, section_id: str) -> None:
        super().__init__(section_id)
        self.section_id = section_id

    def body(self) -> dict[str, Any]:
        # KeyError renders its argument as a repr, which reads as a bare
        # quoted id; say what the id failed to address instead.
        return {"error": f"no section {self.section_id!r} in this document"}


class SectionNotReady(errors.ScrError):
    """The hunks this call is anchored to are not all annotated yet.

    Prose built on half the hunk intents reads exactly as fluently as
    prose built on all of them, which is why this is a refusal rather
    than a caveat in the output. Carries the counts, so the viewer can
    say what it is waiting for.
    """

    status = 409

    def __init__(self, annotated: int, total: int) -> None:
        super().__init__(f"{annotated} of {total} anchored hunks are annotated")
        self.annotated = annotated
        self.total = total

    def body(self) -> dict[str, Any]:
        return {
            "error": f"{self.annotated} of {self.total} hunks under this section are annotated",
            "annotated": self.annotated,
            "total": self.total,
        }


class SectionFailed(errors.ScrError):
    """A prose call raised; its sections are recorded `failed`.

    Carries the persisted document so the session can fan the `failed`
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
    id: str = Field(description="The viewer id, verbatim from the `# The change` listing.")


class SubmittedFigure(BaseModel):
    """One diagram: inline SVG in a structured slot, not markup in prose.

    `stripped` is not here. What the sanitiser removed is recorded on
    the way to disk, so the count the reader is shown is not the model's
    to report.
    """

    svg: str = Field(
        description=(
            "One root `<svg>` with a `viewBox` and no width or height. Geometry and "
            "vocabulary class names only: every `fill`, `stroke`, `style`, font and "
            "opacity attribute is removed before the figure is stored, and the shape "
            "then renders unpainted."
        )
    )
    alt: str = Field(
        description=(
            "Required. One sentence saying what the figure shows, for a reader who "
            "cannot see it — and what renders in its place if it cannot be drawn."
        )
    )
    caption: str = Field(
        default="",
        description="One sentence saying what to take from the figure. Inline markdown.",
    )


class SubmittedSubsection(BaseModel):
    """One model-chosen subsection. Only the Code section may have them."""

    title: str = Field(description="A short noun phrase. Rendered as a heading and as a sidebar child node.")
    body: str = Field(description="Markdown prose for this subsection.")
    refs: list[SubmittedRef] = Field(
        default_factory=list,
        description="What this subsection is about, in reading order.",
    )
    figures: list[SubmittedFigure] = Field(
        default_factory=list,
        description=(
            "Diagrams for this part of the walkthrough, on the same terms as the "
            "section's own. A figure about one part belongs here, next to the prose "
            "that reads it; one about the change as a whole belongs on the section."
        ),
    )


class SubmittedTerm(BaseModel):
    """One entry of a term list."""

    term: str = Field(description="The name as the code spells it.")
    definition: str = Field(description="One or two sentences. Markdown.")


class SubmittedSkipBox(BaseModel):
    """Background's 'you already know this' escape hatch."""

    body: str = Field(description="The 'If you already know X,' clause; the viewer completes it with the jump.")
    target_section_id: str = Field(description="The section to jump to: `intuition` or `code`.")


class SubmittedSection(BaseModel):
    """One section of a prose call's answer."""

    section: Literal["background", "intuition", "code"] = Field(
        description="Which of the sections you were asked for this entry fills."
    )
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
        description="Names your prose introduced, collected as a glossary under it.",
    )
    skip_box: SubmittedSkipBox | None = Field(
        default=None,
        description="Background only: lets a reader who knows the system skip its first layer.",
    )
    figures: list[SubmittedFigure] = Field(
        default_factory=list,
        description=(
            "Diagrams for this section, in reading order, drawn to the figure family "
            "you were given. Only where you were given the figure rules: without them "
            "a figure has no vocabulary to draw in and is dropped."
        ),
    )
    toy_data: bool = Field(
        default=False,
        description="True when a worked example here uses invented identifiers, counts or values.",
    )


class ExplainerProseSubmission(BaseModel):
    """Wire format of `submit_explainer_prose`.

    A list because one call writes one *or more* sections — Intuition
    and Code are merged into a single pass, and a section that came back
    lands even if its sibling did not.
    """

    sections: list[SubmittedSection] = Field(
        default_factory=list,
        description="One entry per section you were asked to write, in the order you were given them.",
    )


def make_explainer_prose_agent(
    model: str | Model,
    system: str,
    *,
    tools: list | None = None,
) -> Agent[RepoTools, ExplainerProseSubmission]:
    """Agent for one prose call.

    `tools` is the recording `RepoTools` surface, or empty when the
    document's turn budget has nothing left to fund a tool loop with.
    `deps_type` is declared either way so one agent shape serves both.
    """
    return Agent(
        model=model,
        deps_type=RepoTools,
        output_type=ToolOutput(ExplainerProseSubmission, name="submit_explainer_prose"),
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


def format_prose_prompt(
    diff: AnnotatedDiff,
    doc: explainer_schema.ExplainerDocument,
    targets: list[explainer_schema.Section],
    *,
    overview_json: str,
) -> str:
    """Assemble one prose call's user text.

    Carries the overview, the skeleton's fixed decisions, the reading
    Map, the prose already written by the document's other calls, the
    whole change — every file and every hunk, with the intent established
    for each — and, per section this call writes, its brief and the files
    the skeleton routed to it.

    Args:
        diff: The run's annotated diff.
        doc: The document as persisted.
        targets: The sections this call writes, in document order.
        overview_json: The change overview, as the skeleton was seeded
            with it.
    """
    parts = [
        f"# Change overview\n{overview_json}",
        "",
        _format_document_context(doc, targets),
        "",
        _format_change(diff),
        "",
        _format_assignments(diff, targets),
        "",
        _format_closing(targets),
    ]
    return "\n".join(parts) + "\n"


def _format_document_context(
    doc: explainer_schema.ExplainerDocument,
    targets: list[explainer_schema.Section],
) -> str:
    """The skeleton's decisions, the Map, and the prose already written.

    The Map and any finished section go in verbatim rather than as
    titles: a call that cannot see what the document already says either
    repeats it or contradicts it, and the merged walkthrough is written
    against a Background that has usually already run.
    """
    mine = {s.id for s in targets}
    lines = ["# The document so far"]
    if doc.verdict_note:
        lines.append(f"Shape of the change: {doc.verdict_note}")
    if doc.figure_family:
        lines.append(f"Figure family (fixed, do not revise): {doc.figure_family}")
    if doc.cast:
        lines.append(f"Recurring cast: {', '.join(doc.cast)}")
    lines.append("Sections, in document order:")
    for s in doc.sections:
        marker = "  <- yours" if s.id in mine else ""
        lines.append(f"  {s.id}  {s.title}  ({s.state}){marker}")
    lines.append("Do not reproduce a section that is not yours; build on it.")
    for s in doc.sections:
        if s.id in mine:
            continue
        written = _written_section(s)
        if written:
            lines += ["", written]
    return "\n".join(lines)


def _written_section(section: explainer_schema.Section) -> str:
    """A finished section as the next call should see it, or `""`."""
    if section.kind == "map":
        rows = [f"  {row.ref.id}  {row.why}" for row in section.map_rows]
        return "\n".join([f"## {section.title} (written — the reading order)", *rows]) if rows else ""
    if section.state != "ready" or not section.body.strip():
        return ""
    return f"## {section.title} (written)\n{section.body.strip()}"


def _format_assignments(
    diff: AnnotatedDiff,
    targets: list[explainer_schema.Section],
) -> str:
    """Each section this call writes: its brief and what it is about."""
    lines = [
        "# Your sections",
        f"Write {'these' if len(targets) > 1 else 'this'}, one entry in `sections` each, in this order.",
    ]
    for section in targets:
        lines += [
            "",
            f"## `{section.id}` — {section.title}",
            EXPLAINER_SECTION_BRIEFS[section.kind],
            "",
            _format_routing(diff, section.refs),
        ]
    return "\n".join(lines)


def _format_closing(targets: list[explainer_schema.Section]) -> str:
    titles = " and ".join(s.title for s in targets)
    plural = "sections" if len(targets) > 1 else "section"
    return f"Write the {titles} {plural}."


def _format_change(diff: AnnotatedDiff) -> str:
    """The whole change: every file, every hunk, every established intent.

    Uniform across the passes and independent of what the skeleton routed
    where. Two lines a hunk, so the listing is strictly smaller than the
    raw diff the overview pass already takes in one call — there is no
    size cap on it.
    """
    lines = [
        "# The change",
        "Every file and every hunk, with the intent already established for each. "
        "These ids are the only valid references.",
    ]
    for fi, f in enumerate(diff.files):
        lines += file_seed_lines(fi, f)
        for hi, h in enumerate(f.hunks):
            lines.append(f"    H{fi}_{hi}  {h.parsed.header.strip()}")
            lines.append(f"          {h.ann.intent.strip() or '(not annotated)'}")
    return "\n".join(lines)


def _format_routing(diff: AnnotatedDiff, refs: list[explainer_schema.Reference]) -> str:
    """What the skeleton routed to this section, as ids and paths."""
    rows = _routed_rows(diff, refs)
    if not rows:
        return (
            "### Routed to this section\n"
            "The skeleton routed nothing here, so what this section is about is yours to "
            "choose out of the change above."
        )
    return "\n".join(
        [
            "### Routed to this section",
            "What this section is ABOUT, and where its references start. The whole change "
            "is above; reach past this list where the section's story needs it.",
            *rows,
        ]
    )


def _routed_rows(diff: AnnotatedDiff, refs: list[explainer_schema.Reference]) -> list[str]:
    """One row per reference that addresses something in this diff."""
    rows: list[str] = []
    seen: set[str] = set()
    for ref in refs:
        pairs = _ref_hunks(diff, ref)
        if not pairs or ref.id in seen:
            continue
        seen.add(ref.id)
        rows.append(f"  {ref.id}  {diff.files[pairs[0][0]].path}")
    return rows


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


def apply_prose_submission(
    doc: explainer_schema.ExplainerDocument,
    targets: list[explainer_schema.Section],
    submission: ExplainerProseSubmission,
    *,
    ids: ViewerIdIndex,
    sources: list[str] | None = None,
) -> list[explainer_schema.Section]:
    """Fold a `submit_explainer_prose` payload into the call's sections.

    One call writes one or more sections, and a section that came back
    lands whether or not its sibling did: failing both because one is
    missing throws away prose that was paid for. A target with no entry
    is left `failed` — retryable, and distinct from `pending`, which
    would have the viewer buy the same call again unasked.

    Args:
        doc: The document, mutated in place.
        targets: The sections this call was asked for, in document order.
        submission: What came back.
        ids: The viewer id maps references are validated against.
        sources: The pass's recorded read list, off the tool surface —
            never from the submission. Every section the call wrote
            carries it.

    Returns:
        The targets that were written, in document order.
    """
    by_id = {s.id: s for s in targets}
    written: list[explainer_schema.Section] = []
    for entry in submission.sections:
        section = by_id.get(entry.section)
        if section is None:
            log.warning(
                "explainer: prose call returned a %r section it was not asked for — dropped",
                entry.section,
            )
            continue
        if section in written:
            log.warning("explainer: prose call returned %r twice — later entry dropped", entry.section)
            continue
        _apply_one(doc, section, entry, ids=ids, sources=sources)
        written.append(section)
    for section in targets:
        if section not in written:
            log.warning("explainer: prose call returned no %r section — left failed", section.id)
            section.state = "failed"
    return written


def _apply_one(
    doc: explainer_schema.ExplainerDocument,
    section: explainer_schema.Section,
    submission: SubmittedSection,
    *,
    ids: ViewerIdIndex,
    sources: list[str] | None = None,
) -> None:
    """Fold one submitted section into `section`, in place.

    References the model invented are dropped and counted into the
    document's `dropped_refs`. A submission that narrows nothing leaves
    the skeleton's references standing — that is what the skeleton routed
    to the section, not a missing value.
    """
    kept, dropped = _validate(submission.refs, ids)
    subsections, sub_dropped = _subsections(doc, section, submission.subsections, ids)

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
    section.figures = _figures(doc, section.id, submission.figures)
    section.sources = list(sources) if sources else []
    section.state = "ready"

    doc.dropped_refs += dropped + sub_dropped
    doc.toy_data = doc.toy_data or submission.toy_data


def _figures(
    doc: explainer_schema.ExplainerDocument,
    owner_id: str,
    submitted: list[SubmittedFigure],
) -> list[explainer_schema.Figure]:
    """The section's diagrams, as submitted; sanitised on the way to disk.

    Nothing is cleaned here: `save_explainer` reduces every figure in the
    document to the drawing vocabulary and records what that removed, so
    doing it twice would only mean two counts of the same loss.

    A figure a call was never given the figure rules for is dropped —
    the same treatment a skip box outside Background gets, and for the
    same reason. Without the family it has no vocabulary to draw in, and
    what the sanitiser leaves of it is unpainted geometry.
    """
    if not submitted:
        return []
    if not explainer_schema.figures_fixed(doc):
        log.warning(
            "explainer: %s submitted %d figures for a document with no figure family — dropped",
            owner_id,
            len(submitted),
        )
        return []
    return [explainer_schema.Figure(svg=f.svg.strip(), alt=f.alt.strip(), caption=f.caption.strip()) for f in submitted]


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
    doc: explainer_schema.ExplainerDocument,
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
        # Minted here, not by the model: an id has to be unique within
        # the document and safe as a DOM id, and a title is neither. It
        # also namespaces the subsection's figure ids.
        sub_id = f"{section.id}-{i}"
        out.append(
            explainer_schema.Section(
                id=sub_id,
                kind=section.kind,
                pass_id=section.pass_id,
                title=sub.title.strip(),
                state="ready",
                body=sub.body.strip(),
                refs=refs,
                figures=_figures(doc, sub_id, sub.figures),
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


def pass_targets(
    doc: explainer_schema.ExplainerDocument,
    section_id: str,
) -> list[explainer_schema.Section]:
    """Every section written by the call that owns `section_id`.

    A POST names one section; what runs is its pass, and a pass may
    write more than one. Order is document order, which is the order the
    call is asked to write them in.

    Raises:
        SectionNotFound: No fillable section with that id.
    """
    addressed = find_section(doc, section_id)
    return [s for s in doc.sections if s.kind != "map" and s.pass_id == addressed.pass_id]


# --- Provenance -----------------------------------------------------------

#: Tool arguments that name a file the pass opened. `grep`'s
#: `path_glob` is a filter over a search, not a read, so it is not one
#: of these — a citation line has to list what was actually looked at.
_PATH_ARGS = ("path",)

#: Where the recorded read list rides in the pass payload. Not a field
#: of `SubmittedSection` — the model must not be able to write
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
    house_style: str | None,
    cache: CacheStore | None = None,
    trace_dir: Path | None = None,
) -> explainer_schema.ExplainerDocument:
    """Write the prose for the call that owns `section_id`; persist it.

    The route is per-section and the call may not be: a POST to
    `intuition` or to `code` runs the one walkthrough pass and lands
    both. The whole document comes back either way, so a caller that
    does not know they are merged still sees everything that changed.

    Args:
        client: The LLM backend handle. Chooses the guidance carrier.
        run_dir: The run directory; must hold `augmented.scr.json` and a
            document for the run's current `(base_sha, head_sha)`.
        section_id: A fillable section id (`background`, `intuition`,
            `code`).
        model: The user-facing model string, for the cache key.
        house_style: The reviewed repo's house-style note, or None. No
            default, on the same terms as the skeleton's.
        cache: Optional response cache.
        trace_dir: Optional `trace/` directory for the call envelope.

    Returns:
        The whole document as it was persisted — the call's sections
        `ready`, and their figures reduced to the drawing vocabulary
        with the strip counts set.

    Raises:
        ExplainerNotReady: No sidecar, or no document to write prose
            into.
        SectionNotFound: No fillable section with that id.
        SectionNotReady: The call's anchored hunks are not all
            annotated.
        SectionFailed: The pass raised. Its sections are persisted
            `failed`; the document rides on the exception.
    """
    diff, doc = _load(run_dir)
    targets = pass_targets(doc, section_id)
    annotated, total = readiness(diff, [ref for s in targets for ref in s.refs])
    if annotated < total:
        raise SectionNotReady(annotated, total)

    # Lazy: keeps the overview formatter off the import path for callers
    # that only want the prompt assembly or the apply step.
    from .hunks import overview_to_prompt_json

    pass_id = targets[0].pass_id
    budget = max(0, DOCUMENT_TURN_BUDGET - doc.turns_used)
    with_tools = budget >= MIN_TOOL_TURNS
    guidance = _guidance(doc, targets, with_tools=with_tools, house_style=house_style)
    system_text, user_prefix = carry_guidance(client, guidance)
    user_text = format_prose_prompt(
        diff,
        doc,
        targets,
        overview_json=overview_to_prompt_json(diff, include_symbols=False),
    )
    if user_prefix:
        user_text = f"{user_prefix}\n\n{user_text}"

    spend = _Spend()
    try:
        payload, sources = await _run_prose_pass(
            client,
            run_dir=run_dir,
            diff=diff,
            pass_id=pass_id,
            base_sha=diff.pr.base_sha,
            guidance=guidance,
            system_text=system_text,
            user_text=user_text,
            model=model,
            budget=budget if with_tools else None,
            cache=cache,
            trace_dir=trace_dir,
            spend=spend,
        )
    except Exception as e:
        for section in targets:
            section.state = "failed"
        # Charged even though nothing landed: a loop that died at its
        # ceiling spent every request it made, and a budget a failing
        # pass can retry against for free is not a budget.
        doc.turns_used += spend.requests
        written = explainer_schema.save_explainer(run_dir, doc)
        log.exception("explainer pass %s failed", pass_id)
        raise SectionFailed(f"{type(e).__name__}: {e}", written.model_dump(mode="json")) from e

    doc.turns_used += spend.requests
    apply_prose_submission(
        doc,
        targets,
        ExplainerProseSubmission.model_validate(payload),
        ids=viewer_id_index(diff),
        sources=sources,
    )
    # The written document, not the one in hand: the figures are
    # sanitised and their strip counts set on the way to disk, and the
    # route fans this out as the frame every tab renders.
    return explainer_schema.save_explainer(run_dir, doc)


def _guidance(
    doc: explainer_schema.ExplainerDocument,
    targets: list[explainer_schema.Section],
    *,
    with_tools: bool,
    house_style: str | None,
) -> str:
    """The bulk guidance block for this call.

    One shared body so the two passes share a cacheable prefix on SDK
    backends, plus the blocks that only some calls earn: the tool
    vocabulary when there is budget to use it, the drawing vocabulary
    and this document's figure family, the reviewed repo's house-style
    note when it configured one, and Background's two-layer /
    skip-box / terms rules when it is the section being written. A pass
    with no budget is not told about tools at all — advertising a
    surface it cannot reach makes the model hedge, which is the same
    reason a document with no figure family is told nothing about
    figures.

    The document-wide blocks come before the section-specific one, so
    the two passes of one document share as long a prefix as they can.
    The house-style note is document-wide, so it sits with them rather
    than last; its standing does not depend on where it lands, because
    `format_house_style` states it outright.
    """
    blocks = [EXPLAINER_SECTION_GUIDANCE]
    if with_tools:
        blocks.append(EXPLAINER_TOOL_GUIDANCE)
    figures = prose_figure_guidance(doc)
    if figures:
        blocks.append(figures)
    if house_style is not None:
        blocks.append(format_house_style(house_style))
    if any(s.kind == "background" for s in targets):
        blocks.append(EXPLAINER_BACKGROUND_GUIDANCE)
    return "\n\n".join(blocks)


class _Spend:
    """Model requests one pass made, accumulated across grammar retries."""

    def __init__(self) -> None:
        self.requests = 0

    def charge(self, requests: int) -> None:
        self.requests += requests


async def _run_prose_pass(
    client: Client,
    *,
    run_dir: Path,
    diff: AnnotatedDiff,
    pass_id: str,
    base_sha: str,
    guidance: str,
    system_text: str,
    user_text: str,
    model: str,
    budget: int | None,
    cache: CacheStore | None,
    trace_dir: Path | None,
    spend: _Spend,
) -> tuple[dict[str, Any], list[str]]:
    """Drive one prose call; return `(payload, files_read)`.

    `budget` is the requests left to the whole document, or None when
    there are too few to fund a tool loop — in which case the pass runs
    seeded and tool-less under the backend's own ceiling.

    Background is keyed on `(base_sha, guidance)`: it describes the system
    before the change, so it is invariant across head movement and across
    prompt-iteration re-runs on the same branch, which is the reason it
    stays its own call. Every other pass is keyed on its assembled user
    text, which is strictly narrower than `(base_sha, head_sha)` — the
    seed carries the hunk intents, and prose written over a different set
    of them is different prose.

    `guidance` is in Background's key because `run_pass` folds in the
    *system* text, and on a subprocess backend the bulk guidance is not
    in the system text — `carry_guidance` puts it on stdin, where argv
    cannot take it. Without it the key is `(name, model, short-role,
    base_sha)`, so editing the prompt on the CLI path served the previous
    prose forever: the property that makes this cache worth having, that
    it outlives a moving head, also made it immune to the one input a
    reviewer iterating on the prompt is deliberately changing. The
    walkthrough pass keys on its user text, which already carries the
    guidance, so it was never affected.

    The reviewed repo's house-style note is part of `guidance`, so
    changing it moves Background's key for the same reason and needs no
    key of its own.
    """
    trace_path = (trace_dir / f"explainer-{pass_id}.json") if trace_dir is not None else None
    cache_inputs: tuple[Any, ...] = (base_sha, guidance) if pass_id == "background" else (user_text,)
    if budget is None:
        payload = await run_pass(
            PassMeta(name=f"explainer-{pass_id}", submit_tool="submit_explainer_prose"),
            client=client,
            agent=make_explainer_prose_agent(client.model, system_text),
            user_content=user_text,
            system=system_text,
            model=model,
            cache_inputs=cache_inputs,
            cache=cache,
            trace_path=trace_path,
            cache_request={"system": system_text, "user": user_text},
            on_requests=spend.charge,
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
                name=f"explainer-{pass_id}",
                submit_tool="submit_explainer_prose",
                tool_names=tuple(fn.__name__ for fn in TOOL_FUNCTIONS),
            ),
            client=client,
            agent=make_explainer_prose_agent(
                client.model,
                system_text,
                tools=_recording_tool_functions(recorder),
            ),
            user_content=user_text,
            system=system_text,
            model=model,
            cache_inputs=cache_inputs,
            deps=repo_tools,
            request_limit=budget,
            cache=cache,
            trace_path=trace_path,
            cache_request={"system": system_text, "user": user_text},
            # The read list is not the model's to submit, but it has to
            # ride the cache with the prose: a cached section whose
            # citation line came back empty would claim it read nothing.
            payload_extra=lambda: {_SOURCES_KEY: list(recorder.paths)},
            on_requests=spend.charge,
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
    "DOCUMENT_TURN_BUDGET",
    "MIN_TOOL_TURNS",
    "ExplainerProseSubmission",
    "SectionFailed",
    "SectionNotFound",
    "SectionNotReady",
    "SubmittedFigure",
    "SubmittedSection",
    "anchored_hunks",
    "apply_prose_submission",
    "find_section",
    "format_prose_prompt",
    "generate_explainer_section",
    "make_explainer_prose_agent",
    "pass_targets",
    "readiness",
]
