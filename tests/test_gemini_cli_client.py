"""GeminiCLIClient: subprocess mocked out, contract with runner verified."""

from __future__ import annotations

import json
from typing import Any

import pytest

from semantic_code_review.augment.gemini_cli_client import (
    GeminiCLIClient,
    GeminiCLIError,
    _build_prompt,
    _extract_json_object,
    _validate_against_schema,
    _SchemaValidationError,
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


def _envelope(response: Any, *, error: dict | str | None = None,
              stats_input_tokens: int = 42, stats_output_tokens: int = 17,
              stats_cached_tokens: int = 0) -> bytes:
    """Build a `gemini -p --output-format json` envelope.

    Shape mirrors what `gemini -p --output-format json` actually emits
    (verified against gemini 0.40.1): `stats.models` is a dict keyed by
    model ID, with `tokens.{input, candidates, cached, ...}` per entry.
    `candidates` is gemini's name for output tokens.
    """
    payload: dict[str, Any] = {
        "response": response if isinstance(response, str) else json.dumps(response),
        "stats": {
            "models": {
                "gemini-2.5-pro": {
                    "tokens": {
                        "input": stats_input_tokens,
                        "candidates": stats_output_tokens,
                        "cached": stats_cached_tokens,
                    },
                },
            },
        },
        "session_id": "gem-sess-abc",
    }
    if error is not None:
        payload["error"] = error
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
def gemini_client(monkeypatch: pytest.MonkeyPatch) -> GeminiCLIClient:
    import semantic_code_review.augment.gemini_cli_client as mod
    monkeypatch.setattr(mod.shutil, "which", lambda _name: "/usr/bin/true")
    return GeminiCLIClient()


def _install_fake_subprocess(
    monkeypatch: pytest.MonkeyPatch, procs: list[_FakeProc]
) -> list[dict[str, Any]]:
    """Replace asyncio.create_subprocess_exec; return one queued FakeProc per call."""
    calls: list[dict[str, Any]] = []
    queue = list(procs)

    async def _fake(*args: str, **kwargs: Any) -> _FakeProc:
        calls.append({"argv": list(args), "env": kwargs.get("env")})
        if not queue:
            raise AssertionError("more subprocess calls than fake procs queued")
        return queue.pop(0)

    import semantic_code_review.augment.gemini_cli_client as mod
    monkeypatch.setattr(mod.asyncio, "create_subprocess_exec", _fake)
    return calls


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_build_prompt_includes_schema_and_no_prose_instruction() -> None:
    p = _build_prompt(
        system_text="you are reviewing",
        messages=[{"role": "user", "content": [{"type": "text", "text": "hello"}]}],
        submit_tool=SUBMIT_TOOL,
        schema=SUBMIT_TOOL["input_schema"],
        prior_error=None,
    )
    assert "you are reviewing" in p
    assert "hello" in p
    assert "submit_annotations" in p
    assert "Do not include any prose" in p
    # The schema body itself must be embedded — that's the whole point.
    assert '"intent"' in p
    assert '"required"' in p


def test_build_prompt_appends_prior_error_on_retry() -> None:
    p = _build_prompt(
        system_text="", messages=[], submit_tool=SUBMIT_TOOL,
        schema=SUBMIT_TOOL["input_schema"],
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
    # The `}` in the string mustn't terminate the scan early.
    text = '{"intent": "the } character", "ok": true}'
    assert _extract_json_object(text) == {"intent": "the } character", "ok": True}


def test_validate_against_schema_passes_on_required_present() -> None:
    _validate_against_schema({"intent": "x"}, SUBMIT_TOOL["input_schema"])


def test_validate_against_schema_rejects_missing_required() -> None:
    with pytest.raises(_SchemaValidationError, match="missing required"):
        _validate_against_schema({}, SUBMIT_TOOL["input_schema"])


def test_validate_against_schema_rejects_wrong_top_type() -> None:
    with pytest.raises(_SchemaValidationError, match="object"):
        _validate_against_schema(["array", "instead"], SUBMIT_TOOL["input_schema"])


# ---------------------------------------------------------------------------
# create_message — happy path, retry, errors
# ---------------------------------------------------------------------------

async def test_create_message_synthesizes_tool_use(
    gemini_client: GeminiCLIClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = _FakeProc(_envelope({"intent": "explain the refactor"}))
    calls = _install_fake_subprocess(monkeypatch, [proc])

    response = await gemini_client.create_message(
        model="gemini-2.5-pro",
        max_tokens=4096,
        system=[{"type": "text", "text": "SYS"}],
        tools=[SUBMIT_TOOL],
        messages=[{"role": "user", "content": [{"type": "text", "text": "USER"}]}],
    )

    assert len(calls) == 1
    argv = calls[0]["argv"]
    assert argv[0] == "/usr/bin/true"  # the patched gemini path
    assert argv[1] == "-p"
    # gemini's -p takes the prompt as an argv value (NOT via stdin —
    # that distinguishes it from claude -p). The argv slot immediately
    # after -p must be the prompt, not another flag, or gemini bails
    # with "Not enough arguments following: p". Regression guard.
    assert argv[2].startswith("# ") or "Reply with" in argv[2], argv[2]
    assert not argv[2].startswith("--"), f"prompt slot looks like a flag: {argv[2]!r}"
    assert "--output-format" in argv and argv[argv.index("--output-format") + 1] == "json"
    # No repo_tools attached → no MCP wiring
    assert "--allowed-mcp-server-names" not in argv
    env = calls[0]["env"]
    assert env is not None
    assert env["GEMINI_CLI_TRUST_WORKSPACE"] == "true"
    # Without repo_tools, no system-settings file should be set up.
    assert "GEMINI_CLI_SYSTEM_SETTINGS_PATH" not in env

    assert response["role"] == "assistant"
    assert response["content"][0]["name"] == "submit_annotations"
    assert response["content"][0]["input"] == {"intent": "explain the refactor"}
    assert response["usage"]["input_tokens"] == 42
    assert response["usage"]["output_tokens"] == 17
    # cache_creation isn't surfaced; cache_read maps from `tokens.cached`.
    assert response["usage"]["cache_creation_input_tokens"] == 0
    assert response["usage"]["cache_read_input_tokens"] == 0


async def test_create_message_retries_on_invalid_json(
    gemini_client: GeminiCLIClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = _FakeProc(_envelope("here is some prose, no json at all"))
    good = _FakeProc(_envelope({"intent": "fixed"}))
    _install_fake_subprocess(monkeypatch, [bad, good])

    response = await gemini_client.create_message(
        model="gemini-2.5-pro", max_tokens=4096,
        system=[], tools=[SUBMIT_TOOL],
        messages=[{"role": "user", "content": [{"type": "text", "text": "USER"}]}],
    )
    assert response["content"][0]["input"] == {"intent": "fixed"}


async def test_create_message_retries_on_missing_required_key(
    gemini_client: GeminiCLIClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = _FakeProc(_envelope({"unrelated": "field"}))
    good = _FakeProc(_envelope({"intent": "now-with-required-key"}))
    _install_fake_subprocess(monkeypatch, [bad, good])

    response = await gemini_client.create_message(
        model="gemini-2.5-pro", max_tokens=4096,
        system=[], tools=[SUBMIT_TOOL], messages=[],
    )
    assert response["content"][0]["input"] == {"intent": "now-with-required-key"}


async def test_create_message_raises_after_max_retries(
    gemini_client: GeminiCLIClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad1 = _FakeProc(_envelope("nothing useful"))
    bad2 = _FakeProc(_envelope("still nothing"))
    _install_fake_subprocess(monkeypatch, [bad1, bad2])

    with pytest.raises(GeminiCLIError, match="schema-conformant"):
        await gemini_client.create_message(
            model="gemini-2.5-pro", max_tokens=4096,
            system=[], tools=[SUBMIT_TOOL], messages=[],
        )


async def test_create_message_surfaces_auth_error_actionably(
    gemini_client: GeminiCLIClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = _FakeProc(
        _envelope("", error={"message": "request unauthenticated"}),
        returncode=1,
    )
    _install_fake_subprocess(monkeypatch, [proc])

    with pytest.raises(GeminiCLIError, match="authenticated"):
        await gemini_client.create_message(
            model="gemini-2.5-pro", max_tokens=4096,
            system=[], tools=[SUBMIT_TOOL], messages=[],
        )


async def test_create_message_sums_tokens_across_routed_models(
    gemini_client: GeminiCLIClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gemini routes through utility models alongside the main one;
    `stats.models` ends up with multiple entries. We must sum tokens
    across all of them, not pick one — otherwise the user's reported
    usage doesn't match what they're actually billed for."""
    payload = {
        "response": json.dumps({"intent": "x"}),
        "stats": {
            "models": {
                "gemini-2.5-pro": {
                    "tokens": {"input": 1000, "candidates": 200, "cached": 800},
                },
                "gemini-utility-router": {
                    "tokens": {"input": 50, "candidates": 5, "cached": 0},
                },
            },
        },
    }
    proc = _FakeProc((json.dumps(payload) + "\n").encode("utf-8"))
    _install_fake_subprocess(monkeypatch, [proc])

    response = await gemini_client.create_message(
        model="gemini-2.5-pro", max_tokens=4096,
        system=[], tools=[SUBMIT_TOOL], messages=[],
    )
    assert response["usage"]["input_tokens"] == 1050
    assert response["usage"]["output_tokens"] == 205
    assert response["usage"]["cache_read_input_tokens"] == 800


async def test_create_message_drives_run_agentic(
    gemini_client: GeminiCLIClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = _FakeProc(_envelope({"intent": "done"}))
    _install_fake_subprocess(monkeypatch, [proc])

    result = await run_agentic(
        gemini_client,
        model="gemini-2.5-pro",
        system="SYS",
        user_content=[{"type": "text", "text": "USER"}],
        tools=[SUBMIT_TOOL],
        submit_tool_name="submit_annotations",
    )
    assert result.submit_args == {"intent": "done"}


async def test_repo_tools_attaches_settings_and_allowed_servers(
    gemini_client: GeminiCLIClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from semantic_code_review.augment.tools import RepoTools
    rt = RepoTools(
        head_worktree=tmp_path / "head",
        repo_git=tmp_path / ".git",
        base_sha="b" * 40,
        head_sha="h" * 40,
    )
    gemini_client.set_repo_tools(rt)

    proc = _FakeProc(_envelope({"intent": "x"}))
    calls = _install_fake_subprocess(monkeypatch, [proc])

    await gemini_client.create_message(
        model="gemini-2.5-pro", max_tokens=4096,
        system=[], tools=[SUBMIT_TOOL], messages=[],
    )

    argv = calls[0]["argv"]
    env = calls[0]["env"]
    assert "--allowed-mcp-server-names" in argv
    assert argv[argv.index("--allowed-mcp-server-names") + 1] == "scr"
    assert "GEMINI_CLI_SYSTEM_SETTINGS_PATH" in env
    settings_path = env["GEMINI_CLI_SYSTEM_SETTINGS_PATH"]
    # The path must point at a file (not a directory) — gemini errors
    # with EISDIR if pointed at a directory. Verified upstream by the
    # source-code probe that motivated this client; reasserted here so
    # any future refactor can't regress.
    settings = json.loads(open(settings_path, encoding="utf-8").read())
    assert "scr" in settings["mcpServers"]
    assert settings["mcpServers"]["scr"]["command"]
    assert "--head-worktree" in settings["mcpServers"]["scr"]["args"]

    await gemini_client.aclose()
