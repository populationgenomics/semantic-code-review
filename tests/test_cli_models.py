"""ClaudeCLIModel / GeminiCLIModel: subprocess mocked, Agent contract verified.

Both Models are exercised through `Agent.run(...)` with a fake
subprocess so we cover the real wire shape pydantic-ai sends them.
The tests assert subprocess argv, env vars, MCP injection, error
handling, and end-to-end output validation back through the Agent.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic_ai import Agent
from pydantic_ai.output import ToolOutput

from semantic_code_review.augment.cli_models import (
    ClaudeCLIError,
    ClaudeCLIModel,
    GeminiCLIError,
    GeminiCLIModel,
    _build_claude_prompt,
    _build_gemini_prompt,
    _extract_json_object,
    _flatten_messages,
    _SchemaValidationError,
    _validate_against_schema,
)
from semantic_code_review.augment.schemas import HunkAnnotations
from semantic_code_review.augment.tools import RepoTools


SCHEMA = HunkAnnotations.model_json_schema(by_alias=True)


# ---------------------------------------------------------------------------
# Fake subprocess primitives
# ---------------------------------------------------------------------------

class _FakeProc:
    def __init__(self, stdout: bytes, stderr: bytes = b"", returncode: int = 0) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.stdin_written: bytes | None = None

    async def communicate(self, stdin: bytes | None = None) -> tuple[bytes, bytes]:
        self.stdin_written = stdin
        return self._stdout, self._stderr


def _install_claude_subproc(monkeypatch: pytest.MonkeyPatch, procs: list[_FakeProc]) -> list[dict[str, Any]]:
    """Replace asyncio.create_subprocess_exec; record argv per call."""
    calls: list[dict[str, Any]] = []
    queue = list(procs)

    async def _fake(*args: str, **kwargs: Any) -> _FakeProc:
        calls.append({"argv": list(args), "kwargs": kwargs})
        if not queue:
            raise AssertionError("more subprocess calls than fake procs queued")
        return queue.pop(0)

    import semantic_code_review.augment.cli_models as mod
    monkeypatch.setattr(mod.asyncio, "create_subprocess_exec", _fake)
    return calls


# ---------------------------------------------------------------------------
# Envelope builders
# ---------------------------------------------------------------------------

def _claude_envelope(
    structured: Any,
    *,
    is_error: bool = False,
    use_structured_output: bool = True,
    usage: dict | None = None,
) -> bytes:
    """Build a `claude -p --output-format=json` envelope.

    With `--json-schema` active the validated JSON lives in
    `structured_output` and `result` is empty. Set
    `use_structured_output=False` to simulate the pre-schema shape
    that older `claude` versions emit.
    """
    payload: dict[str, Any] = {
        "type": "result",
        "subtype": "success",
        "is_error": is_error,
        "stop_reason": "end_turn",
        "session_id": "sess-abc",
        "usage": usage or {
            "input_tokens": 42,
            "output_tokens": 17,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }
    if is_error:
        payload["result"] = structured if isinstance(structured, str) else json.dumps(structured)
    elif use_structured_output:
        payload["result"] = ""
        payload["structured_output"] = structured
    else:
        payload["result"] = structured if isinstance(structured, str) else json.dumps(structured)
    return (json.dumps(payload) + "\n").encode("utf-8")


def _gemini_envelope(
    response: Any,
    *,
    error: dict | str | None = None,
    stats_models: dict | None = None,
) -> bytes:
    payload: dict[str, Any] = {
        "response": response if isinstance(response, str) else json.dumps(response),
        "stats": {
            "models": stats_models or {
                "gemini-2.5-pro": {
                    "tokens": {"input": 42, "candidates": 17, "cached": 0},
                },
            },
        },
        "session_id": "gem-sess-abc",
    }
    if error is not None:
        payload["error"] = error
    return (json.dumps(payload) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def claude_model(monkeypatch: pytest.MonkeyPatch) -> ClaudeCLIModel:
    import semantic_code_review.augment.cli_models as mod
    monkeypatch.setattr(mod.shutil, "which", lambda _name: "/usr/bin/true")
    return ClaudeCLIModel(model="claude-opus-4-7")


@pytest.fixture
def gemini_model(monkeypatch: pytest.MonkeyPatch) -> GeminiCLIModel:
    import semantic_code_review.augment.cli_models as mod
    monkeypatch.setattr(mod.shutil, "which", lambda _name: "/usr/bin/true")
    return GeminiCLIModel(model="gemini-2.5-pro")


def _agent(model) -> Agent:  # type: ignore[no-untyped-def]
    return Agent(
        model=model,
        output_type=ToolOutput(HunkAnnotations, name="submit_annotations"),
        instructions="SYS",
    )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_flatten_messages_separates_system_and_user() -> None:
    """SystemPromptParts go to the system channel; user prompts to the prompt body."""
    from datetime import datetime

    from pydantic_ai.messages import (
        ModelRequest,
        SystemPromptPart,
        UserPromptPart,
    )

    ts = datetime(2026, 5, 6)
    req = ModelRequest(
        parts=[
            SystemPromptPart(content="sys", timestamp=ts),
            UserPromptPart(content="hello", timestamp=ts),
        ],
        timestamp=ts,
    )
    sys_text, user_text = _flatten_messages([req])
    assert sys_text == "sys"
    assert "hello" in user_text


def test_build_claude_prompt_appends_task_instruction() -> None:
    out = _build_claude_prompt("USER TEXT", "submit_annotations")
    assert "USER TEXT" in out
    assert "submit_annotations" in out
    assert "Do not include any prose" in out


def test_build_gemini_prompt_includes_schema_and_no_prose_instruction() -> None:
    p = _build_gemini_prompt(
        system_text="you are reviewing",
        user_text="# user\nhello",
        submit_tool_name="submit_annotations",
        schema={"type": "object", "properties": {"intent": {"type": "string"}}, "required": ["intent"]},
        prior_error=None,
    )
    assert "you are reviewing" in p
    assert "hello" in p
    assert "submit_annotations" in p
    assert "Do not include any prose" in p
    assert '"intent"' in p


def test_build_gemini_prompt_appends_prior_error_on_retry() -> None:
    p = _build_gemini_prompt(
        system_text="", user_text="", submit_tool_name="submit_annotations",
        schema={"type": "object"},
        prior_error="missing required keys: ['intent']",
    )
    assert "Previous attempt failed" in p
    assert "missing required keys" in p


def test_extract_json_clean_object() -> None:
    assert _extract_json_object('{"intent": "x"}') == {"intent": "x"}


def test_extract_json_strips_fenced_block() -> None:
    text = '```json\n{"intent": "x"}\n```'
    assert _extract_json_object(text) == {"intent": "x"}


def test_extract_json_finds_balanced_object_among_prose() -> None:
    text = 'Sure! Here is the JSON:\n\n{"intent": "x", "nested": {"k": 1}}\n\nHope this helps.'
    assert _extract_json_object(text) == {"intent": "x", "nested": {"k": 1}}


def test_extract_json_raises_when_no_object() -> None:
    with pytest.raises(ValueError):
        _extract_json_object("just some prose, no JSON here")


def test_extract_json_handles_braces_inside_strings() -> None:
    text = '{"intent": "the } character", "ok": true}'
    assert _extract_json_object(text) == {"intent": "the } character", "ok": True}


def test_validate_against_schema_passes_on_required_present() -> None:
    schema = {"type": "object", "required": ["intent"]}
    _validate_against_schema({"intent": "x"}, schema)


def test_validate_against_schema_rejects_missing_required() -> None:
    schema = {"type": "object", "required": ["intent"]}
    with pytest.raises(_SchemaValidationError, match="missing required"):
        _validate_against_schema({}, schema)


def test_validate_against_schema_rejects_wrong_top_type() -> None:
    schema = {"type": "object"}
    with pytest.raises(_SchemaValidationError, match="object"):
        _validate_against_schema(["array", "instead"], schema)


# ---------------------------------------------------------------------------
# ClaudeCLIModel — subprocess invocation
# ---------------------------------------------------------------------------

async def test_claude_model_round_trip_through_agent(
    claude_model: ClaudeCLIModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: the Agent parses the synthesized tool_use into HunkAnnotations."""
    proc = _FakeProc(_claude_envelope({"intent": "explain the refactor"}))
    calls = _install_claude_subproc(monkeypatch, [proc])

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
    proc = _FakeProc(b"", stderr=b"claude: rate limit hit\n", returncode=1)
    _install_claude_subproc(monkeypatch, [proc])
    with pytest.raises(ClaudeCLIError, match="rate"):
        await _agent(claude_model).run("USER")


async def test_claude_not_logged_in_actionable_error(
    claude_model: ClaudeCLIModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """claude -p puts the real error in stdout even when exit code != 0."""
    proc = _FakeProc(
        _claude_envelope("Not logged in · Please run /login", is_error=True),
        stderr=b"",
        returncode=1,
    )
    _install_claude_subproc(monkeypatch, [proc])
    with pytest.raises(ClaudeCLIError, match="not logged in"):
        await _agent(claude_model).run("USER")


async def test_claude_nonzero_exit_with_envelope_surfaces_result(
    claude_model: ClaudeCLIModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = _FakeProc(
        _claude_envelope("Model unavailable in your region", is_error=True),
        returncode=1,
    )
    _install_claude_subproc(monkeypatch, [proc])
    with pytest.raises(ClaudeCLIError, match="Model unavailable"):
        await _agent(claude_model).run("USER")


async def test_claude_bad_result_json_raises(
    claude_model: ClaudeCLIModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-schema envelope with garbage in result and no structured_output."""
    proc = _FakeProc(_claude_envelope("not-json-at-all", use_structured_output=False))
    _install_claude_subproc(monkeypatch, [proc])
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
    proc = _FakeProc((json.dumps(envelope) + "\n").encode("utf-8"))
    _install_claude_subproc(monkeypatch, [proc])
    with pytest.raises(ClaudeCLIError, match="no structured_output"):
        await _agent(claude_model).run("USER")


async def test_claude_mcp_injected_when_repo_tools_set(
    claude_model: ClaudeCLIModel, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    claude_model.set_repo_tools(RepoTools(
        head_worktree=tmp_path, repo_git=tmp_path, base_sha="b", head_sha="h",
    ))
    proc = _FakeProc(_claude_envelope({"intent": "with mcp"}))
    calls = _install_claude_subproc(monkeypatch, [proc])
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
    proc = _FakeProc(_claude_envelope({"intent": "single shot"}))
    calls = _install_claude_subproc(monkeypatch, [proc])
    await _agent(claude_model).run("USER")

    argv = calls[0]["argv"]
    assert "--mcp-config" not in argv
    # Single-shot mode allows a few turns so the model has room to
    # redirect if it attempts a disallowed tool call before the JSON.
    assert int(argv[argv.index("--max-turns") + 1]) == 3


# ---------------------------------------------------------------------------
# GeminiCLIModel — subprocess invocation
# ---------------------------------------------------------------------------

async def test_gemini_model_round_trip_through_agent(
    gemini_model: GeminiCLIModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = _FakeProc(_gemini_envelope({"intent": "explain the refactor"}))
    calls = _install_claude_subproc(monkeypatch, [proc])

    result = await _agent(gemini_model).run("USER")
    assert isinstance(result.output, HunkAnnotations)
    assert result.output.intent == "explain the refactor"

    argv = calls[0]["argv"]
    assert argv[0] == "/usr/bin/true"
    assert argv[1] == "-p"
    # gemini's -p takes the prompt as an argv value (NOT via stdin —
    # that distinguishes it from claude -p). The argv slot immediately
    # after -p must be the prompt, not another flag, or gemini bails
    # with "Not enough arguments following: p". Regression guard.
    assert not argv[2].startswith("--"), f"prompt slot looks like a flag: {argv[2]!r}"
    assert "Reply with" in argv[2] or "submit_annotations" in argv[2]
    assert "--output-format" in argv and argv[argv.index("--output-format") + 1] == "json"
    # No repo_tools attached → no MCP wiring
    assert "--allowed-mcp-server-names" not in argv
    env = calls[0]["kwargs"]["env"]
    assert env["GEMINI_CLI_TRUST_WORKSPACE"] == "true"
    assert "GEMINI_CLI_SYSTEM_SETTINGS_PATH" not in env


async def test_gemini_retries_on_invalid_json(
    gemini_model: GeminiCLIModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = _FakeProc(_gemini_envelope("here is some prose, no json at all"))
    good = _FakeProc(_gemini_envelope({"intent": "fixed"}))
    _install_claude_subproc(monkeypatch, [bad, good])

    result = await _agent(gemini_model).run("USER")
    assert result.output.intent == "fixed"


async def test_gemini_retries_on_missing_required_key(
    gemini_model: GeminiCLIModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = _FakeProc(_gemini_envelope({"unrelated": "field"}))
    good = _FakeProc(_gemini_envelope({"intent": "now-with-required-key"}))
    _install_claude_subproc(monkeypatch, [bad, good])

    result = await _agent(gemini_model).run("USER")
    assert result.output.intent == "now-with-required-key"


async def test_gemini_raises_after_max_retries(
    gemini_model: GeminiCLIModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad1 = _FakeProc(_gemini_envelope("nothing useful"))
    bad2 = _FakeProc(_gemini_envelope("still nothing"))
    _install_claude_subproc(monkeypatch, [bad1, bad2])

    with pytest.raises(GeminiCLIError, match="schema-conformant"):
        await _agent(gemini_model).run("USER")


async def test_gemini_surfaces_auth_error_actionably(
    gemini_model: GeminiCLIModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = _FakeProc(
        _gemini_envelope("", error={"message": "request unauthenticated"}),
        returncode=1,
    )
    _install_claude_subproc(monkeypatch, [proc])
    with pytest.raises(GeminiCLIError, match="authenticated"):
        await _agent(gemini_model).run("USER")


async def test_gemini_sums_tokens_across_routed_models(
    gemini_model: GeminiCLIModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gemini routes through utility models alongside the main one;
    `stats.models` ends up with multiple entries. We must sum tokens
    across all of them, not pick one — otherwise the user's reported
    usage doesn't match what they're actually billed for."""
    proc = _FakeProc(_gemini_envelope(
        {"intent": "x"},
        stats_models={
            "gemini-2.5-pro": {
                "tokens": {"input": 1000, "candidates": 200, "cached": 800},
            },
            "gemini-utility-router": {
                "tokens": {"input": 50, "candidates": 5, "cached": 0},
            },
        },
    ))
    _install_claude_subproc(monkeypatch, [proc])

    result = await _agent(gemini_model).run("USER")
    # Usage propagates back through the Agent's RunUsage.
    usage = result.usage()
    assert usage.input_tokens == 1050
    assert usage.output_tokens == 205
    assert usage.cache_read_tokens == 800


async def test_gemini_repo_tools_attaches_settings_and_allowed_servers(
    gemini_model: GeminiCLIModel, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    rt = RepoTools(
        head_worktree=tmp_path / "head",
        repo_git=tmp_path / ".git",
        base_sha="b" * 40,
        head_sha="h" * 40,
    )
    gemini_model.set_repo_tools(rt)
    proc = _FakeProc(_gemini_envelope({"intent": "x"}))
    calls = _install_claude_subproc(monkeypatch, [proc])

    await _agent(gemini_model).run("USER")

    argv = calls[0]["argv"]
    env = calls[0]["kwargs"]["env"]
    assert "--allowed-mcp-server-names" in argv
    assert argv[argv.index("--allowed-mcp-server-names") + 1] == "scr"
    assert "GEMINI_CLI_SYSTEM_SETTINGS_PATH" in env
    settings_path = env["GEMINI_CLI_SYSTEM_SETTINGS_PATH"]
    # The path must point at a file (not a directory) — gemini errors
    # with EISDIR if pointed at a directory.
    settings = json.loads(open(settings_path, encoding="utf-8").read())
    assert "scr" in settings["mcpServers"]
    assert "--head-worktree" in settings["mcpServers"]["scr"]["args"]

    await gemini_model.aclose()


async def test_set_repo_tools_invalidates_cached_config(
    claude_model: ClaudeCLIModel, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Re-binding RepoTools must drop the previous temp config so the
    next call materialises a fresh one for the new worktree."""
    claude_model.set_repo_tools(RepoTools(
        head_worktree=tmp_path / "a", repo_git=tmp_path / "a",
        base_sha="x", head_sha="y",
    ))
    proc1 = _FakeProc(_claude_envelope({"intent": "1"}))
    proc2 = _FakeProc(_claude_envelope({"intent": "2"}))
    calls = _install_claude_subproc(monkeypatch, [proc1, proc2])
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
