"""`gemini-api` adapter — AI-Studio key path and Vertex/ADC short-circuit."""

from __future__ import annotations

import os

import pytest
import typer
from pydantic_ai.models.google import GoogleModel

from semantic_code_review.backends.google_sdk import GoogleSdkBackend
from semantic_code_review.config import BackendDef, BackendType


def test_resolves_key_via_command(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Gemini AI-Studio key fetched from a secret."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    fake_cmd = tmp_path / "secret-fetcher"
    fake_cmd.write_text("#!/bin/sh\nprintf 'AIza-from-secret\\n'\n")
    fake_cmd.chmod(0o755)
    bdef = BackendDef(
        type=BackendType.GOOGLE_SDK,
        default_model="gemini-2.5-pro",
        api_key_env="GEMINI_API_KEY",
        api_key_command=(str(fake_cmd),),
    )
    client = GoogleSdkBackend("gemini-api", bdef).resolve(model="gemini-2.5-pro")
    assert isinstance(client.model, GoogleModel)
    # No environ mutation: the key is held by the Provider only.
    assert "GEMINI_API_KEY" not in os.environ


def test_vertex_short_circuits_credential_resolution(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """When GOOGLE_CLOUD_PROJECT is set, Vertex/ADC path wins; the
    api_key_command must NOT be invoked (would be wasted work, and
    the user clearly chose Vertex by setting the project)."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
    # If this script ran, it would crash the test by exit 99.
    fake_cmd = tmp_path / "must-not-run"
    fake_cmd.write_text("#!/bin/sh\nexit 99\n")
    fake_cmd.chmod(0o755)
    bdef = BackendDef(
        type=BackendType.GOOGLE_SDK,
        default_model="gemini-2.5-pro",
        api_key_env="GEMINI_API_KEY",
        api_key_command=(str(fake_cmd),),
    )
    client = GoogleSdkBackend("gemini-api", bdef).resolve(model="gemini-2.5-pro")
    assert isinstance(client.model, GoogleModel)


def test_no_credentials_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    bdef = BackendDef(type=BackendType.GOOGLE_SDK, default_model="gemini-2.5-pro")
    with pytest.raises(typer.BadParameter, match="no Gemini credentials"):
        GoogleSdkBackend("gemini-api", bdef).resolve(model="gemini-2.5-pro")


def test_coerce_claude_model_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If `--model claude-...` leaks through (global default for an
    Anthropic config), substitute the backend's own default."""
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-x")
    bdef = BackendDef(
        type=BackendType.GOOGLE_SDK,
        default_model="gemini-2.5-flash",
        api_key_env="GEMINI_API_KEY",
    )
    client = GoogleSdkBackend("gemini-api", bdef).resolve(model="claude-opus-4-7")
    assert isinstance(client.model, GoogleModel)
    assert client.model.model_name == "gemini-2.5-flash"
