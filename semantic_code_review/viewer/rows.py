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
