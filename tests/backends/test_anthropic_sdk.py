"""`claude-api` adapter — credential resolution, supports_auto, no env mutation."""

from __future__ import annotations

import os

import pytest
import typer
from pydantic_ai.models.anthropic import AnthropicModel

from semantic_code_review.backends.anthropic_sdk import AnthropicSdkBackend
from semantic_code_review.config import BackendDef, BackendType


def test_resolves_key_via_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """User's stated case: Anthropic key fetched from a gcloud secret."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fake_cmd = tmp_path / "secret-fetcher"
    fake_cmd.write_text("#!/bin/sh\nprintf 'sk-ant-from-secret\\n'\n")
    fake_cmd.chmod(0o755)
    bdef = BackendDef(
        type=BackendType.ANTHROPIC_SDK,
        default_model="claude-opus-4-7",
        api_key_env="ANTHROPIC_API_KEY",
        api_key_command=(str(fake_cmd),),
    )
    client = AnthropicSdkBackend("claude-api", bdef).resolve(model="claude-opus-4-7")
    assert isinstance(client.model, AnthropicModel)
    # No environ mutation: the key is held by the Provider only.
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_resolves_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")
    bdef = BackendDef(
        type=BackendType.ANTHROPIC_SDK,
        api_key_env="ANTHROPIC_API_KEY",
    )
    client = AnthropicSdkBackend("claude-api", bdef).resolve(model="claude-opus-4-7")
    assert isinstance(client.model, AnthropicModel)


def test_no_key_no_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # Builtin without explicit api_key_env: still expects ANTHROPIC_API_KEY.
    bdef = BackendDef(type=BackendType.ANTHROPIC_SDK)
    with pytest.raises(typer.BadParameter, match="ANTHROPIC_API_KEY"):
        AnthropicSdkBackend("claude-api", bdef).resolve(model="claude-opus-4-7")


def test_supports_auto_when_key_resolves(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fake_cmd = tmp_path / "fetch"
    fake_cmd.write_text("#!/bin/sh\nprintf 'sk-ant-x\\n'\n")
    fake_cmd.chmod(0o755)
    bdef = BackendDef(
        type=BackendType.ANTHROPIC_SDK,
        api_key_env="ANTHROPIC_API_KEY",
        api_key_command=(str(fake_cmd),),
    )
    assert AnthropicSdkBackend("claude-api", bdef).supports_auto() is True


def test_supports_auto_false_when_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    bdef = BackendDef(type=BackendType.ANTHROPIC_SDK)
    assert AnthropicSdkBackend("claude-api", bdef).supports_auto() is False
