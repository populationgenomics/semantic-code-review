"""Verify the Pydantic submission models still produce the keys
`apply_*_to_diff` consumes downstream.

The submit-tool *wire schemas* came out in v0.12 — the SDK and CLI
paths now both go through pydantic-ai's `output_type=ToolOutput(...)`,
which derives the wire schema from the model directly. What still
matters is that `model_dump(by_alias=True)` produces dicts with the
keys our existing apply functions read.
"""

from __future__ import annotations

from semantic_code_review.augment.schemas import (
    SMELL_TAGS_TEXT,
    HunkAnnotations,
    OverviewSubmission,
)


def test_overview_submission_dump_has_keys_apply_overview_reads() -> None:
    """`apply_overview_to_diff` reads keys: summary, symbols_added,
    symbols_modified, symbols_removed, callgraph_edges, themes, files,
    groups. The model's dump must produce those same keys."""
    sub = OverviewSubmission(summary="hi", files=[])
    dump = sub.model_dump(by_alias=True)
    expected = {
        "summary",
        "symbols_added",
        "symbols_modified",
        "symbols_removed",
        "callgraph_edges",
        "themes",
        "files",
        "groups",
    }
    assert expected <= dump.keys()


def test_hunk_annotations_dump_has_keys_apply_hunk_reads() -> None:
    """`apply_hunk_annotations` reads: intent, segments, smells,
    context, refs, confidence, line_notes, fold_descriptions."""
    sub = HunkAnnotations(intent="x")
    dump = sub.model_dump(by_alias=True)
    expected = {
        "intent",
        "segments",
        "smells",
        "context",
        "refs",
        "confidence",
        "line_notes",
        "fold_descriptions",
    }
    assert expected <= dump.keys()


def test_smell_vocabulary_surfaces_in_field_description() -> None:
    """The closed smell vocabulary surfaces to the model via the Smell
    tag's Pydantic field description — keep it sourced from the
    catalogue, not duplicated in prompts.py text."""
    schema = HunkAnnotations.model_json_schema(by_alias=True)
    smell = schema["$defs"]["Smell"]
    desc = smell["properties"]["tag"]["description"]
    assert desc == f"One of: {SMELL_TAGS_TEXT}"
    for tag in ("duplication", "string-sql", "race-condition"):
        assert tag in desc


def test_overview_callgraph_edge_uses_alias_keys() -> None:
    """OverviewEdge's Python fields are src/dst with from/to aliases —
    the wire format must use from/to so the model emits valid JSON
    that maps onto Python keyword-aliased fields."""
    schema = OverviewSubmission.model_json_schema(by_alias=True)
    edge = schema["$defs"]["OverviewEdge"]
    assert sorted(edge["required"]) == ["from", "to"]
    assert "src" not in edge["properties"]
    assert "dst" not in edge["properties"]
