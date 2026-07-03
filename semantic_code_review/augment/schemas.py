"""Pydantic models for the augmented-diff annotations.

These types are the canonical structured representation. `format.emit`
writes them out as an augmented unified diff; `format.parse` reads them
back. The same types flow into the viewer JSON bundle.

Two stages exist as distinct types:

- `ParsedDiff` / `ParsedFile` / `ParsedHunk` — what comes out of parsing
  a raw git diff: structure only, no annotations.
- `AnnotatedDiff` / `AnnotatedFile` / `AnnotatedHunk` — pairs the parsed
  structure with annotation payloads (`Overview | SkippedOverview` at the
  diff root, `FileAnnotations` per file, `HunkAnnotations` per hunk).

`emit_augmented_diff` requires the annotated form. The augment pipeline
takes a `ParsedDiff` and produces an `AnnotatedDiff` via pure functions.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Severity(str, Enum):
    INFO = "info"
    MINOR = "minor"
    MAJOR = "major"
    CRITICAL = "critical"


class SmellDef(BaseModel):
    """Entry in the closed smell vocabulary (plan §3.4)."""

    tag: str
    label: str
    severity: Severity
    color: str


SMELL_CATALOGUE: dict[str, SmellDef] = {
    d.tag: d
    for d in (
        SmellDef(tag="duplication", label="Duplicated logic", severity=Severity.MINOR, color="#888"),
        SmellDef(tag="string-sql", label="SQL built by string", severity=Severity.MAJOR, color="#e60"),
        SmellDef(tag="no-input-validation", label="No input validation", severity=Severity.MAJOR, color="#e60"),
        SmellDef(tag="missing-test", label="No test coverage", severity=Severity.MAJOR, color="#e60"),
        SmellDef(tag="security-sensitive", label="Security-sensitive", severity=Severity.CRITICAL, color="#c33"),
        SmellDef(tag="performance-regression", label="Performance regression", severity=Severity.MAJOR, color="#e60"),
        SmellDef(tag="backward-incompatible", label="Backward-incompatible", severity=Severity.CRITICAL, color="#c33"),
        SmellDef(tag="todo-left-behind", label="TODO left in code", severity=Severity.INFO, color="#678"),
        SmellDef(tag="dead-code", label="Dead code", severity=Severity.MINOR, color="#888"),
        SmellDef(tag="unscoped-exception", label="Broad exception handler", severity=Severity.MINOR, color="#888"),
        SmellDef(tag="resource-leak", label="Resource leak", severity=Severity.MAJOR, color="#e60"),
        SmellDef(tag="race-condition", label="Race condition", severity=Severity.CRITICAL, color="#c33"),
    )
}

SMELL_TAGS: frozenset[str] = frozenset(SMELL_CATALOGUE)
# Insertion-ordered comma-separated list, suitable for embedding in
# prompt text and JSON-schema descriptions. SMELL_TAGS (frozenset) is
# the right thing for membership checks; SMELL_TAGS_TEXT is the right
# thing for "tell the model the closed vocabulary".
SMELL_TAGS_TEXT: str = ", ".join(SMELL_CATALOGUE.keys())


class Smell(BaseModel):
    tag: str = Field(description=f"One of: {SMELL_TAGS_TEXT}")
    note: str = ""


class Ref(BaseModel):
    path: str
    line: int
    reason: str = ""


class LineNote(BaseModel):
    line: int
    body: str


class Segment(BaseModel):
    new_start: int
    new_count: int
    intent: str = ""
    smells: list[Smell] = Field(default_factory=list)
    context: str = ""
    refs: list[Ref] = Field(default_factory=list)


class FoldDescription(BaseModel):
    """One-line summary of the body inside an indent fold region.

    Addressed by 1-indexed line ranges into the *files* the diff
    relates — never into the hunk's row sequence — so the
    representation is stable across re-renders and lets folds span
    expanded context as well as hunk bodies.

    `context` picks which side(s) the fold covers:
      - "right": post-image lines only (the common case — describe
        what the new code does). Address with right_start/right_end
        as 1-indexed line numbers in head/<path>.
      - "left": pre-image lines only (a pure deletion fold).
        Address with left_start/left_end in base/<path>.
      - "both": fold straddles changed content. Both ranges populated.

    Generated lazily by the review server's `/fold-summary` route
    the first time the reviewer collapses a region; cached so
    subsequent reviews skip the call.
    """

    context: Literal["right", "left", "both"] = "right"
    right_start: int = 0
    right_end: int = 0
    left_start: int = 0
    left_end: int = 0
    summary: str


class FileRole(str, Enum):
    NEW = "new"
    DELETED = "deleted"
    RENAMED = "renamed"
    MODIFIED = "modified"
    BINARY = "binary"
    GENERATED = "generated"


class FileSymbols(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    added: list[str] = Field(default_factory=list)
    modified: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)


class OverviewSymbol(BaseModel):
    path: str
    kind: str
    name: str


class OverviewEdge(BaseModel):
    src: str = Field(alias="from")
    dst: str = Field(alias="to")

    model_config = ConfigDict(populate_by_name=True)


class OverviewGroupMember(BaseModel):
    """A hunk-address the LLM returned as part of a semantic group.

    `path` is the file's post-image path; `hunk_index` is the 0-based
    offset into that file's hunk list. Invalid references (path not
    present, or index out of range) are dropped at parse time by
    `apply_overview_to_diff`.
    """

    path: str
    hunk_index: int


class OverviewGroup(BaseModel):
    """A cluster of hunks the LLM believes share a purpose.

    Hunks may appear in multiple groups (overlap is expected — one
    hunk can serve two themes) and need not cover every hunk in the
    diff. The viewer renders each group as a sidebar entry; clicking
    one filters the visible hunks to that group's members.
    """

    title: str
    rationale: str = ""
    members: list[OverviewGroupMember] = Field(default_factory=list)


class Overview(BaseModel):
    summary: str = ""
    symbols_added: list[OverviewSymbol] = Field(default_factory=list)
    symbols_modified: list[OverviewSymbol] = Field(default_factory=list)
    symbols_removed: list[OverviewSymbol] = Field(default_factory=list)
    callgraph_edges: list[OverviewEdge] = Field(default_factory=list)
    themes: list[str] = Field(default_factory=list)
    groups: list[OverviewGroup] = Field(default_factory=list)


class SkippedOverview(BaseModel):
    """Sentinel for `AnnotatedDiff.overview` when the overview pass was
    not run (or was skipped via `--no-overview`).

    Distinct from `Overview()` (the pass ran and produced empty fields):
    the viewer can render "no overview was generated" differently from
    "the overview is intentionally empty".
    """


class PRInfo(BaseModel):
    pr_url: str
    base_sha: str
    head_sha: str
    model: str = ""


# ---------------------------------------------------------------------------
# Stage 1 — Parsed structure (no annotations).
# ---------------------------------------------------------------------------


class ParsedHunk(BaseModel):
    """One hunk's structural state as it came off the diff stream."""

    header: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    body: str = ""


class ParsedFile(BaseModel):
    """One file's section of the unified diff (no annotations)."""

    path: str
    old_path: str | None = None
    diff_git_line: str
    extra_header_lines: list[str] = Field(default_factory=list)
    old_file_marker: str = ""
    new_file_marker: str = ""
    hunks: list[ParsedHunk] = Field(default_factory=list)


class ParsedDiff(BaseModel):
    version: int = 1
    pr: PRInfo
    files: list[ParsedFile] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Annotation payloads — what the LLM emits per pass.
#
# `OverviewSubmission` and `HunkAnnotations` are the wire format of
# `submit_overview` / `submit_annotations`. Their JSON schemas (via
# `model_json_schema`) replace the hand-written tool input_schemas in
# `prompts.py`, giving us one source of truth for "what we ask the model
# for" and "how we parse it back".
# ---------------------------------------------------------------------------


class HunkAnnotations(BaseModel):
    """Wire format of `submit_annotations` and the per-hunk annotation
    block carried on `AnnotatedHunk.ann`.
    """

    intent: str = Field(description="1-2 sentences of MOTIVE, not mechanics.")
    segments: list[Segment] = Field(
        default_factory=list,
        description=(
            "Split the hunk into semantically distinct edits when present "
            "(e.g. a refactor plus an unrelated fix). Each segment carries "
            "post-image new_start/new_count and its own intent. Omit if "
            "the hunk is single-intent."
        ),
    )
    smells: list[Smell] = Field(default_factory=list)
    context: str = Field(
        default="",
        description="Cross-file dependencies the reviewer can't see from the diff.",
    )
    refs: list[Ref] = Field(default_factory=list)
    confidence: int | None = Field(default=None, ge=0, le=100)
    line_notes: list[LineNote] = Field(default_factory=list)
    fold_descriptions: list[FoldDescription] = Field(
        default_factory=list,
        description=(
            "One short sentence per indent fold region containing changes. "
            "Match each region's new_start/new_count exactly."
        ),
    )


class FileAnnotations(BaseModel):
    """Per-file annotations carried on `AnnotatedFile.ann`."""

    role: FileRole | None = None
    summary: str = ""
    lang: str | None = None
    symbols: FileSymbols | None = None


class OverviewFileSubmission(BaseModel):
    """Per-file annotations the LLM submits as part of the overview pass."""

    path: str
    summary: str = Field(default="", description="One sentence per file.")
    lang: str | None = Field(default=None, description="Only when the extension is ambiguous.")
    symbols: FileSymbols | None = None


class OverviewSubmission(BaseModel):
    """Wire format of `submit_overview`. Consumed by `apply_overview_to_diff`."""

    summary: str = Field(description="1-3 sentence summary of the PR's intent.")
    symbols_added: list[OverviewSymbol] = Field(default_factory=list)
    symbols_modified: list[OverviewSymbol] = Field(default_factory=list)
    symbols_removed: list[OverviewSymbol] = Field(default_factory=list)
    callgraph_edges: list[OverviewEdge] = Field(
        default_factory=list,
        description="Introduced or modified calls (best-effort — omit if unsure).",
    )
    themes: list[str] = Field(
        default_factory=list,
        description="Short keyword tags (e.g. 'pagination', 'api-surface').",
    )
    files: list[OverviewFileSubmission] = Field(
        default_factory=list,
        description="Per-file summaries; one entry per changed file in the diff.",
    )
    groups: list[OverviewGroup] = Field(
        default_factory=list,
        description=(
            "Semantic clusters of hunks the reviewer can filter by. Each "
            "hunk may appear in 0+ groups — overlap is expected when a hunk "
            "serves multiple purposes. Aim for 2–6 groups on a typical PR "
            "(more for large ones). A group need not cover every hunk; "
            "leave genuinely standalone hunks out."
        ),
    )


# ---------------------------------------------------------------------------
# Stage 2 — Annotated form (parsed structure + annotation payloads).
# ---------------------------------------------------------------------------


def _empty_hunk_annotations() -> HunkAnnotations:
    return HunkAnnotations(intent="")


class AnnotatedHunk(BaseModel):
    """A parsed hunk paired with its annotation payload."""

    parsed: ParsedHunk
    ann: HunkAnnotations = Field(default_factory=_empty_hunk_annotations)


class AnnotatedFile(BaseModel):
    """A parsed file paired with its annotation payload and annotated hunks.

    Structural fields are flat (mirroring `ParsedFile`) so callers can
    write `f.path` rather than `f.parsed.path`. The hunks list carries
    `AnnotatedHunk` items, which themselves wrap a `ParsedHunk`.
    """

    path: str
    old_path: str | None = None
    diff_git_line: str
    extra_header_lines: list[str] = Field(default_factory=list)
    old_file_marker: str = ""
    new_file_marker: str = ""
    ann: FileAnnotations = Field(default_factory=FileAnnotations)
    hunks: list[AnnotatedHunk] = Field(default_factory=list)


class AnnotatedDiff(BaseModel):
    version: int = 1
    pr: PRInfo
    overview: Overview | SkippedOverview = Field(default_factory=SkippedOverview)
    files: list[AnnotatedFile] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Lifting helpers — wrap parsed values in annotated form with empty defaults.
# ---------------------------------------------------------------------------


def lift_hunk(parsed: ParsedHunk, ann: HunkAnnotations | None = None) -> AnnotatedHunk:
    return AnnotatedHunk(parsed=parsed, ann=ann or _empty_hunk_annotations())


def lift_file(
    parsed: ParsedFile,
    *,
    ann: FileAnnotations | None = None,
    hunks: list[AnnotatedHunk] | None = None,
) -> AnnotatedFile:
    return AnnotatedFile(
        path=parsed.path,
        old_path=parsed.old_path,
        diff_git_line=parsed.diff_git_line,
        extra_header_lines=list(parsed.extra_header_lines),
        old_file_marker=parsed.old_file_marker,
        new_file_marker=parsed.new_file_marker,
        ann=ann or FileAnnotations(),
        hunks=hunks if hunks is not None else [lift_hunk(h) for h in parsed.hunks],
    )


def lift_diff(parsed: ParsedDiff) -> AnnotatedDiff:
    """Lift a `ParsedDiff` into an `AnnotatedDiff` with empty annotations."""
    return AnnotatedDiff(
        version=parsed.version,
        pr=parsed.pr,
        overview=SkippedOverview(),
        files=[lift_file(f) for f in parsed.files],
    )
