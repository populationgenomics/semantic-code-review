"""Change-explainer prose passes: seed, readiness, budget, apply step."""

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
            {"id": "map", "kind": "map", "pass_id": "skeleton", "title": "Map", "state": "ready"},
            {
                "id": "background",
                "kind": "background",
                "pass_id": "background",
                "title": "Background",
                "refs": [{"kind": "file", "id": "F0"}],
            },
            {"id": "intuition", "kind": "intuition", "pass_id": "walkthrough", "title": "Intuition"},
            {
                "id": "code",
                "kind": "code",
                "pass_id": "walkthrough",
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


def _prompt(diff, doc, *section_ids, overview_json: str = "{}") -> str:
    targets = [explainer_section.find_section(doc, sid) for sid in section_ids]
    return explainer_section.format_prose_prompt(diff, doc, targets, overview_json=overview_json)


def test_the_seed_carries_the_intents_of_the_anchored_hunks(diff: AnnotatedDiff, doc) -> None:
    text = _prompt(diff, doc, "code", overview_json='{"summary": "pagination"}')
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


def test_a_merged_call_gets_a_brief_and_a_hunk_list_per_section(diff: AnnotatedDiff, doc) -> None:
    """One call, two sections: each has to arrive with its own brief and
    its own anchored code, or the model cannot tell them apart."""
    text = _prompt(diff, doc, "intuition", "code")
    assert text.index("`intuition` — Intuition") < text.index("`code` — Code")
    assert "Intuition: the idea of the change" in text
    assert "Code: the walkthrough" in text
    assert "Write the Intuition and Code sections." in text


def test_the_merged_call_is_seeded_with_the_map_and_the_finished_background(
    diff: AnnotatedDiff,
    doc,
) -> None:
    """A call that cannot see what the document already says either
    repeats it or contradicts it."""
    background = explainer_section.find_section(doc, "background")
    background.state = "ready"
    background.body = "The RPC layer paged by offset."
    map_section = doc.sections[0]
    map_section.map_rows = [
        explainer_schema.MapRow(
            ref=explainer_schema.Reference(kind="file", id="F0"),
            why="the contract every field below follows from",
        )
    ]
    text = _prompt(diff, doc, "intuition", "code")
    assert "The RPC layer paged by offset." in text
    assert "the contract every field below follows from" in text


def test_a_section_the_skeleton_gave_nothing_is_told_so(diff: AnnotatedDiff, doc) -> None:
    assert "assigned this section no files" in _prompt(diff, doc, "intuition")


def test_only_the_anchored_hunks_carry_intents(diff: AnnotatedDiff, doc) -> None:
    text = _prompt(diff, doc, "background")
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


def _submission(**overrides) -> explainer_section.SubmittedSection:
    payload = {
        "section": "code",
        "body": "  The proto is the contract; everything below follows from it [F0].  ",
        "refs": [{"kind": "hunk", "id": "H0_0"}],
        "subsections": [],
        "toy_data": False,
    }
    payload.update(overrides)
    return explainer_section.SubmittedSection.model_validate(payload)


def _apply(doc, section, entry=None, *, ids, sources=None) -> None:
    """Apply one submitted entry to one section, as a pass of one."""
    entry = entry if entry is not None else _submission()
    explainer_section.apply_prose_submission(
        doc,
        [section],
        explainer_section.ExplainerProseSubmission(sections=[entry.model_copy(update={"section": section.kind})]),
        ids=ids,
        sources=sources,
    )


def test_a_written_section_goes_ready_and_keeps_its_own_references(diff: AnnotatedDiff, doc) -> None:
    section = explainer_section.find_section(doc, "code")
    _apply(doc, section, _submission(), ids=build_json.viewer_id_index(diff))
    assert section.state == "ready"
    assert section.body == "The proto is the contract; everything below follows from it [F0]."
    # The model narrowed the skeleton's two files to one hunk.
    assert [(r.kind, r.id) for r in section.refs] == [("hunk", "H0_0")]
    assert doc.dropped_refs == 0


def test_a_submission_that_narrows_nothing_leaves_the_skeleton_scope(diff: AnnotatedDiff, doc) -> None:
    section = explainer_section.find_section(doc, "code")
    _apply(doc, section, _submission(refs=[]), ids=build_json.viewer_id_index(diff))
    assert [r.id for r in section.refs] == ["F0", "F1"]


def test_invented_references_are_dropped_and_counted(diff: AnnotatedDiff, doc) -> None:
    section = explainer_section.find_section(doc, "code")
    _apply(
        doc,
        section,
        _submission(refs=[{"kind": "hunk", "id": "H9_9"}, {"kind": "file", "id": "F0"}]),
        ids=build_json.viewer_id_index(diff),
    )
    assert [r.id for r in section.refs] == ["F0"]
    assert doc.dropped_refs == 1


def test_code_subsections_become_child_sections_with_minted_ids(diff: AnnotatedDiff, doc) -> None:
    section = explainer_section.find_section(doc, "code")
    _apply(
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
    _apply(
        doc,
        section,
        _submission(subsections=[{"title": "invented", "body": "x", "refs": []}]),
        ids=build_json.viewer_id_index(diff),
    )
    assert section.subsections == []


def test_toy_data_latches_on_the_document(diff: AnnotatedDiff, doc) -> None:
    ids = build_json.viewer_id_index(diff)
    _apply(doc, explainer_section.find_section(doc, "intuition"), _submission(toy_data=True), ids=ids)
    assert doc.toy_data is True
    _apply(doc, explainer_section.find_section(doc, "code"), _submission(toy_data=False), ids=ids)
    # One section's invented example is enough for the footer to say so.
    assert doc.toy_data is True


# --- Background: tools, budget, provenance -------------------------------


def test_the_tool_block_is_shared_and_the_background_block_is_not() -> None:
    """Every prose call reads the repository, so the tool vocabulary is
    one block; only Background writes a skip box."""
    from semantic_code_review.augment.prompts import (
        EXPLAINER_BACKGROUND_GUIDANCE,
        EXPLAINER_TOOL_GUIDANCE,
    )

    assert "`references`" in EXPLAINER_TOOL_GUIDANCE
    assert "skip_box" not in EXPLAINER_TOOL_GUIDANCE
    assert "skip_box" in EXPLAINER_BACKGROUND_GUIDANCE
    # The shared block must not name tools: it is also the prefix of a
    # call with no budget left to use them.
    assert "read_file" not in EXPLAINER_SECTION_GUIDANCE


def test_a_call_with_tools_is_told_about_them_and_one_without_is_not() -> None:
    """Advertising a surface the pass cannot reach makes the model hedge
    — the same reason skills are disabled on the CLI path."""
    from semantic_code_review.augment.prompts import EXPLAINER_TOOL_GUIDANCE

    doc = _doc()
    walkthrough = [explainer_section.find_section(doc, k) for k in ("intuition", "code")]
    assert EXPLAINER_TOOL_GUIDANCE in explainer_section._guidance(walkthrough, with_tools=True)
    assert EXPLAINER_TOOL_GUIDANCE not in explainer_section._guidance(walkthrough, with_tools=False)


def test_only_the_background_call_carries_the_background_block() -> None:
    from semantic_code_review.augment.prompts import EXPLAINER_BACKGROUND_GUIDANCE

    doc = _doc()
    background = [explainer_section.find_section(doc, "background")]
    walkthrough = [explainer_section.find_section(doc, k) for k in ("intuition", "code")]
    assert EXPLAINER_BACKGROUND_GUIDANCE in explainer_section._guidance(background, with_tools=True)
    assert EXPLAINER_BACKGROUND_GUIDANCE not in explainer_section._guidance(walkthrough, with_tools=True)


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
    _apply(doc, background, ids=ids, sources=["schema/api.proto", "cmd/list.go"])
    assert background.sources == ["schema/api.proto", "cmd/list.go"]

    code = explainer_section.find_section(doc, "code")
    _apply(doc, code, _submission(), ids=ids)
    assert code.sources == []


def test_the_skip_box_lands_on_background_with_a_target_that_resolves(diff: AnnotatedDiff, doc) -> None:
    section = explainer_section.find_section(doc, "background")
    _apply(
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
    _apply(
        doc,
        section,
        _submission(skip_box={"body": "skip", "target_section_id": "appendix"}),
        ids=build_json.viewer_id_index(diff),
    )
    assert section.skip_box is None


def test_only_background_gets_a_skip_box(diff: AnnotatedDiff, doc) -> None:
    section = explainer_section.find_section(doc, "code")
    _apply(
        doc,
        section,
        _submission(skip_box={"body": "skip", "target_section_id": "intuition"}),
        ids=build_json.viewer_id_index(diff),
    )
    assert section.skip_box is None


def test_terms_land_as_a_definition_list(diff: AnnotatedDiff, doc) -> None:
    section = explainer_section.find_section(doc, "background")
    _apply(
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
    submission = explainer_section.SubmittedSection.model_validate(
        {"section": "code", "body": "x", "recorded_sources": ["invented.py"]}
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
    agent = explainer_section.make_explainer_prose_agent(TestModel(), "sys")
    runs = 0

    async def once() -> dict:
        nonlocal runs
        runs += 1
        args = {"sections": [{"section": "background", "body": "the system before"}]}
        with agent.override(model=TestModel(custom_output_args=args)):
            payload = await pass_.run_pass(
                pass_.PassMeta(name="explainer-background", submit_tool="submit_explainer_prose"),
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


# --- Passes, not sections ------------------------------------------------


def test_addressing_either_half_of_a_merged_pair_runs_the_same_call(doc) -> None:
    """The route names a section; what runs is the call that owns it."""
    for section_id in ("intuition", "code"):
        assert [s.id for s in explainer_section.pass_targets(doc, section_id)] == ["intuition", "code"]


def test_background_stays_a_call_of_its_own(doc) -> None:
    """Merging it would collapse its `base_sha` key to the narrower
    `(base_sha, head_sha)` one, which is the whole reason it is split."""
    assert [s.id for s in explainer_section.pass_targets(doc, "background")] == ["background"]


def test_a_merged_call_lands_both_of_its_sections(diff: AnnotatedDiff, doc) -> None:
    targets = explainer_section.pass_targets(doc, "code")
    written = explainer_section.apply_prose_submission(
        doc,
        targets,
        explainer_section.ExplainerProseSubmission(
            sections=[
                _submission(section="intuition", body="the idea"),
                _submission(section="code", body="the walkthrough"),
            ]
        ),
        ids=build_json.viewer_id_index(diff),
        sources=["schema/api.proto"],
    )
    assert [s.id for s in written] == ["intuition", "code"]
    assert [s.state for s in targets] == ["ready", "ready"]
    # The read list belongs to the call, so both sections carry it.
    assert all(s.sources == ["schema/api.proto"] for s in targets)


def test_a_merged_call_that_returns_one_section_lands_it(diff: AnnotatedDiff, doc) -> None:
    """Failing both because one is missing throws away prose that was
    already paid for."""
    targets = explainer_section.pass_targets(doc, "code")
    explainer_section.apply_prose_submission(
        doc,
        targets,
        explainer_section.ExplainerProseSubmission(sections=[_submission(section="intuition", body="the idea")]),
        ids=build_json.viewer_id_index(diff),
    )
    intuition, code = targets
    assert (intuition.state, intuition.body) == ("ready", "the idea")
    # `failed`, not `pending`: pending is what the viewer auto-queues,
    # and buying the same call again unasked is not a retry policy.
    assert code.state == "failed"


def test_a_section_the_call_was_not_asked_for_is_dropped(diff: AnnotatedDiff, doc) -> None:
    targets = explainer_section.pass_targets(doc, "background")
    explainer_section.apply_prose_submission(
        doc,
        targets,
        explainer_section.ExplainerProseSubmission(
            sections=[
                _submission(section="background", body="ground"),
                _submission(section="code", body="not yours"),
            ]
        ),
        ids=build_json.viewer_id_index(diff),
    )
    assert explainer_section.find_section(doc, "background").body == "ground"
    # Untouched: another call owns it.
    assert explainer_section.find_section(doc, "code").state == "pending"


def test_a_merged_call_is_ready_only_when_every_hunk_under_it_is(doc) -> None:
    """The call writes both sections, so it needs both anchored sets."""
    diff = _diff(intents=("adds cursor", "", "renames the field"))
    targets = explainer_section.pass_targets(doc, "intuition")
    refs = [ref for s in targets for ref in s.refs]
    assert explainer_section.readiness(diff, refs) == (2, 3)


# --- The document's shared turn budget ------------------------------------


def test_the_budget_is_shared_across_the_documents_passes() -> None:
    """A per-section cap is a cap on nothing: three sections at twelve is
    a document at thirty-six, which nobody chose."""
    doc = _doc()
    assert explainer_section.DOCUMENT_TURN_BUDGET - doc.turns_used == explainer_section.DOCUMENT_TURN_BUDGET
    doc.turns_used = 7
    assert explainer_section.DOCUMENT_TURN_BUDGET - doc.turns_used == explainer_section.DOCUMENT_TURN_BUDGET - 7


def test_a_pass_reports_the_requests_it_spent(tmp_path) -> None:
    """The budget is metered off `run_pass`, and the figure it reports is
    the one the trace records — the two cannot drift."""
    import asyncio
    import json

    from pydantic_ai.models.test import TestModel

    from semantic_code_review.augment import pass_

    agent = explainer_section.make_explainer_prose_agent(TestModel(), "sys")
    spent: list[int] = []
    trace_path = tmp_path / "explainer-walkthrough.json"
    args = {"sections": [{"section": "code", "body": "x"}]}
    with agent.override(model=TestModel(custom_output_args=args)):
        asyncio.run(
            pass_.run_pass(
                pass_.PassMeta(name="explainer-walkthrough", submit_tool="submit_explainer_prose"),
                client=Client(model="anthropic:x"),
                agent=agent,
                user_content="u",
                system="sys",
                model="m",
                cache_inputs=("u",),
                request_limit=6,
                trace_path=trace_path,
                on_requests=spent.append,
            )
        )
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert sum(spent) == trace["turn_budget"]["used"]
    assert trace["turn_budget"]["cap"] == 6


def test_a_cached_pass_spends_nothing_against_the_budget(tmp_path) -> None:
    """Prose the document already paid for must not narrow what is left
    for the pass that has not run."""
    import asyncio

    from pydantic_ai.models.test import TestModel

    from semantic_code_review.augment import pass_
    from semantic_code_review.cache.store import CacheStore

    cache = CacheStore(root=tmp_path / "cache", prompt_version="test")
    agent = explainer_section.make_explainer_prose_agent(TestModel(), "sys")
    args = {"sections": [{"section": "background", "body": "the system before"}]}
    spent: list[int] = []

    async def once() -> None:
        with agent.override(model=TestModel(custom_output_args=args)):
            await pass_.run_pass(
                pass_.PassMeta(name="explainer-background", submit_tool="submit_explainer_prose"),
                client=Client(model="anthropic:x"),
                agent=agent,
                user_content="u",
                system="sys",
                model="m",
                cache_inputs=("base1234",),
                cache=cache,
                on_requests=spent.append,
            )

    asyncio.run(once())
    first = sum(spent)
    asyncio.run(once())
    assert first > 0
    assert sum(spent) == first
