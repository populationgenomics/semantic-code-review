"""Pydantic models for the augmented-diff annotations.

These types are the canonical structured representation. `format.emit`
writes them out as an augmented unified diff; `format.parse` reads them
back. The same types flow into the viewer JSON bundle.
"""

from __future__ import annotations

from enum import Enum

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
    """One-line summary of the change inside an indent fold region.

    `new_start`/`new_count` target post-image lines the region covers.
    Emitted by the LLM for every fold region that contains changed text.
    """

    new_start: int
    new_count: int
    summary: str


class Hunk(BaseModel):
    header: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    body: str = ""
    intent: str = ""
    smells: list[Smell] = Field(default_factory=list)
    context: str = ""
    refs: list[Ref] = Field(default_factory=list)
    confidence: int | None = None
    line_notes: list[LineNote] = Field(default_factory=list)
    segments: list[Segment] = Field(default_factory=list)
    fold_descriptions: list[FoldDescription] = Field(default_factory=list)


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


class FilePatch(BaseModel):
    """One file's section of the unified diff plus its annotations."""

    path: str
    old_path: str | None = None
    diff_git_line: str
    extra_header_lines: list[str] = Field(default_factory=list)
    old_file_marker: str = ""
    new_file_marker: str = ""
    role: FileRole | None = None
    summary: str = ""
    lang: str | None = None
    symbols: FileSymbols | None = None
    hunks: list[Hunk] = Field(default_factory=list)


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


class PRInfo(BaseModel):
    pr_url: str
    base_sha: str
    head_sha: str
    model: str = ""


class AugmentedDiff(BaseModel):
    version: int = 1
    pr: PRInfo
    overview: Overview | None = None
    files: list[FilePatch] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Submission shapes — what the LLM is asked to emit.
#
# These models represent the wire format of `submit_overview` /
# `submit_annotations`. Their JSON schemas (via `model_json_schema`)
# replace the hand-written tool input_schemas in `prompts.py`, giving
# us one source of truth for "what we ask the model for" and "how we
# parse it back". A submission is a strict subset of the parsing-side
# state above: post-processing splits / merges / drops fields as
# needed in `apply_overview_to_diff` / `apply_hunk_annotations`.
# ---------------------------------------------------------------------------


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


class HunkAnnotations(BaseModel):
    """Wire format of `submit_annotations`. Consumed by `apply_hunk_annotations`."""

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
