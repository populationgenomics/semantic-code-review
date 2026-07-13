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


def test_resolve_returns_subprocess_client(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
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


def test_supports_auto_when_claude_on_path(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_claude = fake_bin / "claude"
    fake_claude.write_text("#!/bin/sh\nexit 0\n")
    fake_claude.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")

    bdef = BackendDef(type=BackendType.CLAUDE_CLI)
    assert ClaudeCliBackend("claude-cli", bdef).supports_auto() is True


def test_supports_auto_false_when_claude_missing(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
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
    # No MCP server is bound here (the overview shape): the model gets no
    # tools at all. `--tools ""` disables both MCP and the built-ins, so it
    # answers from the prompt in one turn. Advertising `mcp__scr` without a
    # server behind it is the regression that tripped `--max-turns`, so the
    # allow-list must be absent when the server is.
    assert "--json-schema" in argv
    assert argv[argv.index("--tools") + 1] == ""
    assert "--allowedTools" not in argv
    assert "--permission-mode" not in argv
    assert "bypassPermissions" not in argv
    assert "--disable-slash-commands" in argv
    # Single-shot: augment passes don't resume a session.
    assert "--no-session-persistence" in argv
    # --bare must NOT be present: it disables OAuth/keychain auth, which
    # is the only reason we're in the subprocess fallback in the first
    # place. Regression guard.
    assert "--bare" not in argv
    # Instructions registered on the Agent reach claude as --system-prompt.
    sys_idx = argv.index("--system-prompt") + 1
    assert "SYS" in argv[sys_idx]


async def test_claude_nonzero_exit_raises(claude_model: ClaudeCLIModel, monkeypatch: pytest.MonkeyPatch) -> None:
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


async def test_claude_bad_result_json_raises(claude_model: ClaudeCLIModel, monkeypatch: pytest.MonkeyPatch) -> None:
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
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "",
        "stop_reason": "tool_use",
        "num_turns": 2,
        "session_id": "sess",
        "usage": {
            "input_tokens": 1,
            "output_tokens": 1,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }
    proc = FakeProc((json.dumps(envelope) + "\n").encode("utf-8"))
    install_fake_subproc(monkeypatch, [proc])
    with pytest.raises(ClaudeCLIError, match="no structured_output"):
        await _agent(claude_model).run("USER")


async def test_claude_single_shot_when_no_endpoint(
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


_HTTP_ENDPOINT = {
    "type": "http",
    "url": "http://127.0.0.1:9999/mcp",
    "headers": {"Authorization": "Bearer test-token"},
}


async def test_claude_http_endpoint_injected_when_set(
    claude_model: ClaudeCLIModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hosted HTTP endpoint (ADR 0003 Slice 3) is written into the
    --mcp-config as a `type:"http"` server, not a stdio spawn."""
    claude_model.set_mcp_endpoint(_HTTP_ENDPOINT)
    proc = FakeProc(claude_envelope({"intent": "http mcp"}))
    calls = install_fake_subproc(monkeypatch, [proc])
    await _agent(claude_model).run("USER")

    argv = calls[0]["argv"]
    assert "--mcp-config" in argv
    assert "--strict-mcp-config" in argv
    # Server bound → the read-only tools are allow-listed alongside it.
    assert argv[argv.index("--allowedTools") + 1] == "mcp__scr"
    assert argv[argv.index("--permission-mode") + 1] == "default"
    assert "--tools" not in argv
    config_path = argv[argv.index("--mcp-config") + 1]
    server = json.loads(open(config_path, encoding="utf-8").read())["mcpServers"]["scr"]
    assert server == _HTTP_ENDPOINT
    await claude_model.aclose()


async def test_rebinding_endpoint_invalidates_cached_config(
    claude_model: ClaudeCLIModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-binding the endpoint drops the previous temp config so the next
    call materialises a fresh one for the new server."""
    claude_model.set_mcp_endpoint(_HTTP_ENDPOINT)
    proc1 = FakeProc(claude_envelope({"intent": "1"}))
    proc2 = FakeProc(claude_envelope({"intent": "2"}))
    calls = install_fake_subproc(monkeypatch, [proc1, proc2])
    await _agent(claude_model).run("USER")
    first_config = calls[0]["argv"][calls[0]["argv"].index("--mcp-config") + 1]

    claude_model.set_mcp_endpoint({**_HTTP_ENDPOINT, "url": "http://127.0.0.1:9998/mcp"})
    await _agent(claude_model).run("USER")
    second_config = calls[1]["argv"][calls[1]["argv"].index("--mcp-config") + 1]

    assert first_config != second_config
    await claude_model.aclose()


async def test_clearing_endpoint_reverts_to_single_shot(
    claude_model: ClaudeCLIModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """set_mcp_endpoint(None) drops MCP entirely."""
    claude_model.set_mcp_endpoint(_HTTP_ENDPOINT)
    claude_model.set_mcp_endpoint(None)
    proc = FakeProc(claude_envelope({"intent": "cleared"}))
    calls = install_fake_subproc(monkeypatch, [proc])
    await _agent(claude_model).run("USER")
    argv = calls[0]["argv"]
    assert "--mcp-config" not in argv
    # Server dropped → no tools advertised (the single-shot / overview shape).
    assert argv[argv.index("--tools") + 1] == ""
    assert "--allowedTools" not in argv


# ---------------------------------------------------------------------------
# CLI driver — free-form (review console, ADR 0002 Slice 5)
# ---------------------------------------------------------------------------


async def test_claude_freeform_round_trip(claude_model: ClaudeCLIModel, monkeypatch: pytest.MonkeyPatch) -> None:
    """A no-output_type Agent gets the envelope's `result` text back as
    the agent output, and the spawn omits the structured-output flags."""
    proc = FakeProc(claude_envelope("The guard handles the empty-list case.", use_structured_output=False))
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


async def test_claude_freeform_passes_effort(claude_model: ClaudeCLIModel, monkeypatch: pytest.MonkeyPatch) -> None:
    """The console spawn carries `--effort` (reasoning depth) by default,
    so adaptive-thinking models don't answer at the bare default."""
    proc = FakeProc(claude_envelope("answer", use_structured_output=False))
    calls = install_fake_subproc(monkeypatch, [proc])
    await _freeform_agent(claude_model).run("why?")

    argv = calls[0]["argv"]
    assert "--effort" in argv
    assert argv[argv.index("--effort") + 1] == "high"


async def test_claude_freeform_effort_omitted_when_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """console_effort=None drops the flag entirely (CLI default depth)."""
    import semantic_code_review.backends.claude_cli as mod

    monkeypatch.setattr(mod.shutil, "which", lambda _name: "/usr/bin/true")
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


async def test_claude_freeform_mcp_injected_when_endpoint_set(
    claude_model: ClaudeCLIModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The console runs MCP-backed: the hosted server is wired into the
    free-form spawn exactly as for the structured path."""
    claude_model.set_mcp_endpoint(_HTTP_ENDPOINT)
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


async def test_claude_debug_sink_receives_freeform_spawn_record(
    claude_model: ClaudeCLIModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a debug sink bound, a free-form turn emits one record carrying
    the spawn's argv + envelope summary."""
    records: list[dict] = []
    claude_model.set_debug_sink(records.append)
    proc = FakeProc(claude_envelope("the answer", use_structured_output=False))
    install_fake_subproc(monkeypatch, [proc])
    await _freeform_agent(claude_model).run("why?")

    assert len(records) == 1
    r = records[0]
    assert r["provider"] == "claude-cli"
    assert r["free_form"] is True
    assert r["returncode"] == 0
    assert r["envelope"]["session_id"] == "sess-abc"
    assert r["envelope"]["result_preview"] == "the answer"
    assert "--allowedTools" in r["argv"]


async def test_claude_debug_sink_off_by_default(claude_model: ClaudeCLIModel, monkeypatch: pytest.MonkeyPatch) -> None:
    """No sink bound → no record is built (zero overhead off the debug path)."""
    proc = FakeProc(claude_envelope("answer", use_structured_output=False))
    install_fake_subproc(monkeypatch, [proc])
    # Should not raise despite no sink; nothing to assert beyond a clean run.
    result = await _freeform_agent(claude_model).run("why?")
    assert result.output == "answer"


def test_redact_argv_truncates_system_prompt() -> None:
    from semantic_code_review.backends._cli_driver import _redact_argv

    argv = ["claude", "-p", "--system-prompt", "S" * 500, "--model", "opus"]
    out = _redact_argv(argv)
    assert out[:3] == ["claude", "-p", "--system-prompt"]
    assert out[3] != "S" * 500 and out[3].startswith("S") and "chars)" in out[3]
    # Non-redacted flags pass through untouched.
    assert out[4:] == ["--model", "opus"]


async def test_claude_freeform_tool_config_is_read_only_mcp(
    claude_model: ClaudeCLIModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The console exposes only our MCP tools and hard-denies mutating
    built-ins. It must NOT use the `--tools ""` + bypassPermissions combo:
    `--tools ""` disables MCP too (the model then leaks tool-call XML as
    text), and bypassPermissions silently grants built-in Bash/Edit."""
    proc = FakeProc(claude_envelope("answer", use_structured_output=False))
    calls = install_fake_subproc(monkeypatch, [proc])
    await _freeform_agent(claude_model).run("why?")

    argv = calls[0]["argv"]
    assert "--tools" not in argv
    assert "bypassPermissions" not in argv
    assert argv[argv.index("--permission-mode") + 1] == "default"
    assert argv[argv.index("--allowedTools") + 1] == "mcp__scr"
    disallowed = argv[argv.index("--disallowedTools") + 1 : argv.index("--disable-slash-commands")]
    assert {"Bash", "Edit", "Write"} <= set(disallowed)
    # Skills off: an advertised-but-unloadable skill makes the model hedge.
    assert "--disable-slash-commands" in argv


async def test_claude_freeform_first_turn_persists_and_captures_session(
    claude_model: ClaudeCLIModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First console turn: persistence stays on (no --no-session-persistence,
    no --resume) and the driver captures the envelope's session id to resume."""
    proc = FakeProc(claude_envelope("answer", use_structured_output=False))
    calls = install_fake_subproc(monkeypatch, [proc])
    await _freeform_agent(claude_model).run("why?")

    argv = calls[0]["argv"]
    assert "--no-session-persistence" not in argv
    assert "--resume" not in argv
    # `claude_envelope` reports session_id="sess-abc".
    assert claude_model.last_console_session_id == "sess-abc"


async def test_claude_freeform_resume_passes_session_id(
    claude_model: ClaudeCLIModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resumed console turn passes `--resume <id>` so the CLI restores the
    conversation (and its internal tool loop) rather than replaying text."""
    claude_model.set_console_session("sess-prev")
    proc = FakeProc(claude_envelope("answer", use_structured_output=False))
    calls = install_fake_subproc(monkeypatch, [proc])
    await _freeform_agent(claude_model).run("follow-up?")

    argv = calls[0]["argv"]
    assert "--resume" in argv and argv[argv.index("--resume") + 1] == "sess-prev"
    assert "--no-session-persistence" not in argv


async def test_claude_structured_path_keeps_session_persistence_off(
    claude_model: ClaudeCLIModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The structured augment path is single-shot: it stays
    --no-session-persistence and never resumes, even after a console turn set
    a session on the shared driver."""
    claude_model.set_console_session("sess-prev")
    proc = FakeProc(claude_envelope({"intent": "explain the refactor"}))
    calls = install_fake_subproc(monkeypatch, [proc])
    await _agent(claude_model).run("review this hunk")

    argv = calls[0]["argv"]
    assert "--no-session-persistence" in argv
    assert "--resume" not in argv


def _max_turns_envelope() -> bytes:
    """The real `claude -p` envelope for a `--max-turns` cutoff mid tool-loop
    (confirmed against CLI 2.1.201): `is_error=true`, `result=null`, and a
    distinguishing `subtype`."""
    envelope = {
        "type": "result",
        "subtype": "error_max_turns",
        "is_error": True,
        "result": None,
        "stop_reason": "tool_use",
        "num_turns": 20,
        "session_id": "sess",
        "usage": {
            "input_tokens": 1,
            "output_tokens": 1,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }
    return (json.dumps(envelope) + "\n").encode("utf-8")


async def test_claude_structured_max_turns_maps_to_actionable_error(
    claude_model: ClaudeCLIModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A max-turns cutoff (is_error=true, subtype=error_max_turns) names the
    turn limit and the fix, not the opaque generic 'returned error' text."""
    proc = FakeProc(_max_turns_envelope(), returncode=1)
    install_fake_subproc(monkeypatch, [proc])
    with pytest.raises(ClaudeCLIError, match="turn limit"):
        await _agent(claude_model).run("review this hunk")


async def test_claude_freeform_max_turns_maps_to_actionable_error(
    claude_model: ClaudeCLIModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The console path funnels the same envelope through the same mapping,
    so a tool-heavy turn that exhausts --max-turns surfaces the fix."""
    proc = FakeProc(_max_turns_envelope(), returncode=1)
    install_fake_subproc(monkeypatch, [proc])
    with pytest.raises(ClaudeCLIError, match="turn limit"):
        await _freeform_agent(claude_model).run("explain this change")


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
