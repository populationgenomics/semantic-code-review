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


class Smell(BaseModel):
    tag: str
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
