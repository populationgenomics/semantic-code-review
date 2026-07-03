"""Generic OpenAI-compatible adapter tests (groq, github, ollama, ...)."""

from __future__ import annotations

import os

import pytest
import typer
from pydantic_ai.models.openai import OpenAIChatModel

from semantic_code_review.backends.openai_compat import OpenAICompatBackend
from semantic_code_review.config import BUILTIN_BACKENDS, BackendDef, BackendType


def test_with_api_key_env_builds_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_KEY", "sk-test-123")
    bdef = BackendDef(
        type=BackendType.OPENAI_COMPAT,
        base_url="https://api.example.com/v1",
        api_key_env="FAKE_KEY",
        default_model="some-model",
    )
    client = OpenAICompatBackend("groq", bdef).resolve(model="some-model")
    assert isinstance(client.model, OpenAIChatModel)
    # OpenAI-compat is a pydantic-ai SDK path, not a CLI subprocess.
    assert client.is_subprocess_backend is False


def test_without_api_key_env_uses_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local servers (Ollama, llama.cpp) typically don't require a key."""
    bdef = BackendDef(
        type=BackendType.OPENAI_COMPAT,
        base_url="http://localhost:11434/v1",
        api_key_env=None,
        default_model="qwen2.5-coder:32b",
    )
    client = OpenAICompatBackend("ollama", bdef).resolve(model="qwen2.5-coder:32b")
    assert isinstance(client.model, OpenAIChatModel)


def test_missing_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FAKE_KEY", raising=False)
    bdef = BackendDef(
        type=BackendType.OPENAI_COMPAT,
        base_url="https://api.example.com/v1",
        api_key_env="FAKE_KEY",
    )
    with pytest.raises(typer.BadParameter, match="FAKE_KEY"):
        OpenAICompatBackend("groq", bdef).resolve(model="anything")


def test_missing_base_url_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    bdef = BackendDef(
        type=BackendType.OPENAI_COMPAT,
        base_url=None,
        api_key_env=None,
    )
    with pytest.raises(typer.BadParameter, match="no base_url"):
        OpenAICompatBackend("broken", bdef).resolve(model="anything")


def test_github_builtin_falls_back_to_gh_auth_token(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """End-to-end: the shipped github preset uses gh auth token when
    GITHUB_TOKEN is unset. Stub `gh` on PATH to verify the wiring."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gh = fake_bin / "gh"
    fake_gh.write_text("#!/bin/sh\nprintf 'ghp_stubtoken\\n'\n")
    fake_gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")

    client = OpenAICompatBackend("github", BUILTIN_BACKENDS["github"]).resolve(
        model="openai/gpt-4o-mini",
    )
    assert isinstance(client.model, OpenAIChatModel)
