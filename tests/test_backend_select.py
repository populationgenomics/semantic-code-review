"""Backend selection: dispatch from name → BackendDef → wired Backend."""

from __future__ import annotations

import os

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


# ---------------------------------------------------------------------------
# api_key_command fallback (used by github → gh auth token, by users for
# gcloud-secrets-stored bearers, etc.).
# ---------------------------------------------------------------------------

def test_api_key_command_runs_when_env_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.delenv("FAKE_KEY", raising=False)
    fake_cmd = tmp_path / "fake-token-printer"
    fake_cmd.write_text("#!/bin/sh\nprintf 'sk-from-cmd\\n'\n")
    fake_cmd.chmod(0o755)
    bdef = BackendDef(
        type=BackendType.OPENAI_COMPAT,
        base_url="https://x",
        api_key_env="FAKE_KEY",
        api_key_command=(str(fake_cmd),),
    )
    assert cli_module._resolve_api_key("gh-style", bdef) == "sk-from-cmd"


def test_api_key_env_wins_over_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_KEY", "sk-from-env")
    bdef = BackendDef(
        type=BackendType.OPENAI_COMPAT,
        base_url="https://x",
        api_key_env="FAKE_KEY",
        # If env wins, this nonexistent command must never be invoked.
        api_key_command=("/nonexistent/path/should-not-run",),
    )
    assert cli_module._resolve_api_key("gh-style", bdef) == "sk-from-env"


def test_api_key_command_failure_includes_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.delenv("FAKE_KEY", raising=False)
    fake_cmd = tmp_path / "fail-cmd"
    fake_cmd.write_text(
        "#!/bin/sh\necho 'auth: not logged in' 1>&2\nexit 4\n"
    )
    fake_cmd.chmod(0o755)
    bdef = BackendDef(
        type=BackendType.OPENAI_COMPAT,
        base_url="https://x",
        api_key_env="FAKE_KEY",
        api_key_command=(str(fake_cmd),),
    )
    with pytest.raises(typer.BadParameter, match="not logged in"):
        cli_module._resolve_api_key("gh-style", bdef)


def test_api_key_command_empty_output_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.delenv("FAKE_KEY", raising=False)
    fake_cmd = tmp_path / "empty-cmd"
    fake_cmd.write_text("#!/bin/sh\nexit 0\n")
    fake_cmd.chmod(0o755)
    bdef = BackendDef(
        type=BackendType.OPENAI_COMPAT,
        base_url="https://x",
        api_key_env="FAKE_KEY",
        api_key_command=(str(fake_cmd),),
    )
    with pytest.raises(typer.BadParameter, match="empty output"):
        cli_module._resolve_api_key("gh-style", bdef)


def test_api_key_command_missing_binary_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FAKE_KEY", raising=False)
    bdef = BackendDef(
        type=BackendType.OPENAI_COMPAT,
        base_url="https://x",
        api_key_env="FAKE_KEY",
        api_key_command=("/no/such/binary/exists",),
    )
    with pytest.raises(typer.BadParameter, match="not on PATH"):
        cli_module._resolve_api_key("gh-style", bdef)


def test_github_builtin_falls_back_to_gh_auth_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """End-to-end: the shipped github preset uses gh auth token when
    GITHUB_TOKEN is unset. We stub `gh` on PATH to verify the wiring."""
    from semantic_code_review.config import BUILTIN_BACKENDS

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gh = fake_bin / "gh"
    fake_gh.write_text("#!/bin/sh\nprintf 'ghp_stubtoken\\n'\n")
    fake_gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH','')}")

    _set_config(monkeypatch, {"github": BUILTIN_BACKENDS["github"]})
    backend = cli_module._select_client("github", model="openai/gpt-4o-mini")
    assert isinstance(backend.model, OpenAIChatModel)
