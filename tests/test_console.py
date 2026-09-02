"""Review-console agent: factory wiring, hunk accessor, seed, turn driver.

The LLM is driven by pydantic-ai's `TestModel` so nothing touches a real
API — these confirm the free-form agent has no submit tool, the
console-only `hunk(id)` accessor resolves against a bound diff, the seed
carries the bounded context, and `run_console_turn` round-trips history.

The accounting tests are the guard on the console's share of the run's
quota: a turn that leaves no trace is a turn `usage.json` reports as
free, which is how the console's spend went missing in the first place.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import ClassVar, Self

import pytest
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.models import Model
from pydantic_ai.models.test import TestModel

from semantic_code_review import paths
from semantic_code_review.augment import explainer_schema, usage
from semantic_code_review.augment.agents import Client
from semantic_code_review.augment.console import (
    CONSOLE_SYSTEM,
    ConsoleCancelled,
    ConsoleNotReady,
    _format_selection,
    build_console_seed,
    make_console_agent,
    run_console_turn,
    stream_console_turn,
)
from semantic_code_review.augment.tools import RepoTools, console_tool_functions
from semantic_code_review.format.parse import parse_augmented_diff
from semantic_code_review.format.sidecar import dump_sidecar

FIXTURE = Path(__file__).parent / "fixtures" / "sample.augmented.diff"


@pytest.fixture(autouse=True)
def _stub_anthropic_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Anthropic provider validates the key at construction even
    though `agent.override(model=TestModel())` never hits the API."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stub-for-tests")


def _populate_run_dir(tmp_path: Path) -> paths.RunDir:
    run_dir = paths.RunDir(tmp_path).create()
    diff = parse_augmented_diff(FIXTURE.read_text(encoding="utf-8"))
    dump_sidecar(diff, run_dir.sidecar)
    return run_dir


# --- agent factory ------------------------------------------------------


def test_console_agent_is_free_form() -> None:
    """No `ToolOutput` submit tool — the console emits prose (str output)."""
    agent = make_console_agent("anthropic:claude-opus-4-7")
    assert agent.output_type is str


def test_console_agent_registers_repo_tools_plus_hunk() -> None:
    agent = make_console_agent("anthropic:claude-opus-4-7")
    names = {t.name for t in agent._function_toolset.tools.values()}
    assert names == {fn.__name__ for fn in console_tool_functions()}
    # The shared surface plus the console-only diff accessor.
    assert "hunk" in names
    assert "read_file" in names and "grep" in names


# --- hunk(id) accessor --------------------------------------------------


def test_hunk_accessor_resolves_bound_diff() -> None:
    diff = parse_augmented_diff(FIXTURE.read_text(encoding="utf-8"))
    rt = RepoTools(
        head_worktree=Path("/dev/null"),
        repo_git=Path("/dev/null"),
        base_sha="",
        head_sha="",
        diff=diff,
    )
    out = rt.hunk("H0_0")
    assert "src/users.py" in out
    assert "@@" in out  # the hunk header came through


def test_hunk_accessor_unbound_is_error() -> None:
    rt = RepoTools(
        head_worktree=Path("/dev/null"),
        repo_git=Path("/dev/null"),
        base_sha="",
        head_sha="",
    )
    assert rt.hunk("H0_0").startswith("error: no diff bound")


def test_hunk_accessor_bad_id_and_oob() -> None:
    diff = parse_augmented_diff(FIXTURE.read_text(encoding="utf-8"))
    rt = RepoTools(
        head_worktree=Path("/dev/null"),
        repo_git=Path("/dev/null"),
        base_sha="",
        head_sha="",
        diff=diff,
    )
    assert "malformed" in rt.hunk("nope")
    assert "file index" in rt.hunk("H99_0")
    assert "hunk index" in rt.hunk("H0_99")


# --- seed ---------------------------------------------------------------


def test_build_console_seed_carries_bounded_context() -> None:
    diff = parse_augmented_diff(FIXTURE.read_text(encoding="utf-8"))
    seed = build_console_seed(diff, symbol_delta_json='{"added":[]}', explainer=None)
    assert "# PR overview" in seed
    assert "# Changed files" in seed
    assert "src/users.py" in seed
    assert "# Structural symbol delta" in seed
    assert '{"added":[]}' in seed


def test_build_console_seed_omits_delta_when_absent() -> None:
    diff = parse_augmented_diff(FIXTURE.read_text(encoding="utf-8"))
    seed = build_console_seed(diff, symbol_delta_json=None, explainer=None)
    assert "# Structural symbol delta" not in seed


# --- change-explainer reach (Slice 5) -----------------------------------


def _document() -> explainer_schema.ExplainerDocument:
    """A document over the one-file fixture: a ready Map, a pending
    Background, and a Code section with one ready subsection."""
    return explainer_schema.ExplainerDocument(
        base_sha="base",
        head_sha="head",
        verdict="narrate",
        sections=[
            explainer_schema.Section(
                id="map",
                kind="map",
                pass_id="skeleton",
                title="Map",
                state="ready",
                map_rows=[
                    explainer_schema.MapRow(
                        ref=explainer_schema.Reference(kind="file", id="F0"),
                        why="the deactivation path lives here",
                    )
                ],
            ),
            explainer_schema.Section(id="background", kind="background", pass_id="background", title="Background"),
            explainer_schema.Section(
                id="code",
                kind="code",
                pass_id="walkthrough",
                title="Code",
                state="ready",
                body="The change is one guard.",
                subsections=[
                    explainer_schema.Section(
                        id="code_guard",
                        kind="code",
                        pass_id="walkthrough",
                        title="The guard",
                        state="ready",
                        body="`deactivate` now refuses an already-inactive user.",
                        refs=[explainer_schema.Reference(kind="hunk", id="H0_0")],
                    )
                ],
            ),
        ],
    )


def _explainer_tools() -> RepoTools:
    diff = parse_augmented_diff(FIXTURE.read_text(encoding="utf-8"))
    return RepoTools(
        head_worktree=Path("/dev/null"),
        repo_git=Path("/dev/null"),
        base_sha="base",
        head_sha="head",
        diff=diff,
        explainer=_document(),
    )


def test_console_agent_registers_section_accessor() -> None:
    agent = make_console_agent("anthropic:claude-opus-4-7")
    names = {t.name for t in agent._function_toolset.tools.values()}
    assert {"hunk", "section"} <= names


def test_find_section_descends_into_subsections() -> None:
    doc = _document()
    found = explainer_schema.find_section(doc, "code_guard")
    assert found is not None
    assert found.title == "The guard"
    assert explainer_schema.find_section(doc, "nope") is None


def test_section_accessor_renders_body_refs_and_subsections() -> None:
    out = _explainer_tools().section("code")
    assert "Code [id: code, state: ready]" in out
    assert "The change is one guard." in out
    assert "The guard [id: code_guard, state: ready]" in out
    assert "references: H0_0" in out


def test_section_accessor_resolves_file_refs_to_paths() -> None:
    """A `F<i>` id names nothing the other tools accept, so the Map's
    rows carry the path alongside it."""
    out = _explainer_tools().section("map")
    assert "F0 (src/users.py) — the deactivation path lives here" in out


def test_section_accessor_on_a_pending_section_says_so() -> None:
    """A section nobody has generated has no body to fabricate around."""
    out = _explainer_tools().section("background")
    assert "state: pending" in out
    assert "not written yet" in out


def test_section_accessor_unbound_and_unknown_id() -> None:
    diff = parse_augmented_diff(FIXTURE.read_text(encoding="utf-8"))
    unbound = RepoTools(
        head_worktree=Path("/dev/null"),
        repo_git=Path("/dev/null"),
        base_sha="",
        head_sha="",
        diff=diff,
    )
    assert unbound.section("map").startswith("error: no change-explainer document")
    assert _explainer_tools().section("nope").startswith("error: no section")


def test_seed_carries_the_section_list_without_bodies() -> None:
    diff = parse_augmented_diff(FIXTURE.read_text(encoding="utf-8"))
    seed = build_console_seed(diff, symbol_delta_json=None, explainer=_document())
    assert "# Change-explainer document" in seed
    assert "map: Map [ready]" in seed
    assert "background: Background [pending]" in seed
    assert "code_guard: The guard [ready] — refs: H0_0" in seed
    # Titles and references only: ADR 0002's "seed compact, pull on demand"
    # is the whole reason the bodies come through `section(id)`.
    assert "refuses an already-inactive user" not in seed
    assert "The change is one guard." not in seed


def test_seed_omits_the_document_block_when_there_is_none() -> None:
    """Nobody pressed the button — an ordinary state, not an error."""
    diff = parse_augmented_diff(FIXTURE.read_text(encoding="utf-8"))
    seed = build_console_seed(diff, symbol_delta_json=None, explainer=None)
    assert "Change-explainer" not in seed


def test_seed_file_list_carries_viewer_file_ids() -> None:
    """The document's references address `F<i>`; without the ids in the
    seed a seeded reference names nothing."""
    diff = parse_augmented_diff(FIXTURE.read_text(encoding="utf-8"))
    seed = build_console_seed(diff, symbol_delta_json=None, explainer=None)
    assert "- F0 src/users.py" in seed


# --- selection folding (Slice 4) ----------------------------------------


def _bound_tools() -> RepoTools:
    diff = parse_augmented_diff(FIXTURE.read_text(encoding="utf-8"))
    return RepoTools(
        head_worktree=Path("/dev/null"),
        repo_git=Path("/dev/null"),
        base_sha="",
        head_sha="",
        diff=diff,
    )


def test_format_selection_code_inlines_enclosing_hunk() -> None:
    """A code selection with a resolvable hunk id quotes the text and
    inlines the hunk via the `hunk(id)` accessor."""
    block = _format_selection(
        {
            "selection_text": "def deactivate(user):",
            "selection_kind": "code",
            "file": "src/users.py",
            "side": "new",
            "hunk_id": "H0_0",
            "line_range": [10, 12],
        },
        _bound_tools(),
    )
    assert "Reviewer selection (code)" in block
    assert "src/users.py" in block
    assert "lines 10–12" in block
    assert "def deactivate(user):" in block  # the quoted selection
    assert "Enclosing hunk:" in block
    assert "@@" in block  # the inlined hunk header


def test_format_selection_comment_is_text_only() -> None:
    """A comment selection carries just the quoted text — no hunk."""
    block = _format_selection(
        {"selection_text": "is this intentional?", "selection_kind": "comment"},
        _bound_tools(),
    )
    assert "Reviewer selection (comment)" in block
    assert "is this intentional?" in block
    assert "Enclosing hunk:" not in block


def test_format_selection_bad_hunk_id_degrades_to_text() -> None:
    """An unresolvable hunk id never leaks the accessor's error string —
    the block degrades to text-only."""
    block = _format_selection(
        {
            "selection_text": "x = 1",
            "selection_kind": "code",
            "file": "src/users.py",
            "hunk_id": "H99_99",
        },
        _bound_tools(),
    )
    assert "x = 1" in block
    assert "Enclosing hunk:" not in block
    assert "error:" not in block


def test_format_selection_empty_and_non_dict() -> None:
    rt = _bound_tools()
    assert _format_selection(None, rt) == ""
    assert _format_selection({}, rt) == ""
    assert _format_selection({"selection_text": "   "}, rt) == ""
    assert _format_selection("nope", rt) == ""


def test_format_selection_caps_oversized_text() -> None:
    block = _format_selection(
        {"selection_text": "x" * 9000, "selection_kind": "plain"},
        _bound_tools(),
    )
    assert "(truncated)" in block
    assert len(block) < 9000


def test_format_selection_explainer_inlines_body_and_anchored_hunks() -> None:
    """The richest of the four kinds: the claim *and* the code it is
    about, so "why does it claim this?" arrives answerable."""
    block = _format_selection(
        {
            "selection_text": "refuses an already-inactive user",
            "selection_kind": "explainer",
            "section_id": "code_guard",
        },
        _explainer_tools(),
    )
    assert "Reviewer selection (explainer) in section `code_guard`" in block
    assert "Section:" in block
    assert "The guard [id: code_guard, state: ready]" in block
    assert "Hunks this section anchors to:" in block
    assert "@@" in block
    assert "src/users.py" in block


def test_format_selection_explainer_pending_section_carries_no_prose() -> None:
    """A `pending` section still resolves — the accessor says it is not
    written rather than inventing a body — and anchors nothing."""
    block = _format_selection(
        {
            "selection_text": "Background",
            "selection_kind": "explainer",
            "section_id": "background",
        },
        _explainer_tools(),
    )
    assert "not written yet" in block
    assert "Hunks this section anchors to:" not in block


def test_format_selection_explainer_without_a_document_is_text_only() -> None:
    """The accessor's error string never reaches the prompt."""
    block = _format_selection(
        {
            "selection_text": "a claim",
            "selection_kind": "explainer",
            "section_id": "code",
        },
        _bound_tools(),
    )
    assert "a claim" in block
    assert "Section:" not in block
    assert "error:" not in block


def test_format_selection_explainer_outside_any_section() -> None:
    """A selection in the pane's footer or lede carries no `section_id`;
    it is still a selection, just without section context."""
    block = _format_selection(
        {"selection_text": "2 references", "selection_kind": "explainer"},
        _explainer_tools(),
    )
    assert "# Reviewer selection (explainer)\n" in block
    assert "2 references" in block
    assert "Section:" not in block


# --- turn driver --------------------------------------------------------


async def test_run_console_turn_not_ready_without_sidecar(tmp_path: Path) -> None:
    client = Client(model="anthropic:claude-opus-4-7")
    with pytest.raises(ConsoleNotReady):
        await run_console_turn(client, run_dir=paths.RunDir(tmp_path), question="what changed?")


async def test_run_console_turn_seeds_and_returns_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First turn seeds + answers; the returned history feeds the next
    turn. The agent is overridden with a TestModel so no API is hit."""
    run_dir = _populate_run_dir(tmp_path)
    client = Client(model="anthropic:claude-opus-4-7")

    # Force every console agent built in this test onto a canned-text
    # TestModel (call_tools=[] so it answers directly instead of
    # invoking read_file/grep against the absent worktrees). The factory
    # accepts a Model instance, so swap the resolved model out entirely.
    import semantic_code_review.augment.console as console_mod

    real_make = console_mod.make_console_agent

    def _make(_model):
        return real_make(TestModel(custom_output_text="grounded answer", call_tools=[]))

    monkeypatch.setattr(console_mod, "make_console_agent", _make)

    answer, history = await run_console_turn(
        client,
        run_dir=run_dir,
        question="why pagination?",
    )
    assert answer == "grounded answer"
    assert history  # full message_history for the next turn

    # Second turn threads the history back in and still answers.
    answer2, history2 = await run_console_turn(
        client,
        run_dir=run_dir,
        question="follow-up",
        history=history,
    )
    assert answer2 == "grounded answer"
    assert len(history2) > len(history)


# --- streaming driver ---------------------------------------------------


def _patch_test_model(
    monkeypatch: pytest.MonkeyPatch,
    *,
    output_text: str,
    call_tools: list[str],
) -> None:
    """Force every console agent onto a canned TestModel."""
    import semantic_code_review.augment.console as console_mod

    real_make = console_mod.make_console_agent

    def _make(_model):
        return real_make(TestModel(custom_output_text=output_text, call_tools=call_tools))

    monkeypatch.setattr(console_mod, "make_console_agent", _make)


async def test_stream_console_turn_pumps_deltas_and_tool_activity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The streaming driver pushes assistant text through `on_delta`
    chunk-by-chunk and announces each tool call to `on_tool`, and still
    returns the full answer + history."""
    run_dir = _populate_run_dir(tmp_path)
    client = Client(model="anthropic:claude-opus-4-7")
    # `hunk` resolves against the bound diff (no worktree needed), so the
    # forced tool call fires an activity event without erroring out.
    _patch_test_model(monkeypatch, output_text="streamed answer", call_tools=["hunk"])

    deltas: list[str] = []
    tools: list[str] = []
    answer, history = await stream_console_turn(
        client,
        run_dir=run_dir,
        question="why pagination?",
        on_delta=deltas.append,
        on_tool=tools.append,
        cancel=threading.Event(),
    )

    assert answer == "streamed answer"
    assert "".join(deltas) == "streamed answer"  # deltas reconstruct the answer
    assert tools and tools[0].startswith("hunk")
    assert history


async def test_stream_console_turn_cancel_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tripped cancel flag aborts the turn with `ConsoleCancelled`
    rather than returning an answer."""
    run_dir = _populate_run_dir(tmp_path)
    client = Client(model="anthropic:claude-opus-4-7")
    _patch_test_model(monkeypatch, output_text="unused", call_tools=[])

    cancel = threading.Event()
    cancel.set()  # pre-tripped: caught on the first node, before any output
    with pytest.raises(ConsoleCancelled):
        await stream_console_turn(
            client,
            run_dir=run_dir,
            question="why?",
            cancel=cancel,
        )


async def test_stream_console_turn_accepts_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pinned selection threads through the driver without disturbing
    the answer (the selection is folded into the turn's user message)."""
    run_dir = _populate_run_dir(tmp_path)
    client = Client(model="anthropic:claude-opus-4-7")
    _patch_test_model(monkeypatch, output_text="answer", call_tools=[])

    answer, history = await stream_console_turn(
        client,
        run_dir=run_dir,
        question="what does this do?",
        selection={
            "selection_text": "def deactivate(user):",
            "selection_kind": "code",
            "file": "src/users.py",
            "hunk_id": "H0_0",
            "line_range": [10, 12],
        },
    )
    assert answer == "answer"
    assert history


async def test_run_console_turn_is_streaming_wrapper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The blocking shape still works — it's a no-callback wrapper over
    the streaming driver."""
    run_dir = _populate_run_dir(tmp_path)
    client = Client(model="anthropic:claude-opus-4-7")
    _patch_test_model(monkeypatch, output_text="wrapped", call_tools=[])

    answer, history = await run_console_turn(
        client,
        run_dir=run_dir,
        question="q",
    )
    assert answer == "wrapped"
    assert history


# --- CLI subprocess backend (Slice 5) -----------------------------------


class _RecordingCLIModel(Model):
    """A minimal subprocess-style Model: records `set_mcp_endpoint` /
    `set_console_session` calls and answers free-form with a fixed text.
    Stands in for a CLI driver (which can't stream) so the one-shot console
    path can be exercised without a real subprocess. `last_console_session_id`
    reports a fixed id, as the real driver captures from the CLI envelope."""

    is_subprocess_backend = True

    def __init__(self, answer: str = "cli answer", session_id: str = "sess-1") -> None:
        super().__init__()
        self._answer = answer
        self._session_id = session_id
        self.mcp_endpoints: list = []
        self.console_sessions: list = []

    @property
    def model_name(self) -> str:
        return "recording-cli"

    @property
    def system(self) -> str:
        return "recording-cli"

    def set_mcp_endpoint(self, config) -> None:
        self.mcp_endpoints.append(config)

    def set_console_session(self, session_id) -> None:
        self.console_sessions.append(session_id)

    @property
    def last_console_session_id(self) -> str:
        return self._session_id

    async def request(self, messages, model_settings, model_request_parameters):
        from pydantic_ai.messages import ModelResponse, TextPart

        # Free-form turn: pydantic-ai leaves output_tools empty.
        assert not model_request_parameters.output_tools
        return ModelResponse(
            parts=[TextPart(content=self._answer)],
            model_name="recording-cli",
        )


class _FakeConsoleHost:
    """Stand-in for McpHttpHost so unit tests don't bind a real uvicorn."""

    instances: ClassVar[list] = []

    def __init__(self, repo_tools, *, on_tool=None, name="scr") -> None:  # type: ignore[no-untyped-def]
        self.repo_tools = repo_tools
        self.on_tool = on_tool
        self.started = False
        self.stopped = False
        _FakeConsoleHost.instances.append(self)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def mcp_config(self) -> dict:
        return {"type": "http", "url": "http://127.0.0.1:0/mcp", "headers": {"Authorization": "Bearer t"}}

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


@pytest.fixture(autouse=True)
def fake_console_mcp_host(monkeypatch: pytest.MonkeyPatch) -> type[_FakeConsoleHost]:
    """Console subprocess turns start a real HTTP MCP host; swap a fake so
    unit tests neither bind a socket nor need `claude`. A per-test patch (e.g.
    a host that fires on_tool) can still override this."""
    from semantic_code_review.augment import console as console_mod

    _FakeConsoleHost.instances = []
    monkeypatch.setattr(console_mod.mcp_http_host, "McpHttpHost", _FakeConsoleHost)
    return _FakeConsoleHost


async def test_stream_console_turn_cli_backend_runs_oneshot(
    tmp_path: Path,
    fake_console_mcp_host: type[_FakeConsoleHost],
) -> None:
    """A subprocess backend runs one-shot via `Agent.run`: the full
    answer comes back with no incremental `on_delta` chunks, and the turn
    hosts one MCP server for the call, torn down after."""
    run_dir = _populate_run_dir(tmp_path)
    model = _RecordingCLIModel(answer="grounded cli answer")
    client = Client(model=model, is_subprocess_backend=True)

    deltas: list[str] = []
    tools: list[str] = []
    answer, history = await stream_console_turn(
        client,
        run_dir=run_dir,
        question="why pagination?",
        on_delta=deltas.append,
        on_tool=tools.append,
        cancel=threading.Event(),
    )

    assert answer == "grounded cli answer"
    # CLI backends carry the `claude -p` session id forward, not pydantic
    # messages: the first turn resumes nothing and returns the session id.
    assert model.console_sessions == [None]
    assert history == "sess-1"
    # One-shot: nothing streamed incrementally.
    assert deltas == []
    assert tools == []
    # The turn hosts one MCP server: endpoint set for the call, cleared after.
    assert len(model.mcp_endpoints) == 2
    assert model.mcp_endpoints[0]["type"] == "http"
    assert model.mcp_endpoints[1] is None
    # Exactly one host, started then torn down.
    assert len(fake_console_mcp_host.instances) == 1
    assert fake_console_mcp_host.instances[0].started
    assert fake_console_mcp_host.instances[0].stopped


async def test_stream_console_turn_cli_backend_resumes_session(
    tmp_path: Path,
) -> None:
    """The session id from turn 1 threads into turn 2 as the resume target,
    so the CLI restores its own tool-loop context instead of replaying."""
    run_dir = _populate_run_dir(tmp_path)
    model = _RecordingCLIModel(session_id="sess-42")
    client = Client(model=model, is_subprocess_backend=True)

    _answer1, history = await stream_console_turn(client, run_dir=run_dir, question="first?")
    assert history == "sess-42"

    _answer2, history2 = await stream_console_turn(client, run_dir=run_dir, question="second?", history=history)
    # Turn 1 started fresh (None); turn 2 resumed the captured id.
    assert model.console_sessions == [None, "sess-42"]
    assert history2 == "sess-42"


async def test_cli_console_publishes_tool_activity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A tool call on the hosted server surfaces as an on_tool label — the
    console-tool visibility the CLI path gained (ADR 0003 Slice 3)."""
    from semantic_code_review.augment import console as console_mod

    run_dir = _populate_run_dir(tmp_path)

    class _FiringHost:
        def __init__(self, repo_tools, *, on_tool=None, name="scr") -> None:  # type: ignore[no-untyped-def]
            self._on_tool = on_tool

        def __enter__(self) -> Self:
            # Simulate the model reaching for a tool mid-turn.
            if self._on_tool is not None:
                self._on_tool("read_file", {"path": "users.py"})
            return self

        def __exit__(self, *exc: object) -> None:
            pass

        def mcp_config(self) -> dict:
            return {"type": "http", "url": "http://127.0.0.1:0/mcp", "headers": {"Authorization": "Bearer t"}}

    monkeypatch.setattr(console_mod.mcp_http_host, "McpHttpHost", _FiringHost)

    tools: list[str] = []
    await stream_console_turn(
        Client(model=_RecordingCLIModel(), is_subprocess_backend=True),
        run_dir=run_dir,
        question="q",
        on_tool=tools.append,
    )
    assert tools == ["read_file users.py"]


async def test_stream_console_turn_cli_backend_honours_cancel(
    tmp_path: Path,
) -> None:
    """A pre-tripped cancel aborts the CLI turn with `ConsoleCancelled`
    rather than returning the subprocess answer."""
    run_dir = _populate_run_dir(tmp_path)
    client = Client(model=_RecordingCLIModel(), is_subprocess_backend=True)

    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ConsoleCancelled):
        await stream_console_turn(
            client,
            run_dir=run_dir,
            question="why?",
            cancel=cancel,
        )


# --- accounting ---------------------------------------------------------


def _console_traces(run_dir: paths.RunDir) -> list[Path]:
    return sorted(run_dir.trace.glob("console-*.json"))


def _one_trace(run_dir: paths.RunDir) -> dict:
    traces = _console_traces(run_dir)
    assert len(traces) == 1, [t.name for t in traces]
    return json.loads(traces[0].read_text(encoding="utf-8"))


async def test_console_turn_records_a_trace_the_usage_summary_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn's spend lands in `trace/`, bucketed as its own pass — on a
    CLI backend it comes off the same subscription quota as the review."""
    run_dir = _populate_run_dir(tmp_path)
    client = Client(model="anthropic:claude-opus-4-7")
    _patch_test_model(monkeypatch, output_text="grounded answer", call_tools=[])

    await run_console_turn(client, run_dir=run_dir, question="why pagination?")

    trace = _one_trace(run_dir)
    assert trace["system"] == CONSOLE_SYSTEM
    assert "hunk" in trace["tools"]
    assert trace["submit_tool"] == ""  # free-form: no structured-output sink
    assert trace["iterations"]

    summary = usage.summarize_trace_dir(run_dir.trace)
    assert summary["passes"]["console"]["calls"] == 1
    assert summary["totals"]["calls"] == 1
    assert summary["totals"]["total_tokens"] > 0


async def test_each_console_turn_records_its_own_trace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two turns are two calls: a second turn must not overwrite the
    first's trace, which would under-report the run by a whole turn."""
    run_dir = _populate_run_dir(tmp_path)
    client = Client(model="anthropic:claude-opus-4-7")
    _patch_test_model(monkeypatch, output_text="answer", call_tools=[])

    _answer, history = await run_console_turn(client, run_dir=run_dir, question="same question")
    await run_console_turn(client, run_dir=run_dir, question="same question", history=history)

    assert len(_console_traces(run_dir)) == 2
    assert usage.summarize_trace_dir(run_dir.trace)["passes"]["console"]["calls"] == 2


async def test_console_turn_is_bounded_by_the_request_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The backend's ceiling bounds the conversation's tool loop too, and
    the requests it spent reaching the ceiling are still accounted."""
    run_dir = _populate_run_dir(tmp_path)
    client = Client(model="anthropic:claude-opus-4-7", request_limit=1)
    # A tool call on the first request, so the loop wants a second one.
    _patch_test_model(monkeypatch, output_text="never reached", call_tools=["hunk"])

    with pytest.raises(UsageLimitExceeded):
        await run_console_turn(client, run_dir=run_dir, question="what calls this?")

    trace = _one_trace(run_dir)
    assert trace["turn_budget"] == {"cap": 1, "used": 1}
    assert trace["error"]["type"] == "UsageLimitExceeded"
    assert usage.summarize_trace_dir(run_dir.trace)["passes"]["console"]["failed"] == 1


async def test_a_cancelled_turn_records_what_it_spent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling abandons the answer, not the bill: the requests made
    before the flag tripped are already spent and stay accounted."""
    run_dir = _populate_run_dir(tmp_path)
    client = Client(model="anthropic:claude-opus-4-7")
    _patch_test_model(monkeypatch, output_text="partial", call_tools=["hunk"])

    cancel = threading.Event()
    with pytest.raises(ConsoleCancelled):
        # Trip the flag once the first request has come back and its tool
        # call fires — mid-turn, with a request already paid for.
        await stream_console_turn(
            client,
            run_dir=run_dir,
            question="why?",
            on_tool=lambda _label: cancel.set(),
            cancel=cancel,
        )

    trace = _one_trace(run_dir)
    assert trace["error"]["type"] == "ConsoleCancelled"
    assert usage.summarize_trace_dir(run_dir.trace)["totals"]["total_tokens"] > 0


async def test_a_turn_cancelled_before_its_first_request_records_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-tripped cancel spends nothing, and a trace for it would
    report a call that never happened."""
    run_dir = _populate_run_dir(tmp_path)
    client = Client(model="anthropic:claude-opus-4-7")
    _patch_test_model(monkeypatch, output_text="unused", call_tools=[])

    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ConsoleCancelled):
        await stream_console_turn(client, run_dir=run_dir, question="why?", cancel=cancel)

    assert _console_traces(run_dir) == []


async def test_cli_console_turn_records_a_trace(tmp_path: Path) -> None:
    """The non-streaming CLI path records through the same envelope — it
    is the backend whose quota the console competes for."""
    run_dir = _populate_run_dir(tmp_path)
    client = Client(model=_RecordingCLIModel(), is_subprocess_backend=True)

    await stream_console_turn(client, run_dir=run_dir, question="why pagination?")

    trace = _one_trace(run_dir)
    assert trace["system"] == CONSOLE_SYSTEM
    assert len(trace["iterations"]) == 1
    assert usage.summarize_trace_dir(run_dir.trace)["passes"]["console"]["calls"] == 1


async def test_recording_leaves_the_streamed_frames_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The callbacks behind `console-tool` / `console-delta` fire in the
    same order and carry the same payloads as before the turn was
    recorded — the frontend contract is the sequence, not just the text."""
    run_dir = _populate_run_dir(tmp_path)
    client = Client(model="anthropic:claude-opus-4-7")
    _patch_test_model(monkeypatch, output_text="streamed answer", call_tools=["hunk"])

    events: list[tuple[str, str]] = []
    answer, _history = await stream_console_turn(
        client,
        run_dir=run_dir,
        question="why pagination?",
        on_delta=lambda text: events.append(("delta", text)),
        on_tool=lambda label: events.append(("tool", label)),
    )

    kinds = [kind for kind, _ in events]
    assert kinds[0] == "tool"  # the tool call is announced before the answer
    assert set(kinds[1:]) == {"delta"}
    assert events[0][1].startswith("hunk")
    assert "".join(text for kind, text in events if kind == "delta") == answer == "streamed answer"
