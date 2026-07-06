"""Tests for the `scr init` wizard."""

from __future__ import annotations

import stat
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from semantic_code_review.cli import app, credentials, init_cmd
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


def _drive(monkeypatch, *, backend, source=None, model="", command="", key="", models=None):
    """Script the interactive seam so `scr init` runs non-interactively.

    `backend` and `source` are the values the two selects return; `model`
    /`command`/`key` feed the text + password prompts. `models` is what
    the live lister returns (None → free-text model entry, and — since the
    real lister would hit the network — the default for tests)."""
    selects = iter([backend, *([source] if source else [])])
    monkeypatch.setattr(init_cmd.prompt, "select", lambda *_a, **_k: next(selects))
    monkeypatch.setattr(
        init_cmd.prompt,
        "text",
        lambda msg="", default="", **_k: command if "Command" in str(msg) else (model or default),
    )
    monkeypatch.setattr(init_cmd.prompt, "password", lambda *_a, **_k: key)
    monkeypatch.setattr(init_cmd.credentials, "list_models", lambda *_a, **_k: models)


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


def test_init_configures_api_key_command(clean_env, tmp_path, monkeypatch):
    _drive(monkeypatch, backend="groq", source="command", command="gh auth token")
    result = CliRunner().invoke(app, ["init"])
    assert result.exit_code == 0, result.output

    parsed = tomllib.loads(_user_config(tmp_path).read_text())
    assert parsed["backend"] == "groq"
    assert parsed["backends"]["groq"]["api_key_command"] == "gh auth token"


def test_init_instruct_only_writes_no_secret(clean_env, tmp_path, monkeypatch):
    _drive(monkeypatch, backend="groq", source="env")
    result = CliRunner().invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    assert "GROQ_API_KEY=<your-key>" in result.output
    parsed = tomllib.loads(_user_config(tmp_path).read_text())
    assert parsed["backend"] == "groq"
    assert "backends" not in parsed  # instruct-only never persists a block


def test_init_paste_writes_gitignored_env(clean_env, tmp_path, monkeypatch):
    # Run outside a git repo so _ensure_gitignored no-ops; .env lands in cwd.
    monkeypatch.chdir(tmp_path)
    _drive(monkeypatch, backend="groq", source="dotenv", key="supersecret")
    result = CliRunner().invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    env_path = tmp_path / ".env"
    assert "GROQ_API_KEY=supersecret" in env_path.read_text()
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600  # secret file is 0600
    # The secret must never reach the TOML config on the .env route.
    assert "supersecret" not in _user_config(tmp_path).read_text()


def test_init_config_key_writes_env_table(clean_env, tmp_path, monkeypatch):
    """The relaxed policy: a key may go into user config's [env] (0600)."""
    _drive(monkeypatch, backend="groq", source="config", key="sk-in-config")
    result = CliRunner().invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    cfg_path = _user_config(tmp_path)
    parsed = tomllib.loads(cfg_path.read_text())
    assert parsed["env"]["GROQ_API_KEY"] == "sk-in-config"
    assert stat.S_IMODE(cfg_path.stat().st_mode) == 0o600


def test_config_source_offered_only_for_user_scope():
    from semantic_code_review.config import BUILTIN_BACKENDS

    bdef = BUILTIN_BACKENDS["groq"]
    assert "config" in credentials.allowed_source_ids("groq", bdef, scope="user")
    assert "config" not in credentials.allowed_source_ids("groq", bdef, scope="repo")


def test_init_model_override_writes_backend_block(clean_env, tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    _drive(monkeypatch, backend="groq", model="llama-3.1-8b-instant")
    result = CliRunner().invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    parsed = tomllib.loads(_user_config(tmp_path).read_text())
    assert parsed["backends"]["groq"]["model"] == "llama-3.1-8b-instant"


def test_init_model_picker_selects_from_live_list(clean_env, tmp_path, monkeypatch):
    """When the lister returns models, the model select's value is persisted."""
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    # A model distinct from groq's builtin default, so an override block is written.
    selects = iter(["groq", "moonshotai/kimi-k2"])  # backend, then model
    monkeypatch.setattr(init_cmd.prompt, "select", lambda *_a, **_k: next(selects))
    monkeypatch.setattr(init_cmd.credentials, "list_models", lambda *_a, **_k: ["moonshotai/kimi-k2", "other-model"])
    result = CliRunner().invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    parsed = tomllib.loads(_user_config(tmp_path).read_text())
    assert parsed["backends"]["groq"]["model"] == "moonshotai/kimi-k2"


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
