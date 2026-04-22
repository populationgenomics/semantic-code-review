"""Row pairing: context, solo changes, paired runs, imbalanced pairs."""

from __future__ import annotations

from semantic_code_review.augment.schemas import Hunk
from semantic_code_review.viewer.rows import build_rows


def _hunk(body: str, *, old_start: int = 1, old_count: int = 1, new_start: int = 1, new_count: int = 1) -> Hunk:
    return Hunk(
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
