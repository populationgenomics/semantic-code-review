"""Run positioning: the slide range, the cut scoring, and the invariants
every positioned hunk keeps (ADR 0008, slice 2)."""

from __future__ import annotations

import collections
import json
import os
import pathlib
import textwrap

import pytest

from semantic_code_review.augment.schemas import ParsedHunk
from semantic_code_review.viewer import hunk_layout
from semantic_code_review.viewer.hunk_layout import _Row, build_rows, position_runs, slide_range

_CASES_PATH = pathlib.Path(__file__).parent / "fixtures" / "run_positioning_cases.json"
_CASES = json.loads(_CASES_PATH.read_text(encoding="utf-8"))


def _hunk(body: str, *, old_start: int = 1, new_start: int = 1) -> ParsedHunk:
    """A hunk from a dedented body; counts are derived from the body."""
    body = textwrap.dedent(body).removeprefix("\n")
    lines = body.splitlines()
    old_count = sum(1 for ln in lines if not ln.startswith("+"))
    new_count = sum(1 for ln in lines if not ln.startswith("-"))
    return ParsedHunk(
        header=f"@@ -{old_start},{old_count} +{new_start},{new_count} @@",
        old_start=old_start,
        old_count=old_count,
        new_start=new_start,
        new_count=new_count,
        body=body,
    )


def _side_map(rows: list[_Row], side: str) -> dict[int, str]:
    attr_line, attr_text = f"{side}_line", f"{side}_text"
    return {getattr(r, attr_line): getattr(r, attr_text) for r in rows if getattr(r, attr_line) is not None}


def _runs(rows: list[_Row]) -> list[tuple[str, int, list[str]]]:
    """`(kind, start_idx, texts)` for every run of solo ins / del rows."""
    out: list[tuple[str, int, list[str]]] = []
    i = 0
    while i < len(rows):
        kind = rows[i].kind
        if kind not in ("ins", "del"):
            i += 1
            continue
        j = i
        while j < len(rows) and rows[j].kind == kind:
            j += 1
        side = "new" if kind == "ins" else "old"
        out.append((kind, i, [getattr(r, f"{side}_text") for r in rows[i:j]]))
        i = j
    return out


def _assert_invariants(before: list[_Row], after: list[_Row]) -> None:
    """What positioning may never change: the text at every (side, line),
    and how many rows of each kind the hunk has."""
    for side in ("old", "new"):
        assert _side_map(before, side) == _side_map(after, side)
    assert collections.Counter(r.kind for r in before) == collections.Counter(r.kind for r in after)
    assert len(before) == len(after)


def _spans(*defs: tuple[str, int, int, int]) -> list[dict]:
    """`(qualified_name, start_line, end_line, depth)` → flattened spans."""
    return [
        {"qualified_name": qn, "start_line": s, "end_line": e, "depth": d, "kind": "function"} for qn, s, e, d in defs
    ]


# --- slide_range ------------------------------------------------------------


def test_slide_range_zero_when_nothing_repeats() -> None:
    assert slide_range(["x", "y"], ["a", "b"], ["c", "d"]) == (0, 0)


def test_slide_range_up_when_tail_matches_context_above() -> None:
    # run's last two lines equal the two above it
    assert slide_range(["z", "a", "b"], ["q", "a", "b"], ["r"]) == (2, 0)


def test_slide_range_down_when_head_matches_context_below() -> None:
    assert slide_range(["a", "b", "z"], ["q"], ["a", "b", "r"]) == (0, 2)


def test_slide_range_both_ways() -> None:
    # ... a b | a b | a b ...   — every position renders the same file
    assert slide_range(["a", "b"], ["x", "a", "b"], ["a", "b", "y"]) == (2, 2)


def test_slide_range_whole_run_repeats_above() -> None:
    assert slide_range(["a", "b", "c"], ["a", "b", "c"], []) == (3, 0)


def test_slide_range_exceeds_run_length_when_context_is_periodic() -> None:
    # two blank lines inserted into a stretch of four: any of five positions
    assert slide_range(["", ""], ["x", "", "", ""], ["", "y"]) == (3, 1)


def test_slide_range_bounded_by_context_available() -> None:
    assert slide_range(["a", "a"], ["a"], ["a", "a", "a", "a"]) == (1, 4)


def test_slide_range_rejects_empty_run() -> None:
    with pytest.raises(ValueError, match="empty run"):
        slide_range([], ["a"], ["b"])


# --- position_runs: constructed cases ----------------------------------------

# The motivating shape: a new function added after one whose body ends the
# same way, rendered by the differ from the shared tail.
_MID_BODY = """
     def old_fn():
         setup()
    +    return cleanup()
    +
    +def new_fn():
    +    other()
         return cleanup()

     def third():
"""


def test_mid_body_insertion_moves_to_the_definition_by_indentation() -> None:
    before = build_rows(_hunk(_MID_BODY))
    after = position_runs(before)
    _assert_invariants(before, after)
    assert _runs(after) == [("ins", 4, ["def new_fn():", "    other()", "    return cleanup()", ""])]
    assert [r.kind for r in after] == ["ctx", "ctx", "ctx", "ctx", "ins", "ins", "ins", "ins", "ctx"]
    # the run kept the line numbers of the rows it now occupies
    assert [r.new_line for r in after if r.kind == "ins"] == [5, 6, 7, 8]


def test_mid_body_insertion_moves_to_the_definition_by_spans() -> None:
    spans = _spans(("old_fn", 1, 3, 0), ("new_fn", 5, 7, 0), ("third", 9, 9, 0))
    before = build_rows(_hunk(_MID_BODY))
    after = position_runs(before, head_spans=spans, base_spans=[])
    assert _runs(after)[0][2][0] == "def new_fn():"


def test_decorated_definition_starts_at_the_decorator_not_the_def() -> None:
    """A run may start on `def g` (a symbol start) or on its `@property`;
    only the decorator is the definition's edge. Starting at `def` would
    strand the decorator at the end of the run, above the next method."""
    body = """
     class C:
         @property
         def f(self):
             return 1

         @property
    +    def g(self):
    +        return 2
    +
    +    @property
         def h(self):
             return 3
    """
    spans = _spans(("C", 1, 12, 0), ("C.f", 3, 4, 1), ("C.g", 7, 8, 1), ("C.h", 11, 12, 1))
    before = build_rows(_hunk(body))
    after = position_runs(before, head_spans=spans, base_spans=[])
    _assert_invariants(before, after)
    assert _runs(after) == [("ins", 5, ["    @property", "    def g(self):", "        return 2", ""])]


def test_doc_comment_is_the_edge_of_a_typescript_function() -> None:
    body = """
     }

     /** @internal */
    +function f() {}
    +
    +/** @internal */
     function g() {}
    """
    spans = _spans(("f", 4, 4, 0), ("g", 7, 7, 0))
    before = build_rows(_hunk(body))
    after = position_runs(before, head_spans=spans, base_spans=[])
    assert _runs(after) == [("ins", 2, ["/** @internal */", "function f() {}", ""])]


def test_plain_statements_above_a_definition_are_not_its_edge() -> None:
    """Only comment / decorator lines attach; `const x = ...` above a nested
    arrow function is a statement of the enclosing body, not an edge."""
    line_text = {1: "function outer() {", 2: "  const a = 1;", 3: "  const b = 2;", 4: "  const f = () => {", 5: "  };"}
    spans = _spans(("outer", 1, 6, 0), ("outer.f", 4, 5, 1))
    assert hunk_layout._definition_edges(line_text, spans) == {1, 4}


def test_deletion_run_mirrors_insertion() -> None:
    body = """
     def old_fn():
         setup()
    -    return cleanup()
    -
    -def gone():
    -    other()
         return cleanup()

     def third():
    """
    before = build_rows(_hunk(body))
    after = position_runs(before)
    _assert_invariants(before, after)
    assert _runs(after) == [("del", 4, ["def gone():", "    other()", "    return cleanup()", ""])]
    assert [r.old_line for r in after if r.kind == "del"] == [5, 6, 7, 8]
    # the context rows that moved above the run keep their old lines and
    # took the new lines of the positions they now occupy
    assert [(r.old_line, r.new_line) for r in after if r.kind == "ctx"] == [(1, 1), (2, 2), (3, 3), (4, 4), (9, 5)]


def test_single_row_run_stays_put() -> None:
    body = """
     a

    +
     b
    """
    before = build_rows(_hunk(body))
    assert position_runs(before) == before


def test_replacement_tail_cannot_slide_up_through_the_pair() -> None:
    """A run moves only through context rows. Here the pair's new text would
    let the tail slide up onto a definition seam; the pair is a wall, so the
    run takes the one context row below it instead."""
    body = """
    -x
    +def f:
    +    y
    +def f:
         y
    """
    before = build_rows(_hunk(body))
    assert [r.kind for r in before] == ["pair", "ins", "ins", "ctx"]
    after = position_runs(before)
    _assert_invariants(before, after)
    assert [r.kind for r in after] == ["pair", "ctx", "ins", "ins"]
    assert after[0] == before[0]
    assert _runs(after) == [("ins", 2, ["def f:", "    y"])]


def test_entry_then_blank_is_not_flipped_to_blank_then_entry() -> None:
    """Both positions cut at the same blank/entry seam; the differ's stays."""
    body = """
     [a]

    +[b]
    +
     [c]

    """
    before = build_rows(_hunk(body))
    assert position_runs(before) == before


def test_blank_seam_beats_mid_statement_seam() -> None:
    body = """
         x = 1
    +    y = 2
    +
    +    x = 1
         z = 3
    """
    before = build_rows(_hunk(body))
    after = position_runs(before)
    # Original seams: (x = 1 | y = 2) and (x = 1 | z = 3), both mid-statement.
    # One up: (top of hunk | x = 1) and (blank | x = 1) — a blank seam wins.
    assert _runs(after) == [("ins", 0, ["    x = 1", "    y = 2", ""])]


def test_tie_at_equal_displacement_goes_up() -> None:
    """Period-3 content where the differ's position cuts mid-pattern and the
    two nearest alternatives score equally: the upward one is chosen."""
    body = """

     x
    +y
    +
    +x
     y

    """
    before = build_rows(_hunk(body))
    after = position_runs(before)
    _assert_invariants(before, after)
    assert _runs(after) == [("ins", 1, ["x", "y", ""])]


def test_run_may_slide_into_the_context_before_the_next_run() -> None:
    """The context row between two runs is available to the first; taking it
    merges the runs, and every invariant still holds."""
    body = """
     a
    +
    +b

    +def x:
    +    y
     e
    """
    before = build_rows(_hunk(body))
    after = position_runs(before)
    _assert_invariants(before, after)
    assert _runs(after) == [("ins", 2, ["b", "", "def x:", "    y"])]


def test_viewer_block_rows_are_positioned() -> None:
    from semantic_code_review.augment.schemas import AnnotatedHunk

    block = hunk_layout.build_hunk_viewer_block(AnnotatedHunk(parsed=_hunk(_MID_BODY)), 0, 0)
    kinds = [r["kind"] for r in block["rows"]]
    assert kinds == ["ctx", "ctx", "ctx", "ctx", "ins", "ins", "ins", "ins", "ctx"]
    assert block["adds"] == 4 and block["dels"] == 0


# --- fixture: this repo's hunks from the cached corpus ------------------------


@pytest.mark.parametrize("case", _CASES, ids=lambda c: c["name"])
def test_corpus_fixture(case: dict) -> None:
    """Hunks the ADR's mid-body detection flags, and hunks a run moves in.
    Moved hunks land where the fixture says; the rest are untouched."""
    parsed = ParsedHunk(**{k: case[k] for k in ("header", "old_start", "old_count", "new_start", "new_count", "body")})
    before = build_rows(parsed)
    after = position_runs(before, case["head_spans"], case["base_spans"])
    _assert_invariants(before, after)
    if not case["moved"]:
        assert after == before
        return
    assert after != before
    got = [{"kind": k, "start_idx": i, "texts": t} for k, i, t in _runs(after) if len(t) >= 2]
    assert got == case["expect_runs"]


def test_fixture_covers_both_outcomes() -> None:
    assert any(c["moved"] for c in _CASES) and any(not c["moved"] for c in _CASES)


# --- optional: the whole cached corpus ---------------------------------------

_CORPUS = pathlib.Path.home() / ".cache" / "scr" / "runs"


@pytest.mark.skipif(
    not (os.environ.get("SCR_CORPUS") == "1" and _CORPUS.is_dir()),
    reason="set SCR_CORPUS=1 with a populated ~/.cache/scr/runs to sweep the corpus",
)
def test_corpus_sweep_invariants() -> None:
    """Every hunk in every cached run: the invariants hold, and a hunk with
    no run of two or more solo rows is byte-identical."""
    from semantic_code_review import structural
    from semantic_code_review.format import parse as fparse
    from semantic_code_review.viewer import build_json

    checked = 0
    for raw in sorted(_CORPUS.glob("*/*/raw.diff")):
        run_dir = raw.parent
        diff = fparse.parse_raw_diff(raw.read_text(encoding="utf-8", errors="replace"))
        for f in diff.files:
            lang = structural.language_for_path(f.path)
            spans: dict[str, list[dict]] = {"head": [], "base": []}
            if lang is not None:
                for side, rel in (("head", f.path), ("base", f.old_path or f.path)):
                    try:
                        src = (run_dir / side / rel).read_text(encoding="utf-8")
                    except (OSError, UnicodeDecodeError):
                        continue
                    spans[side] = build_json._fold_spans(structural.outline_symbols(src, lang))
            for h in f.hunks:
                before = build_rows(h)
                after = position_runs(before, spans["head"], spans["base"])
                _assert_invariants(before, after)
                if not any(len(t) >= 2 for _, _, t in _runs(before)):
                    assert after == before, (raw, f.path, h.header)
                checked += 1
    assert checked > 0
