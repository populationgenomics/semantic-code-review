"""Boundary lists: what a span may start or end on (ADR 0008, slice 4)."""

from __future__ import annotations

import pytest

from semantic_code_review.augment import boundaries
from semantic_code_review.augment.schemas import ParsedHunk


def _hunk(body: str, *, new_start: int = 10, old_start: int = 10) -> ParsedHunk:
    lines = body.splitlines()
    new_count = sum(1 for ln in lines if not ln.startswith(("-", "\\")))
    old_count = sum(1 for ln in lines if not ln.startswith(("+", "\\")))
    return ParsedHunk(
        header=f"@@ -{old_start},{old_count} +{new_start},{new_count} @@",
        old_start=old_start,
        old_count=old_count,
        new_start=new_start,
        new_count=new_count,
        body=body,
    )


def _span(start: int, end: int, name: str = "f", kind: str = "function") -> dict:
    return {"start_line": start, "end_line": end, "kind": kind, "qualified_name": name, "depth": 0}


# --- composition -----------------------------------------------------------


def test_hunk_edges_and_changed_lines_are_boundaries() -> None:
    #  10 ctx / 11 ctx / 12 + / 13 ctx / 14 ctx / 15 ctx
    body = " a\n b\n+c\n d\n e\n f\n"
    assert boundaries.boundary_lines(_hunk(body)) == [10, 12, 15]


def test_a_deletion_lands_on_the_post_image_line_after_it() -> None:
    # The `-` lines have no post-image line; the note that describes them
    # anchors to the line the deletion sits before.
    body = " a\n b\n-gone\n-gone too\n c\n d\n e\n"
    assert 12 in boundaries.boundary_lines(_hunk(body))
    # A pair (`-` then `+`) is anchored by its `+` line alone.
    body = " a\n b\n-old\n+new\n c\n d\n e\n"  # 10..15
    assert boundaries.boundary_lines(_hunk(body)) == [10, 12, 15]


def test_definition_edges_inside_the_hunk_are_boundaries() -> None:
    body = " a\n b\n c\n d\n e\n f\n g\n h\n"  # 10..17, nothing changed
    spans = [_span(12, 15, "inner"), _span(5, 30, "outer"), _span(40, 50, "elsewhere")]
    assert boundaries.boundary_lines(_hunk(body), spans) == [10, 12, 15, 17]


def test_indent_blocks_and_blank_separated_runs_stand_in_for_ast_edges() -> None:
    # No grammar: a YAML-shaped hunk. Blocks open where indentation deepens;
    # runs are separated by blank lines.
    body = " services:\n   db:\n     image: pg\n     ports:\n       - 5432\n \n   web:\n     image: x\n"
    # 10 services / 11 db / 12 image / 13 ports / 14 - 5432 / 15 blank / 16 web / 17 image
    assert boundaries.boundary_lines(_hunk(body)) == [10, 11, 13, 14, 16, 17]


def test_a_deletion_only_hunk_has_no_boundaries() -> None:
    parsed = ParsedHunk(header="@@ -9,2 +8,0 @@", old_start=9, old_count=2, new_start=8, new_count=0, body="-a\n-b\n")
    assert boundaries.Boundaries.for_hunk(parsed).lines == ()


def test_the_no_newline_marker_is_not_a_line() -> None:
    body = "-a\n+b\n\\ No newline at end of file\n"
    assert boundaries.boundary_lines(_hunk(body)) == [10]


# --- ids -------------------------------------------------------------------


def test_ids_are_sequential_and_resolve_both_ways() -> None:
    b = boundaries.Boundaries((10, 12, 15))
    assert b.gutter() == {10: "b1", 12: "b2", 15: "b3"}
    assert b.id_for(12) == "b2"
    assert b.line_for("b3") == 15
    assert b.next_id == 4


@pytest.mark.parametrize("bad", ["b0", "b4", "b", "3", "+12", "12", "B2", "b2x"])
def test_an_id_outside_the_list_resolves_to_nothing(bad: str) -> None:
    b = boundaries.Boundaries((10, 12, 15))
    assert b.line_for(bad) is None


def test_a_line_that_is_not_a_boundary_has_no_id() -> None:
    with pytest.raises(KeyError):
        boundaries.Boundaries((10, 12, 15)).id_for(11)


def test_a_batch_numbers_its_hunks_continuously() -> None:
    h0 = _hunk("+a\n+b\n", new_start=1)
    h1 = _hunk("+c\n+d\n+e\n", new_start=20)
    table = boundaries.for_batch([(0, h0), (3, h1)])
    assert table[0].gutter() == {1: "b1", 2: "b2"}
    assert table[3].gutter() == {20: "b3", 21: "b4", 22: "b5"}
    # An id from the other hunk does not resolve against this one.
    assert table[3].line_for("b1") is None
    assert table[0].line_for("b3") is None


def test_the_list_must_be_sorted_and_unique() -> None:
    with pytest.raises(ValueError, match="sorted and unique"):
        boundaries.Boundaries((12, 10))
    with pytest.raises(ValueError, match="sorted and unique"):
        boundaries.Boundaries((10, 10))


# --- structure line ----------------------------------------------------------


def test_structure_names_each_definition_the_hunk_touches_as_a_pair() -> None:
    body = " a\n b\n c\n d\n e\n f\n g\n h\n"  # 10..17
    spans = [_span(12, 15, "m.inner", "method"), _span(5, 30, "m", "class"), _span(40, 50, "other")]
    b = boundaries.Boundaries.for_hunk(_hunk(body), spans)
    assert b.structure == (
        "method m.inner: b2..b3",
        "class m: b1..b4 (continues past the hunk)",
    )
