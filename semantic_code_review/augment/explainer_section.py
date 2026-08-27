"""Per-section prose for the change explainer (ADR 0007, slice 2).

The skeleton (`explainer.py`) writes the Map and leaves Background,
Intuition and Code `pending`. Each of those is filled by one structured
call from `POST /explainer/section/{id}`, on first open — a section
nobody opens is never paid for.

The pass is seeded and tool-less: the overview, the skeleton's fixed
decisions, and — for the files the skeleton assigned the section — every
hunk under them with its established intent. Connective tissue between
those intents is the thing forty independent per-hunk calls structurally
cannot produce. Background's tool grant is slice 3; until then it runs
seeded like its neighbours.

A section whose anchored hunks are not all annotated is refused rather
than written: prose built on half the intents reads exactly as fluently
as prose built on all of them. A pass that raises leaves its section
`failed` — retryable, and it must not poison its neighbours — with the
updated document attached so the route can fan the state out.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.output import ToolOutput

from ..cache.store import CacheStore
from ..viewer.build_json import ViewerIdIndex, viewer_id_index
from . import explainer_schema
from .agents import Client
from .explainer import ExplainerNotReady, carry_guidance
from .pass_ import PassMeta, run_pass
from .prompts import EXPLAINER_SECTION_BRIEFS, EXPLAINER_SECTION_GUIDANCE
from .schemas import AnnotatedDiff

log = logging.getLogger(__name__)


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
    toy_data: bool = Field(
        default=False,
        description="True when a worked example here uses invented identifiers, counts or values.",
    )


def make_explainer_section_agent(model: str | Model, system: str) -> Agent[None, ExplainerSectionSubmission]:
    """Agent for one prose section. No repo tools — the seed is the code
    it may speak about, and Background's grant is slice 3.
    """
    return Agent(
        model=model,
        output_type=ToolOutput(ExplainerSectionSubmission, name="submit_explainer_section"),
        instructions=system,
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
) -> None:
    """Fold a `submit_explainer_section` payload into `section`, in place.

    References the model invented are dropped and counted into the
    document's `dropped_refs`. A submission that narrows nothing leaves
    the skeleton's references standing — that is the section's assigned
    scope, not a missing value.
    """
    kept, dropped = _validate(submission.refs, ids)
    subsections, sub_dropped = _subsections(section, submission.subsections, ids)

    section.body = submission.body.strip()
    if kept:
        section.refs = kept
    section.subsections = subsections
    section.state = "ready"

    doc.dropped_refs += dropped + sub_dropped
    doc.toy_data = doc.toy_data or submission.toy_data


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

    system_text, user_prefix = carry_guidance(client, EXPLAINER_SECTION_GUIDANCE)
    user_text = format_section_prompt(
        diff,
        doc,
        section,
        overview_json=overview_to_prompt_json(diff, include_symbols=False),
    )
    if user_prefix:
        user_text = f"{user_prefix}\n\n{user_text}"

    meta = PassMeta(name=f"explainer-{section.kind}", submit_tool="submit_explainer_section")
    try:
        payload = await run_pass(
            meta,
            client=client,
            agent=make_explainer_section_agent(client.model, system_text),
            user_content=user_text,
            system=system_text,
            model=model,
            cache_inputs=(user_text,),
            cache=cache,
            trace_path=(trace_dir / f"explainer-{section.id}.json") if trace_dir is not None else None,
            cache_request={"system": system_text, "user": user_text},
        )
    except Exception as e:
        section.state = "failed"
        explainer_schema.save_explainer(run_dir, doc)
        log.exception("explainer section %s failed", section_id)
        raise SectionFailed(f"{type(e).__name__}: {e}", doc.model_dump(mode="json")) from e
    assert payload is not None  # `meta.swallow_errors` is false

    apply_section_submission(
        doc,
        section,
        ExplainerSectionSubmission.model_validate(payload),
        ids=viewer_id_index(diff),
    )
    explainer_schema.save_explainer(run_dir, doc)
    return doc


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
