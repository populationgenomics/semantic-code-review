"""Parser edge cases: minimal diffs, malformed input, unknown directives."""

from __future__ import annotations

import pytest

from semantic_code_review.format.parse import ParseError, parse_augmented_diff


MINIMAL_PREAMBLE = (
    "#scr: scr-version: 1\n"
    "#scr: scr-pr: x\n"
    "#scr: scr-base: a\n"
    "#scr: scr-head: b\n"
)


def _diff(body: str, *, old: int = 1, new: int = 1, trailer: str = "") -> str:
    return (
        MINIMAL_PREAMBLE
        + "diff --git a/f b/f\n--- a/f\n+++ b/f\n"
        + f"@@ -1,{old} +1,{new} @@\n"
        + body
        + trailer
    )


def test_parses_minimal_diff() -> None:
    d = parse_augmented_diff(_diff("-x\n+y\n"))
    h = d.files[0].hunks[0]
    assert h.parsed.old_count == 1
    assert h.parsed.new_count == 1
    assert h.parsed.body == "-x\n+y\n"


def test_parses_empty_preamble_directive() -> None:
    from semantic_code_review.augment.schemas import SkippedOverview
    # scr-pr is required; ensure we can still parse without overview/model.
    d = parse_augmented_diff(_diff("-x\n+y\n"))
    assert isinstance(d.overview, SkippedOverview)
    assert d.pr.model == ""


def test_rejects_bare_scr_colon() -> None:
    text = (
        "#scr: scr-version: 1\n"
        "#scr: scr-pr: x\n"
        "#scr: scr-base: a\n"
        "#scr: scr-head: b\n"
        "#scr:\n"
        "diff --git a/f b/f\n--- a/f\n+++ b/f\n"
        "@@ -1,1 +1,1 @@\n-x\n+y\n"
    )
    with pytest.raises(ParseError, match="bare '#scr:'"):
        parse_augmented_diff(text)


def test_rejects_orphan_continuation() -> None:
    text = (
        "#scr> dangling\n"
        "#scr: scr-version: 1\n"
        "#scr: scr-pr: x\n"
        "#scr: scr-base: a\n"
        "#scr: scr-head: b\n"
        "diff --git a/f b/f\n--- a/f\n+++ b/f\n"
        "@@ -1,1 +1,1 @@\n-x\n+y\n"
    )
    with pytest.raises(ParseError, match="without a preceding"):
        parse_augmented_diff(text)


def test_rejects_unknown_preamble_directive() -> None:
    text = (
        "#scr: scr-version: 1\n"
        "#scr: scr-pr: x\n"
        "#scr: scr-base: a\n"
        "#scr: scr-head: b\n"
        "#scr: scr-bogus: hello\n"
        "diff --git a/f b/f\n--- a/f\n+++ b/f\n"
        "@@ -1,1 +1,1 @@\n-x\n+y\n"
    )
    with pytest.raises(ParseError, match="unknown preamble directive"):
        parse_augmented_diff(text)


def test_rejects_hunk_count_mismatch() -> None:
    body = "-x\n+y\n+z\n"  # header says 1 new, body has 2
    with pytest.raises(ParseError, match="counts do not match"):
        parse_augmented_diff(_diff(body, old=1, new=1))


def test_rejects_malformed_hunk_header() -> None:
    text = (
        MINIMAL_PREAMBLE
        + "diff --git a/f b/f\n--- a/f\n+++ b/f\n"
        + "@@ bogus @@\n-x\n+y\n"
    )
    with pytest.raises(ParseError, match="malformed hunk header"):
        parse_augmented_diff(text)


def test_no_newline_marker_in_body() -> None:
    body = "-x\n+y\n\\ No newline at end of file\n"
    d = parse_augmented_diff(_diff(body))
    assert d.files[0].hunks[0].parsed.body.endswith("\\ No newline at end of file\n")


def test_continuation_joins_with_space() -> None:
    text = (
        "#scr: scr-version: 1\n"
        "#scr: scr-pr: http://example/\n"
        "#scr: scr-base: a\n"
        "#scr: scr-head: b\n"
        "diff --git a/f b/f\n--- a/f\n+++ b/f\n"
        "@@ -1,1 +1,1 @@\n-x\n+y\n"
        "#scr: scr-hunk-intent: First sentence.\n"
        "#scr>   Continued text.\n"
    )
    d = parse_augmented_diff(text)
    assert d.files[0].hunks[0].ann.intent == "First sentence. Continued text."
