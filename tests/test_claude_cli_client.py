"""ClaudeCLIClient: subprocess mocked out, contract with runner verified."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from semantic_code_review.augment.claude_cli_client import (
    ClaudeCLIClient,
    ClaudeCLIError,
    _flatten_system,
    _pick_submit_tool,
    _serialize_messages,
)
from semantic_code_review.augment.runner import run_agentic


SUBMIT_TOOL = {
    "name": "submit_annotations",
    "description": "",
    "input_schema": {
        "type": "object",
        "properties": {"intent": {"type": "string"}},
        "required": ["intent"],
    },
}


def _envelope(result: Any, *, is_error: bool = False, usage: dict | None = None) -> bytes:
    payload = {
        "type": "result",
        "subtype": "success",
        "is_error": is_error,
        "result": result if isinstance(result, str) else json.dumps(result),
        "stop_reason": "end_turn",
        "session_id": "sess-abc",
        "usage": usage or {
            "input_tokens": 42,
            "output_tokens": 17,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }
    return (json.dumps(payload) + "\n").encode("utf-8")


class _FakeProc:
    def __init__(self, stdout: bytes, stderr: bytes = b"", returncode: int = 0) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.stdin_written: bytes | None = None

    async def communicate(self, stdin: bytes | None = None) -> tuple[bytes, bytes]:
        self.stdin_written = stdin
        return self._stdout, self._stderr


@pytest.fixture
def cli_client(monkeypatch: pytest.MonkeyPatch) -> ClaudeCLIClient:
    # shutil.which used in __init__: pretend `claude` exists.
    import semantic_code_review.augment.claude_cli_client as mod
    monkeypatch.setattr(mod.shutil, "which", lambda _name: "/usr/bin/true")
    return ClaudeCLIClient()


def _install_fake_subprocess(
    monkeypatch: pytest.MonkeyPatch, proc: _FakeProc
) -> list[list[str]]:
    """Replace asyncio.create_subprocess_exec and record argv lists."""
    calls: list[list[str]] = []

    async def _fake(*args: str, **kwargs: Any) -> _FakeProc:
        calls.append(list(args))
        return proc

    import semantic_code_review.augment.claude_cli_client as mod
    monkeypatch.setattr(mod.asyncio, "create_subprocess_exec", _fake)
    return calls


def test_pick_submit_tool_last_wins() -> None:
    tools = [
        {"name": "read_file"},
        {"name": "submit_overview"},
        {"name": "submit_annotations"},
    ]
    assert _pick_submit_tool(tools) is tools[-1]


def test_flatten_system_joins_text_blocks() -> None:
    blocks = [
        {"type": "text", "text": "You are reviewing."},
        {"type": "text", "text": "Follow the schema."},
    ]
    assert _flatten_system(blocks) == "You are reviewing.\n\nFollow the schema."


def test_serialize_messages_appends_task() -> None:
    out = _serialize_messages(
        [{"role": "user", "content": [{"type": "text", "text": "hello"}]}],
        SUBMIT_TOOL,
    )
    assert "hello" in out
    assert "submit_annotations" in out


async def test_create_message_synthesizes_tool_use(
    cli_client: ClaudeCLIClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = _FakeProc(_envelope({"intent": "explain the refactor"}))
    calls = _install_fake_subprocess(monkeypatch, proc)

    response = await cli_client.create_message(
        model="claude-opus-4-7",
        max_tokens=4096,
        system=[{"type": "text", "text": "SYS"}],
        tools=[SUBMIT_TOOL],
        messages=[{"role": "user", "content": [{"type": "text", "text": "USER"}]}],
    )

    assert len(calls) == 1
    argv = calls[0]
    assert "--json-schema" in argv
    assert "--tools" in argv and argv[argv.index("--tools") + 1] == ""
    # --bare must NOT be present: it disables OAuth/keychain auth, which
    # is the only reason we're in the subprocess fallback in the first
    # place. See claude_cli_client.create_message for the full rationale.
    assert "--bare" not in argv
    assert "--permission-mode" in argv
    assert response["role"] == "assistant"
    assert response["content"] == [
        {
            "type": "tool_use",
            "id": response["content"][0]["id"],
            "name": "submit_annotations",
            "input": {"intent": "explain the refactor"},
        },
    ]
    assert response["usage"]["input_tokens"] == 42


async def test_create_message_drives_run_agentic(
    cli_client: ClaudeCLIClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: run_agentic accepts the synthesized response and terminates."""
    proc = _FakeProc(_envelope({"intent": "done"}))
    _install_fake_subprocess(monkeypatch, proc)

    result = await run_agentic(
        cli_client,
        model="claude-opus-4-7",
        system="SYS",
        user_content=[{"type": "text", "text": "USER"}],
        tools=[SUBMIT_TOOL],
        submit_tool_name="submit_annotations",
    )
    assert result.submit_args == {"intent": "done"}


async def test_nonzero_exit_raises(
    cli_client: ClaudeCLIClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = _FakeProc(b"", stderr=b"claude: rate limit hit\n", returncode=1)
    _install_fake_subprocess(monkeypatch, proc)
    with pytest.raises(ClaudeCLIError, match="rate"):
        await cli_client.create_message(
            model="claude-opus-4-7", max_tokens=4096,
            system=[], tools=[SUBMIT_TOOL], messages=[],
        )


async def test_not_logged_in_gets_actionable_error(
    cli_client: ClaudeCLIClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """claude -p puts the real error in stdout even when exit code != 0."""
    proc = _FakeProc(
        _envelope("Not logged in · Please run /login", is_error=True),
        stderr=b"",
        returncode=1,
    )
    _install_fake_subprocess(monkeypatch, proc)
    with pytest.raises(ClaudeCLIError, match="not logged in"):
        await cli_client.create_message(
            model="claude-opus-4-7", max_tokens=4096,
            system=[], tools=[SUBMIT_TOOL], messages=[],
        )


async def test_nonzero_exit_with_envelope_surfaces_result(
    cli_client: ClaudeCLIClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = _FakeProc(
        _envelope("Model unavailable in your region", is_error=True),
        returncode=1,
    )
    _install_fake_subprocess(monkeypatch, proc)
    with pytest.raises(ClaudeCLIError, match="Model unavailable"):
        await cli_client.create_message(
            model="claude-opus-4-7", max_tokens=4096,
            system=[], tools=[SUBMIT_TOOL], messages=[],
        )


async def test_bad_result_json_raises(
    cli_client: ClaudeCLIClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = _FakeProc(_envelope("not-json-at-all"))
    _install_fake_subprocess(monkeypatch, proc)
    with pytest.raises(ClaudeCLIError, match="not valid JSON"):
        await cli_client.create_message(
            model="claude-opus-4-7", max_tokens=4096,
            system=[], tools=[SUBMIT_TOOL], messages=[],
        )


async def test_missing_submit_tool_raises(
    cli_client: ClaudeCLIClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No submit_* present → we can't build --json-schema.
    _install_fake_subprocess(monkeypatch, _FakeProc(b""))
    with pytest.raises(ClaudeCLIError, match="submit_"):
        await cli_client.create_message(
            model="claude-opus-4-7", max_tokens=4096,
            system=[], tools=[{"name": "read_file", "input_schema": {}}],
            messages=[],
        )


async def test_mcp_config_injected_when_repo_tools_set(
    cli_client: ClaudeCLIClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from semantic_code_review.augment.tools import RepoTools

    cli_client.set_repo_tools(RepoTools(
        head_worktree=tmp_path, repo_git=tmp_path, base_sha="b", head_sha="h",
    ))

    proc = _FakeProc(_envelope({"intent": "with mcp"}))
    calls = _install_fake_subprocess(monkeypatch, proc)

    await cli_client.create_message(
        model="claude-opus-4-7", max_tokens=4096,
        system=[], tools=[SUBMIT_TOOL], messages=[],
    )

    argv = calls[0]
    assert "--mcp-config" in argv
    config_path = argv[argv.index("--mcp-config") + 1]
    assert "--strict-mcp-config" in argv
    # max-turns should be the MCP default (>1) so the agent can explore.
    max_turns_value = int(argv[argv.index("--max-turns") + 1])
    assert max_turns_value > 1
    # Config file content references the stdio MCP entrypoint.
    config = json.loads(open(config_path, encoding="utf-8").read())
    server = config["mcpServers"]["scr"]
    assert server["type"] == "stdio"
    assert "semantic_code_review.augment.mcp_server" in server["args"]

    await cli_client.aclose()


async def test_single_shot_when_no_repo_tools(
    cli_client: ClaudeCLIClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = _FakeProc(_envelope({"intent": "single shot"}))
    calls = _install_fake_subprocess(monkeypatch, proc)
    await cli_client.create_message(
        model="claude-opus-4-7", max_tokens=4096,
        system=[], tools=[SUBMIT_TOOL], messages=[],
    )
    argv = calls[0]
    assert "--mcp-config" not in argv
    assert int(argv[argv.index("--max-turns") + 1]) == 1
