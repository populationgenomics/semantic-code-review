r"""Build a side-by-side row stream from a unified-diff hunk body.

Each row carries old/new line numbers and the text to display on each side.
Consecutive `-` / `+` runs are paired positionally (sequential pairing, not
LCS). Leftover deletions within a run are emitted as solo `del` rows after
the paired rows; leftover additions as solo `ins` rows.

Row kinds:
  - ctx:  context line, identical text both sides.
  - pair: paired delete+insert, text may differ.
  - del:  deletion-only row (new side is an empty placeholder).
  - ins:  insertion-only row (old side is an empty placeholder).

The hunk body's "\ No newline at end of file" marker is silently dropped
for v1 rendering (it doesn't affect side-by-side layout).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..augment.schemas import Hunk


RowKind = Literal["ctx", "ins", "del", "pair"]


@dataclass
class FoldRegion:
    """An indent-based fold region within a hunk's row sequence.

    `header_idx` is the row whose content opens the block (the `def foo():`
    row for a function body, for example). `body_start_idx`..`body_end_idx`
    are the rows that fold up under the header. `new_start`/`new_end` are
    post-image line numbers for the whole region (header + body).
    `has_changes` is true iff any row in [header_idx, body_end_idx]
    contributes a change (ins / del / pair).
    """

    header_idx: int
    body_start_idx: int
    body_end_idx: int
    new_start: int | None
    new_end: int | None
    has_changes: bool


@dataclass
class Row:
    kind: RowKind
    old_line: int | None
    new_line: int | None
    old_text: str
    new_text: str

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "old_line": self.old_line,
            "new_line": self.new_line,
            "old_text": self.old_text,
            "new_text": self.new_text,
        }


def build_rows(hunk: Hunk) -> list[Row]:
    rows: list[Row] = []
    old_line = hunk.old_start
    new_line = hunk.new_start
    dels_buf: list[str] = []  # text of pending '-' lines (without marker)

    def flush_dels_as_solo() -> None:
        nonlocal old_line
        for text in dels_buf:
            rows.append(Row(
                kind="del", old_line=old_line, new_line=None,
                old_text=text, new_text="",
            ))
            old_line += 1
        dels_buf.clear()

    body_lines = hunk.body.splitlines()
    i = 0
    while i < len(body_lines):
        line = body_lines[i]

        if line.startswith("\\"):
            # "\ No newline at end of file" — drop silently.
            i += 1
            continue

        if line == "" or line.startswith(" "):
            flush_dels_as_solo()
            text = "" if line == "" else line[1:]
            rows.append(Row(
                kind="ctx", old_line=old_line, new_line=new_line,
                old_text=text, new_text=text,
            ))
            old_line += 1
            new_line += 1
            i += 1
            continue

        if line.startswith("-"):
            dels_buf.append(line[1:])
            i += 1
            continue

        if line.startswith("+"):
            # Collect the full '+' run, then pair with any buffered dels.
            adds: list[str] = []
            while i < len(body_lines) and body_lines[i].startswith("+"):
                adds.append(body_lines[i][1:])
                i += 1
            paired = min(len(dels_buf), len(adds))
            for j in range(paired):
                rows.append(Row(
                    kind="pair", old_line=old_line, new_line=new_line,
                    old_text=dels_buf[j], new_text=adds[j],
                ))
                old_line += 1
                new_line += 1
            for j in range(paired, len(dels_buf)):
                rows.append(Row(
                    kind="del", old_line=old_line, new_line=None,
                    old_text=dels_buf[j], new_text="",
                ))
                old_line += 1
            for j in range(paired, len(adds)):
                rows.append(Row(
                    kind="ins", old_line=None, new_line=new_line,
                    old_text="", new_text=adds[j],
                ))
                new_line += 1
            dels_buf = []
            continue

        # Unknown marker — skip.
        i += 1

    flush_dels_as_solo()
    return rows


def _row_indent(row: Row) -> int:
    """Indent level (in spaces; tab = 4). -1 means blank/whitespace-only."""
    text = row.old_text if row.kind == "del" else row.new_text
    if not text or not text.strip():
        return -1
    ind = 0
    for ch in text:
        if ch == " ":
            ind += 1
        elif ch == "\t":
            ind += 4
        else:
            break
    return ind


def compute_fold_regions(rows: list[Row]) -> list[FoldRegion]:
    """Return indent-based fold regions over the row sequence.

    A region opens at a non-blank row whose next non-blank neighbour has
    deeper indent, and closes at the next row whose indent is <= its own.
    The algorithm matches the viewer's JS implementation so the line ranges
    line up deterministically.
    """
    indents = [_row_indent(r) for r in rows]

    def next_non_blank(i: int) -> int | None:
        for j in range(i + 1, len(indents)):
            if indents[j] != -1:
                return indents[j]
        return None

    raw: list[tuple[int, int]] = []  # (header_idx, body_end_idx) in close order
    stack: list[tuple[int, int]] = []  # (indent, header_idx)
    for i, ind in enumerate(indents):
        if ind == -1:
            continue
        while stack and stack[-1][0] >= ind:
            top_ind, top_idx = stack.pop()
            raw.append((top_idx, i - 1))
        ni = next_non_blank(i)
        if ni is not None and ni > ind:
            stack.append((ind, i))
    while stack:
        top_ind, top_idx = stack.pop()
        raw.append((top_idx, len(indents) - 1))

    # Convert to FoldRegion records, ordered by header_idx ascending.
    regions: list[FoldRegion] = []
    for header_idx, body_end in sorted(raw):
        body_start = header_idx + 1
        if body_start > body_end:
            continue
        has_changes = any(
            rows[j].kind in ("ins", "del", "pair")
            for j in range(header_idx, body_end + 1)
        )
        new_start = _first_new_line(rows, header_idx, body_end)
        new_end = _last_new_line(rows, header_idx, body_end)
        regions.append(FoldRegion(
            header_idx=header_idx,
            body_start_idx=body_start,
            body_end_idx=body_end,
            new_start=new_start,
            new_end=new_end,
            has_changes=has_changes,
        ))
    return regions


def _first_new_line(rows: list[Row], start: int, end: int) -> int | None:
    for i in range(start, end + 1):
        if rows[i].new_line is not None:
            return rows[i].new_line
    return None


def _last_new_line(rows: list[Row], start: int, end: int) -> int | None:
    for i in range(end, start - 1, -1):
        if rows[i].new_line is not None:
            return rows[i].new_line
    return None
