"""Backend selection: dispatch from name → BackendDef → wired Backend."""

from __future__ import annotations

import pytest
import typer

from pydantic_ai.models.openai import OpenAIChatModel

from semantic_code_review import cli as cli_module
from semantic_code_review.config import BackendDef, BackendType, ScrConfig


def _set_config(monkeypatch: pytest.MonkeyPatch, backends: dict[str, BackendDef]) -> None:
    cfg = ScrConfig(backends=dict(backends))
    monkeypatch.setattr(cli_module, "_CONFIG", cfg)


def test_openai_compat_with_api_key_env_builds_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_KEY", "sk-test-123")
    _set_config(monkeypatch, {
        "groq": BackendDef(
            type=BackendType.OPENAI_COMPAT,
            base_url="https://api.example.com/v1",
            api_key_env="FAKE_KEY",
            default_model="some-model",
        ),
    })
    backend = cli_module._select_client("groq", model="some-model")
    assert isinstance(backend.model, OpenAIChatModel)
    # Subprocess flag stays off — repo tools flow through pydantic-ai
    # natively, same as the other SDK paths.
    assert backend.is_subprocess_backend is False


def test_openai_compat_without_api_key_env_uses_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local servers (Ollama, llama.cpp) typically don't require a key."""
    _set_config(monkeypatch, {
        "ollama": BackendDef(
            type=BackendType.OPENAI_COMPAT,
            base_url="http://localhost:11434/v1",
            api_key_env=None,
            default_model="qwen2.5-coder:32b",
        ),
    })
    backend = cli_module._select_client("ollama", model="qwen2.5-coder:32b")
    assert isinstance(backend.model, OpenAIChatModel)


def test_openai_compat_missing_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FAKE_KEY", raising=False)
    _set_config(monkeypatch, {
        "groq": BackendDef(
            type=BackendType.OPENAI_COMPAT,
            base_url="https://api.example.com/v1",
            api_key_env="FAKE_KEY",
        ),
    })
    with pytest.raises(typer.BadParameter, match="FAKE_KEY"):
        cli_module._select_client("groq", model="anything")


def test_openai_compat_missing_base_url_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_config(monkeypatch, {
        "broken": BackendDef(
            type=BackendType.OPENAI_COMPAT,
            base_url=None,
            api_key_env=None,
        ),
    })
    with pytest.raises(typer.BadParameter, match="no base_url"):
        cli_module._select_client("broken", model="anything")


def test_unknown_backend_lists_known_choices(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_config(monkeypatch, {
        "groq": BackendDef(
            type=BackendType.OPENAI_COMPAT,
            base_url="https://example.com",
            api_key_env="FAKE",
        ),
    })
    with pytest.raises(typer.BadParameter, match="auto, groq"):
        cli_module._select_client("does-not-exist", model="anything")
