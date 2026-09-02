"""Persisted shape of the change-explainer document (ADR 0007).

The document lives in `explainer.json` in the run directory: prose
*about* the change (Background, Intuition, Code and Map) carrying typed
references into the diff. It is deliberately not a field on
`AnnotatedDiff` — it references the annotation tree but is regenerable
section by section without touching it, and it is discarded wholesale
when `(base_sha, head_sha)` moves rather than re-anchored.

This module owns the models, the on-disk read/write, and reference
validation. The LLM pass that fills a document lives in
`explainer.py`.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterable, Iterator
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from .. import errors, paths
from . import explainer_figures

log = logging.getLogger(__name__)

#: Bump when a stored document stops being one this build renders
#: correctly — an incompatible shape, or a change to how its contents are
#: drawn. A document at a different version is discarded on load, the same
#: way a SHA mismatch is: it is cheap to regenerate and never worth
#: migrating. Checked before the document is parsed, so a shape the
#: current models reject is discarded rather than raising
#: :class:`ExplainerCorrupt`.
DOCUMENT_VERSION = 3


RefKind = Literal["file", "hunk"]
SectionKind = Literal["background", "intuition", "code", "map"]
SectionState = Literal["pending", "ready", "failed"]
Verdict = Literal["narrate", "not_warranted"]

#: A top-level section's id *is* its kind, so a cross-section pointer
#: (the Background skip box's `target_section_id`) resolves without the
#: model having to invent stable names. Model-chosen subsections under
#: Code mint their own ids.
SECTION_TITLES: dict[SectionKind, str] = {
    "background": "Background",
    "intuition": "Intuition",
    "code": "Code",
    "map": "Map",
}

#: The Map's pass. Written by the skeleton, so it is not addressable by
#: the per-section prose route.
SKELETON_PASS = "skeleton"

#: Which prose sections one call writes, in document order. The section
#: taxonomy and the call structure are separate axes: the four sections
#: are what a reader navigates, a pass is what gets paid for.
#:
#: Background is its own call because it is keyed on `base_sha` alone —
#: it describes the system before the change, so it survives head
#: movement and every prompt-iteration re-run on the branch. Merging it
#: with anything would collapse it to the narrower `(base_sha,
#: head_sha)` key. Intuition and Code share that narrower key already,
#: are the two that most need to agree with each other, and re-paying
#: the large seed twice is the dominant cost on backends where the user
#: prompt is not cached.
PROSE_PASSES: tuple[tuple[str, tuple[SectionKind, ...]], ...] = (
    ("background", ("background",)),
    ("walkthrough", ("intuition", "code")),
)


def prose_kinds() -> tuple[SectionKind, ...]:
    """Every prose section kind, in document order."""
    return tuple(kind for _, kinds in PROSE_PASSES for kind in kinds)


def pass_for_kind(kind: SectionKind) -> str:
    """The id of the call that writes sections of this kind.

    Raises:
        KeyError: `kind` is the Map, which the skeleton writes.
    """
    for pass_id, kinds in PROSE_PASSES:
        if kind in kinds:
            return pass_id
    raise KeyError(kind)


def kinds_in_pass(pass_id: str) -> tuple[SectionKind, ...]:
    """The section kinds one prose call writes.

    Raises:
        KeyError: No prose pass with that id.
    """
    for candidate, kinds in PROSE_PASSES:
        if candidate == pass_id:
            return kinds
    raise KeyError(pass_id)


class Reference(BaseModel):
    """A pointer from the document into the diff.

    Addresses a whole file (`F<i>`) or a whole hunk (`H<fi>_<hi>`) —
    never a line range. Ids are position-derived and so valid only
    within one build of one diff, which is safe because the document
    itself does not outlive `(base_sha, head_sha)`.
    """

    kind: RefKind
    id: str


class MapRow(BaseModel):
    """One step of the reading order: a file, and why it is read here."""

    ref: Reference
    why: str


class Term(BaseModel):
    """One entry of a section's term list."""

    term: str
    definition: str


class SkipBox(BaseModel):
    """Background's escape hatch for a reader who knows the system.

    Background is written in two layers — ground for a newcomer, then
    what the change lands on — and this is what lets the second reader
    past the first layer. `target_section_id` is validated against the
    document's own sections when the box is folded in; an unresolvable
    jump is dropped rather than rendered dead.
    """

    body: str
    target_section_id: str


class Figure(BaseModel):
    """A diagram in a structured slot: inline SVG, a caption, and alt text.

    `svg` on a persisted document has been through
    :func:`explainer_figures.sanitize_svg`, so it carries geometry and
    vocabulary classes only. `stripped` is what that removed — a figure
    that lost content is kept and says so, rather than vanishing.

    `alt` is required: a figure nobody can read without seeing it is
    not an accessible document, and the renderer makes it the SVG's
    `aria-label`.
    """

    svg: str
    alt: str
    caption: str = ""
    stripped: int = 0


class Section(BaseModel):
    """One section of the document.

    `state` is the generation state of this section's prose, not of the
    document: the skeleton writes every prose section `pending` and each
    prose call flips the sections it writes to `ready` (or `failed`,
    which is retryable and must not poison its neighbours).

    `pass_id` is which call writes this section — see `PROSE_PASSES`.
    Sections do not map one-to-one onto calls, so it is carried on the
    document rather than re-derived: the viewer needs it to keep one
    press from buying one call twice, and a reader needs it to know
    which prose a citation line accounts for. A subsection carries its
    parent's.
    """

    id: str
    kind: SectionKind
    title: str
    pass_id: str
    state: SectionState = "pending"
    body: str = ""
    refs: list[Reference] = Field(default_factory=list)
    map_rows: list[MapRow] = Field(default_factory=list)
    terms: list[Term] = Field(default_factory=list)
    skip_box: SkipBox | None = None
    #: Repo paths the section's pass actually opened, in first-read
    #: order — recorded from the tool surface, never from the model's
    #: account of itself. Rendered as a citation line: a section citing
    #: no reads is one that made it up, and that is legible without
    #: judging the prose. It is the *pass's* read list, so every section
    #: one call wrote carries the same one and the viewer renders it
    #: once per pass.
    sources: list[str] = Field(default_factory=list)
    figures: list[Figure] = Field(default_factory=list)
    subsections: list[Section] = Field(default_factory=list)


class ExplainerDocument(BaseModel):
    """The whole change-explainer document, as persisted.

    `figure_family` and `cast` are fixed once by the skeleton and handed
    to every later prose call, so the document's figures read as one
    visual language rather than three that each invented their own.
    """

    version: int = DOCUMENT_VERSION
    base_sha: str
    head_sha: str
    verdict: Verdict
    #: Why the verdict. Rendered as the document body when the verdict
    #: is `not_warranted` — that case is a real answer ("three renames,
    #: read them directly"), not an empty state.
    verdict_note: str = ""
    figure_family: str = ""
    cast: list[str] = Field(default_factory=list)
    #: Set when any section's worked examples use invented identifiers,
    #: counts or values; the footer says so.
    toy_data: bool = False
    #: Model requests the document's prose passes have spent between
    #: them, against the shared budget in `explainer_section`. Persisted
    #: because the passes are separated in time — a reload, a second tab
    #: or a restart must not re-grant a budget that has already been
    #: spent — and because a ceiling nobody can see is one nobody can
    #: tune. A cache hit spends nothing and adds nothing.
    turns_used: int = 0
    sections: list[Section] = Field(default_factory=list)
    #: References the model emitted that addressed no file or hunk in
    #: this diff. Dropped, counted here, and rendered — thinning
    #: references unnoticed is the failure this count exists to prevent.
    dropped_refs: int = 0


class ExplainerCorrupt(errors.ScrError):
    """`explainer.json` is on disk but is not a readable document.

    Distinct from the SHA-mismatch and version-mismatch cases, which
    discard the document quietly (with a log line) because they are
    expected outcomes of a moving diff. Corruption is not expected, so
    it is raised rather than swallowed.
    """


def load_explainer(
    run_dir: paths.RunDir,
    *,
    base_sha: str,
    head_sha: str,
) -> ExplainerDocument | None:
    """Load the run's document, or None when there isn't a usable one.

    Args:
        run_dir: The run directory.
        base_sha: The run's base SHA, as the document must have been
            built against.
        head_sha: The run's head SHA, likewise.

    Returns:
        The document, or None when the file is absent, was written for
        a different `(base_sha, head_sha)`, or carries a different
        `version`.

    Raises:
        ExplainerCorrupt: The file exists but is not valid JSON or does
            not parse as a document.
    """
    path = run_dir.explainer
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ExplainerCorrupt(f"{path} is not readable JSON: {e}") from e
    # Before parsing, not after: an older version is a *different* shape,
    # so validating it first would report an expected outcome of a bumped
    # version as corruption.
    version = raw.get("version") if isinstance(raw, dict) else None
    if version != DOCUMENT_VERSION:
        log.warning(
            "%s was written at document version %r (want %d) — discarded",
            path,
            version,
            DOCUMENT_VERSION,
        )
        return None
    try:
        doc = ExplainerDocument.model_validate(raw)
    except ValidationError as e:
        raise ExplainerCorrupt(f"{path} does not parse as an explainer document: {e}") from e
    if (doc.base_sha, doc.head_sha) != (base_sha, head_sha):
        log.info(
            "%s describes %s..%s but the run is %s..%s — discarded",
            path,
            doc.base_sha[:8],
            doc.head_sha[:8],
            base_sha[:8],
            head_sha[:8],
        )
        return None
    return doc


def sanitize_figures(doc: ExplainerDocument) -> ExplainerDocument:
    """Reduce every figure to the drawing vocabulary; record what went.

    Applied on the way to disk rather than at each call site, so every
    route that writes a document — the skeleton, each prose section —
    gets the guarantee without having to remember it. The renderer
    sanitises again; see `explainer_figures`.

    A document is written once per prose call, so a figure written by
    the first call is sanitised again by every later one. The second
    pass finds nothing left to remove, which is why the count is the
    highest any pass recorded rather than the last one's: assigning it
    would erase what the write that removed it counted.

    Returns:
        A copy of the document with each figure's `svg` sanitised and
        its `stripped` count set. The input is not mutated.
    """
    out = doc.model_copy(deep=True)
    for section in _walk(out.sections):
        for i, figure in enumerate(section.figures):
            clean = explainer_figures.sanitize_svg(figure.svg, namespace=f"{_id_slug(section.id)}-{i}")
            figure.svg = clean.svg
            figure.stripped = max(figure.stripped, clean.stripped)
    return out


def _walk(sections: Iterable[Section]) -> Iterator[Section]:
    for section in sections:
        yield section
        yield from _walk(section.subsections)


def _id_slug(section_id: str) -> str:
    """A section id reduced to something usable as an SVG id prefix.

    Subsection ids are model-chosen, so they can carry anything.
    """
    slug = re.sub(r"[^A-Za-z0-9_-]", "-", section_id).strip("-")
    return slug if slug and slug[0].isalpha() else f"s-{slug}"


def save_explainer(run_dir: paths.RunDir, doc: ExplainerDocument) -> ExplainerDocument:
    """Sanitise the document's figures, write it atomically, hand it back.

    Returns the document as written, not the one passed in: the strip
    counts are set here, and the caller fans the same bytes out over
    the SSE bus that the next `GET /explainer` will serve.
    """
    clean = sanitize_figures(doc)
    path = run_dir.explainer
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(
        json.dumps(clean.model_dump(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(tmp, path)
    return clean


def iter_sections(doc: ExplainerDocument) -> Iterator[Section]:
    """Yield every section of the document, subsections after parents."""

    def walk(sections: Iterable[Section]) -> Iterator[Section]:
        for section in sections:
            yield section
            yield from walk(section.subsections)

    yield from walk(doc.sections)


def find_section(doc: ExplainerDocument, section_id: str) -> Section | None:
    """Find a section by id anywhere in the tree, or None.

    Top-level ids are the section kind; model-chosen subsections under
    Code mint their own, so the lookup has to descend.
    """
    for section in iter_sections(doc):
        if section.id == section_id:
            return section
    return None


def figures_fixed(doc: ExplainerDocument) -> bool:
    """Whether the skeleton fixed a figure family, so figures are on.

    The one predicate the two halves of the figure route share: the
    prose guidance carries the drawing vocabulary only when it can also
    say what the shapes mean in *this* change, and a figure submitted
    without one is dropped rather than rendered. With no family there is
    nothing keeping two passes from drawing the same component two ways,
    and a figure drawn outside the vocabulary is one the sanitiser
    reduces to unpainted geometry.
    """
    return bool(doc.figure_family.strip())


def validate_references(
    refs: Iterable[Reference],
    *,
    file_ids: frozenset[str],
    hunk_ids: frozenset[str],
) -> tuple[list[Reference], int]:
    """Keep the references that address something; count the rest.

    Validation is membership in the viewer's two id maps — no AST work,
    no resolution. Aborting a document because one reference of forty
    is invented would discard the thirty-nine good ones and the prose
    with them, so the bad ones are dropped and counted instead.

    Args:
        refs: The references as the model emitted them.
        file_ids: Every `F<i>` in this build of the diff.
        hunk_ids: Every `H<fi>_<hi>` in this build of the diff.

    Returns:
        `(kept, dropped_count)`, `kept` in the input order.
    """
    kept: list[Reference] = []
    dropped = 0
    for ref in refs:
        valid = ref.id in (file_ids if ref.kind == "file" else hunk_ids)
        if valid:
            kept.append(ref)
        else:
            dropped += 1
            log.warning("explainer: %s reference %r addresses nothing — dropped", ref.kind, ref.id)
    return kept, dropped


__all__ = [
    "DOCUMENT_VERSION",
    "PROSE_PASSES",
    "SECTION_TITLES",
    "SKELETON_PASS",
    "ExplainerCorrupt",
    "ExplainerDocument",
    "Figure",
    "MapRow",
    "Reference",
    "Section",
    "SkipBox",
    "Term",
    "figures_fixed",
    "find_section",
    "iter_sections",
    "kinds_in_pass",
    "load_explainer",
    "pass_for_kind",
    "prose_kinds",
    "sanitize_figures",
    "save_explainer",
    "validate_references",
]
