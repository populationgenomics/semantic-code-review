"""Fold regions from the AST, once, over the whole file (ADR 0008 slice 3).

The fixture `fold_regions_cases.json` holds `(path, head, base, hunks)`
inputs with the regions they must yield; `compute_fold_regions` is the
only implementation, so the fixture is a plain data-driven test of it.
"""

from __future__ import annotations

import json
import pathlib
import textwrap

import pytest

from semantic_code_review import structural
from semantic_code_review.augment.schemas import ParsedHunk
from semantic_code_review.viewer import build_json, fold_regions, hunk_layout

_CASES_PATH = pathlib.Path(__file__).parent / "fixtures" / "fold_regions_cases.json"
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


def _regions(
    path: str,
    head: str | None,
    base: str | None,
    hunks: list[ParsedHunk],
    *,
    positioned: bool = True,
) -> list[fold_regions.FoldRegion]:
    """Regions the way `build_json._file_block` computes them: symbols from
    the texts, rows positioned against them."""
    lang = structural.language_for_path(path)
    head_syms = structural.outline_symbols(head, lang) if head is not None and lang else []
    base_syms = structural.outline_symbols(base, lang) if base is not None and lang else []
    head_spans = build_json._fold_spans(head_syms)
    base_spans = build_json._fold_spans(base_syms)
    rows = [
        hunk_layout.layout_hunk_rows(h, head_spans, base_spans) if positioned else hunk_layout.build_rows(h)
        for h in hunks
    ]
    return fold_regions.compute_fold_regions(
        list(zip(hunks, rows, strict=True)),
        build_json._split_lines(head),
        build_json._split_lines(base),
        head_syms,
        base_syms,
        has_grammar=lang is not None,
    )


# --- fixture ----------------------------------------------------------------


@pytest.mark.parametrize("case", _CASES, ids=lambda c: c["name"])
def test_fixture_case(case: dict) -> None:
    hunks = [ParsedHunk(**h) for h in case["hunks"]]
    got = [r.to_dict(summary="") for r in _regions(case["path"], case["head"], case["base"], hunks)]
    for r in got:
        del r["summary"]
    assert got == case["expected"]


# --- the whole file, both sides -----------------------------------------------

_THREE_FUNCTIONS = "def a():\n    return 1\n\n\ndef b():\n    return 2\n\n\ndef c():\n    return 3\n"


def test_regions_exist_for_lines_no_hunk_carries() -> None:
    """A hunk touching only `c` still yields `a` and `b`: the diff decides
    what is shown, not what folds."""
    head = _THREE_FUNCTIONS
    base = head.replace("return 3", "return 0")
    hunk = _hunk(
        """
         def c():
        -    return 0
        +    return 3
        """,
        old_start=9,
        new_start=9,
    )
    got = _regions("a.py", head, base, [hunk])
    assert [(r.qualified_name, r.context, r.right_start, r.right_end, r.has_changes) for r in got] == [
        ("a", "right", 1, 2, False),
        ("b", "right", 5, 6, False),
        ("c", "both", 9, 10, True),
    ]
    # An unchanged region needs no base range: its lines are the same text.
    assert (got[0].left_start, got[0].left_end) == (None, None)
    # A changed one carries both, from each side's own parse.
    assert (got[2].left_start, got[2].left_end) == (9, 10)


def test_region_ranges_are_unmoved_by_run_positioning() -> None:
    """The mid-body insertion `position_runs` slides to the definition
    opener (slice 2) has the same line ranges either way: they are the
    AST's, not the rows'. Only `has_changes` reads the rows — positioned,
    `old_fn` is untouched and the insertion is all `new_fn`."""
    old_fn = "def old_fn():\n    setup()\n    return cleanup()\n\n"
    new_fn = "def new_fn():\n    other()\n    return cleanup()\n\n"
    third = "def third():\n    pass\n"
    head = old_fn + new_fn + third
    base = old_fn + third
    hunk = _hunk(
        """
         def old_fn():
             setup()
        +    return cleanup()
        +
        +def new_fn():
        +    other()
             return cleanup()

         def third():
        """
    )
    moved = _regions("a.py", head, base, [hunk], positioned=True)
    unmoved = _regions("a.py", head, base, [hunk], positioned=False)
    ranges = [(r.qualified_name, r.right_start, r.right_end) for r in moved]
    assert ranges == [(r.qualified_name, r.right_start, r.right_end) for r in unmoved]
    assert ranges == [("old_fn", 1, 3), ("new_fn", 5, 7), ("third", 9, 10)]
    assert [(r.qualified_name, r.context, r.has_changes) for r in moved] == [
        ("old_fn", "right", False),
        ("new_fn", "right", True),
        ("third", "right", False),
    ]
    # Unpositioned, the differ's mid-body run reads as a change to old_fn.
    assert unmoved[0].context == "both"


def test_deleted_file_folds_its_base_definitions() -> None:
    base = "def gone():\n    return 1\n"
    hunk = _hunk(
        """
        -def gone():
        -    return 1
        """
    )
    hunk = hunk.model_copy(update={"new_start": 0, "new_count": 0})
    got = _regions("a.py", None, base, [hunk])
    assert [r.to_dict(summary="") for r in got] == [
        {
            "context": "left",
            "right_start": None,
            "right_end": None,
            "left_start": 1,
            "left_end": 2,
            "has_changes": True,
            "qualified_name": "gone",
            "kind": "function",
            "summary": "",
        }
    ]


def test_a_side_without_text_yields_no_regions() -> None:
    """No worktree, no regions — never a guess from the hunk's rows."""
    hunk = _hunk(
        """
         def foo():
        -    return 1
        +    return 2
        """
    )
    assert _regions("a.py", None, None, [hunk]) == []


# --- the indentation fallback -------------------------------------------------


def test_no_grammar_folds_every_indentation_level() -> None:
    text = "top:\n  mid:\n    leaf: 1\n  other: 2\nflat: 3\n"
    got = _regions("conf.yaml", text, text, [])
    assert [(r.right_start, r.right_end, r.qualified_name) for r in got] == [(1, 4, None), (2, 3, None)]


def test_with_a_grammar_only_column_zero_block_openers_fold() -> None:
    """Inside a definition the AST is the structure; a docstring's hanging
    indents are prose; a multi-line import is a block."""
    text = (
        '"""Module.\n\n- a bullet that wraps\n  onto a second line\n"""\n\n'
        "from x import (\n    a,\n    b,\n)\n\n"
        "def f():\n    if a:\n        return b\n    return a\n"
    )
    got = _regions("m.py", text, text, [])
    assert [(r.right_start, r.right_end, r.qualified_name) for r in got] == [(7, 9, None), (12, 15, "f")]


def test_indent_stanza_does_not_swallow_trailing_blank_lines() -> None:
    text = "a:\n  b: 1\n\n\nc: 2\n"
    got = _regions("conf.yaml", text, text, [])
    assert [(r.right_start, r.right_end) for r in got] == [(1, 2)]


def test_a_generated_lockfile_still_folds() -> None:
    """A lockfile's arrays are structure; only a binary file gets no
    regions, and `build_json` decides that by role."""
    text = "[[package]]\ndeps = [\n  'a',\n  'b',\n]\n"
    got = _regions("uv.lock", text, text, [])
    assert [(r.right_start, r.right_end) for r in got] == [(2, 4)]


def test_a_region_the_rows_do_not_reach_is_dropped_with_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    """A dirty-tree head edited under review can shrink below what the
    diff recorded; a base region in the tail then has no rows. It is
    dropped and logged rather than taking the build down."""
    base = "def a():\n    return 1\n\n\ndef tail():\n    return 2\n"
    head_now = "def a():\n    return 0\n"  # `tail` gone since the diff was taken
    hunk = _hunk(
        """
         def a():
        -    return 1
        +    return 0
        """
    )
    with caplog.at_level("WARNING"):
        got = _regions("a.py", head_now, base, [hunk])
    assert [(r.qualified_name, r.context) for r in got] == [("a", "both")]
    assert "base lines 5-6 are outside the diff's rows" in caplog.text
