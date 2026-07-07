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


async def test_live_console_session_resume_contract() -> None:
    """Free-form console continuity: a fact stated in turn 1 is recalled in
    turn 2 purely by resuming the `claude -p` session (`--resume <id>`), with
    no pydantic `message_history` replay. Guards the resume flag + envelope
    `session_id` round-trip the console relies on for cross-turn context —
    the mocked tests can only assert our half of that contract."""
    model = ClaudeCLIModel(model=_MODEL)
    agent = Agent(model=model, instructions="You are a helper.")

    await agent.run("Remember this secret code: PURPLE-ELEPHANT-42. Just acknowledge.")
    session_id = model.last_console_session_id
    assert session_id, "free-form turn did not capture a session id from the envelope"

    model.set_console_session(session_id)
    result = await agent.run(
        "What was the secret code? Answer from our prior conversation; reply with only the code.",
        message_history=None,
    )
    assert "PURPLE-ELEPHANT-42" in result.output, (
        f"resumed session did not restore turn-1 context (got {result.output!r}); "
        "the --resume contract may have drifted"
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
    confirm claude's MCP client completes the handshake + tool loop into a
    validated result.

    The file carries a distinctive constant value the prompt never states, so
    the assertion that it reappears in the answer proves the model *actually
    read the file over MCP* — not just that it produced some validated JSON.
    This guards the contract that a non-empty-result check missed: with the
    old `--tools ""` flag the MCP tools were silently unavailable and the pass
    answered from the prompt alone. Guards the claude<->our-MCP-server
    contract, which the mocked tests can only assert about our own half of."""
    repo = tmp_path / "repo"
    repo.mkdir()
    # An unguessable value: it can only appear in the answer via a real read.
    (repo / "mod.py").write_text("CONNECT_TIMEOUT_SECONDS = 91337\n")
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
            "Use the read_file tool to read mod.py. In your intent, state the exact "
            "numeric value assigned to the constant defined there."
        )
        assert isinstance(result.output, HunkAnnotations)
        assert "91337" in result.output.model_dump_json(), (
            "live MCP-backed claude did not surface the file's value — the read_file "
            "tool was likely unavailable (MCP tools disabled by the spawn flags)"
        )
    finally:
        await model.aclose()
