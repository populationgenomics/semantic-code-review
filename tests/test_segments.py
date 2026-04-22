"""Segment anchoring: ranges, overlap detection, out-of-range rejection."""

from __future__ import annotations

import pytest

from semantic_code_review.augment.schemas import (
    AugmentedDiff, FilePatch, Hunk, PRInfo, Segment, Smell,
)
from semantic_code_review.format.emit import emit_augmented_diff
from semantic_code_review.format.parse import ParseError, parse_augmented_diff


def _minimal(hunk_body: str, *, old_count: int, new_count: int, segments: list[Segment] | None = None) -> AugmentedDiff:
    return AugmentedDiff(
        pr=PRInfo(pr_url="x", base_sha="a", head_sha="b"),
        files=[
            FilePatch(
                path="f.py",
                diff_git_line="diff --git a/f.py b/f.py",
                old_file_marker="--- a/f.py",
                new_file_marker="+++ b/f.py",
                hunks=[
                    Hunk(
                        header=f"@@ -1,{old_count} +1,{new_count} @@",
                        old_start=1, old_count=old_count,
                        new_start=1, new_count=new_count,
                        body=hunk_body,
                        segments=segments or [],
                    ),
                ],
            ),
        ],
    )


def test_two_segments_round_trip() -> None:
    body = (
        "-a\n"
        "+a1\n"
        "+a2\n"
        "+a3\n"
        "+a4\n"
    )
    diff = _minimal(
        body, old_count=1, new_count=4,
        segments=[
            Segment(new_start=1, new_count=2, intent="first edit"),
            Segment(new_start=3, new_count=2, intent="second edit",
                    smells=[Smell(tag="string-sql", note="demo")]),
        ],
    )
    text = emit_augmented_diff(diff)
    reparsed = parse_augmented_diff(text)
    segs = reparsed.files[0].hunks[0].segments
    assert len(segs) == 2
    assert segs[0].new_start == 1 and segs[0].new_count == 2
    assert segs[0].intent == "first edit"
    assert segs[1].new_start == 3 and segs[1].new_count == 2
    assert segs[1].smells[0].tag == "string-sql"


def test_overlapping_segments_rejected() -> None:
    body = "-a\n+a1\n+a2\n+a3\n"
    diff = _minimal(
        body, old_count=1, new_count=3,
        segments=[
            Segment(new_start=1, new_count=2),
            Segment(new_start=2, new_count=2),  # overlaps previous
        ],
    )
    text = emit_augmented_diff(diff)
    with pytest.raises(ParseError, match="overlaps"):
        parse_augmented_diff(text)


def test_segment_out_of_hunk_range_rejected() -> None:
    body = "-a\n+a1\n+a2\n"
    diff = _minimal(
        body, old_count=1, new_count=2,
        segments=[Segment(new_start=1, new_count=5)],  # exceeds hunk
    )
    text = emit_augmented_diff(diff)
    with pytest.raises(ParseError, match="outside hunk range"):
        parse_augmented_diff(text)


def test_missing_segment_end_rejected() -> None:
    text = (
        "#scr: scr-version: 1\n"
        "#scr: scr-pr: x\n"
        "#scr: scr-base: a\n"
        "#scr: scr-head: b\n"
        "diff --git a/f.py b/f.py\n"
        "--- a/f.py\n"
        "+++ b/f.py\n"
        "@@ -1,1 +1,2 @@\n"
        "-a\n"
        "+a1\n"
        "+a2\n"
        "#scr: scr-segment-begin: +1..+2\n"
        "#scr: scr-segment-intent: leaks out\n"
    )
    with pytest.raises(ParseError, match="without matching scr-segment-end"):
        parse_augmented_diff(text)


def test_segment_directive_outside_block_rejected() -> None:
    text = (
        "#scr: scr-version: 1\n"
        "#scr: scr-pr: x\n"
        "#scr: scr-base: a\n"
        "#scr: scr-head: b\n"
        "diff --git a/f.py b/f.py\n"
        "--- a/f.py\n"
        "+++ b/f.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-a\n"
        "+a1\n"
        "#scr: scr-segment-intent: floating\n"
    )
    with pytest.raises(ParseError, match="outside of a scr-segment-begin"):
        parse_augmented_diff(text)
