"""Segment anchoring: ranges, overlap detection, out-of-range rejection."""

from __future__ import annotations

import logging

import pytest

from semantic_code_review.augment import hunks as hunks_mod
from semantic_code_review.augment.schemas import (
    AnnotatedDiff,
    AnnotatedFile,
    AnnotatedHunk,
    FoldDescription,
    HunkAnnotations,
    ParsedHunk,
    PRInfo,
    Segment,
    Smell,
)
from semantic_code_review.format.emit import emit_augmented_diff
from semantic_code_review.format.parse import ParseError, parse_augmented_diff


def _minimal(
    hunk_body: str,
    *,
    old_count: int,
    new_count: int,
    segments: list[Segment] | None = None,
) -> AnnotatedDiff:
    parsed = ParsedHunk(
        header=f"@@ -1,{old_count} +1,{new_count} @@",
        old_start=1,
        old_count=old_count,
        new_start=1,
        new_count=new_count,
        body=hunk_body,
    )
    return AnnotatedDiff(
        pr=PRInfo(pr_url="x", base_sha="a", head_sha="b"),
        files=[
            AnnotatedFile(
                path="f.py",
                diff_git_line="diff --git a/f.py b/f.py",
                old_file_marker="--- a/f.py",
                new_file_marker="+++ b/f.py",
                hunks=[
                    AnnotatedHunk(
                        parsed=parsed,
                        ann=HunkAnnotations(intent="", segments=segments or []),
                    ),
                ],
            ),
        ],
    )


def test_two_segments_round_trip() -> None:
    body = "-a\n+a1\n+a2\n+a3\n+a4\n"
    diff = _minimal(
        body,
        old_count=1,
        new_count=4,
        segments=[
            Segment(new_start=1, new_count=2, intent="first edit"),
            Segment(new_start=3, new_count=2, intent="second edit", smells=[Smell(tag="string-sql", note="demo")]),
        ],
    )
    text = emit_augmented_diff(diff)
    reparsed = parse_augmented_diff(text)
    segs = reparsed.files[0].hunks[0].ann.segments
    assert len(segs) == 2
    assert segs[0].new_start == 1 and segs[0].new_count == 2
    assert segs[0].intent == "first edit"
    assert segs[1].new_start == 3 and segs[1].new_count == 2
    assert segs[1].smells[0].tag == "string-sql"


def test_overlapping_segments_rejected() -> None:
    body = "-a\n+a1\n+a2\n+a3\n"
    diff = _minimal(
        body,
        old_count=1,
        new_count=3,
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
        body,
        old_count=1,
        new_count=2,
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


def test_fold_description_round_trip() -> None:
    body = "-a\n+a1\n+a2\n+a3\n+a4\n"
    diff = _minimal(
        body,
        old_count=1,
        new_count=4,
        segments=[],
    )
    diff.files[0].ann.fold_descriptions = [
        FoldDescription(context="right", right_start=1, right_end=2, summary="Intro block"),
        FoldDescription(context="left", left_start=3, left_end=4, summary="Deleted tail"),
        FoldDescription(
            context="both",
            right_start=5,
            right_end=8,
            left_start=4,
            left_end=6,
            summary="Refactor",
        ),
    ]
    text = emit_augmented_diff(diff)
    assert 'scr-fold: right 1..2 "Intro block"' in text
    assert 'scr-fold: left 3..4 "Deleted tail"' in text
    assert 'scr-fold: both R5..8 L4..6 "Refactor"' in text
    # Emitted in the file header, before the first hunk.
    assert text.index("scr-fold: right") < text.index("@@ ")
    reparsed = parse_augmented_diff(text)
    fds = reparsed.files[0].ann.fold_descriptions
    assert len(fds) == 3
    assert fds[0].context == "right" and fds[0].right_start == 1 and fds[0].right_end == 2
    assert fds[1].context == "left" and fds[1].left_start == 3 and fds[1].left_end == 4
    assert fds[2].context == "both"
    assert fds[2].right_start == 5 and fds[2].right_end == 8
    assert fds[2].left_start == 4 and fds[2].left_end == 6


def test_hunk_level_scr_fold_from_an_older_diff_lifts_to_the_file() -> None:
    """Before fold summaries moved to the file, `scr-fold` sat under a
    hunk. Such a diff still parses, with the summary on the file."""
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
        "+b\n"
        '#scr: scr-fold: right 1..1 "old home"\n'
    )
    diff = parse_augmented_diff(text)
    assert [fd.summary for fd in diff.files[0].ann.fold_descriptions] == ["old home"]
    assert "fold_descriptions" not in diff.files[0].hunks[0].ann.model_dump()
    # Re-emitted, it moves into the file header.
    assert emit_augmented_diff(diff).index("scr-fold") < emit_augmented_diff(diff).index("@@ ")


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


# --- build_hunk_annotations: boundary arithmetic on model-supplied ranges ---
#
# The per-hunk pass reports segments as POST-IMAGE `new_start`/`new_count`
# and reliably miscounts the edges: it treats `new_start + new_count` as the
# hunk's last line, and starts a segment on the line the previous one ended.
# Both name the right code with a wrong edge, so they are clamped; a range
# the clamp cannot rescue is dropped.


def _addition_hunk(*, new_start: int, new_count: int) -> ParsedHunk:
    return ParsedHunk(
        header=f"@@ -{new_start - 1},0 +{new_start},{new_count} @@",
        old_start=new_start - 1,
        old_count=0,
        new_start=new_start,
        new_count=new_count,
        body="".join(f"+line {n}\n" for n in range(new_start, new_start + new_count)),
    )


def _kept(parsed: ParsedHunk, *ranges: tuple[int, int]) -> list[tuple[int, int]]:
    """`build_hunk_annotations` over `(new_start, new_count)` pairs."""
    ann = _annotate(parsed, *ranges)
    return [(s.new_start, s.new_count) for s in ann.segments]


def _annotate(parsed: ParsedHunk, *ranges: tuple[int, int]) -> HunkAnnotations:
    return hunks_mod.build_hunk_annotations(
        parsed,
        {"segments": [{"new_start": s, "new_count": c, "intent": f"seg {s}"} for s, c in ranges]},
    )


def test_segment_reaching_the_hunks_true_last_line_survives_untouched() -> None:
    # +9..+82 is the hunk; a segment ending exactly on +82 is correct as given.
    assert _kept(_addition_hunk(new_start=9, new_count=74), (60, 23)) == [(60, 23)]


def test_segment_one_past_the_hunk_end_is_clamped_not_dropped() -> None:
    # The exclusive-end error: +60..+83 against a hunk whose last line is +82.
    assert _kept(_addition_hunk(new_start=9, new_count=74), (60, 24)) == [(60, 23)]


def test_segment_starting_on_the_previous_segments_last_line_is_clamped() -> None:
    # The same error at an internal boundary: +9..+20 then +20..+40.
    parsed = _addition_hunk(new_start=9, new_count=74)
    assert _kept(parsed, (9, 12), (20, 21)) == [(9, 12), (21, 20)]


def test_a_run_of_segments_survives_the_exclusive_end_convention_throughout() -> None:
    # Every boundary off by one, the last one included.
    parsed = _addition_hunk(new_start=1, new_count=30)
    assert _kept(parsed, (1, 11), (11, 11), (21, 11)) == [(1, 11), (12, 10), (22, 9)]


@pytest.mark.parametrize(
    ("ranges", "expected", "warning"),
    [
        pytest.param([(20, 0)], [], "outside range", id="empty-count"),
        pytest.param([(20, -3)], [], "outside range", id="inverted-count"),
        pytest.param([(5, 4)], [], "outside range", id="starts-before-the-hunk"),
        pytest.param([(83, 4)], [], "outside range", id="starts-past-the-hunks-last-line"),
        pytest.param([(20, 30), (25, 3)], [(20, 30)], "covered by previous", id="covered-by-the-previous"),
    ],
)
def test_segment_ranges_the_clamp_cannot_rescue_are_dropped(
    ranges: list[tuple[int, int]],
    expected: list[tuple[int, int]],
    warning: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The warning text is the only telemetry a drop has — it is what a
    sweep of `trace/augment.log` classifies, so the two shapes must stay
    distinguishable."""
    with caplog.at_level(logging.WARNING, logger=hunks_mod.__name__):
        assert _kept(_addition_hunk(new_start=9, new_count=74), *ranges) == expected
    assert warning in caplog.text


def test_a_deletion_only_hunk_accepts_no_segments() -> None:
    parsed = ParsedHunk(header="@@ -9,4 +8,0 @@", old_start=9, old_count=4, new_start=8, new_count=0, body="-a\n")
    assert _kept(parsed, (8, 1)) == []


def test_a_clamped_segment_round_trips_through_the_on_disk_format() -> None:
    """The clamp is also what keeps the sidecar readable: `parse_augmented_diff`
    rejects an out-of-hunk segment outright, so an unclamped overshoot would
    make the emitted diff unparseable rather than merely wrong."""
    parsed = ParsedHunk(
        header="@@ -1,1 +1,4 @@",
        old_start=1,
        old_count=1,
        new_start=1,
        new_count=4,
        body="-a\n+a1\n+a2\n+a3\n+a4\n",
    )
    diff = AnnotatedDiff(
        pr=PRInfo(pr_url="x", base_sha="a", head_sha="b"),
        files=[
            AnnotatedFile(
                path="f.py",
                diff_git_line="diff --git a/f.py b/f.py",
                old_file_marker="--- a/f.py",
                new_file_marker="+++ b/f.py",
                hunks=[AnnotatedHunk(parsed=parsed, ann=_annotate(parsed, (1, 5)))],
            ),
        ],
    )
    reparsed = parse_augmented_diff(emit_augmented_diff(diff))
    segs = reparsed.files[0].hunks[0].ann.segments
    assert [(s.new_start, s.new_count) for s in segs] == [(1, 4)]
