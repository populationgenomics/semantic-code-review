"""Row pairing: context, solo changes, paired runs, imbalanced pairs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from semantic_code_review.augment.schemas import ParsedHunk
from semantic_code_review.viewer.hunk_layout import (
    _Row, build_rows, compute_fold_regions,
)

# Shared cross-language lockstep fixture: the same (rows, spans) input
# drives this pytest case and the vitest case in tests/js/folds.test.ts;
# both detectors must produce the regions baked here.
_FOLD_CASES_PATH = Path(__file__).parent / "fixtures" / "fold_regions_cases.json"
_FOLD_REGION_KEYS = (
    "header_idx", "body_start_idx", "body_end_idx", "context",
    "right_start", "right_end", "left_start", "left_end",
    "qualified_name", "kind",
)


def _hunk(body: str, *, old_start: int = 1, old_count: int = 1, new_start: int = 1, new_count: int = 1) -> ParsedHunk:
    return ParsedHunk(
        header=f"@@ -{old_start},{old_count} +{new_start},{new_count} @@",
        old_start=old_start, old_count=old_count,
        new_start=new_start, new_count=new_count,
        body=body,
    )


def test_all_context() -> None:
    rows = build_rows(_hunk(" a\n b\n c\n", old_count=3, new_count=3))
    assert [r.kind for r in rows] == ["ctx", "ctx", "ctx"]
    assert [r.old_line for r in rows] == [1, 2, 3]
    assert [r.new_line for r in rows] == [1, 2, 3]
    assert [r.old_text for r in rows] == ["a", "b", "c"]


def test_balanced_replace_pairs() -> None:
    body = "-old1\n-old2\n+new1\n+new2\n"
    rows = build_rows(_hunk(body, old_count=2, new_count=2))
    assert [r.kind for r in rows] == ["pair", "pair"]
    assert rows[0].old_text == "old1" and rows[0].new_text == "new1"
    assert rows[1].old_text == "old2" and rows[1].new_text == "new2"
    assert rows[0].old_line == 1 and rows[0].new_line == 1
    assert rows[1].old_line == 2 and rows[1].new_line == 2


def test_unbalanced_more_dels() -> None:
    body = "-a\n-b\n-c\n+x\n"
    rows = build_rows(_hunk(body, old_count=3, new_count=1))
    assert [r.kind for r in rows] == ["pair", "del", "del"]
    assert rows[0].old_text == "a" and rows[0].new_text == "x"
    assert rows[1].old_text == "b" and rows[1].new_line is None
    assert rows[2].old_text == "c" and rows[2].new_line is None
    # Old line numbers advance for every del row.
    assert [r.old_line for r in rows] == [1, 2, 3]


def test_unbalanced_more_adds() -> None:
    body = "-a\n+x\n+y\n+z\n"
    rows = build_rows(_hunk(body, old_count=1, new_count=3))
    assert [r.kind for r in rows] == ["pair", "ins", "ins"]
    assert rows[0].old_text == "a" and rows[0].new_text == "x"
    assert rows[1].new_text == "y" and rows[1].old_line is None
    assert rows[2].new_text == "z"
    assert [r.new_line for r in rows] == [1, 2, 3]


def test_context_between_changes() -> None:
    body = " ctx1\n-old\n+new\n ctx2\n"
    rows = build_rows(_hunk(body, old_count=3, new_count=3))
    assert [r.kind for r in rows] == ["ctx", "pair", "ctx"]
    assert rows[0].old_line == 1 and rows[0].new_line == 1
    assert rows[1].old_line == 2 and rows[1].new_line == 2
    assert rows[2].old_line == 3 and rows[2].new_line == 3


def test_solo_delete_without_paired_add() -> None:
    body = "-a\n ctx\n"
    rows = build_rows(_hunk(body, old_count=2, new_count=1))
    assert [r.kind for r in rows] == ["del", "ctx"]
    assert rows[0].old_line == 1 and rows[0].new_line is None
    assert rows[1].old_line == 2 and rows[1].new_line == 1


def test_solo_insert_without_preceding_delete() -> None:
    body = " ctx\n+new\n"
    rows = build_rows(_hunk(body, old_count=1, new_count=2))
    assert [r.kind for r in rows] == ["ctx", "ins"]
    assert rows[1].new_line == 2 and rows[1].old_line is None


def test_no_newline_marker_dropped() -> None:
    body = " a\n-b\n\\ No newline at end of file\n+c\n"
    rows = build_rows(_hunk(body, old_count=2, new_count=2))
    assert [r.kind for r in rows] == ["ctx", "pair"]
    assert rows[1].old_text == "b" and rows[1].new_text == "c"


def test_empty_context_line() -> None:
    # Blank lines in the diff body appear as bare '' (no leading space after
    # splitlines). Treat as ctx with empty text.
    body = "\n a\n"
    rows = build_rows(_hunk(body, old_count=2, new_count=2))
    assert [r.kind for r in rows] == ["ctx", "ctx"]
    assert rows[0].old_text == "" and rows[0].new_text == ""


def test_interleaved_delete_run() -> None:
    # -a -b +c -d +e should pair (a,c) (d,e) with b leftover as del.
    body = "-a\n-b\n+c\n-d\n+e\n"
    rows = build_rows(_hunk(body, old_count=3, new_count=2))
    assert [r.kind for r in rows] == ["pair", "del", "pair"]
    assert rows[0].old_text == "a" and rows[0].new_text == "c"
    assert rows[1].old_text == "b"
    assert rows[2].old_text == "d" and rows[2].new_text == "e"


# --- compute_fold_regions ---------------------------------------------------

def test_fold_regions_simple_function() -> None:
    body = (
        " def foo():\n"
        "     x = 1\n"
        "     y = 2\n"
        " def bar():\n"
        "     z = 3\n"
    )
    rows = build_rows(_hunk(body, old_count=5, new_count=5))
    regions = compute_fold_regions(rows)
    # Two fold regions: `def foo():` body, `def bar():` body
    assert len(regions) == 2
    # First region header is the 'def foo' row, body is lines 2-3 (x, y).
    assert regions[0].header_idx == 0
    assert regions[0].body_start_idx == 1 and regions[0].body_end_idx == 2
    assert regions[0].has_changes is False
    assert regions[0].right_start == 1 and regions[0].right_end == 3
    # Second region
    assert regions[1].header_idx == 3
    assert regions[1].body_start_idx == 4 and regions[1].body_end_idx == 4


def test_fold_regions_flags_changed_content() -> None:
    body = (
        " def foo():\n"
        "-    x = 1\n"
        "+    x = 2\n"
    )
    rows = build_rows(_hunk(body, old_count=2, new_count=2))
    regions = compute_fold_regions(rows)
    assert len(regions) == 1
    assert regions[0].has_changes is True


def test_fold_regions_nested() -> None:
    body = (
        " def outer():\n"
        "     def inner():\n"
        "         pass\n"
    )
    rows = build_rows(_hunk(body, old_count=3, new_count=3))
    regions = compute_fold_regions(rows)
    assert len(regions) == 2
    # Inner closes before outer, but sorted by header_idx so outer comes first.
    assert regions[0].header_idx == 0
    assert regions[0].body_end_idx == 2
    assert regions[1].header_idx == 1
    assert regions[1].body_end_idx == 2


def test_fold_regions_ignores_blank_lines() -> None:
    body = (
        " def foo():\n"
        "     x = 1\n"
        "\n"  # blank line inside body shouldn't close the region
        "     y = 2\n"
    )
    rows = build_rows(_hunk(body, old_count=4, new_count=4))
    regions = compute_fold_regions(rows)
    assert len(regions) == 1
    assert regions[0].body_end_idx == 3  # last indented row, not the blank


def test_fold_regions_pure_deletion_picks_left_context() -> None:
    """A fold region whose body is entirely deleted has no post-image
    lines to address. compute_fold_regions falls back to left context
    (pre-image addressing) so /fold-summary can still reach it."""
    body = (
        "-def removed():\n"
        "-    x = 1\n"
        "-    y = 2\n"
    )
    rows = build_rows(_hunk(body, old_count=3, new_count=0, old_start=10, new_start=1))
    regions = compute_fold_regions(rows)
    assert len(regions) == 1
    r = regions[0]
    assert r.context == "left"
    assert r.right_start is None and r.right_end is None
    assert r.left_start == 10 and r.left_end == 12
    assert r.has_changes is True


def test_fold_regions_pair_with_changes_picks_both_context() -> None:
    """A fold region with both sides populated AND contains changes
    addresses as 'both' so the server can emit a diff-style body."""
    body = (
        " def foo():\n"
        "-    x = 1\n"
        "+    x = 2\n"
        "     return x\n"
    )
    rows = build_rows(_hunk(body, old_count=3, new_count=3))
    regions = compute_fold_regions(rows)
    assert len(regions) == 1
    r = regions[0]
    assert r.context == "both"
    assert r.right_start is not None and r.right_end is not None
    assert r.left_start is not None and r.left_end is not None


# --- symbol-aware folding (slice 2) -----------------------------------------

def _rows_from_dicts(specs: list[dict]) -> list[_Row]:
    return [
        _Row(
            kind=s["kind"], old_line=s["old_line"], new_line=s["new_line"],
            old_text=s["old_text"], new_text=s["new_text"],
        )
        for s in specs
    ]


def test_symbol_folding_snaps_method_under_class_and_drops_inner_block() -> None:
    """A changed method folds as one region under its (unchanged) class;
    the indentation guess that would split the method's inner `if` block
    is dropped in favour of the definition boundary."""
    rows = _rows_from_dicts([
        {"kind": "ctx", "old_line": 1, "new_line": 1,
         "old_text": "class Foo:", "new_text": "class Foo:"},
        {"kind": "ctx", "old_line": 2, "new_line": 2,
         "old_text": "    def bar(self):", "new_text": "    def bar(self):"},
        {"kind": "ctx", "old_line": 3, "new_line": 3,
         "old_text": "        if x:", "new_text": "        if x:"},
        {"kind": "pair", "old_line": 4, "new_line": 4,
         "old_text": "            return 1", "new_text": "            return 2"},
        {"kind": "ctx", "old_line": 5, "new_line": 5,
         "old_text": "        return 0", "new_text": "        return 0"},
    ])
    spans = [
        {"start_line": 1, "end_line": 5, "kind": "class",
         "qualified_name": "Foo", "depth": 0},
        {"start_line": 2, "end_line": 5, "kind": "function",
         "qualified_name": "Foo.bar", "depth": 1},
    ]
    regions = compute_fold_regions(rows, spans, spans)
    # Exactly two regions — class and method — not three (no `if` fold).
    assert [(r.header_idx, r.body_end_idx) for r in regions] == [(0, 4), (1, 4)]


def test_symbol_folding_no_spans_is_identical_to_indentation() -> None:
    """An all-unsupported file (no spans) folds byte-identically to the
    pre-slice indentation output."""
    rows = build_rows(_hunk(
        " def foo():\n     x = 1\n     y = 2\n", old_count=3, new_count=3,
    ))
    with_empty = compute_fold_regions(rows, [], [])
    indent_only = compute_fold_regions(rows)
    assert [r.__dict__ for r in with_empty] == [r.__dict__ for r in indent_only]


@pytest.mark.parametrize(
    "case", json.loads(_FOLD_CASES_PATH.read_text(encoding="utf-8")),
    ids=lambda c: c["name"],
)
def test_fold_regions_lockstep_fixture(case: dict) -> None:
    """The Python detector reproduces the shared fixture's regions; the
    vitest case asserts the TS detector reproduces the same ones."""
    rows = _rows_from_dicts(case["rows"])
    regions = compute_fold_regions(rows, case["head_spans"], case["base_spans"])
    got = [{k: getattr(r, k) for k in _FOLD_REGION_KEYS} for r in regions]
    assert got == case["expected"]
