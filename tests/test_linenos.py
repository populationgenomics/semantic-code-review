"""Tests for the prompt line-number gutter (`format/linenos.py`)."""

from __future__ import annotations

from semantic_code_review.format import linenos


def test_new_file_numbers_every_added_line():
    body = "@@ -0,0 +1,3 @@\n+alpha\n+beta\n+gamma"
    out = linenos.number_for_prompt(body).split("\n")
    assert out[0] == "@@ -0,0 +1,3 @@"
    assert out[1] == "1 +alpha"
    assert out[2] == "2 +beta"
    assert out[3] == "3 +gamma"


def test_counter_starts_at_new_start():
    body = "@@ -60,2 +65,3 @@\n context\n+added\n more"
    out = linenos.number_for_prompt(body).split("\n")
    assert out[1] == "65  context"
    assert out[2] == "66 +added"
    assert out[3] == "67  more"


def test_deletions_get_blank_gutter_and_do_not_advance():
    body = "@@ -10,3 +10,2 @@\n keep\n-gone\n+new"
    out = linenos.number_for_prompt(body).split("\n")
    assert out[1] == "10  keep"
    # deletion: blank number column, still gutter-padded (width 2 here)
    assert out[2] == "   -gone"
    assert out[3] == "11 +new"


def test_counter_resets_each_hunk():
    body = "@@ -1,1 +1,1 @@\n+first\n@@ -50,1 +80,1 @@\n+later"
    out = linenos.number_for_prompt(body).split("\n")
    assert out[1] == " 1 +first"
    assert out[2] == "@@ -50,1 +80,1 @@"
    assert out[3] == "80 +later"


def test_file_header_markers_pass_through_unnumbered():
    # `--- a/f` starts with '-' but is a header, not a body deletion.
    body = "diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1,1 +1,2 @@\n ctx\n+added"
    out = linenos.number_for_prompt(body).split("\n")
    assert out[0] == "diff --git a/f b/f"
    assert out[1] == "--- a/f"
    assert out[2] == "+++ b/f"
    assert out[4] == "1  ctx"
    assert out[5] == "2 +added"


def test_header_without_counts():
    body = "@@ -5 +7 @@\n+only"
    out = linenos.number_for_prompt(body).split("\n")
    assert out[1] == "7 +only"


def test_no_newline_marker_gets_blank_gutter():
    body = "@@ -1,1 +1,1 @@\n+text\n\\ No newline at end of file"
    out = linenos.number_for_prompt(body).split("\n")
    assert out[1] == "1 +text"
    assert out[2] == "  \\ No newline at end of file"


def test_trailing_newline_preserved():
    assert linenos.number_for_prompt("@@ -0,0 +1,1 @@\n+x\n").endswith("\n")
    assert not linenos.number_for_prompt("@@ -0,0 +1,1 @@\n+x").endswith("\n")


def test_gutter_width_tracks_largest_number():
    body = "@@ -0,0 +1,1 @@\n+a\n@@ -0,0 +100,1 @@\n+b"
    out = linenos.number_for_prompt(body).split("\n")
    # width sized to 3 digits (max line 100)
    assert out[1] == "  1 +a"
    assert out[3] == "100 +b"
