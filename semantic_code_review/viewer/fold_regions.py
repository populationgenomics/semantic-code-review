"""Fold regions: where a file folds, from its structure (ADR 0008).

One implementation, over the whole file on both sides. A region is a
definition the language's tag query captures — class, function, method,
module-level constant — with its tree-sitter line range, or an
indentation stanza where no definition covers the lines. Both sides are
read: a deleted definition folds base lines. The regions ship on
`FileBlock.fold_regions`; the viewer hangs chevrons on whichever of a
region's rows it has rendered and derives nothing.

A region is addressed by 1-indexed line ranges into the worktree files,
the same address `/fold-summary` and `FoldDescription` use:

- `right` — post-image lines only, `right_start..right_end` into
  head/<path>. Unchanged regions and added definitions.
- `left` — pre-image lines only, into base/<path>. Deleted definitions.
- `both` — the region straddles changed content; both ranges are set and
  the summariser sees a diff of the two.

The address depends on the file text alone, not on how the diff's rows
are ordered, so `position_runs` cannot move it.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Iterator, Sequence
from typing import Any, Literal

from .. import structural
from ..augment.schemas import ParsedHunk
from . import hunk_layout

log = logging.getLogger(__name__)

FoldContext = Literal["right", "left", "both"]

#: How a column-0 block opens in the shipped grammars (Python, JavaScript,
#: TypeScript): a bracket or a colon. A module-level stanza in such a file
#: folds only when its opener ends this way.
_BLOCK_OPENERS = ("(", "[", "{", ":")


@dataclasses.dataclass(frozen=True)
class FoldRegion:
    """One foldable region of a file, as the viewer JSON carries it.

    The range for a side `context` does not cover is None. `has_changes`
    is true when a hunk changes a line inside the region on either side.
    `qualified_name` / `kind` name the definition the region is; both are
    None for an indentation stanza.
    """

    context: FoldContext
    right_start: int | None
    right_end: int | None
    left_start: int | None
    left_end: int | None
    has_changes: bool
    qualified_name: str | None
    kind: str | None

    @property
    def key(self) -> tuple[str, int, int, int, int]:
        """The address as `FoldDescription` stores it: an absent range is 0."""
        return (
            self.context,
            self.right_start or 0,
            self.right_end or 0,
            self.left_start or 0,
            self.left_end or 0,
        )

    def to_dict(self, summary: str) -> dict[str, Any]:
        return {**dataclasses.asdict(self), "summary": summary}


@dataclasses.dataclass(frozen=True)
class _SideRegion:
    """A foldable line range on one side, before the sides are paired."""

    start: int
    end: int
    qualified_name: str | None
    kind: str | None


@dataclasses.dataclass(frozen=True)
class _Slot:
    """One row of the whole-file row stream: its kind and lines, no text."""

    kind: str
    old_line: int | None
    new_line: int | None

    @property
    def changed(self) -> bool:
        return self.kind != "ctx"


def compute_fold_regions(
    hunks: Sequence[tuple[ParsedHunk, Sequence[hunk_layout.Row]]],
    head_lines: Sequence[str] | None,
    base_lines: Sequence[str] | None,
    head_symbols: Sequence[structural.Symbol],
    base_symbols: Sequence[structural.Symbol],
    *,
    has_grammar: bool,
    path: str = "",
) -> list[FoldRegion]:
    """Every fold region of one file, outermost first.

    Args:
        hunks: The file's hunks in order, each with its positioned rows
            (`hunk_layout.layout_hunk_rows`).
        head_lines: The post-image text split into lines, or None when
            the file has no post-image (deleted) or the worktree is
            unavailable; a side with no text yields no regions.
        base_lines: The pre-image text, likewise.
        head_symbols: The post-image `Symbol` forest (`structural`).
        base_symbols: The pre-image forest.
        has_grammar: Whether the file's language has a tree-sitter
            grammar. With one, the indentation fallback folds only
            column-0 stanzas outside every definition — the definitions
            are the structure inside them. Without one, indentation is
            the only structure and every level folds.
        path: The file, for the log line when a region cannot be placed.

    Returns:
        Regions sorted by their first row in the file's row stream, an
        enclosing region before the regions it encloses. A region whose
        lines the row stream does not reach — a dirty-tree head edited
        under review, so the text and the diff disagree — is dropped with
        a warning; the diff's own rows are unaffected.
    """
    stream = _row_stream(hunks, None if head_lines is None else len(head_lines))
    new_idx = {s.new_line: i for i, s in enumerate(stream) if s.new_line is not None}
    old_idx = {s.old_line: i for i, s in enumerate(stream) if s.old_line is not None}

    def rows_of(region: _SideRegion, idx: dict[int, int], side: str) -> tuple[int, int] | None:
        first, last = idx.get(region.start), idx.get(region.end)
        if first is None or last is None:
            log.warning(
                "%s: %s lines %d-%d are outside the diff's rows; region not folded",
                path,
                side,
                region.start,
                region.end,
            )
            return None
        return first, last

    base_by_first: dict[int, _SideRegion] = {}
    for b in _side_regions(base_lines, base_symbols, has_grammar):
        span = rows_of(b, old_idx, "base")
        if span is not None:
            base_by_first[span[0]] = b

    placed: list[tuple[int, int, FoldRegion]] = []  # (first row, last row, region)
    paired: set[int] = set()
    for h in _side_regions(head_lines, head_symbols, has_grammar):
        span = rows_of(h, new_idx, "head")
        if span is None:
            continue
        first, last = span
        b = base_by_first.get(first)
        if b is not None:
            paired.add(first)
            last = max(last, old_idx[b.end])
            has_changes = _any_changed(stream, first, last)
            placed.append(
                (
                    first,
                    last,
                    FoldRegion(
                        context="both" if has_changes else "right",
                        right_start=h.start,
                        right_end=h.end,
                        left_start=b.start if has_changes else None,
                        left_end=b.end if has_changes else None,
                        has_changes=has_changes,
                        qualified_name=h.qualified_name or b.qualified_name,
                        kind=h.kind if h.qualified_name else b.kind,
                    ),
                )
            )
            continue
        has_changes = _any_changed(stream, first, last)
        left = _line_span(stream, first, last, "old") if has_changes else None
        placed.append(
            (
                first,
                last,
                FoldRegion(
                    context="both" if left else "right",
                    right_start=h.start,
                    right_end=h.end,
                    left_start=left[0] if left else None,
                    left_end=left[1] if left else None,
                    has_changes=has_changes,
                    qualified_name=h.qualified_name,
                    kind=h.kind,
                ),
            )
        )
    for first, b in base_by_first.items():
        if first in paired:
            continue
        last = old_idx[b.end]
        if not _any_changed(stream, first, last):
            # Unchanged base lines are head lines too; the head side has
            # already said whether they fold.
            continue
        right = _line_span(stream, first, last, "new")
        placed.append(
            (
                first,
                last,
                FoldRegion(
                    context="both" if right else "left",
                    right_start=right[0] if right else None,
                    right_end=right[1] if right else None,
                    left_start=b.start,
                    left_end=b.end,
                    has_changes=True,
                    qualified_name=b.qualified_name,
                    kind=b.kind,
                ),
            )
        )

    placed.sort(key=lambda p: (p[0], -p[1]))
    out: list[FoldRegion] = []
    seen: set[tuple[int, int]] = set()
    for first, last, region in placed:
        if (first, last) in seen:
            continue  # two captures of one span (e.g. a const and the arrow function it holds)
        seen.add((first, last))
        out.append(region)
    return out


def _row_stream(
    hunks: Sequence[tuple[ParsedHunk, Sequence[hunk_layout.Row]]],
    head_line_count: int | None,
) -> list[_Slot]:
    """The file's rows in order: unchanged context between hunks, then each
    hunk's positioned rows. The same walk the viewer's collapsible regions
    do (`render._walkRegion`), so a region resolves to the same rows here
    and on screen.
    """
    out: list[_Slot] = []
    cn = 1
    co = 1

    def ctx_to(up_to: int) -> None:
        nonlocal cn, co
        while cn < up_to:
            out.append(_Slot("ctx", co, cn))
            co += 1
            cn += 1

    for parsed, rows in hunks:
        ctx_to(parsed.new_start)
        out.extend(_Slot(r.kind, r.old_line, r.new_line) for r in rows)
        cn = parsed.new_start + parsed.new_count
        co = parsed.old_start + parsed.old_count
    if head_line_count is not None:
        ctx_to(head_line_count + 1)
    return out


def _any_changed(stream: Sequence[_Slot], first: int, last: int) -> bool:
    return any(stream[i].changed for i in range(first, last + 1))


def _line_span(stream: Sequence[_Slot], first: int, last: int, side: Literal["old", "new"]) -> tuple[int, int] | None:
    """`(min, max)` line on `side` over the rows `first..last`, or None when
    no row there has that side.
    """
    lines = [
        line
        for i in range(first, last + 1)
        if (line := stream[i].old_line if side == "old" else stream[i].new_line) is not None
    ]
    return (min(lines), max(lines)) if lines else None


def _side_regions(
    lines: Sequence[str] | None,
    symbols: Sequence[structural.Symbol],
    has_grammar: bool,
) -> list[_SideRegion]:
    """One side's regions: every multi-line definition, plus the indentation
    stanzas no definition covers.
    """
    if lines is None:
        return []
    defs = list(_definitions(symbols))
    covered: set[int] = set()
    for d in defs:
        covered.update(range(d.start, d.end + 1))
    out = [d for d in defs if d.end > d.start]
    for start, end in indent_regions(lines):
        if any(line in covered for line in range(start, end + 1)):
            continue
        opener = lines[start - 1]
        if opener.lstrip().startswith(hunk_layout.ATTACHMENT_PREFIXES):
            continue  # a doc comment's ` *` lines are not a block of the `/**`
        if has_grammar and (hunk_layout.text_indent(opener) != 0 or not opener.rstrip().endswith(_BLOCK_OPENERS)):
            continue  # hanging-indent prose in a docstring is not a block
        out.append(_SideRegion(start, end, None, None))
    return out


def _definitions(symbols: Sequence[structural.Symbol]) -> Iterator[_SideRegion]:
    """Flatten a `Symbol` forest depth-first, enclosing before enclosed."""
    for s in symbols:
        yield _SideRegion(s.range.start_line, s.range.end_line, s.qualified_name, s.kind)
        yield from _definitions(s.children)


def indent_regions(lines: Sequence[str]) -> list[tuple[int, int]]:
    """`(start_line, end_line)` of every indentation stanza, 1-indexed.

    A stanza opens at a non-blank line whose next non-blank line is
    indented deeper and runs to the line before the next line indented at
    most as deep, less any trailing blank lines. Blank lines inside never
    close it.
    """
    indents = [hunk_layout.text_indent(t) for t in lines]
    non_blank = [i for i, ind in enumerate(indents) if ind != -1]
    out: list[tuple[int, int]] = []
    stack: list[tuple[int, int]] = []  # (indent, opener index)

    def close(opener: int, before: int) -> None:
        end = before - 1
        while end > opener and indents[end] == -1:
            end -= 1
        out.append((opener + 1, end + 1))

    for pos, i in enumerate(non_blank):
        ind = indents[i]
        while stack and stack[-1][0] >= ind:
            close(stack.pop()[1], i)
        if pos + 1 < len(non_blank) and indents[non_blank[pos + 1]] > ind:
            stack.append((ind, i))
    while stack:
        close(stack.pop()[1], len(indents))
    return out
