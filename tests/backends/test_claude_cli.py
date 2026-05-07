"""`claude-cli` adapter — PATH preflight, supports_auto, subprocess Model wiring."""

from __future__ import annotations

import os

import pytest
import typer

from semantic_code_review.augment.cli_models import ClaudeCLIModel
from semantic_code_review.backends.claude_cli import ClaudeCliBackend
from semantic_code_review.config import BackendDef, BackendType


def test_resolve_returns_subprocess_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
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


def test_supports_auto_when_claude_on_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_claude = fake_bin / "claude"
    fake_claude.write_text("#!/bin/sh\nexit 0\n")
    fake_claude.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")

    bdef = BackendDef(type=BackendType.CLAUDE_CLI)
    assert ClaudeCliBackend("claude-cli", bdef).supports_auto() is True


def test_supports_auto_false_when_claude_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    bdef = BackendDef(type=BackendType.CLAUDE_CLI)
    assert ClaudeCliBackend("claude-cli", bdef).supports_auto() is False
