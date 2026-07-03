r"""Hunk → viewer block: row pairing, fold detection, output assembly.

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

`Row` and `FoldRegion` are module-private value types; callers consume the
shape returned by ``build_hunk_viewer_block`` (a JSON-friendly dict). The
``build_rows`` and ``compute_fold_regions`` functions remain public because
the augment-side hunk prompt also walks fold regions (it only reads
attributes off the returned values, never imports the type names).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from ..augment.schemas import AnnotatedHunk, ParsedHunk, Segment

_RowKind = Literal["ctx", "ins", "del", "pair"]


@dataclass
class _FoldRegion:
    """An indent-based fold region within a hunk's row sequence.

    `header_idx` is the row whose content opens the block; `body_start_idx`
    ..`body_end_idx` are the rows that fold up under the header.
    `context` picks the addressing scheme the viewer uses for /fold-summary:
      - "right": region has post-image lines only. right_start/right_end
        are 1-indexed line numbers in head/<path>.
      - "left": region has pre-image lines only (pure deletion).
        left_start/left_end are 1-indexed line numbers in base/<path>.
      - "both": region has lines on both sides (typically because it
        straddles changed content). Both range pairs populated.

    `has_changes` is true iff any row in [header_idx, body_end_idx]
    contributes a change (ins / del / pair).

    `qualified_name` / `kind` carry the identity of the definition the
    region snapped to (e.g. "Foo.bar" / "function"); both are None for an
    indentation-fallback region, which has no symbol behind it.
    """

    header_idx: int
    body_start_idx: int
    body_end_idx: int
    context: Literal["right", "left", "both"]
    right_start: int | None
    right_end: int | None
    left_start: int | None
    left_end: int | None
    has_changes: bool
    qualified_name: str | None
    kind: str | None


@dataclass
class _Row:
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


def build_rows(hunk: ParsedHunk) -> list[_Row]:
    rows: list[_Row] = []
    old_line = hunk.old_start
    new_line = hunk.new_start
    dels_buf: list[str] = []  # text of pending '-' lines (without marker)

    def flush_dels_as_solo() -> None:
        nonlocal old_line
        for text in dels_buf:
            rows.append(
                _Row(
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
                _Row(
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
                    _Row(
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
                    _Row(
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
                    _Row(
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


def _row_indent(row: _Row) -> int:
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


def _indent_raw_regions(rows: list[_Row]) -> list[tuple[int, int]]:
    """`(header_idx, body_end_idx)` pairs from indent structure alone.

    A region opens at a non-blank row whose next non-blank neighbour has
    deeper indent, and closes at the next row whose indent is <= its own.
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
    return raw


def _row_symbols(
    row: _Row,
    head_spans: list[dict[str, Any]],
    base_spans: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Definition spans enclosing `row`, outermost-first.

    A row is mapped by line number into one side's tree: `new_line` into
    the head spans (ctx / pair / ins rows), else `old_line` into the base
    spans (del-only rows). Returns the enclosing spans sorted shallow→deep
    so the innermost definition is last.
    """
    if row.new_line is not None:
        line, spans = row.new_line, head_spans
    elif row.old_line is not None:
        line, spans = row.old_line, base_spans
    else:
        return []
    enc = [s for s in spans if s["start_line"] <= line <= s["end_line"]]
    enc.sort(key=lambda s: s["depth"])
    return enc


def _symbol_raw_regions(
    rows: list[_Row],
    head_spans: list[dict[str, Any]],
    base_spans: list[dict[str, Any]],
) -> tuple[list[tuple[int, int, str | None, str | None]], set[int]]:
    """`(header_idx, body_end_idx, qualified_name, kind)` snapped to spans.

    Every definition with at least one present row becomes a region whose
    header is its first present row and whose body runs to its last present
    row — clamped to the rows in `rows` — carrying that definition's
    `qualified_name` and `kind`. Nested definitions nest because a row
    carries its whole enclosing chain. Also returns the set of row indices
    that fall inside any definition, so the caller can fall back to
    indentation folds for the uncovered runs.
    """
    runs: dict[str, list[int]] = {}  # qualified_name -> [first_idx, last_idx]
    kinds: dict[str, str] = {}  # qualified_name -> kind
    order: list[str] = []  # first-seen order, for determinism
    covered: set[int] = set()
    for i, row in enumerate(rows):
        for s in _row_symbols(row, head_spans, base_spans):
            covered.add(i)
            qn = s["qualified_name"]
            run = runs.get(qn)
            if run is None:
                runs[qn] = [i, i]
                kinds[qn] = s["kind"]
                order.append(qn)
            else:
                run[1] = i
    out: list[tuple[int, int, str | None, str | None]] = [(runs[qn][0], runs[qn][1], qn, kinds[qn]) for qn in order]
    return out, covered


def compute_fold_regions(
    rows: list[_Row],
    head_spans: list[dict[str, Any]] | None = None,
    base_spans: list[dict[str, Any]] | None = None,
) -> list[_FoldRegion]:
    """Return fold regions over the row sequence.

    With definition spans (`head_spans` / `base_spans`, the file's
    flattened per-side `fold_symbols`), regions snap to the innermost
    enclosing definition's boundaries; rows inside no definition fall back
    to indentation detection. With no spans — an unsupported language, an
    unavailable worktree — every region is indentation-based, byte-identical
    to the pre-symbol output. The algorithm mirrors the viewer's JS
    implementation so the line ranges line up deterministically.
    """
    head_spans = head_spans or []
    base_spans = base_spans or []
    # Uniform shape: (header_idx, body_end_idx, qualified_name|None, kind|None).
    # Indentation regions carry no symbol.
    raw: list[tuple[int, int, str | None, str | None]]
    if head_spans or base_spans:
        raw, covered = _symbol_raw_regions(rows, head_spans, base_spans)
        # Keep an indentation region only where no row it spans is already
        # covered by a definition — the snapped region owns that stretch.
        raw += [
            (h, e, None, None) for h, e in _indent_raw_regions(rows) if not any(j in covered for j in range(h, e + 1))
        ]
    else:
        raw = [(h, e, None, None) for h, e in _indent_raw_regions(rows)]

    regions: list[_FoldRegion] = []
    for header_idx, body_end, qualified_name, kind in sorted(
        raw,
        key=lambda r: (r[0], r[1]),
    ):
        body_start = header_idx + 1
        if body_start > body_end:
            continue
        has_changes = any(rows[j].kind in ("ins", "del", "pair") for j in range(header_idx, body_end + 1))
        right_start = _first_side_line(rows, header_idx, body_end, "right")
        right_end = _last_side_line(rows, header_idx, body_end, "right")
        left_start = _first_side_line(rows, header_idx, body_end, "left")
        left_end = _last_side_line(rows, header_idx, body_end, "left")
        # context picks the addressing axis(es). Pair regions (both
        # sides populated and the diff straddles changed content) are
        # addressed as "both" so the server can produce a diff-style
        # body. Right-only and left-only regions stay single-sided.
        if right_start is not None and left_start is not None and has_changes:
            context: Literal["right", "left", "both"] = "both"
        elif right_start is not None:
            context = "right"
        else:
            context = "left"
        regions.append(
            _FoldRegion(
                header_idx=header_idx,
                body_start_idx=body_start,
                body_end_idx=body_end,
                context=context,
                right_start=right_start,
                right_end=right_end,
                left_start=left_start,
                left_end=left_end,
                has_changes=has_changes,
                qualified_name=qualified_name,
                kind=kind,
            )
        )
    return regions


def _first_side_line(rows: list[_Row], start: int, end: int, side: str) -> int | None:
    """First non-None line number on the named side within the row
    span. `side` is "right" (post-image) or "left" (pre-image).
    """
    attr = "new_line" if side == "right" else "old_line"
    for i in range(start, end + 1):
        v = getattr(rows[i], attr)
        if v is not None:
            return v
    return None


def _last_side_line(rows: list[_Row], start: int, end: int, side: str) -> int | None:
    attr = "new_line" if side == "right" else "old_line"
    for i in range(end, start - 1, -1):
        v = getattr(rows[i], attr)
        if v is not None:
            return v
    return None


def build_hunk_viewer_block(
    h: AnnotatedHunk,
    file_idx: int,
    hunk_idx: int,
    head_spans: list[dict[str, Any]] | None = None,
    base_spans: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build one hunk's viewer-JSON block: rows, folds, segments, counts.

    `head_spans` / `base_spans` are the file's flattened `fold_symbols`
    for each side; passing them snaps folds to definition boundaries (and
    keeps the wire `fold_regions` addresses in lockstep with the viewer's
    client-side detector). Omitting them yields indentation-based folds.
    """
    hunk_id = f"H{file_idx}_{hunk_idx}"
    parsed = h.parsed
    ann = h.ann
    rows = build_rows(parsed)
    regions = compute_fold_regions(rows, head_spans, base_spans)
    # Index summaries by (context, ranges) so right/left/both descriptions
    # don't collide when a hunk has folds of multiple kinds.
    summary_by_key: dict[tuple[str, int, int, int, int], str] = {
        (fd.context, fd.right_start, fd.right_end, fd.left_start, fd.left_end): fd.summary
        for fd in ann.fold_descriptions
    }
    fold_region_blocks: list[dict[str, Any]] = []
    for reg in regions:
        key = (
            reg.context,
            reg.right_start or 0,
            reg.right_end or 0,
            reg.left_start or 0,
            reg.left_end or 0,
        )
        summary = summary_by_key.get(key, "")
        fold_region_blocks.append(
            {
                "header_idx": reg.header_idx,
                "body_start_idx": reg.body_start_idx,
                "body_end_idx": reg.body_end_idx,
                "context": reg.context,
                "right_start": reg.right_start,
                "right_end": reg.right_end,
                "left_start": reg.left_start,
                "left_end": reg.left_end,
                "has_changes": reg.has_changes,
                "qualified_name": reg.qualified_name,
                "kind": reg.kind,
                "summary": summary,
            }
        )
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
        "fold_regions": fold_region_blocks,
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
