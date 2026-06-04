"""ScrConfig: TOML loader, merging, env application, resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from semantic_code_review.config import (
    BUILTIN_BACKENDS,
    BackendDef,
    BackendType,
    ConfigError,
    ScrConfig,
)
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

def test_load_empty_config_starts_with_builtins(tmp_path: Path) -> None:
    cfg = ScrConfig.load(user_path=tmp_path / "missing.toml", repo_path=None)
    assert cfg.backend is None
    assert cfg.model_default is None
    assert cfg.env == {}
    # Builtins are populated regardless of whether a config file exists.
    assert set(cfg.backends) == set(BUILTIN_BACKENDS)


def test_openai_compat_presets_registered() -> None:
    """The free-tier-friendly providers ship as builtins."""
    expected = {"groq", "github", "cerebras", "openrouter", "mistral", "ollama"}
    for name in expected:
        assert name in BUILTIN_BACKENDS, f"missing builtin: {name}"
        bdef = BUILTIN_BACKENDS[name]
        assert bdef.type is BackendType.OPENAI_COMPAT
        assert bdef.base_url, f"{name}: base_url is required"
    # Ollama is the only one without an api_key_env.
    assert BUILTIN_BACKENDS["ollama"].api_key_env is None
    assert BUILTIN_BACKENDS["groq"].api_key_env == "GROQ_API_KEY"


def test_every_builtin_has_a_description() -> None:
    """The template renderer leans on description for the lead comment."""
    for name, bdef in BUILTIN_BACKENDS.items():
        assert bdef.description, f"{name} is missing a description"


def test_field_doc_extracts_annotated_metadata() -> None:
    from semantic_code_review.config import field_doc

    assert field_doc("default_model").startswith("Model used")
    assert "shell-quoted" in field_doc("api_key_command")
    assert field_doc("nonexistent") == ""


def test_augment_extra_prompt_loaded_inline(tmp_path: Path) -> None:
    """`[augment].extra_prompt = "..."` lands on the resolved config as
    a stripped string. Source attribution points at the file that set it."""
    user = _write(tmp_path / "user.toml", '''
[augment]
extra_prompt = """
You are doing an extra code review pass.
Look for bugs, perf, security.
"""
''')
    cfg = ScrConfig.load(user_path=user, repo_path=None)
    assert cfg.extra_review_prompt is not None
    assert cfg.extra_review_prompt.startswith("You are doing an extra")
    assert cfg.extra_review_prompt.endswith("perf, security.")
    assert cfg.sources["augment.extra_prompt"] == str(user)


def test_augment_extra_prompt_empty_string_is_ignored(tmp_path: Path) -> None:
    """An all-whitespace value is treated as 'unset' rather than
    spinning up an extra pass with an empty system prompt."""
    user = _write(tmp_path / "user.toml", '''
[augment]
extra_prompt = "   \\n   "
''')
    cfg = ScrConfig.load(user_path=user, repo_path=None)
    assert cfg.extra_review_prompt is None
    assert "augment.extra_prompt" not in cfg.sources


def test_augment_extra_prompt_repo_overrides_user(tmp_path: Path) -> None:
    """Standard config layering: per-repo `[augment].extra_prompt`
    wins over the user-global value, but inherits when the per-repo
    config doesn't set it."""
    user = _write(tmp_path / "user.toml", '''
[augment]
extra_prompt = "team-wide review checklist"
''')
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    repo = _write(repo_dir / "repo.toml", '''
[augment]
extra_prompt = "this repo wants a different lens"
''')
    cfg = ScrConfig.load(user_path=user, repo_path=repo)
    assert cfg.extra_review_prompt == "this repo wants a different lens"
    assert cfg.sources["augment.extra_prompt"] == str(repo)

    # Without a per-repo override, the user-global setting flows through.
    cfg_global_only = ScrConfig.load(user_path=user, repo_path=None)
    assert cfg_global_only.extra_review_prompt == "team-wide review checklist"


def test_augment_extra_prompt_rejects_non_string(tmp_path: Path) -> None:
    user = _write(tmp_path / "user.toml", '''
[augment]
extra_prompt = 42
''')
    with pytest.raises(ConfigError, match="must be a string"):
        ScrConfig.load(user_path=user, repo_path=None)


def test_load_user_only(tmp_path: Path) -> None:
    user = _write(tmp_path / "user.toml", '''
backend = "gemini-api"

[model]
default = "claude-opus-4-7"

[backends.gemini-api]
model = "gemini-2.5-pro"

[env]
GOOGLE_CLOUD_PROJECT = "aasgard-dev"
''')
    cfg = ScrConfig.load(user_path=user, repo_path=None)
    assert cfg.backend == "gemini-api"
    assert cfg.model_default == "claude-opus-4-7"
    assert cfg.backends["gemini-api"].default_model == "gemini-2.5-pro"
    assert cfg.env == {"GOOGLE_CLOUD_PROJECT": "aasgard-dev"}


def test_legacy_model_table_folds_into_backend(tmp_path: Path) -> None:
    """`[model] "claude-api" = ...` is sugar for `[backends.claude-api] model = ...`."""
    user = _write(tmp_path / "user.toml", '''
[model]
"claude-api" = "claude-sonnet-4-7"
''')
    cfg = ScrConfig.load(user_path=user, repo_path=None)
    assert cfg.backends["claude-api"].default_model == "claude-sonnet-4-7"
    # Builtin type preserved.
    assert cfg.backends["claude-api"].type is BackendType.ANTHROPIC_SDK


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
    assert cfg.model_default == "repo-model"
    # Repo overrode shared keys; user's exclusive keys survive.
    assert cfg.env["GOOGLE_CLOUD_PROJECT"] == "repo-project"
    assert cfg.env["GOOGLE_CLOUD_LOCATION"] == "us-central1"
    # Sources reflect the winning layer.
    assert "repo.toml" in cfg.sources["backend"]
    assert "user.toml" in cfg.sources["env.GOOGLE_CLOUD_LOCATION"]


def test_backends_table_can_add_a_new_entry(tmp_path: Path) -> None:
    user = _write(tmp_path / "user.toml", '''
[backends.localollama]
type = "openai-compat"
base_url = "http://localhost:11434/v1"
model = "qwen2.5-coder:32b"
''')
    cfg = ScrConfig.load(user_path=user, repo_path=None)
    assert "localollama" in cfg.backends
    bdef = cfg.backends["localollama"]
    assert bdef.type is BackendType.OPENAI_COMPAT
    assert bdef.base_url == "http://localhost:11434/v1"
    assert bdef.default_model == "qwen2.5-coder:32b"
    assert bdef.api_key_env is None


def test_api_key_command_parses_from_toml(tmp_path: Path) -> None:
    user = _write(tmp_path / "user.toml", '''
[backends.gcloud-secret]
type = "openai-compat"
base_url = "https://example.com/v1"
api_key_command = ["gcloud", "secrets", "versions", "access", "latest", "--secret=anth"]
''')
    cfg = ScrConfig.load(user_path=user, repo_path=None)
    bdef = cfg.backends["gcloud-secret"]
    assert bdef.api_key_command == (
        "gcloud", "secrets", "versions", "access", "latest", "--secret=anth",
    )


def test_api_key_command_accepts_shell_quoted_string(tmp_path: Path) -> None:
    user = _write(tmp_path / "user.toml", '''
[backends.shell-string]
type = "openai-compat"
base_url = "https://example.com/v1"
api_key_command = "gcloud secrets versions access latest --secret=anth"
''')
    cfg = ScrConfig.load(user_path=user, repo_path=None)
    bdef = cfg.backends["shell-string"]
    assert bdef.api_key_command == (
        "gcloud", "secrets", "versions", "access", "latest", "--secret=anth",
    )


def test_api_key_command_string_handles_quoting(tmp_path: Path) -> None:
    """Embedded whitespace in args via shell-style quoting."""
    user = _write(tmp_path / "user.toml", r'''
[backends.quoting]
type = "openai-compat"
base_url = "https://example.com/v1"
api_key_command = "fetch --header 'Auth: Bearer xyz' /path"
''')
    cfg = ScrConfig.load(user_path=user, repo_path=None)
    assert cfg.backends["quoting"].api_key_command == (
        "fetch", "--header", "Auth: Bearer xyz", "/path",
    )


def test_api_key_command_list_still_works(tmp_path: Path) -> None:
    """The list form remains the escape hatch for fiddly quoting."""
    user = _write(tmp_path / "user.toml", '''
[backends.list-form]
type = "openai-compat"
base_url = "https://example.com/v1"
api_key_command = ["bash", "-c", "cat /tmp/key"]
''')
    cfg = ScrConfig.load(user_path=user, repo_path=None)
    assert cfg.backends["list-form"].api_key_command == (
        "bash", "-c", "cat /tmp/key",
    )


def test_api_key_command_unbalanced_quotes_raises(tmp_path: Path) -> None:
    user = _write(tmp_path / "user.toml", '''
[backends.bad]
type = "openai-compat"
base_url = "https://example.com/v1"
api_key_command = "echo 'unterminated"
''')
    with pytest.raises(ConfigError, match="unbalanced quotes"):
        ScrConfig.load(user_path=user, repo_path=None)


def test_api_key_command_wrong_type_raises(tmp_path: Path) -> None:
    user = _write(tmp_path / "user.toml", '''
[backends.bad]
type = "openai-compat"
base_url = "https://example.com/v1"
api_key_command = 42
''')
    with pytest.raises(ConfigError, match="must be a list of strings or a"):
        ScrConfig.load(user_path=user, repo_path=None)


def test_api_key_command_must_not_be_empty(tmp_path: Path) -> None:
    user = _write(tmp_path / "user.toml", '''
[backends.bad]
type = "openai-compat"
base_url = "https://example.com/v1"
api_key_command = []
''')
    with pytest.raises(ConfigError, match="must not be empty"):
        ScrConfig.load(user_path=user, repo_path=None)


def test_api_key_command_empty_string_raises(tmp_path: Path) -> None:
    user = _write(tmp_path / "user.toml", '''
[backends.bad]
type = "openai-compat"
base_url = "https://example.com/v1"
api_key_command = ""
''')
    with pytest.raises(ConfigError, match="must not be empty"):
        ScrConfig.load(user_path=user, repo_path=None)


def test_backends_table_overrides_builtin_field_by_field(tmp_path: Path) -> None:
    user = _write(tmp_path / "user.toml", '''
[backends.claude-api]
model = "claude-sonnet-4-7"
''')
    cfg = ScrConfig.load(user_path=user, repo_path=None)
    bdef = cfg.backends["claude-api"]
    # type unchanged from builtin
    assert bdef.type is BackendType.ANTHROPIC_SDK
    # model overridden
    assert bdef.default_model == "claude-sonnet-4-7"


def test_invalid_toml_raises_config_error(tmp_path: Path) -> None:
    bad = _write(tmp_path / "bad.toml", "this is = = not valid")
    with pytest.raises(ConfigError, match="invalid TOML"):
        ScrConfig.load(user_path=bad, repo_path=None)


def test_unknown_backend_name_raises(tmp_path: Path) -> None:
    user = _write(tmp_path / "user.toml", 'backend = "telepathy"')
    with pytest.raises(ConfigError, match="not one of"):
        ScrConfig.load(user_path=user, repo_path=None)


def test_unknown_backend_type_raises(tmp_path: Path) -> None:
    user = _write(tmp_path / "user.toml", '''
[backends.weird]
type = "carrier-pigeon"
''')
    with pytest.raises(ConfigError, match="carrier-pigeon"):
        ScrConfig.load(user_path=user, repo_path=None)


def test_new_backend_without_type_raises(tmp_path: Path) -> None:
    user = _write(tmp_path / "user.toml", '''
[backends.brand-new]
model = "x"
''')
    with pytest.raises(ConfigError, match="`type` is required"):
        ScrConfig.load(user_path=user, repo_path=None)


def test_legacy_model_referencing_unknown_backend_raises(tmp_path: Path) -> None:
    user = _write(tmp_path / "user.toml", '''
[model]
"made-up" = "x"
''')
    with pytest.raises(ConfigError, match="unknown backend"):
        ScrConfig.load(user_path=user, repo_path=None)


def test_non_string_model_value_raises(tmp_path: Path) -> None:
    user = _write(tmp_path / "user.toml", '[model]\ndefault = 42')
    with pytest.raises(ConfigError, match="must be a string"):
        ScrConfig.load(user_path=user, repo_path=None)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def test_resolve_backend_cli_wins_over_config() -> None:
    cfg = ScrConfig(backend="claude-api", backends=dict(BUILTIN_BACKENDS))
    assert cfg.resolve_backend("gemini-api") == "gemini-api"


def test_resolve_backend_falls_back_to_auto() -> None:
    cfg = ScrConfig(backends=dict(BUILTIN_BACKENDS))
    assert cfg.resolve_backend(None) == "auto"


def test_resolve_model_cli_wins() -> None:
    cfg = ScrConfig(
        backends={
            "claude-api": BackendDef(type=BackendType.ANTHROPIC_SDK, default_model="x"),
        },
    )
    assert cfg.resolve_model(backend="claude-api", cli_value="cli-pick") == "cli-pick"


def test_resolve_model_per_backend_wins_over_default() -> None:
    cfg = ScrConfig(
        backends={
            "claude-api": BackendDef(type=BackendType.ANTHROPIC_SDK, default_model="claude-opus-4-7"),
            "gemini-api": BackendDef(type=BackendType.GOOGLE_SDK, default_model="gemini-3-pro"),
        },
        model_default="claude-fallback",
    )
    assert cfg.resolve_model(backend="gemini-api", cli_value=None) == "gemini-3-pro"
    assert cfg.resolve_model(backend="claude-api", cli_value=None) == "claude-opus-4-7"


def test_resolve_model_falls_back_to_global_default() -> None:
    cfg = ScrConfig(
        backends={
            "no-model": BackendDef(type=BackendType.OPENAI_COMPAT, base_url="x"),
        },
        model_default="global-fallback",
    )
    assert cfg.resolve_model(backend="no-model", cli_value=None) == "global-fallback"


def test_resolve_model_falls_back_to_hardcoded_default() -> None:
    cfg = ScrConfig(backends={})
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


# ---------------------------------------------------------------------------
# write_inline_extra_prompt — round-trip helper
# ---------------------------------------------------------------------------


def test_write_inline_prompt_appends_section_when_absent(tmp_path: Path) -> None:
    """No [augment] section yet — a new one is appended, with the
    triple-quoted assignment underneath."""
    from semantic_code_review.config import write_inline_extra_prompt

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text('backend = "claude-api"\n', encoding="utf-8")
    write_inline_extra_prompt(cfg_path, "Look for race conditions.\nAlso typos.")
    text = cfg_path.read_text(encoding="utf-8")
    assert "backend = \"claude-api\"" in text   # other sections preserved
    assert "[augment]" in text
    # Body landed inside a triple-quoted block.
    cfg = ScrConfig.load(user_path=cfg_path, repo_path=None)
    assert cfg.extra_review_prompt == "Look for race conditions.\nAlso typos."


def test_write_inline_prompt_replaces_existing_assignment(tmp_path: Path) -> None:
    """An existing extra_prompt is replaced in place; the rest of the
    file (header comment + other sections) is left alone."""
    from semantic_code_review.config import write_inline_extra_prompt

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        "# user-added comment\n"
        "backend = \"claude-api\"\n\n"
        "[augment]\n"
        "extra_prompt = \"old prompt\"\n\n"
        "[env]\n"
        "MY_VAR = \"x\"\n",
        encoding="utf-8",
    )
    write_inline_extra_prompt(cfg_path, "new prompt")
    text = cfg_path.read_text(encoding="utf-8")
    assert "# user-added comment" in text
    assert "[env]" in text
    assert "MY_VAR" in text
    assert "old prompt" not in text
    cfg = ScrConfig.load(user_path=cfg_path, repo_path=None)
    assert cfg.extra_review_prompt == "new prompt"


def test_write_inline_prompt_empty_body_removes_assignment(tmp_path: Path) -> None:
    """An empty body clears the assignment but leaves the [augment]
    section header in place for any future keys."""
    from semantic_code_review.config import write_inline_extra_prompt

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        "[augment]\n"
        "extra_prompt = \"\"\"\nsome prompt\n\"\"\"\n",
        encoding="utf-8",
    )
    write_inline_extra_prompt(cfg_path, "")
    cfg = ScrConfig.load(user_path=cfg_path, repo_path=None)
    assert cfg.extra_review_prompt is None
    # Section header remains.
    text = cfg_path.read_text(encoding="utf-8")
    assert "[augment]" in text


def test_write_inline_prompt_inserts_under_existing_augment_section(tmp_path: Path) -> None:
    """[augment] section exists but no extra_prompt — the assignment
    is inserted at the top of the section."""
    from semantic_code_review.config import write_inline_extra_prompt

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        "[augment]\n"
        "# future key goes here\n",
        encoding="utf-8",
    )
    write_inline_extra_prompt(cfg_path, "new prompt")
    cfg = ScrConfig.load(user_path=cfg_path, repo_path=None)
    assert cfg.extra_review_prompt == "new prompt"
    assert "future key goes here" in cfg_path.read_text(encoding="utf-8")


def test_write_inline_prompt_creates_file_when_absent(tmp_path: Path) -> None:
    """A missing config file: written from scratch with just [augment]."""
    from semantic_code_review.config import write_inline_extra_prompt

    cfg_path = tmp_path / "fresh.toml"
    assert not cfg_path.exists()
    write_inline_extra_prompt(cfg_path, "fresh prompt")
    cfg = ScrConfig.load(user_path=cfg_path, repo_path=None)
    assert cfg.extra_review_prompt == "fresh prompt"
