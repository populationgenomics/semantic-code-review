"""Tests for the `scr init` wizard."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from semantic_code_review.cli import app, init_cmd
from semantic_code_review.config import ScrConfig

# Every backend env var init probes, so a stray one in the test runner's
# environment can't make a backend show "ready" and shuffle the menu.
_CRED_ENVS = [
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_API_TOKEN",
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "GITHUB_TOKEN",
    "CEREBRAS_API_KEY",
    "OPENROUTER_API_KEY",
    "MISTRAL_API_KEY",
    "GOOGLE_CLOUD_PROJECT",
]


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """Deterministic detection: no creds, no `claude`, no working commands."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    for var in _CRED_ENVS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(init_cmd.shutil, "which", lambda _name: None)
    monkeypatch.setattr(init_cmd, "_command_yields_key", lambda _argv: False)


def _index_of(backend: str) -> int:
    """Menu number the wizard will assign to ``backend`` right now."""
    rows = init_cmd._ordered_backends(ScrConfig.load())
    return [r[0] for r in rows].index(backend) + 1


def _user_config(tmp_path: Path) -> Path:
    return tmp_path / "xdg" / "scr" / "config.toml"


def test_init_writes_chosen_backend_to_fresh_config(clean_env, tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")  # groq → ready → only ready row
    idx = _index_of("groq")
    # backend=<idx>, model=<enter to keep default>. groq is ready, so no
    # credential prompt.
    result = CliRunner().invoke(app, ["init"], input=f"{idx}\n\n")
    assert result.exit_code == 0, result.output

    cfg_path = _user_config(tmp_path)
    parsed = tomllib.loads(cfg_path.read_text())
    assert parsed["backend"] == "groq"
    assert "backends" not in parsed  # default model kept → no override block


def test_init_configures_api_key_command(clean_env, tmp_path):
    idx = _index_of("groq")  # setup (no key) → credential menu appears
    # backend, model(enter), credential=2 (fetch command), command string.
    result = CliRunner().invoke(app, ["init"], input=f"{idx}\n\n2\ngh auth token\n")
    assert result.exit_code == 0, result.output

    parsed = tomllib.loads(_user_config(tmp_path).read_text())
    assert parsed["backend"] == "groq"
    assert parsed["backends"]["groq"]["api_key_command"] == "gh auth token"


def test_init_instruct_only_writes_no_secret(clean_env, tmp_path):
    idx = _index_of("groq")
    result = CliRunner().invoke(app, ["init"], input=f"{idx}\n\n1\n")
    assert result.exit_code == 0, result.output
    assert "GROQ_API_KEY=<your-key>" in result.output
    parsed = tomllib.loads(_user_config(tmp_path).read_text())
    assert parsed["backend"] == "groq"
    assert "backends" not in parsed  # instruct-only never persists a block


def test_init_paste_writes_gitignored_env(clean_env, tmp_path, monkeypatch):
    # Run outside a git repo so _ensure_gitignored no-ops; .env lands in cwd.
    monkeypatch.chdir(tmp_path)
    idx = _index_of("groq")
    result = CliRunner().invoke(app, ["init"], input=f"{idx}\n\n3\nsupersecret\n")
    assert result.exit_code == 0, result.output
    env_text = (tmp_path / ".env").read_text()
    assert "GROQ_API_KEY=supersecret" in env_text
    # The secret must never reach the TOML config.
    assert "supersecret" not in _user_config(tmp_path).read_text()


def test_init_model_override_writes_backend_block(clean_env, tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    idx = _index_of("groq")
    result = CliRunner().invoke(app, ["init"], input=f"{idx}\nllama-3.1-8b-instant\n")
    assert result.exit_code == 0, result.output
    parsed = tomllib.loads(_user_config(tmp_path).read_text())
    assert parsed["backends"]["groq"]["model"] == "llama-3.1-8b-instant"


# --- unit coverage of the pieces the flow tests can't isolate cleanly ------


def test_detect_ready_when_env_present(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "x")
    from semantic_code_review.config import BUILTIN_BACKENDS

    assert init_cmd._detect("groq", BUILTIN_BACKENDS["groq"]).state == "ready"


def test_detect_setup_when_missing(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr(init_cmd, "_command_yields_key", lambda _argv: False)
    from semantic_code_review.config import BUILTIN_BACKENDS

    assert init_cmd._detect("groq", BUILTIN_BACKENDS["groq"]).state == "setup"


def test_detect_local_for_keyless_backend(monkeypatch):
    from semantic_code_review.config import BUILTIN_BACKENDS

    assert init_cmd._detect("ollama", BUILTIN_BACKENDS["ollama"]).state == "local"


def test_command_yields_key_true_and_false():
    assert init_cmd._command_yields_key(("printf", "tok")) is True
    assert init_cmd._command_yields_key(("true",)) is False  # exits 0, no output
    assert init_cmd._command_yields_key(("scr-no-such-binary-xyz",)) is False


def test_set_backend_line_uncomments_template():
    template = '# header\n# backend = "claude-api"\n\n[model]\n'
    out = init_cmd._set_backend_line(template, "groq")
    assert 'backend = "groq"' in out
    assert '# backend = "claude-api"' not in out


def test_write_config_warns_when_backend_section_exists(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('backend = "groq"\n\n[backends.groq]\nmodel = "keep-me"\n')
    warning = init_cmd._write_config(p, backend="groq", model_override="new", api_key_command=None)
    assert warning is not None
    # Existing hand-edited section is preserved, not clobbered.
    assert 'model = "keep-me"' in p.read_text()
