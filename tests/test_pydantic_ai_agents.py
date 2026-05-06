"""Smoke tests for the SDK-path Agent factories.

Uses pydantic-ai's `TestModel` so the tests don't touch real APIs. The
goal is to confirm that the factories wire the right output_type,
instructions, and tool registration — leaving deeper provider-specific
behaviour to the real-API smokes documented in the migration plan.
"""

from __future__ import annotations

import os

import pytest
from pydantic_ai.models.test import TestModel

from semantic_code_review.augment.agents import (
    SDKBackend,
    make_hunk_agent,
    make_overview_agent,
)
from semantic_code_review.augment.repo_tool_fns import TOOL_FUNCTIONS
from semantic_code_review.augment.schemas import HunkAnnotations, OverviewSubmission
from semantic_code_review.augment.tools import RepoTools


@pytest.fixture(autouse=True)
def _stub_anthropic_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Anthropic provider validates `ANTHROPIC_API_KEY` at construction.

    These tests never hit a real API (they `agent.override(model=TestModel())`)
    but the factory still has to walk the provider lookup, so a stub
    env var keeps the constructor happy.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stub-for-tests")


def test_overview_agent_factory_wires_output_type() -> None:
    agent = make_overview_agent("anthropic:claude-opus-4-7")
    # `output_type` is wrapped in `ToolOutput(name='submit_overview')`,
    # so unwrap to compare to the Pydantic model.
    assert agent.output_type.output is OverviewSubmission
    assert agent.output_type.name == "submit_overview"


def test_overview_agent_has_no_repo_tools() -> None:
    """Overview pass works from the prompt alone — no @agent.tool calls."""
    agent = make_overview_agent("anthropic:claude-opus-4-7")
    assert list(agent._function_toolset.tools.values()) == []


def test_hunk_agent_factory_wires_output_type() -> None:
    agent = make_hunk_agent("anthropic:claude-opus-4-7")
    assert agent.output_type.output is HunkAnnotations
    assert agent.output_type.name == "submit_annotations"


def test_hunk_agent_registers_repo_tools() -> None:
    agent = make_hunk_agent("anthropic:claude-opus-4-7")
    names = {t.name for t in agent._function_toolset.tools.values()}
    assert names == {fn.__name__ for fn in TOOL_FUNCTIONS}
    assert names == {"read_file", "read_file_at", "grep", "list_dir", "git_log"}


def test_overview_agent_runs_with_test_model() -> None:
    agent = make_overview_agent("anthropic:claude-opus-4-7")
    test_model = TestModel(custom_output_args={"summary": "ok", "files": []})
    with agent.override(model=test_model):
        result = agent.run_sync("# PR\ntitle: x\n")
    assert isinstance(result.output, OverviewSubmission)
    assert result.output.summary == "ok"


def test_hunk_agent_runs_with_test_model(tmp_path) -> None:
    agent = make_hunk_agent("anthropic:claude-opus-4-7")
    # `call_tools=[]` so TestModel doesn't try to invoke read_file etc.
    # against the stub RepoTools — we're verifying output_type wiring,
    # not tool execution (test_repo_tool_fns covers that).
    test_model = TestModel(custom_output_args={"intent": "noop"}, call_tools=[])
    head = tmp_path / "head"
    head.mkdir()
    deps = RepoTools(
        head_worktree=head,
        repo_git=head,
        base_sha="",
        head_sha="",
    )
    with agent.override(model=test_model):
        result = agent.run_sync("# Hunk", deps=deps)
    assert isinstance(result.output, HunkAnnotations)
    assert result.output.intent == "noop"


def test_sdk_backend_aclose_is_noop() -> None:
    import asyncio

    backend = SDKBackend(model_id="anthropic:claude-opus-4-7")
    asyncio.run(backend.aclose())
    assert backend.repo_tools is None
