"""Live contract tests for the `claude-cli` backend.

Opt-in and normally skipped: these spawn the **real** Claude Code CLI, so
they cost subscription tokens, are slow, and are mildly non-deterministic.
Every other `claude-cli` test mocks the subprocess, which means they all
validate our *assumption* of the `claude -p --output-format json`
envelope and the MCP handshake — never the live contract. If Anthropic
changes either (the CLI is not a versioned API surface, so they can), the
mocked suite stays green and users hit it first. These tests are the guard
that catches that drift.

Run them when bumping the pinned `claude`, or on a schedule:

    SCR_LIVE_CLI=1 uv run pytest tests/backends/test_claude_cli_live.py -q

They require `claude` on PATH and a logged-in Claude Code (OAuth). CI skips
them (no `claude`, flag unset), so they never gate offline builds.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic_ai import Agent
from pydantic_ai.output import ToolOutput

from semantic_code_review.augment.schemas import HunkAnnotations
from semantic_code_review.augment.tools import RepoTools
from semantic_code_review.backends.claude_cli import ClaudeCLIModel

pytestmark = pytest.mark.skipif(
    not shutil.which("claude") or os.environ.get("SCR_LIVE_CLI") != "1",
    reason="live claude-cli contract test: run with SCR_LIVE_CLI=1 and `claude` on PATH",
)

_MODEL = "claude-opus-4-7"


def _structured_agent(model: ClaudeCLIModel) -> Agent[None, HunkAnnotations]:
    """The augment shape: a schema-constrained submit tool. Exercises the
    `--json-schema` / `structured_output` envelope path end to end."""
    return Agent(
        model=model,
        output_type=ToolOutput(HunkAnnotations, name="submit_annotations"),
        instructions="You review code diffs. Answer only via the submit tool.",
    )


async def test_live_structured_envelope_contract() -> None:
    """Single-shot: a real `claude -p --json-schema ...` envelope still
    parses into `HunkAnnotations`. Breaks if the envelope shape or the
    structured-output flags change upstream — the core CLI contract."""
    model = ClaudeCLIModel(model=_MODEL)
    result = await _structured_agent(model).run(
        "Hunk added to config.py:\n+ CONNECT_TIMEOUT = 30\nSummarise the intent of this change."
    )
    assert isinstance(result.output, HunkAnnotations)
    assert result.output.intent.strip(), "live claude returned an empty intent"


async def test_live_mcp_tool_loop_contract(tmp_path: Path) -> None:
    """MCP-backed: wire the stdio `RepoTools` server into a real spawn and
    confirm claude's MCP client still completes the handshake + tool loop
    into a validated result. Guards the claude<->our-MCP-server contract,
    which the mocked tests can only assert about our own half of."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "mod.py").write_text("TIMEOUT = 30\n")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@e",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@e",
    }
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "mod.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True, env=env)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    model = ClaudeCLIModel(model=_MODEL)
    model.set_repo_tools(RepoTools(head_worktree=repo, repo_git=repo, base_sha=sha, head_sha=sha))
    try:
        result = await _structured_agent(model).run(
            "Use the read_file tool to read mod.py, then summarise the intent of introducing its TIMEOUT constant."
        )
        assert isinstance(result.output, HunkAnnotations)
        assert result.output.intent.strip(), "live MCP-backed claude returned an empty intent"
    finally:
        await model.aclose()
