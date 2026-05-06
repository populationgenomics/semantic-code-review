"""ScrConfig: TOML loader, merging, env application, resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from semantic_code_review.config import ConfigError, ScrConfig
from semantic_code_review.paths import default_config_path, find_repo_config_path


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Path discovery
# ---------------------------------------------------------------------------

def test_default_config_path_uses_xdg_config_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "conf"))
    assert default_config_path() == tmp_path / "conf" / "scr" / "config.toml"


def test_default_config_path_falls_back_to_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    assert default_config_path() == tmp_path / ".config" / "scr" / "config.toml"


def test_find_repo_config_walks_up(tmp_path: Path) -> None:
    """A `.scr/config.toml` at any ancestor dir gets picked up."""
    repo = tmp_path / "proj"
    deep = repo / "a" / "b" / "c"
    deep.mkdir(parents=True)
    cfg = _write(repo / ".scr" / "config.toml", "")
    assert find_repo_config_path(deep) == cfg


def test_find_repo_config_returns_none_when_absent(tmp_path: Path) -> None:
    assert find_repo_config_path(tmp_path) is None


# ---------------------------------------------------------------------------
# Parsing + merge
# ---------------------------------------------------------------------------

def test_load_empty_config_is_empty(tmp_path: Path) -> None:
    cfg = ScrConfig.load(user_path=tmp_path / "missing.toml", repo_path=None)
    assert cfg.backend is None
    assert cfg.model == {}
    assert cfg.env == {}


def test_load_user_only(tmp_path: Path) -> None:
    user = _write(tmp_path / "user.toml", '''
backend = "gemini-api"

[model]
default = "claude-opus-4-7"
"gemini-api" = "gemini-2.5-pro"

[env]
GOOGLE_CLOUD_PROJECT = "aasgard-dev"
''')
    cfg = ScrConfig.load(user_path=user, repo_path=None)
    assert cfg.backend == "gemini-api"
    assert cfg.model == {"default": "claude-opus-4-7", "gemini-api": "gemini-2.5-pro"}
    assert cfg.env == {"GOOGLE_CLOUD_PROJECT": "aasgard-dev"}


def test_repo_overrides_user(tmp_path: Path) -> None:
    """Per-repo config takes precedence on conflicting keys."""
    user = _write(tmp_path / "user.toml", '''
backend = "claude-api"
[model]
default = "user-model"
[env]
GOOGLE_CLOUD_PROJECT = "user-project"
GOOGLE_CLOUD_LOCATION = "us-central1"
''')
    repo = _write(tmp_path / "repo.toml", '''
backend = "gemini-api"
[model]
default = "repo-model"
[env]
GOOGLE_CLOUD_PROJECT = "repo-project"
''')
    cfg = ScrConfig.load(user_path=user, repo_path=repo)
    assert cfg.backend == "gemini-api"
    assert cfg.model["default"] == "repo-model"
    # Repo overrode shared keys; user's exclusive keys survive.
    assert cfg.env["GOOGLE_CLOUD_PROJECT"] == "repo-project"
    assert cfg.env["GOOGLE_CLOUD_LOCATION"] == "us-central1"
    # Sources reflect the winning layer.
    assert "repo.toml" in cfg.sources["backend"]
    assert "user.toml" in cfg.sources["env.GOOGLE_CLOUD_LOCATION"]


def test_invalid_toml_raises_config_error(tmp_path: Path) -> None:
    bad = _write(tmp_path / "bad.toml", "this is = = not valid")
    with pytest.raises(ConfigError, match="invalid TOML"):
        ScrConfig.load(user_path=bad, repo_path=None)


def test_unknown_backend_raises(tmp_path: Path) -> None:
    user = _write(tmp_path / "user.toml", 'backend = "telepathy"')
    with pytest.raises(ConfigError, match="not one of"):
        ScrConfig.load(user_path=user, repo_path=None)


def test_non_string_model_value_raises(tmp_path: Path) -> None:
    user = _write(tmp_path / "user.toml", '[model]\ndefault = 42')
    with pytest.raises(ConfigError, match="must be a string"):
        ScrConfig.load(user_path=user, repo_path=None)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def test_resolve_backend_cli_wins_over_config() -> None:
    cfg = ScrConfig(backend="claude-api")
    assert cfg.resolve_backend("gemini-api") == "gemini-api"


def test_resolve_backend_falls_back_to_auto() -> None:
    cfg = ScrConfig()
    assert cfg.resolve_backend(None) == "auto"


def test_resolve_model_cli_wins() -> None:
    cfg = ScrConfig(model={"default": "x", "claude-api": "y"})
    assert cfg.resolve_model(backend="claude-api", cli_value="cli-pick") == "cli-pick"


def test_resolve_model_per_backend_wins_over_default() -> None:
    cfg = ScrConfig(model={"default": "claude-opus-4-7", "gemini-api": "gemini-3-pro"})
    assert cfg.resolve_model(backend="gemini-api", cli_value=None) == "gemini-3-pro"
    assert cfg.resolve_model(backend="claude-api", cli_value=None) == "claude-opus-4-7"


def test_resolve_model_falls_back_to_hardcoded_default() -> None:
    cfg = ScrConfig()
    assert cfg.resolve_model(backend="claude-api", cli_value=None) == "claude-opus-4-7"


# ---------------------------------------------------------------------------
# apply_env: setdefault semantics so shell + .env always win
# ---------------------------------------------------------------------------

def test_apply_env_sets_only_when_unset() -> None:
    cfg = ScrConfig(env={"FOO": "from-config", "BAR": "from-config"})
    env: dict[str, str] = {"FOO": "from-shell"}
    cfg.apply_env(env)
    assert env["FOO"] == "from-shell"  # shell wins
    assert env["BAR"] == "from-config"  # config fills the gap
