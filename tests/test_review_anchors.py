"""Anchor resolution: what GitHub will actually thread a comment to."""

from __future__ import annotations

from semantic_code_review.review import anchors

DIFF = """diff --git a/CONTEXT.md b/CONTEXT.md
--- a/CONTEXT.md
+++ b/CONTEXT.md
@@ -198,7 +198,7 @@ stable only within one build
 context
-old
+new
 context
diff --git a/other.py b/other.py
--- a/other.py
+++ b/other.py
@@ -10,3 +12,4 @@ def f():
+added
"""


def test_ranges_cover_both_sides() -> None:
    r = anchors.postable_ranges(DIFF)
    assert r[("CONTEXT.md", "RIGHT")] == [(198, 204)]
    assert r[("CONTEXT.md", "LEFT")] == [(198, 204)]
    assert r[("other.py", "RIGHT")] == [(12, 15)]
    assert r[("other.py", "LEFT")] == [(10, 12)]


def test_a_line_inside_a_hunk_is_left_alone() -> None:
    a = anchors.resolve("CONTEXT.md", 201, "RIGHT", anchors.postable_ranges(DIFF))
    assert (a.line, a.side, a.note) == (201, "RIGHT", None)
    assert not a.is_file_level


def test_a_line_just_outside_moves_to_the_nearest_hunk_line() -> None:
    """The real case: a comment at 117 against a hunk starting at 118.

    GitHub returns a null thread with no error for the original line, so
    the comment would be lost; position is worth keeping when the hunk
    is right there.
    """
    a = anchors.resolve("CONTEXT.md", 197, "RIGHT", anchors.postable_ranges(DIFF))
    assert a.line == 198
    assert a.note is not None and "moved to 198" in a.note


def test_a_line_far_from_any_hunk_becomes_a_file_thread() -> None:
    """Moving a comment 100 lines to reach a hunk would misrepresent it;
    a file-level thread is the honest degradation."""
    a = anchors.resolve("CONTEXT.md", 3, "RIGHT", anchors.postable_ranges(DIFF))
    assert a.is_file_level
    assert a.line is None and a.side is None
    assert a.note is not None and "outside the diff" in a.note


def test_a_file_with_no_hunks_on_that_side_becomes_a_file_thread() -> None:
    a = anchors.resolve("unknown.py", 5, "RIGHT", anchors.postable_ranges(DIFF))
    assert a.is_file_level


def test_the_note_is_appended_to_the_body() -> None:
    """A moved comment must say so — a silently relocated remark reads
    as being about code it was never written about."""
    assert anchors.with_note("body", None) == "body"
    out = anchors.with_note("body", "moved from 117")
    assert out.startswith("body")
    assert "moved from 117" in out
