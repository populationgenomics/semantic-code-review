"""`gemini-cli` adapter — PATH preflight, credential gate, model coercion."""

from __future__ import annotations

import os

import pytest
import typer

from semantic_code_review.augment.cli_models import GeminiCLIModel
from semantic_code_review.backends.gemini_cli import GeminiCliBackend
from semantic_code_review.config import BackendDef, BackendType


def _stub_gemini_on_path(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gemini = fake_bin / "gemini"
    fake_gemini.write_text("#!/bin/sh\nexit 0\n")
    fake_gemini.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")


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
