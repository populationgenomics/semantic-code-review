"""`gemini-cli`: backend resolution + `GeminiCLIModel` driver behaviour.

Two interfaces share one file:

- The backend adapter (`GeminiCliBackend`): PATH preflight, credential
  gate, model coercion, `resolve()` → subprocess `Client`.
- The CLI driver (`GeminiCLIModel`): subprocess argv assembly, prompt
  composition, envelope parsing, validation-retry loop, MCP injection.
  Tests run through `Agent.run` with the subprocess mocked.
"""

from __future__ import annotations

import os

import pytest
import typer
from pydantic_ai import Agent
from pydantic_ai.output import ToolOutput

from semantic_code_review.augment.schemas import HunkAnnotations
from semantic_code_review.augment.tools import RepoTools
from semantic_code_review.backends.gemini_cli import (
    GeminiCliBackend,
    GeminiCLIError,
    GeminiCLIModel,
)
from semantic_code_review.config import BackendDef, BackendType

from ._subproc_fakes import FakeProc, gemini_envelope, install_fake_subproc


def _stub_gemini_on_path(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gemini = fake_bin / "gemini"
    fake_gemini.write_text("#!/bin/sh\nexit 0\n")
    fake_gemini.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")


# ---------------------------------------------------------------------------
# Backend adapter
# ---------------------------------------------------------------------------

def test_resolve_returns_subprocess_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _stub_gemini_on_path(monkeypatch, tmp_path)
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-x")
    bdef = BackendDef(type=BackendType.GEMINI_CLI, default_model="gemini-2.5-pro")
    client = GeminiCliBackend("gemini-cli", bdef).resolve(model="gemini-2.5-pro")
    assert isinstance(client.model, GeminiCLIModel)
    assert client.is_subprocess_backend is True


def test_resolve_raises_when_gemini_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    bdef = BackendDef(type=BackendType.GEMINI_CLI)
    with pytest.raises(typer.BadParameter, match="not on PATH"):
        GeminiCliBackend("gemini-cli", bdef).resolve(model="gemini-2.5-pro")


def test_resolve_raises_when_no_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _stub_gemini_on_path(monkeypatch, tmp_path)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    # Steer the OAuth-creds probe at a definitely-empty home.
    monkeypatch.setenv("HOME", str(tmp_path))
    bdef = BackendDef(type=BackendType.GEMINI_CLI)
    with pytest.raises(typer.BadParameter, match="no Gemini credentials"):
        GeminiCliBackend("gemini-cli", bdef).resolve(model="gemini-2.5-pro")


def test_oauth_creds_satisfy_credential_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _stub_gemini_on_path(monkeypatch, tmp_path)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".gemini").mkdir()
    (tmp_path / ".gemini" / "oauth_creds.json").write_text("{}")
    bdef = BackendDef(type=BackendType.GEMINI_CLI, default_model="gemini-2.5-pro")
    client = GeminiCliBackend("gemini-cli", bdef).resolve(model="gemini-2.5-pro")
    assert isinstance(client.model, GeminiCLIModel)


def test_coerce_claude_model_to_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _stub_gemini_on_path(monkeypatch, tmp_path)
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-x")
    bdef = BackendDef(
        type=BackendType.GEMINI_CLI,
        default_model="gemini-2.5-flash",
    )
    client = GeminiCliBackend("gemini-cli", bdef).resolve(model="claude-opus-4-7")
    assert isinstance(client.model, GeminiCLIModel)
    assert client.model.model_name == "gemini-2.5-flash"


# ---------------------------------------------------------------------------
# CLI driver — fixtures + helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def gemini_model(monkeypatch: pytest.MonkeyPatch) -> GeminiCLIModel:
    import semantic_code_review.backends.gemini_cli as mod
    monkeypatch.setattr(mod.shutil, "which", lambda _name: "/usr/bin/true")
    return GeminiCLIModel(model="gemini-2.5-pro")


def _agent(model) -> Agent:  # type: ignore[no-untyped-def]
    return Agent(
        model=model,
        output_type=ToolOutput(HunkAnnotations, name="submit_annotations"),
        instructions="SYS",
    )


# ---------------------------------------------------------------------------
# CLI driver — prompt builder
# ---------------------------------------------------------------------------

def test_build_gemini_prompt_includes_schema_and_no_prose_instruction() -> None:
    p = GeminiCLIModel._build_prompt(
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
    p = GeminiCLIModel._build_prompt(
        system_text="", user_text="", submit_tool_name="submit_annotations",
        schema={"type": "object"},
        prior_error="missing required keys: ['intent']",
    )
    assert "Previous attempt failed" in p
    assert "missing required keys" in p


# ---------------------------------------------------------------------------
# CLI driver — subprocess invocation
# ---------------------------------------------------------------------------

async def test_gemini_model_round_trip_through_agent(
    gemini_model: GeminiCLIModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = FakeProc(gemini_envelope({"intent": "explain the refactor"}))
    calls = install_fake_subproc(monkeypatch, [proc])

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
    bad = FakeProc(gemini_envelope("here is some prose, no json at all"))
    good = FakeProc(gemini_envelope({"intent": "fixed"}))
    install_fake_subproc(monkeypatch, [bad, good])

    result = await _agent(gemini_model).run("USER")
    assert result.output.intent == "fixed"


async def test_gemini_retries_on_missing_required_key(
    gemini_model: GeminiCLIModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = FakeProc(gemini_envelope({"unrelated": "field"}))
    good = FakeProc(gemini_envelope({"intent": "now-with-required-key"}))
    install_fake_subproc(monkeypatch, [bad, good])

    result = await _agent(gemini_model).run("USER")
    assert result.output.intent == "now-with-required-key"


async def test_gemini_raises_after_max_retries(
    gemini_model: GeminiCLIModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad1 = FakeProc(gemini_envelope("nothing useful"))
    bad2 = FakeProc(gemini_envelope("still nothing"))
    install_fake_subproc(monkeypatch, [bad1, bad2])

    with pytest.raises(GeminiCLIError, match="schema-conformant"):
        await _agent(gemini_model).run("USER")


async def test_gemini_surfaces_auth_error_actionably(
    gemini_model: GeminiCLIModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = FakeProc(
        gemini_envelope("", error={"message": "request unauthenticated"}),
        returncode=1,
    )
    install_fake_subproc(monkeypatch, [proc])
    with pytest.raises(GeminiCLIError, match="authenticated"):
        await _agent(gemini_model).run("USER")


async def test_gemini_sums_tokens_across_routed_models(
    gemini_model: GeminiCLIModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gemini routes through utility models alongside the main one;
    `stats.models` ends up with multiple entries. We must sum tokens
    across all of them, not pick one — otherwise the user's reported
    usage doesn't match what they're actually billed for."""
    proc = FakeProc(gemini_envelope(
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
    install_fake_subproc(monkeypatch, [proc])

    result = await _agent(gemini_model).run("USER")
    # Usage propagates back through the Agent's RunUsage.
    usage = result.usage()
    assert usage.input_tokens == 1050
    assert usage.output_tokens == 205
    assert usage.cache_read_tokens == 800


async def test_gemini_repo_tools_attaches_settings_and_allowed_servers(
    gemini_model: GeminiCLIModel, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    import json

    rt = RepoTools(
        head_worktree=tmp_path / "head",
        repo_git=tmp_path / ".git",
        base_sha="b" * 40,
        head_sha="h" * 40,
    )
    gemini_model.set_repo_tools(rt)
    proc = FakeProc(gemini_envelope({"intent": "x"}))
    calls = install_fake_subproc(monkeypatch, [proc])

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
