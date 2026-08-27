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
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from . import explainer_figures

log = logging.getLogger(__name__)

EXPLAINER_FILENAME = "explainer.json"

#: Bump when the persisted shape changes incompatibly. A document at a
#: different version is discarded on load, the same way a SHA mismatch
#: is — the document is cheap to regenerate and never worth migrating.
DOCUMENT_VERSION = 1


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
    prose call flips one to `ready` (or `failed`, which is retryable and
    must not poison its neighbours).
    """

    id: str
    kind: SectionKind
    title: str
    state: SectionState = "pending"
    body: str = ""
    refs: list[Reference] = Field(default_factory=list)
    map_rows: list[MapRow] = Field(default_factory=list)
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
    sections: list[Section] = Field(default_factory=list)
    #: References the model emitted that addressed no file or hunk in
    #: this diff. Dropped, counted here, and rendered — thinning
    #: references unnoticed is the failure this count exists to prevent.
    dropped_refs: int = 0


class ExplainerCorrupt(RuntimeError):
    """`explainer.json` is on disk but is not a readable document.

    Distinct from the SHA-mismatch and version-mismatch cases, which
    discard the document quietly (with a log line) because they are
    expected outcomes of a moving diff. Corruption is not expected, so
    it is raised rather than swallowed.
    """


def explainer_path(run_dir: Path) -> Path:
    return run_dir / EXPLAINER_FILENAME


def load_explainer(
    run_dir: Path,
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
    path = explainer_path(run_dir)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ExplainerCorrupt(f"{path} is not readable JSON: {e}") from e
    try:
        doc = ExplainerDocument.model_validate(raw)
    except ValidationError as e:
        raise ExplainerCorrupt(f"{path} does not parse as an explainer document: {e}") from e
    if doc.version != DOCUMENT_VERSION:
        log.warning(
            "%s was written at document version %d (want %d) — discarded",
            path,
            doc.version,
            DOCUMENT_VERSION,
        )
        return None
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

    Returns:
        A copy of the document with each figure's `svg` sanitised and
        its `stripped` count set. The input is not mutated.
    """
    out = doc.model_copy(deep=True)
    for section in _walk(out.sections):
        for i, figure in enumerate(section.figures):
            clean = explainer_figures.sanitize_svg(figure.svg, namespace=f"{_id_slug(section.id)}-{i}")
            figure.svg = clean.svg
            figure.stripped = clean.stripped
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


def save_explainer(run_dir: Path, doc: ExplainerDocument) -> ExplainerDocument:
    """Sanitise the document's figures, write it atomically, hand it back.

    Returns the document as written, not the one passed in: the strip
    counts are set here, and the caller fans the same bytes out over
    the SSE bus that the next `GET /explainer` will serve.
    """
    clean = sanitize_figures(doc)
    path = explainer_path(run_dir)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(
        json.dumps(clean.model_dump(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(tmp, path)
    return clean


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
    "EXPLAINER_FILENAME",
    "SECTION_TITLES",
    "ExplainerCorrupt",
    "ExplainerDocument",
    "Figure",
    "MapRow",
    "Reference",
    "Section",
    "explainer_path",
    "load_explainer",
    "sanitize_figures",
    "save_explainer",
    "validate_references",
]
