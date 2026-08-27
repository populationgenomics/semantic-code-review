"""Change-explainer skeleton pass: prompt carrier, prompt shape, apply step."""

from __future__ import annotations

import pytest

from semantic_code_review.augment import explainer, explainer_schema
from semantic_code_review.augment.agents import Client
from semantic_code_review.augment.prompts import (
    EXPLAINER_ROLE,
    EXPLAINER_SKELETON_GUIDANCE,
)
from semantic_code_review.augment.schemas import (
    AnnotatedDiff,
    FileAnnotations,
    FileRole,
    Overview,
    PRInfo,
    lift_diff,
)
from semantic_code_review.format.parse import parse_raw_diff
from semantic_code_review.structural import ChangedSymbol, SymbolDelta
from semantic_code_review.structural.symbols import SymbolRange
from semantic_code_review.viewer import build_json

_RAW = (
    "diff --git a/schema/api.proto b/schema/api.proto\n"
    "--- a/schema/api.proto\n"
    "+++ b/schema/api.proto\n"
    "@@ -1,1 +1,2 @@\n"
    " x\n"
    "+string cursor = 3;\n"
    "diff --git a/gen/api_pb.ts b/gen/api_pb.ts\n"
    "--- a/gen/api_pb.ts\n"
    "+++ b/gen/api_pb.ts\n"
    "@@ -1,1 +1,2 @@\n"
    " y\n"
    "+cursor?: string;\n"
    "@@ -20,1 +21,1 @@\n"
    "-old\n"
    "+new\n"
)


@pytest.fixture
def diff() -> AnnotatedDiff:
    d = lift_diff(parse_raw_diff(_RAW))
    d = d.model_copy(update={"pr": PRInfo(pr_url="", base_sha="base1234", head_sha="head5678", model="")})
    files = list(d.files)
    files[0] = files[0].model_copy(update={"ann": FileAnnotations(summary="adds a cursor field to the list request")})
    files[1] = files[1].model_copy(update={"ann": FileAnnotations(role=FileRole.GENERATED)})
    return d.model_copy(update={"files": files, "overview": Overview(summary="pagination", themes=["api"])})


# --- Prompt carrier ------------------------------------------------------


def test_sdk_backend_carries_the_guidance_in_the_system_prompt() -> None:
    """SDK backends cache on the system prefix, so the bulk block lives there."""
    system, prefix = explainer.carry_guidance(Client(model="anthropic:x"), EXPLAINER_SKELETON_GUIDANCE)
    assert EXPLAINER_ROLE in system
    assert EXPLAINER_SKELETON_GUIDANCE in system
    assert prefix == ""


def test_subprocess_backend_keeps_the_guidance_off_argv() -> None:
    """`--system-prompt` is argv, and a long command line is parsed
    quadratically by the endpoint agent on CPG laptops. Only the short
    fixed role string may ride it."""
    system, prefix = explainer.carry_guidance(
        Client(model="anthropic:x", is_subprocess_backend=True), EXPLAINER_SKELETON_GUIDANCE
    )
    assert system == EXPLAINER_ROLE
    assert prefix == EXPLAINER_SKELETON_GUIDANCE
    assert EXPLAINER_SKELETON_GUIDANCE not in system


def test_both_carriers_show_the_model_the_same_words() -> None:
    sdk_system, sdk_prefix = explainer.carry_guidance(Client(model="anthropic:x"), EXPLAINER_SKELETON_GUIDANCE)
    cli_system, cli_prefix = explainer.carry_guidance(
        Client(model="anthropic:x", is_subprocess_backend=True), EXPLAINER_SKELETON_GUIDANCE
    )
    assert f"{sdk_system}\n\n{sdk_prefix}".strip() == f"{cli_system}\n\n{cli_prefix}".strip()


# --- Prompt shape --------------------------------------------------------


def test_prompt_lists_every_file_by_its_viewer_id(diff: AnnotatedDiff) -> None:
    text = explainer.format_skeleton_prompt(diff, overview_json='{"summary": "pagination"}')
    assert "F0  schema/api.proto  +1 -0  (1 hunks, modified)" in text
    assert "F1  gen/api_pb.ts  +2 -1  (2 hunks, generated)" in text
    assert "adds a cursor field to the list request" in text
    assert '{"summary": "pagination"}' in text


def test_prompt_omits_the_symbol_section_when_the_delta_is_empty(diff: AnnotatedDiff) -> None:
    assert "# Symbols changed" not in explainer.format_skeleton_prompt(diff, overview_json="{}")
    assert "# Symbols changed" not in explainer.format_skeleton_prompt(diff, overview_json="{}", delta=SymbolDelta())


def test_prompt_seeds_the_symbol_delta_when_there_is_one(diff: AnnotatedDiff) -> None:
    delta = SymbolDelta(
        added=[
            ChangedSymbol(
                path="schema/api.proto",
                kind="field",
                name="cursor",
                qualified_name="ListRequest.cursor",
                range=SymbolRange(start_line=3, end_line=3, start_col=0, end_col=0),
            )
        ]
    )
    text = explainer.format_skeleton_prompt(diff, overview_json="{}", delta=delta)
    assert "# Symbols changed" in text
    assert "field ListRequest.cursor  (schema/api.proto)" in text
    assert "removed:" not in text  # empty buckets are not listed


# --- Apply step ----------------------------------------------------------


def _submission(**overrides) -> explainer.ExplainerSkeletonSubmission:
    payload = {
        "verdict": "narrate",
        "verdict_note": "a pagination cursor threaded from the proto to the client.",
        "figure_family": "boxes are services, dashed arrows are generated artefacts",
        "cast": ["ListRequest", " ", "api_pb.ts"],
        "map_rows": [
            {"file_id": "F0", "why": "  the contract; every field below follows from it  "},
            {"file_id": "F1", "why": "regenerated from the proto — confirm, do not read"},
        ],
        "section_refs": [
            {"section": "background", "file_ids": ["F0"]},
            {"section": "intuition", "file_ids": ["F0"]},
            {"section": "code", "file_ids": ["F0", "F1"]},
        ],
    }
    payload.update(overrides)
    return explainer.ExplainerSkeletonSubmission.model_validate(payload)


def test_skeleton_fills_the_map_and_leaves_prose_pending(diff: AnnotatedDiff) -> None:
    doc = explainer.build_skeleton_document(
        _submission(),
        base_sha="base1234",
        head_sha="head5678",
        ids=build_json.viewer_id_index(diff),
    )
    # Map leads: it is the only section the skeleton fills.
    assert [s.kind for s in doc.sections] == ["map", "background", "intuition", "code"]
    assert [s.state for s in doc.sections] == ["ready", "pending", "pending", "pending"]
    assert [s.body for s in doc.sections[1:]] == ["", "", ""]

    map_section = doc.sections[0]
    assert [r.ref.id for r in map_section.map_rows] == ["F0", "F1"]
    assert map_section.map_rows[0].why == "the contract; every field below follows from it"
    # The section's refs mirror its rows, so the sidebar and the coverage
    # count read one list.
    assert [r.id for r in map_section.refs] == ["F0", "F1"]
    assert doc.dropped_refs == 0
    assert doc.cast == ["ListRequest", "api_pb.ts"]
    assert doc.base_sha == "base1234"
    assert doc.head_sha == "head5678"


def test_prose_sections_land_with_the_files_they_are_written_about(diff: AnnotatedDiff) -> None:
    """A section's references are what its prose pass is seeded with, so
    the skeleton has to assign them — a section with none is written
    from the overview alone."""
    doc = explainer.build_skeleton_document(
        _submission(),
        base_sha="base1234",
        head_sha="head5678",
        ids=build_json.viewer_id_index(diff),
    )
    by_kind = {s.kind: [r.id for r in s.refs] for s in doc.sections}
    assert by_kind["background"] == ["F0"]
    assert by_kind["intuition"] == ["F0"]
    assert by_kind["code"] == ["F0", "F1"]
    assert all(r.kind == "file" for s in doc.sections for r in s.refs)


def test_a_section_the_skeleton_omitted_gets_no_references(diff: AnnotatedDiff) -> None:
    doc = explainer.build_skeleton_document(
        _submission(section_refs=[{"section": "code", "file_ids": ["F0"]}]),
        base_sha="base1234",
        head_sha="head5678",
        ids=build_json.viewer_id_index(diff),
    )
    by_kind = {s.kind: s.refs for s in doc.sections}
    assert by_kind["background"] == []
    assert by_kind["intuition"] == []
    assert [r.id for r in by_kind["code"]] == ["F0"]


def test_section_references_are_deduped_and_bad_ones_counted(diff: AnnotatedDiff) -> None:
    doc = explainer.build_skeleton_document(
        _submission(
            map_rows=[],
            section_refs=[
                {"section": "code", "file_ids": ["F0", "F0", "F7", "schema/api.proto"]},
            ],
        ),
        base_sha="base1234",
        head_sha="head5678",
        ids=build_json.viewer_id_index(diff),
    )
    code = next(s for s in doc.sections if s.kind == "code")
    assert [r.id for r in code.refs] == ["F0"]
    assert doc.dropped_refs == 2


def test_map_rows_naming_nothing_are_dropped_and_counted(diff: AnnotatedDiff) -> None:
    doc = explainer.build_skeleton_document(
        _submission(
            map_rows=[
                {"file_id": "F0", "why": "the contract"},
                {"file_id": "F9", "why": "a file that is not in this diff"},
                {"file_id": "schema/api.proto", "why": "a path, not an id"},
                {"file_id": "H0_0", "why": "a hunk id in the file id slot"},
            ]
        ),
        base_sha="base1234",
        head_sha="head5678",
        ids=build_json.viewer_id_index(diff),
    )
    rows = doc.sections[0].map_rows
    assert [r.ref.id for r in rows] == ["F0"]
    assert doc.dropped_refs == 3


def test_not_warranted_is_an_answer_not_an_empty_document(diff: AnnotatedDiff) -> None:
    """A small change gets "read them directly" rather than three
    pending sections inviting spend on a document nobody needs."""
    doc = explainer.build_skeleton_document(
        _submission(
            verdict="not_warranted",
            verdict_note="Two files, one added field. Read the proto, then confirm the generated client.",
            map_rows=[{"file_id": "F0", "why": "the added field"}],
        ),
        base_sha="base1234",
        head_sha="head5678",
        ids=build_json.viewer_id_index(diff),
    )
    assert doc.verdict == "not_warranted"
    assert doc.verdict_note.startswith("Two files")
    assert [s.kind for s in doc.sections] == ["map"]
    assert doc.sections[0].state == "ready"


def test_a_skeleton_document_round_trips_through_disk(tmp_path, diff: AnnotatedDiff) -> None:
    doc = explainer.build_skeleton_document(
        _submission(),
        base_sha="base1234",
        head_sha="head5678",
        ids=build_json.viewer_id_index(diff),
    )
    explainer_schema.save_explainer(tmp_path, doc)
    loaded = explainer_schema.load_explainer(tmp_path, base_sha="base1234", head_sha="head5678")
    assert loaded == doc
    assert explainer.document_to_payload(doc)["sections"][0]["map_rows"][0]["ref"]["kind"] == "file"
