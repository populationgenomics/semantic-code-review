"""Change-explainer per-section prose: seed, readiness, apply step."""

from __future__ import annotations

import pytest

from semantic_code_review.augment import explainer_schema, explainer_section
from semantic_code_review.augment.agents import Client
from semantic_code_review.augment.explainer import carry_guidance
from semantic_code_review.augment.prompts import (
    EXPLAINER_ROLE,
    EXPLAINER_SECTION_GUIDANCE,
)
from semantic_code_review.augment.schemas import (
    AnnotatedDiff,
    FileAnnotations,
    HunkAnnotations,
    Overview,
    PRInfo,
    lift_diff,
)
from semantic_code_review.format.parse import parse_raw_diff
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


def _diff(*, intents: tuple[str, str, str] = ("adds cursor", "declares cursor", "renames the field")) -> AnnotatedDiff:
    d = lift_diff(parse_raw_diff(_RAW))
    d = d.model_copy(update={"pr": PRInfo(pr_url="", base_sha="base1234", head_sha="head5678", model="")})
    files = list(d.files)
    files[0] = files[0].model_copy(
        update={
            "ann": FileAnnotations(summary="adds a cursor field"),
            "hunks": [files[0].hunks[0].model_copy(update={"ann": HunkAnnotations(intent=intents[0])})],
        }
    )
    files[1] = files[1].model_copy(
        update={
            "hunks": [
                files[1].hunks[0].model_copy(update={"ann": HunkAnnotations(intent=intents[1])}),
                files[1].hunks[1].model_copy(update={"ann": HunkAnnotations(intent=intents[2])}),
            ]
        }
    )
    return d.model_copy(update={"files": files, "overview": Overview(summary="pagination", themes=["api"])})


def _doc(**overrides) -> explainer_schema.ExplainerDocument:
    payload = {
        "base_sha": "base1234",
        "head_sha": "head5678",
        "verdict": "narrate",
        "verdict_note": "a cursor threaded from the proto to the client.",
        "figure_family": "boxes are services",
        "cast": ["ListRequest"],
        "sections": [
            {"id": "map", "kind": "map", "title": "Map", "state": "ready"},
            {
                "id": "background",
                "kind": "background",
                "title": "Background",
                "refs": [{"kind": "file", "id": "F0"}],
            },
            {"id": "intuition", "kind": "intuition", "title": "Intuition"},
            {
                "id": "code",
                "kind": "code",
                "title": "Code",
                "refs": [{"kind": "file", "id": "F0"}, {"kind": "file", "id": "F1"}],
            },
        ],
    }
    payload.update(overrides)
    return explainer_schema.ExplainerDocument.model_validate(payload)


@pytest.fixture
def diff() -> AnnotatedDiff:
    return _diff()


@pytest.fixture
def doc() -> explainer_schema.ExplainerDocument:
    return _doc()


# --- Prompt carrier ------------------------------------------------------


def test_the_section_guidance_rides_the_same_carrier_split_as_the_skeleton() -> None:
    """The rule is per-backend, not per-pass: argv carries only bounded
    fixed strings, so every bulk block goes the same way."""
    sdk, sdk_prefix = carry_guidance(Client(model="anthropic:x"), EXPLAINER_SECTION_GUIDANCE)
    cli, cli_prefix = carry_guidance(
        Client(model="anthropic:x", is_subprocess_backend=True), EXPLAINER_SECTION_GUIDANCE
    )
    assert EXPLAINER_SECTION_GUIDANCE in sdk
    assert sdk_prefix == ""
    assert cli == EXPLAINER_ROLE
    assert cli_prefix == EXPLAINER_SECTION_GUIDANCE


# --- Anchors and readiness -----------------------------------------------


def test_a_file_reference_anchors_every_hunk_in_that_file(diff: AnnotatedDiff) -> None:
    refs = [explainer_schema.Reference(kind="file", id="F1")]
    assert explainer_section.anchored_hunks(diff, refs) == [(1, 0), (1, 1)]


def test_a_hunk_reference_anchors_only_itself_and_duplicates_collapse(diff: AnnotatedDiff) -> None:
    refs = [
        explainer_schema.Reference(kind="hunk", id="H1_1"),
        explainer_schema.Reference(kind="file", id="F1"),
    ]
    # Reference order wins: the hunk named first stays first.
    assert explainer_section.anchored_hunks(diff, refs) == [(1, 1), (1, 0)]


def test_a_section_whose_hunks_are_annotated_is_ready(diff: AnnotatedDiff, doc) -> None:
    code = explainer_section.find_section(doc, "code")
    assert explainer_section.readiness(diff, code.refs) == (3, 3)


def test_a_section_over_unannotated_hunks_is_not_ready(doc) -> None:
    """A per-hunk call that failed leaves an empty intent; narrating over
    it reads exactly as fluently as narrating over a real one."""
    diff = _diff(intents=("adds cursor", "", ""))
    code = explainer_section.find_section(doc, "code")
    assert explainer_section.readiness(diff, code.refs) == (1, 3)


def test_a_section_with_no_references_is_trivially_ready(diff: AnnotatedDiff, doc) -> None:
    intuition = explainer_section.find_section(doc, "intuition")
    assert explainer_section.readiness(diff, intuition.refs) == (0, 0)


# --- Prompt shape --------------------------------------------------------


def test_the_seed_carries_the_intents_of_the_anchored_hunks(diff: AnnotatedDiff, doc) -> None:
    text = explainer_section.format_section_prompt(
        diff,
        doc,
        explainer_section.find_section(doc, "code"),
        overview_json='{"summary": "pagination"}',
    )
    assert "H0_0" in text
    assert "adds cursor" in text
    assert "H1_1" in text
    assert "renames the field" in text
    # The skeleton's decisions are handed down, not re-chosen.
    assert "boxes are services" in text
    assert "ListRequest" in text
    # Every file id is valid to cite, even the ones this section is not
    # anchored to — the walkthrough may point outward.
    assert "F1  gen/api_pb.ts" in text
    assert "Write the Code section." in text


def test_a_section_the_skeleton_gave_nothing_is_told_so(diff: AnnotatedDiff, doc) -> None:
    text = explainer_section.format_section_prompt(
        diff,
        doc,
        explainer_section.find_section(doc, "intuition"),
        overview_json="{}",
    )
    assert "assigned this section no files" in text


def test_only_the_anchored_hunks_carry_intents(diff: AnnotatedDiff, doc) -> None:
    text = explainer_section.format_section_prompt(
        diff,
        doc,
        explainer_section.find_section(doc, "background"),
        overview_json="{}",
    )
    assert "H0_0" in text
    assert "H1_0" not in text


# --- Lookup --------------------------------------------------------------


def test_the_map_is_not_a_fillable_section(doc) -> None:
    """The skeleton writes it; a prose pass over it would overwrite the
    reading order with prose."""
    with pytest.raises(explainer_section.SectionNotFound):
        explainer_section.find_section(doc, "map")


def test_an_unknown_section_id_is_not_found(doc) -> None:
    with pytest.raises(explainer_section.SectionNotFound):
        explainer_section.find_section(doc, "code-1")


# --- Apply step ----------------------------------------------------------


def _submission(**overrides) -> explainer_section.ExplainerSectionSubmission:
    payload = {
        "body": "  The proto is the contract; everything below follows from it [F0].  ",
        "refs": [{"kind": "hunk", "id": "H0_0"}],
        "subsections": [],
        "toy_data": False,
    }
    payload.update(overrides)
    return explainer_section.ExplainerSectionSubmission.model_validate(payload)


def test_a_written_section_goes_ready_and_keeps_its_own_references(diff: AnnotatedDiff, doc) -> None:
    section = explainer_section.find_section(doc, "code")
    explainer_section.apply_section_submission(doc, section, _submission(), ids=build_json.viewer_id_index(diff))
    assert section.state == "ready"
    assert section.body == "The proto is the contract; everything below follows from it [F0]."
    # The model narrowed the skeleton's two files to one hunk.
    assert [(r.kind, r.id) for r in section.refs] == [("hunk", "H0_0")]
    assert doc.dropped_refs == 0


def test_a_submission_that_narrows_nothing_leaves_the_skeleton_scope(diff: AnnotatedDiff, doc) -> None:
    section = explainer_section.find_section(doc, "code")
    explainer_section.apply_section_submission(doc, section, _submission(refs=[]), ids=build_json.viewer_id_index(diff))
    assert [r.id for r in section.refs] == ["F0", "F1"]


def test_invented_references_are_dropped_and_counted(diff: AnnotatedDiff, doc) -> None:
    section = explainer_section.find_section(doc, "code")
    explainer_section.apply_section_submission(
        doc,
        section,
        _submission(refs=[{"kind": "hunk", "id": "H9_9"}, {"kind": "file", "id": "F0"}]),
        ids=build_json.viewer_id_index(diff),
    )
    assert [r.id for r in section.refs] == ["F0"]
    assert doc.dropped_refs == 1


def test_code_subsections_become_child_sections_with_minted_ids(diff: AnnotatedDiff, doc) -> None:
    section = explainer_section.find_section(doc, "code")
    explainer_section.apply_section_submission(
        doc,
        section,
        _submission(
            subsections=[
                {"title": "The contract", "body": "one", "refs": [{"kind": "file", "id": "F0"}]},
                {"title": "Its consumers", "body": "two", "refs": [{"kind": "hunk", "id": "H1_0"}]},
            ]
        ),
        ids=build_json.viewer_id_index(diff),
    )
    assert [s.id for s in section.subsections] == ["code-1", "code-2"]
    assert [s.title for s in section.subsections] == ["The contract", "Its consumers"]
    assert [s.state for s in section.subsections] == ["ready", "ready"]
    assert [r.id for r in section.subsections[1].refs] == ["H1_0"]


def test_subsections_outside_code_are_dropped(diff: AnnotatedDiff, doc) -> None:
    """The top level is fixed and Code is the only section whose parts
    are the model's to choose."""
    section = explainer_section.find_section(doc, "intuition")
    explainer_section.apply_section_submission(
        doc,
        section,
        _submission(subsections=[{"title": "invented", "body": "x", "refs": []}]),
        ids=build_json.viewer_id_index(diff),
    )
    assert section.subsections == []


def test_toy_data_latches_on_the_document(diff: AnnotatedDiff, doc) -> None:
    ids = build_json.viewer_id_index(diff)
    explainer_section.apply_section_submission(
        doc, explainer_section.find_section(doc, "intuition"), _submission(toy_data=True), ids=ids
    )
    assert doc.toy_data is True
    explainer_section.apply_section_submission(
        doc, explainer_section.find_section(doc, "code"), _submission(toy_data=False), ids=ids
    )
    # One section's invented example is enough for the footer to say so.
    assert doc.toy_data is True


# --- Background: tools, budget, provenance -------------------------------


def test_background_guidance_only_reaches_the_background_pass() -> None:
    """Tool instructions handed to a tool-less pass describe a surface
    that is not there."""
    from semantic_code_review.augment.prompts import EXPLAINER_BACKGROUND_GUIDANCE

    assert "read_file" in EXPLAINER_BACKGROUND_GUIDANCE
    assert "read_file" not in EXPLAINER_SECTION_GUIDANCE


def test_the_read_recorder_keeps_first_read_order_without_duplicates() -> None:
    rec = explainer_section._ReadRecorder()
    rec.record({"path": "a.py", "purpose": "x"})
    rec.record({"path": "b.py"})
    rec.record({"path": "a.py"})
    rec.record({"pattern": "cursor", "path_glob": "*.py"})  # a search, not a read
    rec.record({"path": "   "})
    assert rec.paths == ["a.py", "b.py"]


def test_the_recording_wrappers_keep_the_tool_schema_intact() -> None:
    """pydantic-ai builds each tool's schema by introspection, so a
    wrapper that loses the signature loses the tool."""
    import inspect

    from semantic_code_review.augment.tools import TOOL_FUNCTIONS

    rec = explainer_section._ReadRecorder()
    wrapped = explainer_section._recording_tool_functions(rec)
    assert [f.__name__ for f in wrapped] == [f.__name__ for f in TOOL_FUNCTIONS]
    for original, w in zip(TOOL_FUNCTIONS, wrapped, strict=True):
        assert inspect.signature(w) == inspect.signature(original)
        assert w.__doc__ == original.__doc__


def test_background_records_what_it_read_and_the_others_record_nothing(diff: AnnotatedDiff, doc) -> None:
    ids = build_json.viewer_id_index(diff)
    background = explainer_section.find_section(doc, "background")
    explainer_section.apply_section_submission(
        doc, background, _submission(), ids=ids, sources=["schema/api.proto", "cmd/list.go"]
    )
    assert background.sources == ["schema/api.proto", "cmd/list.go"]

    code = explainer_section.find_section(doc, "code")
    explainer_section.apply_section_submission(doc, code, _submission(), ids=ids)
    assert code.sources == []


def test_the_skip_box_lands_on_background_with_a_target_that_resolves(diff: AnnotatedDiff, doc) -> None:
    section = explainer_section.find_section(doc, "background")
    explainer_section.apply_section_submission(
        doc,
        section,
        _submission(skip_box={"body": "If you know the RPC layer,", "target_section_id": "code"}),
        ids=build_json.viewer_id_index(diff),
    )
    assert section.skip_box is not None
    assert section.skip_box.target_section_id == "code"


def test_a_skip_box_pointing_nowhere_is_dropped(diff: AnnotatedDiff, doc) -> None:
    """A jump to a section that is not there is worse than no jump."""
    section = explainer_section.find_section(doc, "background")
    explainer_section.apply_section_submission(
        doc,
        section,
        _submission(skip_box={"body": "skip", "target_section_id": "appendix"}),
        ids=build_json.viewer_id_index(diff),
    )
    assert section.skip_box is None


def test_only_background_gets_a_skip_box(diff: AnnotatedDiff, doc) -> None:
    section = explainer_section.find_section(doc, "code")
    explainer_section.apply_section_submission(
        doc,
        section,
        _submission(skip_box={"body": "skip", "target_section_id": "intuition"}),
        ids=build_json.viewer_id_index(diff),
    )
    assert section.skip_box is None


def test_terms_land_as_a_definition_list(diff: AnnotatedDiff, doc) -> None:
    section = explainer_section.find_section(doc, "background")
    explainer_section.apply_section_submission(
        doc,
        section,
        _submission(
            terms=[
                {"term": "  ListRequest  ", "definition": "  the paged request  "},
                {"term": "  ", "definition": "an entry with no name"},
            ]
        ),
        ids=build_json.viewer_id_index(diff),
    )
    assert [(t.term, t.definition) for t in section.terms] == [("ListRequest", "the paged request")]


def test_a_submission_carrying_a_sources_key_cannot_forge_the_citation_line() -> None:
    """The read list rides the payload so it survives the cache, but it
    is not a submission field — a model that emits one is ignored."""
    submission = explainer_section.ExplainerSectionSubmission.model_validate(
        {"body": "x", "recorded_sources": ["invented.py"]}
    )
    assert not hasattr(submission, "recorded_sources")


def test_the_recorded_read_list_rides_the_cache_with_the_prose(tmp_path) -> None:
    """A cache hit that lost the citation line would have the section
    claim it read nothing — which is exactly the claim the line exists to
    make believable."""
    import asyncio

    from pydantic_ai.models.test import TestModel

    from semantic_code_review.augment import pass_
    from semantic_code_review.cache.store import CacheStore

    cache = CacheStore(root=tmp_path / "cache", prompt_version="test")
    agent = explainer_section.make_explainer_section_agent(TestModel(), "sys")
    runs = 0

    async def once() -> dict:
        nonlocal runs
        runs += 1
        with agent.override(model=TestModel(custom_output_args={"body": "the system before"})):
            payload = await pass_.run_pass(
                pass_.PassMeta(name="explainer-background", submit_tool="submit_explainer_section"),
                client=Client(model="anthropic:x"),
                agent=agent,
                user_content="u",
                system="sys",
                model="m",
                cache_inputs=("base1234",),
                cache=cache,
                payload_extra=lambda: {"recorded_sources": [f"read-{runs}.py"]},
            )
        assert payload is not None
        return payload

    first = asyncio.run(once())
    second = asyncio.run(once())
    assert first["recorded_sources"] == ["read-1.py"]
    # Served from cache: the prose AND the provenance that produced it.
    assert second == first
