"""The normalized `Symbol` tree — the structural layer's single currency.

Introduced here once and never re-shaped (ADR 0001): every consumer
(the `outline` tool, the overview seed, the sidebar Symbols axis) reads
this exact shape. Produced deterministically from tree-sitter; carries
no LLM-derived meaning.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SymbolRange(BaseModel):
    """Source span of a definition.

    Lines are 1-indexed inclusive (matching the rest of the codebase —
    `FoldDescription`, hunk addressing). Columns are 0-indexed; `end_col`
    is exclusive, mirroring tree-sitter's end point.
    """

    start_line: int
    end_line: int
    start_col: int
    end_col: int


class Symbol(BaseModel):
    """One definition (class / function / constant / …) and its nesting.

    `qualified_name` is the dotted path through enclosing definitions
    (e.g. `Bar.method.inner`). `signature` carries the literally-declared
    type text where the source has it (params, return/variable
    annotations), `None` otherwise. `children` nests by source
    containment.
    """

    kind: str
    name: str
    qualified_name: str
    range: SymbolRange
    signature: str | None = None
    children: list["Symbol"] = Field(default_factory=list)
