"""Annotation spans: the text grammar, the sidecar migration, and the
validator that resolves boundary ids (ADR 0008, slice 4)."""

from __future__ import annotations

import logging

import pytest

from semantic_code_review.augment import boundaries
from semantic_code_review.augment import hunks as hunks_mod
from semantic_code_review.augment.schemas import (
    AnnotatedDiff,
    AnnotatedFile,
    AnnotatedHunk,
    AnnotationSpan,
    FoldDescription,
    HunkAnnotations,
    HunkSubmission,
    ParsedHunk,
    PRInfo,
    Ref,
    Smell,
    SpanSubmission,
)
from semantic_code_review.format.emit import emit_augmented_diff
from semantic_code_review.format.parse import ParseError, parse_augmented_diff

_PREAMBLE = (
    "#scr: scr-version: 1\n"
    "#scr: scr-pr: x\n"
    "#scr: scr-base: a\n"
    "#scr: scr-head: b\n"
    "diff --git a/f.py b/f.py\n"
    "--- a/f.py\n"
    "+++ b/f.py\n"
)


def _minimal(
    hunk_body: str,
    *,
    old_count: int,
    new_count: int,
    spans: list[AnnotationSpan] | None = None,
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
                hunks=[AnnotatedHunk(parsed=parsed, ann=HunkAnnotations(intent="", spans=spans or []))],
            ),
        ],
    )


def _ranges(diff: AnnotatedDiff) -> list[tuple[int, int]]:
    return [(s.start, s.end) for s in diff.files[0].hunks[0].ann.spans]


# --- text grammar ------------------------------------------------------------


def test_spans_round_trip_in_both_forms() -> None:
    """An intent-only span is one `scr-span` line; one with smells, context
    or refs is a `scr-span-begin` block."""
    body = "-a\n+a1\n+a2\n+a3\n+a4\n"
    diff = _minimal(
        body,
        old_count=1,
        new_count=4,
        spans=[
            AnnotationSpan(start=1, end=2, intent="first edit"),
            AnnotationSpan(
                start=3,
                end=4,
                intent="second edit",
                smells=[Smell(tag="string-sql", note="demo")],
                context="ctx",
                refs=[Ref(path="g.py", line=3, reason="r")],
            ),
            AnnotationSpan(start=4, end=4, intent="a callout"),
        ],
    )
    text = emit_augmented_diff(diff)
    assert '#scr: scr-span: +1..+2 "first edit"\n' in text
    assert "#scr: scr-span-begin: +3..+4\n#scr: scr-span-intent: second edit\n" in text
    assert '#scr: scr-span: +4..+4 "a callout"\n' in text
    reparsed = parse_augmented_diff(text)
    assert reparsed.model_dump() == diff.model_dump()
    assert emit_augmented_diff(reparsed) == text


def test_nested_spans_round_trip() -> None:
    body = "".join(f"+l{n}\n" for n in range(1, 9))
    diff = _minimal(
        body,
        old_count=0,
        new_count=8,
        spans=[
            AnnotationSpan(start=1, end=6, intent="region"),
            AnnotationSpan(start=2, end=3, intent="inner"),
            AnnotationSpan(start=5, end=5, intent="callout"),
            AnnotationSpan(start=7, end=8, intent="sibling"),
        ],
    )
    assert _ranges(parse_augmented_diff(emit_augmented_diff(diff))) == [(1, 6), (2, 3), (5, 5), (7, 8)]


def test_partially_overlapping_spans_are_rejected() -> None:
    body = "-a\n+a1\n+a2\n+a3\n"
    diff = _minimal(
        body,
        old_count=1,
        new_count=3,
        spans=[AnnotationSpan(start=1, end=2), AnnotationSpan(start=2, end=3)],
    )
    with pytest.raises(ParseError, match="partially overlaps"):
        parse_augmented_diff(emit_augmented_diff(diff))


def test_span_out_of_hunk_range_rejected() -> None:
    body = "-a\n+a1\n+a2\n"
    diff = _minimal(body, old_count=1, new_count=2, spans=[AnnotationSpan(start=1, end=5)])
    with pytest.raises(ParseError, match="outside hunk range"):
        parse_augmented_diff(emit_augmented_diff(diff))


def test_missing_span_end_rejected() -> None:
    text = _PREAMBLE + (
        "@@ -1,1 +1,2 @@\n-a\n+a1\n+a2\n#scr: scr-span-begin: +1..+2\n#scr: scr-span-intent: leaks out\n"
    )
    with pytest.raises(ParseError, match="without matching scr-span-end"):
        parse_augmented_diff(text)


def test_span_directive_outside_block_rejected() -> None:
    text = _PREAMBLE + "@@ -1,1 +1,1 @@\n-a\n+a1\n#scr: scr-span-intent: floating\n"
    with pytest.raises(ParseError, match="outside of a scr-span-begin"):
        parse_augmented_diff(text)


def test_legacy_segments_and_line_notes_parse_as_spans() -> None:
    """A diff written before spans carries `scr-segment-*` blocks and
    `scr-line` notes. They load as spans and re-emit in the new grammar."""
    text = _PREAMBLE + (
        "@@ -1,1 +1,4 @@\n-a\n+a1\n+a2\n+a3\n+a4\n"
        "#scr: scr-segment-begin: +1..+2\n"
        "#scr: scr-segment-intent: old segment\n"
        '#scr: scr-segment-smell: string-sql "demo"\n'
        "#scr: scr-segment-end\n"
        '#scr: scr-line: +3 "old note"\n'
    )
    diff = parse_augmented_diff(text)
    spans = diff.files[0].hunks[0].ann.spans
    assert [(s.start, s.end, s.intent) for s in spans] == [(1, 2, "old segment"), (3, 3, "old note")]
    assert spans[0].smells[0].tag == "string-sql"
    emitted = emit_augmented_diff(diff)
    assert "scr-segment" not in emitted and "scr-line" not in emitted
    assert "#scr: scr-span-begin: +1..+2\n" in emitted
    assert '#scr: scr-span: +3..+3 "old note"\n' in emitted


def test_legacy_sidecar_segments_and_line_notes_lift_to_spans() -> None:
    raw = {
        "intent": "x",
        "segments": [{"new_start": 5, "new_count": 3, "intent": "seg", "smells": [{"tag": "dead-code", "note": ""}]}],
        "line_notes": [{"line": 6, "body": "note"}],
    }
    ann = HunkAnnotations.model_validate(raw)
    assert [(s.start, s.end, s.intent) for s in ann.spans] == [(5, 7, "seg"), (6, 6, "note")]
    assert ann.spans[0].smells[0].tag == "dead-code"
    assert "segments" not in ann.model_dump() and "line_notes" not in ann.model_dump()
    assert "segments" in raw  # the input is not mutated


def test_a_stored_span_cannot_be_inverted() -> None:
    with pytest.raises(ValueError, match="before start"):
        AnnotationSpan(start=5, end=3)


def test_fold_description_round_trip() -> None:
    body = "-a\n+a1\n+a2\n+a3\n+a4\n"
    diff = _minimal(body, old_count=1, new_count=4)
    diff.files[0].ann.fold_descriptions = [
        FoldDescription(context="right", right_start=1, right_end=2, summary="Intro block"),
        FoldDescription(context="left", left_start=3, left_end=4, summary="Deleted tail"),
        FoldDescription(context="both", right_start=5, right_end=8, left_start=4, left_end=6, summary="Refactor"),
    ]
    text = emit_augmented_diff(diff)
    assert 'scr-fold: right 1..2 "Intro block"' in text
    assert 'scr-fold: left 3..4 "Deleted tail"' in text
    assert 'scr-fold: both R5..8 L4..6 "Refactor"' in text
    assert text.index("scr-fold: right") < text.index("@@ ")
    fds = parse_augmented_diff(text).files[0].ann.fold_descriptions
    assert [fd.context for fd in fds] == ["right", "left", "both"]
    assert (fds[2].right_start, fds[2].right_end, fds[2].left_start, fds[2].left_end) == (5, 8, 4, 6)


def test_hunk_level_scr_fold_from_an_older_diff_lifts_to_the_file() -> None:
    text = _PREAMBLE + '@@ -1,1 +1,1 @@\n-a\n+b\n#scr: scr-fold: right 1..1 "old home"\n'
    diff = parse_augmented_diff(text)
    assert [fd.summary for fd in diff.files[0].ann.fold_descriptions] == ["old home"]
    assert "fold_descriptions" not in diff.files[0].hunks[0].ann.model_dump()
    assert emit_augmented_diff(diff).index("scr-fold") < emit_augmented_diff(diff).index("@@ ")


# --- build_hunk_annotations: boundary ids resolve, nothing is computed -------


def _addition_hunk(*, new_start: int, new_count: int) -> ParsedHunk:
    return ParsedHunk(
        header=f"@@ -{new_start - 1},0 +{new_start},{new_count} @@",
        old_start=new_start - 1,
        old_count=0,
        new_start=new_start,
        new_count=new_count,
        body="".join(f"+line {n}\n" for n in range(new_start, new_start + new_count)),
    )


def _kept(parsed: ParsedHunk, *spans: dict) -> list[tuple[int, int]]:
    ann = hunks_mod.build_hunk_annotations(parsed, {"spans": list(spans)}, boundaries.Boundaries.for_hunk(parsed))
    return [(s.start, s.end) for s in ann.spans]


def _sp(start: str, end: str | None = None, **rest: object) -> dict:
    d: dict = {"start": start, "intent": f"span {start}", **rest}
    if end is not None:
        d["end"] = end
    return d


def test_boundary_ids_resolve_to_post_image_lines() -> None:
    # An all-`+` hunk +9..+82: every line is a boundary, b1 = +9, b74 = +82.
    parsed = _addition_hunk(new_start=9, new_count=74)
    assert _kept(parsed, _sp("b1", "b12"), _sp("b52", "b74")) == [(9, 20), (60, 82)]


def test_a_single_id_is_a_single_line_span() -> None:
    parsed = _addition_hunk(new_start=9, new_count=74)
    assert _kept(parsed, _sp("b5")) == [(13, 13)]
    assert _kept(parsed, _sp("b5", "b5")) == [(13, 13)]


def test_nested_spans_are_kept_outermost_first() -> None:
    parsed = _addition_hunk(new_start=1, new_count=30)
    assert _kept(parsed, _sp("b8"), _sp("b3", "b12"), _sp("b1", "b20"), _sp("b25", "b30")) == [
        (1, 20),
        (3, 12),
        (8, 8),
        (25, 30),
    ]


def test_two_spans_over_one_line_are_two_observations() -> None:
    parsed = _addition_hunk(new_start=1, new_count=5)
    assert _kept(parsed, _sp("b2"), _sp("b2")) == [(2, 2), (2, 2)]


def test_resolved_spans_carry_their_label() -> None:
    parsed = _addition_hunk(new_start=1, new_count=5)
    ann = hunks_mod.build_hunk_annotations(
        parsed,
        {
            "intent": "hunk",
            "spans": [
                {
                    "start": "b1",
                    "end": "b3",
                    "intent": "first",
                    "smells": [{"tag": "dead-code", "note": "n"}],
                    "context": "c",
                    "refs": [{"path": "g.py", "line": 7, "reason": "r"}],
                }
            ],
        },
        boundaries.Boundaries.for_hunk(parsed),
    )
    span = ann.spans[0]
    assert (span.start, span.end, span.intent, span.context) == (1, 3, "first", "c")
    assert span.smells[0].tag == "dead-code"
    assert span.refs[0].path == "g.py"


@pytest.mark.parametrize(
    ("spans", "expected", "warning"),
    [
        pytest.param([_sp("b75")], [], "unknown boundary b75", id="id-past-the-list"),
        pytest.param([_sp("b1", "b75")], [], "unknown boundary b75", id="end-past-the-list"),
        pytest.param([_sp("b0", "b3")], [], "unknown boundary b0", id="id-before-the-list"),
        pytest.param([_sp("+9", "+20")], [], "unknown boundary +9", id="a-line-number-is-not-an-id"),
        pytest.param([_sp("b12", "b1")], [], "is inverted", id="inverted-pair"),
        pytest.param([_sp("b1", "b12"), _sp("b12", "b30")], [(9, 20)], "partially overlaps", id="shares-an-edge"),
        pytest.param([_sp("b1", "b12"), _sp("b6", "b30")], [(9, 20)], "partially overlaps", id="straddles"),
        pytest.param([{"intent": "no ids"}], [], "malformed boundary ids", id="no-ids"),
        pytest.param([{"start": 12, "intent": "int"}], [], "malformed boundary ids", id="integer-id"),
    ],
)
def test_spans_the_list_cannot_resolve_are_dropped(
    spans: list[dict],
    expected: list[tuple[int, int]],
    warning: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The warning text is the only telemetry a drop has — it is what a
    sweep of `trace/augment.log` classifies."""
    with caplog.at_level(logging.WARNING, logger=hunks_mod.__name__):
        assert _kept(_addition_hunk(new_start=9, new_count=74), *spans) == expected
    assert warning in caplog.text


def test_a_deletion_only_hunk_accepts_no_spans(caplog: pytest.LogCaptureFixture) -> None:
    parsed = ParsedHunk(header="@@ -9,4 +8,0 @@", old_start=9, old_count=4, new_start=8, new_count=0, body="-a\n")
    with caplog.at_level(logging.WARNING, logger=hunks_mod.__name__):
        assert _kept(parsed, _sp("b1")) == []
    assert "unknown boundary b1" in caplog.text


def test_the_validators_output_round_trips_through_the_on_disk_format() -> None:
    parsed = ParsedHunk(
        header="@@ -1,1 +1,4 @@", old_start=1, old_count=1, new_start=1, new_count=4, body="-a\n+a1\n+a2\n+a3\n+a4\n"
    )
    bounds = boundaries.Boundaries.for_hunk(parsed)
    ann = hunks_mod.build_hunk_annotations(parsed, {"spans": [_sp("b1", "b4"), _sp("b2", "b3"), _sp("b3")]}, bounds)
    diff = _minimal(parsed.body, old_count=1, new_count=4, spans=ann.spans)
    assert _ranges(parse_augmented_diff(emit_augmented_diff(diff))) == [(1, 4), (2, 3), (3, 3)]


# --- #21: every drop bucket is unrepresentable in the submission schema -----
#
# The corpus buckets were: end overshoot (by one and by more), a start on
# the previous segment's last line, a start before the hunk / in pre-image
# coordinates, a segment subsumed by another, and `count <= 0`. Each was an
# integer the model computed. The schema carries no integer a span could
# be positioned by, and the ids it does carry resolve only inside the list
# the prompt showed.


def test_the_submission_schema_carries_no_integer_span_coordinate() -> None:
    schema = HunkSubmission.model_json_schema()
    span = schema["$defs"]["SpanSubmission"]["properties"]
    assert set(span) == {"start", "end", "intent", "smells", "context", "refs"}
    for name in ("start", "end"):
        types = {span[name].get("type")} | {a.get("type") for a in span[name].get("anyOf", [])}
        assert "integer" not in types and "number" not in types
    # The old coordinates are not fields at all.
    text = str(schema)
    for retired in ("new_start", "new_count", "line_notes", "segments"):
        assert retired not in text


@pytest.mark.parametrize("value", [83, 12.0, -1, 0])
def test_a_numeric_span_edge_is_rejected_by_the_schema(value: object) -> None:
    with pytest.raises(ValueError):
        SpanSubmission.model_validate({"start": value})


def test_end_overshoot_has_no_id_to_be_written_with() -> None:
    """The hunk +9..+82: the last id names +82 and no id names +83."""
    bounds = boundaries.Boundaries.for_hunk(_addition_hunk(new_start=9, new_count=74))
    last_id = bounds.id_for(82)
    assert bounds.line_for(last_id) == 82
    assert bounds.line_for(f"b{int(last_id[1:]) + 1}") is None
    assert all(9 <= line <= 82 for line in bounds.lines)


def test_a_pre_image_coordinate_has_no_id() -> None:
    """A hunk whose pre-image starts at +71 and post-image at +89: the ids
    name +89.. only, so a base-side number is not among them."""
    parsed = ParsedHunk(
        header="@@ -71,50 +89,71 @@",
        old_start=71,
        old_count=50,
        new_start=89,
        new_count=71,
        body="".join(f"+l{n}\n" for n in range(71)) + "".join("-g\n" for _ in range(50)),
    )
    bounds = boundaries.Boundaries.for_hunk(parsed)
    assert min(bounds.lines) == 89
    for base_line in range(71, 89):
        with pytest.raises(KeyError):
            bounds.id_for(base_line)


def test_a_zero_or_negative_extent_cannot_be_expressed() -> None:
    """There is no count. A pair of ids is at least one line, and an
    inverted pair is dropped rather than read as a negative count."""
    parsed = _addition_hunk(new_start=1, new_count=5)
    assert _kept(parsed, _sp("b3", "b3")) == [(3, 3)]
    assert _kept(parsed, _sp("b4", "b2")) == []


def test_a_subsumed_span_is_nesting_not_an_error() -> None:
    """The corpus' 'covered by the previous segment' bucket, and the 18
    region-then-callout hunks: hierarchy the flat partition could not hold."""
    parsed = _addition_hunk(new_start=452, new_count=6)  # +452..+457
    assert _kept(parsed, _sp("b1", "b6"), _sp("b5")) == [(452, 457), (456, 456)]
