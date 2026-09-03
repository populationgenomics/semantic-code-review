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
    AnnotatedFile,
    FileAnnotations,
    HunkAnnotations,
    OverviewSubmission,
)


def test_overview_submission_dump_has_keys_apply_overview_reads() -> None:
    """`apply_overview_to_diff` reads keys: summary, callgraph_edges,
    themes, files, groups. The model's dump must produce those same keys
    — and must not carry the retired symbol inventories, which the
    deterministic `SymbolDelta` owns (ADR 0001)."""
    sub = OverviewSubmission(summary="hi", files=[])
    dump = sub.model_dump(by_alias=True)
    expected = {"summary", "callgraph_edges", "themes", "files", "groups"}
    assert expected <= dump.keys()
    assert not {"symbols_added", "symbols_modified", "symbols_removed"} & dump.keys()


def test_hunk_annotations_dump_has_keys_apply_hunk_reads() -> None:
    """`apply_hunk_annotations` reads: intent, segments, smells,
    context, refs, confidence, line_notes. Fold summaries are the file's
    (`FileAnnotations.fold_descriptions`), not the hunk's."""
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
    }
    assert expected <= dump.keys()
    assert "fold_descriptions" not in dump
    assert "fold_descriptions" in FileAnnotations().model_dump()


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


def test_older_sidecar_fold_descriptions_lift_from_the_first_hunk_to_the_file() -> None:
    """A sidecar written when fold summaries lived on a hunk's annotations
    loads with them on the file, and the input dict is left as it was."""
    raw = {
        "path": "f.py",
        "diff_git_line": "diff --git a/f.py b/f.py",
        "hunks": [
            {
                "parsed": {
                    "header": "@@ -1 +1 @@",
                    "old_start": 1,
                    "old_count": 1,
                    "new_start": 1,
                    "new_count": 1,
                    "body": "",
                },
                "ann": {
                    "intent": "i",
                    "fold_descriptions": [{"context": "right", "right_start": 1, "right_end": 3, "summary": "lifted"}],
                },
            }
        ],
    }
    f = AnnotatedFile.model_validate(raw)
    assert [(fd.context, fd.right_start, fd.right_end, fd.summary) for fd in f.ann.fold_descriptions] == [
        ("right", 1, 3, "lifted")
    ]
    assert f.hunks[0].ann.intent == "i"
    assert "fold_descriptions" in raw["hunks"][0]["ann"]  # not mutated
    assert AnnotatedFile.model_validate({"path": "g.py", "diff_git_line": "x"}).ann.fold_descriptions == []
