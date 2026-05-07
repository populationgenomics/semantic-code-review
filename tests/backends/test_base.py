"""`resolve_api_key` — env first, command fallback, error reporting."""

from __future__ import annotations

import pytest
import typer

from semantic_code_review.backends.base import resolve_api_key
from semantic_code_review.config import BackendDef, BackendType


def test_command_runs_when_env_unset(
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
    assert resolve_api_key("gh-style", bdef) == "sk-from-cmd"


def test_env_wins_over_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_KEY", "sk-from-env")
    bdef = BackendDef(
        type=BackendType.OPENAI_COMPAT,
        base_url="https://x",
        api_key_env="FAKE_KEY",
        # If env wins, this nonexistent command must never be invoked.
        api_key_command=("/nonexistent/path/should-not-run",),
    )
    assert resolve_api_key("gh-style", bdef) == "sk-from-env"


def test_command_failure_includes_stderr(
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
        resolve_api_key("gh-style", bdef)


def test_command_empty_output_raises(
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
        resolve_api_key("gh-style", bdef)


def test_command_missing_binary_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FAKE_KEY", raising=False)
    bdef = BackendDef(
        type=BackendType.OPENAI_COMPAT,
        base_url="https://x",
        api_key_env="FAKE_KEY",
        api_key_command=("/no/such/binary/exists",),
    )
    with pytest.raises(typer.BadParameter, match="not on PATH"):
        resolve_api_key("gh-style", bdef)
