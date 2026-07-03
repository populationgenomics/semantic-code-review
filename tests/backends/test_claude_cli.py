"""`claude-cli`: backend resolution + `ClaudeCLIModel` driver behaviour.

Two interfaces share one file:

- The backend adapter (`ClaudeCliBackend`): PATH preflight,
  `supports_auto`, `resolve()` → subprocess `Client`.
- The CLI driver (`ClaudeCLIModel`): subprocess argv assembly, envelope
  parsing, error mapping, MCP injection. Tests run through `Agent.run`
  with the subprocess mocked, so the real pydantic-ai wire shape is
  exercised end-to-end.
"""

from __future__ import annotations

import json
import os

import pytest
import typer
from pydantic_ai import Agent
from pydantic_ai.output import ToolOutput

from semantic_code_review.augment.schemas import HunkAnnotations
from semantic_code_review.augment.tools import RepoTools
from semantic_code_review.backends.claude_cli import (
    ClaudeCliBackend,
    ClaudeCLIError,
    ClaudeCLIModel,
)
from semantic_code_review.config import BackendDef, BackendType

from ._subproc_fakes import FakeProc, claude_envelope, install_fake_subproc

# ---------------------------------------------------------------------------
# Backend adapter
# ---------------------------------------------------------------------------

def test_resolve_returns_subprocess_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_claude = fake_bin / "claude"
    fake_claude.write_text("#!/bin/sh\nexit 0\n")
    fake_claude.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")

    bdef = BackendDef(type=BackendType.CLAUDE_CLI, default_model="claude-opus-4-7")
    client = ClaudeCliBackend("claude-cli", bdef).resolve(model="claude-opus-4-7")
    assert isinstance(client.model, ClaudeCLIModel)
    assert client.is_subprocess_backend is True


def test_resolve_raises_when_claude_missing(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    bdef = BackendDef(type=BackendType.CLAUDE_CLI)
    with pytest.raises(typer.BadParameter, match="not on PATH"):
        ClaudeCliBackend("claude-cli", bdef).resolve(model="claude-opus-4-7")


def test_supports_auto_when_claude_on_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_claude = fake_bin / "claude"
    fake_claude.write_text("#!/bin/sh\nexit 0\n")
    fake_claude.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")

    bdef = BackendDef(type=BackendType.CLAUDE_CLI)
    assert ClaudeCliBackend("claude-cli", bdef).supports_auto() is True


def test_supports_auto_false_when_claude_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    bdef = BackendDef(type=BackendType.CLAUDE_CLI)
    assert ClaudeCliBackend("claude-cli", bdef).supports_auto() is False


# ---------------------------------------------------------------------------
# CLI driver — fixtures + helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def claude_model(monkeypatch: pytest.MonkeyPatch) -> ClaudeCLIModel:
    import semantic_code_review.backends.claude_cli as mod
    monkeypatch.setattr(mod.shutil, "which", lambda _name: "/usr/bin/true")
    return ClaudeCLIModel(model="claude-opus-4-7")


def _agent(model) -> Agent:  # type: ignore[no-untyped-def]
    return Agent(
        model=model,
        output_type=ToolOutput(HunkAnnotations, name="submit_annotations"),
        instructions="SYS",
    )


def _freeform_agent(model) -> Agent:  # type: ignore[no-untyped-def]
    """A no-`output_type` Agent — the review-console shape (ADR 0002)."""
    return Agent(model=model, instructions="SYS")


# ---------------------------------------------------------------------------
# CLI driver — prompt builder
# ---------------------------------------------------------------------------

def test_build_claude_prompt_appends_task_instruction() -> None:
    out = ClaudeCLIModel._build_prompt("USER TEXT", "submit_annotations")
    assert "USER TEXT" in out
    assert "submit_annotations" in out
    assert "Do not include any prose" in out


# ---------------------------------------------------------------------------
# CLI driver — subprocess invocation
# ---------------------------------------------------------------------------

async def test_claude_model_round_trip_through_agent(
    claude_model: ClaudeCLIModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: the Agent parses the synthesized tool_use into HunkAnnotations."""
    proc = FakeProc(claude_envelope({"intent": "explain the refactor"}))
    calls = install_fake_subproc(monkeypatch, [proc])

    result = await _agent(claude_model).run("USER")
    assert isinstance(result.output, HunkAnnotations)
    assert result.output.intent == "explain the refactor"

    argv = calls[0]["argv"]
    # --json-schema carries the output_type's schema; --tools "" disables
    # claude's built-in tool catalogue (we drive tools via MCP only).
    assert "--json-schema" in argv
    assert "--tools" in argv and argv[argv.index("--tools") + 1] == ""
    # --bare must NOT be present: it disables OAuth/keychain auth, which
    # is the only reason we're in the subprocess fallback in the first
    # place. Regression guard.
    assert "--bare" not in argv
    assert "--permission-mode" in argv
    # Instructions registered on the Agent reach claude as --system-prompt.
    sys_idx = argv.index("--system-prompt") + 1
    assert "SYS" in argv[sys_idx]


async def test_claude_nonzero_exit_raises(
    claude_model: ClaudeCLIModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = FakeProc(b"", stderr=b"claude: rate limit hit\n", returncode=1)
    install_fake_subproc(monkeypatch, [proc])
    with pytest.raises(ClaudeCLIError, match="rate"):
        await _agent(claude_model).run("USER")


async def test_claude_not_logged_in_actionable_error(
    claude_model: ClaudeCLIModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """claude -p puts the real error in stdout even when exit code != 0."""
    proc = FakeProc(
        claude_envelope("Not logged in · Please run /login", is_error=True),
        stderr=b"",
        returncode=1,
    )
    install_fake_subproc(monkeypatch, [proc])
    with pytest.raises(ClaudeCLIError, match="not logged in"):
        await _agent(claude_model).run("USER")


async def test_claude_nonzero_exit_with_envelope_surfaces_result(
    claude_model: ClaudeCLIModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = FakeProc(
        claude_envelope("Model unavailable in your region", is_error=True),
        returncode=1,
    )
    install_fake_subproc(monkeypatch, [proc])
    with pytest.raises(ClaudeCLIError, match="Model unavailable"):
        await _agent(claude_model).run("USER")


async def test_claude_bad_result_json_raises(
    claude_model: ClaudeCLIModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-schema envelope with garbage in result and no structured_output."""
    proc = FakeProc(claude_envelope("not-json-at-all", use_structured_output=False))
    install_fake_subproc(monkeypatch, [proc])
    with pytest.raises(ClaudeCLIError, match="not valid JSON"):
        await _agent(claude_model).run("USER")


async def test_claude_missing_structured_output_raises(
    claude_model: ClaudeCLIModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Common failure: model tried to call a tool, produced no JSON."""
    envelope = {
        "type": "result", "subtype": "success", "is_error": False,
        "result": "",
        "stop_reason": "tool_use", "num_turns": 2,
        "session_id": "sess",
        "usage": {"input_tokens": 1, "output_tokens": 1,
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    }
    proc = FakeProc((json.dumps(envelope) + "\n").encode("utf-8"))
    install_fake_subproc(monkeypatch, [proc])
    with pytest.raises(ClaudeCLIError, match="no structured_output"):
        await _agent(claude_model).run("USER")


async def test_claude_mcp_injected_when_repo_tools_set(
    claude_model: ClaudeCLIModel, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    claude_model.set_repo_tools(RepoTools(
        head_worktree=tmp_path, repo_git=tmp_path, base_sha="b", head_sha="h",
    ))
    proc = FakeProc(claude_envelope({"intent": "with mcp"}))
    calls = install_fake_subproc(monkeypatch, [proc])
    await _agent(claude_model).run("USER")

    argv = calls[0]["argv"]
    assert "--mcp-config" in argv
    config_path = argv[argv.index("--mcp-config") + 1]
    assert "--strict-mcp-config" in argv
    # max-turns should be the MCP default (>1) so the agent can explore.
    assert int(argv[argv.index("--max-turns") + 1]) > 1
    config = json.loads(open(config_path, encoding="utf-8").read())
    server = config["mcpServers"]["scr"]
    assert server["type"] == "stdio"
    assert "semantic_code_review.augment.mcp_server" in server["args"]

    await claude_model.aclose()


async def test_claude_single_shot_when_no_repo_tools(
    claude_model: ClaudeCLIModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = FakeProc(claude_envelope({"intent": "single shot"}))
    calls = install_fake_subproc(monkeypatch, [proc])
    await _agent(claude_model).run("USER")

    argv = calls[0]["argv"]
    assert "--mcp-config" not in argv
    # Single-shot mode allows a few turns so the model has room to
    # redirect if it attempts a disallowed tool call before the JSON.
    assert int(argv[argv.index("--max-turns") + 1]) == 3


async def test_set_repo_tools_invalidates_cached_config(
    claude_model: ClaudeCLIModel, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Re-binding RepoTools must drop the previous temp config so the
    next call materialises a fresh one for the new worktree."""
    claude_model.set_repo_tools(RepoTools(
        head_worktree=tmp_path / "a", repo_git=tmp_path / "a",
        base_sha="x", head_sha="y",
    ))
    proc1 = FakeProc(claude_envelope({"intent": "1"}))
    proc2 = FakeProc(claude_envelope({"intent": "2"}))
    calls = install_fake_subproc(monkeypatch, [proc1, proc2])
    await _agent(claude_model).run("USER")
    first_config = calls[0]["argv"][calls[0]["argv"].index("--mcp-config") + 1]

    claude_model.set_repo_tools(RepoTools(
        head_worktree=tmp_path / "b", repo_git=tmp_path / "b",
        base_sha="x", head_sha="y",
    ))
    await _agent(claude_model).run("USER")
    second_config = calls[1]["argv"][calls[1]["argv"].index("--mcp-config") + 1]

    assert first_config != second_config
    await claude_model.aclose()


# ---------------------------------------------------------------------------
# CLI driver — free-form (review console, ADR 0002 Slice 5)
# ---------------------------------------------------------------------------

async def test_claude_freeform_round_trip(
    claude_model: ClaudeCLIModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A no-output_type Agent gets the envelope's `result` text back as
    the agent output, and the spawn omits the structured-output flags."""
    proc = FakeProc(
        claude_envelope("The guard handles the empty-list case.",
                        use_structured_output=False)
    )
    calls = install_fake_subproc(monkeypatch, [proc])

    result = await _freeform_agent(claude_model).run("why this guard?")
    assert result.output == "The guard handles the empty-list case."

    argv = calls[0]["argv"]
    # Free-form: no schema constraint and no submit-tool nudge in the prompt.
    assert "--json-schema" not in argv
    assert "--output-format" in argv and argv[argv.index("--output-format") + 1] == "json"
    # The flattened user text is piped via stdin, without the
    # "reply with JSON" task block the structured path appends.
    assert proc.stdin_written is not None
    stdin = proc.stdin_written.decode("utf-8")
    assert "why this guard?" in stdin
    assert "single JSON object" not in stdin


async def test_claude_freeform_passes_effort(
    claude_model: ClaudeCLIModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The console spawn carries `--effort` (reasoning depth) by default,
    so adaptive-thinking models don't answer at the bare default."""
    proc = FakeProc(claude_envelope("answer", use_structured_output=False))
    calls = install_fake_subproc(monkeypatch, [proc])
    await _freeform_agent(claude_model).run("why?")

    argv = calls[0]["argv"]
    assert "--effort" in argv
    assert argv[argv.index("--effort") + 1] == "high"


async def test_claude_freeform_effort_omitted_when_none(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """console_effort=None drops the flag entirely (CLI default depth)."""
    model = ClaudeCLIModel(model="claude-opus-4-7", console_effort=None)
    proc = FakeProc(claude_envelope("answer", use_structured_output=False))
    calls = install_fake_subproc(monkeypatch, [proc])
    await _freeform_agent(model).run("why?")
    assert "--effort" not in calls[0]["argv"]


async def test_claude_structured_path_has_no_effort(
    claude_model: ClaudeCLIModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The structured augment path is tuned separately — it must not
    inherit the console's `--effort`."""
    proc = FakeProc(claude_envelope({"intent": "explain the refactor"}))
    calls = install_fake_subproc(monkeypatch, [proc])
    await _agent(claude_model).run("review this hunk")
    assert "--effort" not in calls[0]["argv"]


async def test_claude_freeform_mcp_injected_when_repo_tools_set(
    claude_model: ClaudeCLIModel, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The console runs MCP-backed: the worktree server is wired into the
    free-form spawn exactly as for the structured path."""
    claude_model.set_repo_tools(RepoTools(
        head_worktree=tmp_path, repo_git=tmp_path, base_sha="b", head_sha="h",
    ))
    proc = FakeProc(claude_envelope("answer", use_structured_output=False))
    calls = install_fake_subproc(monkeypatch, [proc])
    await _freeform_agent(claude_model).run("explain")

    argv = calls[0]["argv"]
    assert "--mcp-config" in argv and "--strict-mcp-config" in argv
    assert int(argv[argv.index("--max-turns") + 1]) > 1
    await claude_model.aclose()


async def test_claude_freeform_empty_result_raises(
    claude_model: ClaudeCLIModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty free-form result (e.g. max-turns exhausted) is an error,
    not a silent empty answer."""
    proc = FakeProc(claude_envelope("", use_structured_output=False))
    install_fake_subproc(monkeypatch, [proc])
    with pytest.raises(ClaudeCLIError, match="empty result"):
        await _freeform_agent(claude_model).run("explain")


async def test_claude_freeform_not_logged_in_raises(
    claude_model: ClaudeCLIModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Error envelopes funnel through the same login-mapping as the
    structured path."""
    proc = FakeProc(
        claude_envelope("Not logged in · Please run /login", is_error=True),
        returncode=1,
    )
    install_fake_subproc(monkeypatch, [proc])
    with pytest.raises(ClaudeCLIError, match="not logged in"):
        await _freeform_agent(claude_model).run("explain")
