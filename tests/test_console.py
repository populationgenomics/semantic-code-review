"""Review-console agent: factory wiring, hunk accessor, seed, turn driver.

The LLM is driven by pydantic-ai's `TestModel` so nothing touches a real
API — these confirm the free-form agent has no submit tool, the
console-only `hunk(id)` accessor resolves against a bound diff, the seed
carries the bounded context, and `run_console_turn` round-trips history.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from pydantic_ai.models.test import TestModel

from semantic_code_review.augment.agents import Client
from semantic_code_review.augment.console import (
    ConsoleCancelled,
    ConsoleNotReady,
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


def _populate_run_dir(tmp_path: Path) -> Path:
    diff = parse_augmented_diff(FIXTURE.read_text(encoding="utf-8"))
    dump_sidecar(diff, tmp_path / "augmented.scr.json")
    return tmp_path


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
        head_worktree=Path("/dev/null"), repo_git=Path("/dev/null"),
        base_sha="", head_sha="", diff=diff,
    )
    out = rt.hunk("H0_0")
    assert "src/users.py" in out
    assert "@@" in out  # the hunk header came through


def test_hunk_accessor_unbound_is_error() -> None:
    rt = RepoTools(
        head_worktree=Path("/dev/null"), repo_git=Path("/dev/null"),
        base_sha="", head_sha="",
    )
    assert rt.hunk("H0_0").startswith("error: no diff bound")


def test_hunk_accessor_bad_id_and_oob() -> None:
    diff = parse_augmented_diff(FIXTURE.read_text(encoding="utf-8"))
    rt = RepoTools(
        head_worktree=Path("/dev/null"), repo_git=Path("/dev/null"),
        base_sha="", head_sha="", diff=diff,
    )
    assert "malformed" in rt.hunk("nope")
    assert "file index" in rt.hunk("H99_0")
    assert "hunk index" in rt.hunk("H0_99")


# --- seed ---------------------------------------------------------------


def test_build_console_seed_carries_bounded_context() -> None:
    diff = parse_augmented_diff(FIXTURE.read_text(encoding="utf-8"))
    seed = build_console_seed(diff, symbol_delta_json='{"added":[]}')
    assert "# PR overview" in seed
    assert "# Changed files" in seed
    assert "src/users.py" in seed
    assert "# Structural symbol delta" in seed
    assert '{"added":[]}' in seed


def test_build_console_seed_omits_delta_when_absent() -> None:
    diff = parse_augmented_diff(FIXTURE.read_text(encoding="utf-8"))
    seed = build_console_seed(diff, symbol_delta_json=None)
    assert "# Structural symbol delta" not in seed


# --- turn driver --------------------------------------------------------


async def test_run_console_turn_not_ready_without_sidecar(tmp_path: Path) -> None:
    client = Client(model="anthropic:claude-opus-4-7")
    with pytest.raises(ConsoleNotReady):
        await run_console_turn(client, run_dir=tmp_path, question="what changed?")


async def test_run_console_turn_seeds_and_returns_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
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

    def _make(_model):  # noqa: ANN001 — ignore the resolved string model
        return real_make(TestModel(custom_output_text="grounded answer", call_tools=[]))

    monkeypatch.setattr(console_mod, "make_console_agent", _make)

    answer, history = await run_console_turn(
        client, run_dir=run_dir, question="why pagination?",
    )
    assert answer == "grounded answer"
    assert history  # full message_history for the next turn

    # Second turn threads the history back in and still answers.
    answer2, history2 = await run_console_turn(
        client, run_dir=run_dir, question="follow-up", history=history,
    )
    assert answer2 == "grounded answer"
    assert len(history2) > len(history)


# --- streaming driver ---------------------------------------------------


def _patch_test_model(
    monkeypatch: pytest.MonkeyPatch, *, output_text: str, call_tools: list[str],
) -> None:
    """Force every console agent onto a canned TestModel."""
    import semantic_code_review.augment.console as console_mod

    real_make = console_mod.make_console_agent

    def _make(_model):  # noqa: ANN001 — ignore the resolved string model
        return real_make(
            TestModel(custom_output_text=output_text, call_tools=call_tools)
        )

    monkeypatch.setattr(console_mod, "make_console_agent", _make)


async def test_stream_console_turn_pumps_deltas_and_tool_activity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
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
        client, run_dir=run_dir, question="why pagination?",
        on_delta=deltas.append, on_tool=tools.append, cancel=threading.Event(),
    )

    assert answer == "streamed answer"
    assert "".join(deltas) == "streamed answer"  # deltas reconstruct the answer
    assert tools and tools[0].startswith("hunk")
    assert history


async def test_stream_console_turn_cancel_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
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
            client, run_dir=run_dir, question="why?", cancel=cancel,
        )


async def test_run_console_turn_is_streaming_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The blocking shape still works — it's a no-callback wrapper over
    the streaming driver."""
    run_dir = _populate_run_dir(tmp_path)
    client = Client(model="anthropic:claude-opus-4-7")
    _patch_test_model(monkeypatch, output_text="wrapped", call_tools=[])

    answer, history = await run_console_turn(
        client, run_dir=run_dir, question="q",
    )
    assert answer == "wrapped"
    assert history
