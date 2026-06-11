"""Deterministic base→head symbol delta (ADR 0001).

The `Symbol` forest carries *where the code is*; this module carries
*what moved between two revisions*. Added / removed / modified are a
`qualified_name` set-diff over the flattened base and head forests:

  * added    — qualified name present on head only
  * removed   — present on base only
  * modified  — present on both, with a **differing range**

A same-line body edit that leaves every span identical is therefore
*not* flagged — by design (ADR 0001): the range is the deterministic
signal, and the LLM layer owns finer "what actually changed" meaning.

Each `ChangedSymbol` carries its `path` (the delta is diff-wide, across
files) and the span on its *live* side: head for added/modified, base
for removed.
"""

from __future__ import annotations

from typing import Iterable

from pydantic import BaseModel, Field

from .symbols import Symbol, SymbolRange


class ChangedSymbol(BaseModel):
    """One symbol that changed between base and head.

    Flat (the tree is flattened by `qualified_name` before diffing), so
    `children` is intentionally absent. `range` is the symbol's span on
    its live side — head for added/modified, base for removed.
    """

    path: str
    kind: str
    name: str
    qualified_name: str
    range: SymbolRange
    signature: str | None = None


class SymbolDelta(BaseModel):
    """The diff-wide structural delta: three `qualified_name` set-diff buckets."""

    added: list[ChangedSymbol] = Field(default_factory=list)
    removed: list[ChangedSymbol] = Field(default_factory=list)
    modified: list[ChangedSymbol] = Field(default_factory=list)


def flatten(symbols: list[Symbol]) -> dict[str, Symbol]:
    """Map `qualified_name → Symbol` over the whole forest, depth-first.

    Within a file `qualified_name` is unique (the dotted path through
    enclosing definitions), so collisions don't occur; insertion order
    is source order, which the diff buckets inherit.
    """
    out: dict[str, Symbol] = {}

    def walk(syms: list[Symbol]) -> None:
        for s in syms:
            out[s.qualified_name] = s
            walk(s.children)

    walk(symbols)
    return out


def _changed(path: str, sym: Symbol) -> ChangedSymbol:
    return ChangedSymbol(
        path=path,
        kind=sym.kind,
        name=sym.name,
        qualified_name=sym.qualified_name,
        range=sym.range,
        signature=sym.signature,
    )


def diff_file(path: str, base: list[Symbol], head: list[Symbol]) -> SymbolDelta:
    """Per-file `qualified_name` set-diff between two `Symbol` forests.

    An added file passes `base=[]`; a deleted file passes `head=[]`.
    """
    b = flatten(base)
    h = flatten(head)
    added = [_changed(path, h[q]) for q in h if q not in b]
    removed = [_changed(path, b[q]) for q in b if q not in h]
    modified = [
        _changed(path, h[q])
        for q in h
        if q in b and h[q].range != b[q].range
    ]
    return SymbolDelta(added=added, removed=removed, modified=modified)


def merge(deltas: Iterable[SymbolDelta]) -> SymbolDelta:
    """Concatenate per-file deltas into one diff-wide delta, order preserved."""
    out = SymbolDelta()
    for d in deltas:
        out.added.extend(d.added)
        out.removed.extend(d.removed)
        out.modified.extend(d.modified)
    return out
