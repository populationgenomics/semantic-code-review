r"""Hunk → viewer block: row pairing, run positioning, output assembly.

Each row carries old/new line numbers and the text to display on each side.
Consecutive `-` / `+` runs are paired positionally (sequential pairing, not
LCS). Leftover deletions within a run are emitted as solo `del` rows after
the paired rows; leftover additions as solo `ins` rows.

Row order within a hunk is then chosen, not inherited (ADR 0008): a run of
solo `ins` (or `del`) rows whose text repeats the context beside it can sit
at any position in that repeat and render the identical file, so
``position_runs`` places it where its seams fall on definition openers,
else blank lines. Line numbers and the text at each line are unaffected.

Row kinds:
  - ctx:  context line, identical text both sides.
  - pair: paired delete+insert, text may differ.
  - del:  deletion-only row (new side is an empty placeholder).
  - ins:  insertion-only row (old side is an empty placeholder).

The hunk body's "\ No newline at end of file" marker is silently dropped
for v1 rendering (it doesn't affect side-by-side layout).

Fold regions are not a hunk's concern: ``viewer.fold_regions`` computes
them over the whole file from every hunk's positioned rows.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from ..augment.schemas import AnnotatedHunk, ParsedHunk, Segment

_RowKind = Literal["ctx", "ins", "del", "pair"]


@dataclass
class Row:
    kind: _RowKind
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


def build_rows(hunk: ParsedHunk) -> list[Row]:
    rows: list[Row] = []
    old_line = hunk.old_start
    new_line = hunk.new_start
    dels_buf: list[str] = []  # text of pending '-' lines (without marker)

    def flush_dels_as_solo() -> None:
        nonlocal old_line
        for text in dels_buf:
            rows.append(
                Row(
                    kind="del",
                    old_line=old_line,
                    new_line=None,
                    old_text=text,
                    new_text="",
                )
            )
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
            rows.append(
                Row(
                    kind="ctx",
                    old_line=old_line,
                    new_line=new_line,
                    old_text=text,
                    new_text=text,
                )
            )
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
                rows.append(
                    Row(
                        kind="pair",
                        old_line=old_line,
                        new_line=new_line,
                        old_text=dels_buf[j],
                        new_text=adds[j],
                    )
                )
                old_line += 1
                new_line += 1
            for j in range(paired, len(dels_buf)):
                rows.append(
                    Row(
                        kind="del",
                        old_line=old_line,
                        new_line=None,
                        old_text=dels_buf[j],
                        new_text="",
                    )
                )
                old_line += 1
            for j in range(paired, len(adds)):
                rows.append(
                    Row(
                        kind="ins",
                        old_line=None,
                        new_line=new_line,
                        old_text="",
                        new_text=adds[j],
                    )
                )
                new_line += 1
            dels_buf = []
            continue

        # Unknown marker — skip.
        i += 1

    flush_dels_as_solo()
    return rows


# ---------------------------------------------------------------------------
# Run positioning
# ---------------------------------------------------------------------------

_Side = Literal["old", "new"]

#: Score of one cut (the seam between a run and its neighbour), highest
#: wins: the line below the cut opens a definition; either side is blank;
#: anything else.
_CUT_OPENER = 2
_CUT_BLANK = 1
_CUT_OTHER = 0


def slide_range(run: Sequence[str], above: Sequence[str], below: Sequence[str]) -> tuple[int, int]:
    """How far a run of changed lines can move without changing the file.

    `above` / `below` are the unchanged lines on either side of `run`, on
    the side of the diff the run lives on. The run can move up `k` lines
    exactly when each of the `k` lines above it equals the line `n` further
    on (`n = len(run)`) — for `k <= n` that is "the run's last `k` lines
    equal the `k` above it"; past `n` it means the context itself repeats
    with period `n`. Down is the mirror.

    Returns `(up, down)`, the maximal moves in each direction.

    Raises:
        ValueError: If `run` is empty.
    """
    n = len(run)
    if n == 0:
        raise ValueError("slide_range: empty run")
    s = [*above, *run, *below]
    a = len(above)
    up = 0
    while up < a and s[a - 1 - up] == s[a - 1 - up + n]:
        up += 1
    down = 0
    while down < len(below) and s[a + n + down] == s[a + down]:
        down += 1
    return up, down


def _side_line(row: Row, side: _Side) -> int | None:
    return row.new_line if side == "new" else row.old_line


def _side_text(row: Row, side: _Side) -> str:
    return row.new_text if side == "new" else row.old_text


def _set_side(row: Row, side: _Side, line: int | None, text: str) -> None:
    if side == "new":
        row.new_line, row.new_text = line, text
    else:
        row.old_line, row.old_text = line, text


def _side_lines(rows: Sequence[Row], side: _Side) -> dict[int, str]:
    """`line -> text` for every row present on `side`."""
    out: dict[int, str] = {}
    for r in rows:
        line = _side_line(r, side)
        if line is not None:
            out[line] = _side_text(r, side)
    return out


def _indent_openers(line_text: Mapping[int, str]) -> set[int]:
    """Lines that open an indented block: non-blank, next non-blank deeper.

    The indentation fallback for a side with no definition spans; the same
    predicate `_indent_raw_regions` opens a fold region on.
    """
    out: set[int] = set()
    next_indent = -1
    for line in sorted(line_text, reverse=True):
        ind = text_indent(line_text[line])
        if ind == -1:
            continue
        if next_indent > ind:
            out.add(line)
        next_indent = ind
    return out


#: How a line attached to the definition below it begins, in the shipped
#: grammars (Python, JavaScript, TypeScript): a comment or a decorator.
ATTACHMENT_PREFIXES = ("#", "@", "//", "/*", "*")


def _definition_edges(line_text: Mapping[int, str], spans: Sequence[Mapping[str, Any]]) -> set[int]:
    """Leading-edge line of every definition whose edge falls in `line_text`.

    A definition's edge is its span's `start_line` extended upward over the
    lines attached to it — decorators and doc comments — which the grammar's
    definition node excludes. A line is attached when it begins like one
    (`ATTACHMENT_PREFIXES`), is indented at least as deep as the
    definition, is not the start of any span, and lies inside no span as
    deep as or deeper than this one (so the previous sibling's body ends
    the walk; the enclosing class does not). Only the edge scores as an
    opener: a decorated definition's `def` line does not, since a run
    starting there would strand the decorator at the run's end, attached
    visually to whatever follows.
    """
    starts = {s["start_line"] for s in spans}
    out: set[int] = set()
    for span in spans:
        start = span["start_line"]
        if start in line_text:
            def_indent = text_indent(line_text[start])
        elif start - 1 in line_text:
            def_indent = 0  # definition just past the hunk: only its attachments are visible
        else:
            continue
        depth = span["depth"]
        edge = start
        while (above := edge - 1) in line_text:
            text = line_text[above]
            if not text.lstrip().startswith(ATTACHMENT_PREFIXES):
                break
            if text_indent(text) < def_indent or above in starts:
                break
            if any(s["depth"] >= depth and s["start_line"] <= above <= s["end_line"] for s in spans):
                break
            edge = above
        if edge in line_text:
            out.add(edge)
    return out


def _opener_lines(line_text: Mapping[int, str], spans: Sequence[Mapping[str, Any]] | None) -> set[int]:
    return _definition_edges(line_text, spans) if spans else _indent_openers(line_text)


def _cut_score(above: str | None, below_line: int | None, below: str | None, openers: set[int]) -> int:
    """Score the seam between two adjacent lines; `None` is a line the hunk
    does not carry.
    """
    if below_line is not None and below_line in openers:
        return _CUT_OPENER
    if (above is not None and text_indent(above) == -1) or (below is not None and text_indent(below) == -1):
        return _CUT_BLANK
    return _CUT_OTHER


def _best_shift(rows: Sequence[Row], start: int, end: int, side: _Side, openers: set[int]) -> int:
    """Signed shift (negative = up) that places `rows[start:end]` best.

    Candidates are every position in the run's slide range through the
    contiguous `ctx` rows around it. Each is scored by its two cuts — the
    seam above the run's first line and the seam below its last — summed,
    so a run that starts on a definition and ends before the next beats
    one that starts on a blank separator, which beats one that starts
    mid-body. Scoring the seam rather than the first line makes
    "entry then blank" and "blank then entry" the tie they are. Ties go to
    the smallest displacement — the differ's placement stands unless a
    better one exists — and, at equal displacement, upward.
    """
    a0 = start
    while a0 > 0 and rows[a0 - 1].kind == "ctx":
        a0 -= 1
    b1 = end
    while b1 < len(rows) and rows[b1].kind == "ctx":
        b1 += 1
    texts = [_side_text(r, side) for r in rows[a0:b1]]
    up, down = slide_range(texts[start - a0 : end - a0], texts[: start - a0], texts[end - a0 :])

    # The seams are scored against whatever row is adjacent, including the
    # non-ctx row that bounds the window, when it carries this side.
    def line_at(i: int) -> int | None:
        return _side_line(rows[i], side) if 0 <= i < len(rows) else None

    def text_at(i: int) -> str | None:
        return _side_text(rows[i], side) if line_at(i) is not None else None

    def score(k: int) -> int:
        first, after = start + k, end + k
        top = _cut_score(text_at(first - 1), line_at(first), text_at(first), openers)
        bottom = _cut_score(text_at(after - 1), line_at(after), text_at(after), openers)
        return top + bottom

    return max(range(-up, down + 1), key=lambda k: (score(k), -abs(k), k < 0))


def _shift_run(rows: list[Row], start: int, end: int, k: int, side: _Side) -> None:
    """Move the run `rows[start:end]` by `k` rows through its `ctx` neighbours.

    Every row in the affected window keeps its `side` line number and text;
    the other side's `(line, text)` pairs — carried by the `ctx` rows the
    run passes — re-thread in order onto the rows that become `ctx`.
    """
    n = end - start
    if k > 0:
        lo, hi, run_at = start, end + k, k
        passed = rows[end : end + k]
    else:
        lo, hi, run_at = start + k, end, 0
        passed = rows[start + k : start]
    kind: _RowKind = "ins" if side == "new" else "del"
    other: _Side = "old" if side == "new" else "new"
    out: list[Row] = []
    passed_iter = iter(passed)
    for p, row in enumerate(rows[lo:hi]):
        line, text = _side_line(row, side), _side_text(row, side)
        if run_at <= p < run_at + n:
            moved = Row(kind=kind, old_line=None, new_line=None, old_text="", new_text="")
        else:
            ctx = next(passed_iter)
            # Internal invariant: slide_range only admits equal lines.
            assert _side_text(ctx, other) == text
            moved = Row(kind="ctx", old_line=None, new_line=None, old_text=text, new_text=text)
            _set_side(moved, other, _side_line(ctx, other), text)
        _set_side(moved, side, line, text)
        out.append(moved)
    rows[lo:hi] = out


def position_runs(
    rows: Sequence[Row],
    head_spans: Sequence[Mapping[str, Any]] | None = None,
    base_spans: Sequence[Mapping[str, Any]] | None = None,
) -> list[Row]:
    """Place each run of solo `ins` / `del` rows at the best position in its
    content-neutral slide range (ADR 0008, "positioning follows the diff").

    A run moves only through the `ctx` rows adjacent to it, within the hunk.
    Runs of one row stay put: every position shows the same line. Definition
    openers come from `head_spans` (insertions) / `base_spans` (deletions),
    the file's flattened `fold_symbols`; a side with no spans scores openers
    by indentation instead.

    Returns a new row list. For every row, `(side, line) -> text` is
    unchanged; so is the count of each row kind.
    """
    rows = list(rows)
    openers: dict[_Side, set[int]] = {}
    i = 0
    while i < len(rows):
        kind = rows[i].kind
        if kind not in ("ins", "del"):
            i += 1
            continue
        j = i
        while j < len(rows) and rows[j].kind == kind:
            j += 1
        if j - i < 2:
            i = j
            continue
        side: _Side = "new" if kind == "ins" else "old"
        if side not in openers:
            spans = head_spans if side == "new" else base_spans
            openers[side] = _opener_lines(_side_lines(rows, side), spans)
        k = _best_shift(rows, i, j, side, openers[side])
        if k:
            _shift_run(rows, i, j, k, side)
        i = max(j, j + k)
    return rows


def text_indent(text: str) -> int:
    """Indent of a line in spaces (a tab counts 4); -1 for a blank line."""
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


def layout_hunk_rows(
    parsed: ParsedHunk,
    head_spans: Sequence[Mapping[str, Any]] | None = None,
    base_spans: Sequence[Mapping[str, Any]] | None = None,
) -> list[Row]:
    """The hunk's rows as the viewer shows them: paired, then positioned.

    `head_spans` / `base_spans` are the file's flattened definition spans
    per side, which `position_runs` scores seams against; omitted, seams
    score by indentation.
    """
    return position_runs(build_rows(parsed), head_spans, base_spans)


def build_hunk_viewer_block(h: AnnotatedHunk, file_idx: int, hunk_idx: int, rows: Sequence[Row]) -> dict[str, Any]:
    """Build one hunk's viewer-JSON block: rows, segments, counts.

    `rows` is `layout_hunk_rows(h.parsed, ...)`, taken as an argument so the
    caller can hand the same rows to the file's fold-region computation.
    """
    hunk_id = f"H{file_idx}_{hunk_idx}"
    parsed = h.parsed
    ann = h.ann
    body_lines = parsed.body.splitlines()
    adds = sum(1 for ln in body_lines if ln.startswith("+"))
    dels = sum(1 for ln in body_lines if ln.startswith("-"))
    return {
        "id": hunk_id,
        "header": parsed.header,
        "old_start": parsed.old_start,
        "old_count": parsed.old_count,
        "new_start": parsed.new_start,
        "new_count": parsed.new_count,
        "adds": adds,
        "dels": dels,
        "intent": ann.intent,
        "smells": [s.model_dump() for s in ann.smells],
        "confidence": ann.confidence,
        "context": ann.context,
        "refs": [r.model_dump() for r in ann.refs],
        "line_notes": [ln.model_dump() for ln in ann.line_notes],
        "segments": [_segment_block(s, hunk_id, si) for si, s in enumerate(ann.segments)],
        "rows": [r.to_dict() for r in rows],
    }


def _segment_block(s: Segment, parent_id: str, si: int) -> dict[str, Any]:
    return {
        "id": f"{parent_id}_S{si}",
        "new_start": s.new_start,
        "new_count": s.new_count,
        "intent": s.intent,
        "smells": [sm.model_dump() for sm in s.smells],
        "context": s.context,
        "refs": [r.model_dump() for r in s.refs],
    }
