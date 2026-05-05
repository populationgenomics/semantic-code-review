"""Verify that submit_* JSON schemas come from `schemas.py` Pydantic
models and stay aligned with what `apply_*_to_diff` consumes."""

from __future__ import annotations

from semantic_code_review.augment.prompts import (
    SUBMIT_ANNOTATIONS_TOOL,
    SUBMIT_OVERVIEW_TOOL,
)
from semantic_code_review.augment.schemas import (
    HunkAnnotations,
    OverviewSubmission,
    SMELL_TAGS_TEXT,
)


# ---------------------------------------------------------------------------
# Schema is sourced from the model
# ---------------------------------------------------------------------------

def test_overview_tool_schema_matches_model_modulo_required() -> None:
    """The tool's input_schema is the model's JSON schema with `files`
    pinned back into the required list (the post-process keeps the
    submission contract strict while Python-side construction stays
    permissive). Anything else has to track the model exactly."""
    model_schema = OverviewSubmission.model_json_schema(by_alias=True)
    tool_schema = SUBMIT_OVERVIEW_TOOL["input_schema"]

    # Top-level required must include both summary (model-required) and
    # files (forced by prompts._force_required).
    assert "summary" in tool_schema["required"]
    assert "files" in tool_schema["required"]
    # Everything else under top-level should be identical.
    for key in ("type", "properties", "$defs"):
        if key in model_schema or key in tool_schema:
            assert tool_schema.get(key) == model_schema.get(key), (
                f"key {key!r} drifted between model and tool schema"
            )


def test_annotations_tool_schema_is_pure_passthrough() -> None:
    """No required-list patching for annotations — `intent` is the
    only mandatory field and it's already required by the model."""
    model_schema = HunkAnnotations.model_json_schema(by_alias=True)
    tool_schema = SUBMIT_ANNOTATIONS_TOOL["input_schema"]
    assert tool_schema == model_schema


# ---------------------------------------------------------------------------
# Required-key parity with the original hand-written schema
# ---------------------------------------------------------------------------

def test_overview_required_top_level() -> None:
    schema = SUBMIT_OVERVIEW_TOOL["input_schema"]
    assert sorted(schema["required"]) == ["files", "summary"]


def test_overview_callgraph_edge_uses_alias_keys() -> None:
    """OverviewEdge's Python fields are src/dst with from/to aliases —
    the wire format must use from/to so the model emits valid Python
    keywords-as-strings."""
    schema = SUBMIT_OVERVIEW_TOOL["input_schema"]
    edge = schema["$defs"]["OverviewEdge"]
    assert sorted(edge["required"]) == ["from", "to"]
    assert "src" not in edge["properties"]
    assert "dst" not in edge["properties"]


def test_annotations_required_top_level() -> None:
    schema = SUBMIT_ANNOTATIONS_TOOL["input_schema"]
    assert schema["required"] == ["intent"]


def test_smell_vocabulary_in_schema_matches_constant() -> None:
    """The closed smell vocabulary surfaces to the model via the Smell
    tag's description — keep it sourced from the catalogue, not
    duplicated in prompts.py text."""
    schema = SUBMIT_ANNOTATIONS_TOOL["input_schema"]
    smell = schema["$defs"]["Smell"]
    desc = smell["properties"]["tag"]["description"]
    assert desc == f"One of: {SMELL_TAGS_TEXT}"
    # Spot-check that every tag actually appears in the description.
    for tag in ("duplication", "string-sql", "race-condition"):
        assert tag in desc


# ---------------------------------------------------------------------------
# apply_* layer still consumes model_dump() shapes
# ---------------------------------------------------------------------------

def test_overview_submission_dump_has_keys_apply_overview_reads() -> None:
    """`apply_overview_to_diff` reads keys: summary, symbols_added,
    symbols_modified, symbols_removed, callgraph_edges, themes, files,
    groups. The model's dump must produce those same keys."""
    sub = OverviewSubmission(
        summary="hi",
        files=[],
    )
    dump = sub.model_dump(by_alias=True)
    expected = {
        "summary", "symbols_added", "symbols_modified", "symbols_removed",
        "callgraph_edges", "themes", "files", "groups",
    }
    assert expected <= dump.keys()


def test_hunk_annotations_dump_has_keys_apply_hunk_reads() -> None:
    """`apply_hunk_annotations` reads: intent, segments, smells,
    context, refs, confidence, line_notes, fold_descriptions."""
    sub = HunkAnnotations(intent="x")
    dump = sub.model_dump(by_alias=True)
    expected = {
        "intent", "segments", "smells", "context", "refs", "confidence",
        "line_notes", "fold_descriptions",
    }
    assert expected <= dump.keys()
