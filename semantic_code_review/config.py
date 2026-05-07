"""User-level config for scr.

Loads (and merges) two optional TOML files:

  - `~/.config/scr/config.toml` (or `$XDG_CONFIG_HOME/scr/config.toml`)
    — user-wide defaults.
  - `<repo>/.scr/config.toml` — per-repo overrides, found by walking
    up from cwd.

Per-repo wins on conflict. Both files are optional; their absence is
the same as an empty config.

Schema (all top-level fields optional):

    backend = "claude-api"     # default backend if --backend not passed

    [model]
    default = "claude-opus-4-7"     # global model fallback

    [backends.claude-api]           # override a builtin backend's model
    model = "claude-sonnet-4-7"

    [backends.groq]                 # add a new backend (openai-compat type)
    type = "openai-compat"
    base_url = "https://api.groq.com/openai/v1"
    api_key_env = "GROQ_API_KEY"
    model = "llama-3.3-70b-versatile"

    [env]
    GOOGLE_CLOUD_PROJECT = "aasgard-dev"
    GOOGLE_CLOUD_LOCATION = "global"

`[env]` entries get applied via `os.environ.setdefault(...)` so the
user's existing `.env` / shell exports always take precedence — useful
for non-secret defaults like GCP project/location.

DO NOT put API keys here. Config files leak too easily (accidental
commits, dotfile repos, screen-shares). Use `.env`, your shell's
startup, or a system keychain (future work).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any

from .paths import default_config_path, find_repo_config_path


class BackendType(str, Enum):
    """The handler family a backend dispatches to.

    Each value corresponds to one branch in the CLI's `_select_client`
    dispatch. Adding a new family means a new branch there; adding a
    new backend that reuses an existing family is just a new entry in
    a builtin table or a `[backends.<name>]` block in user config.
    """
    ANTHROPIC_SDK = "anthropic-sdk"
    CLAUDE_CLI = "claude-cli"
    GOOGLE_SDK = "google-sdk"
    GEMINI_CLI = "gemini-cli"
    OPENAI_COMPAT = "openai-compat"


@dataclass(frozen=True)
class BackendDef:
    """One entry in the merged backend table.

    `type` selects the dispatch branch. `default_model` is the model
    used when neither `--model` nor `[model] default` resolves first.
    `base_url` and `api_key_env` are only meaningful for
    `BackendType.OPENAI_COMPAT`.
    """
    type: BackendType
    default_model: str | None = None
    base_url: str | None = None
    api_key_env: str | None = None


# Code-side preset table. Users can override any entry's `model` (and
# any other field, for power users) via `[backends.<name>]` in their
# TOML, or add brand-new named backends.
#
# Multi-transport vendors keep a `-api` / `-cli` suffix because the
# vendor name alone is ambiguous; single-transport providers use the
# bare vendor name. All non-Anthropic / non-Google entries reach the
# provider via the OpenAI Chat Completions wire format.
BUILTIN_BACKENDS: dict[str, BackendDef] = {
    "claude-api": BackendDef(
        type=BackendType.ANTHROPIC_SDK,
        default_model="claude-opus-4-7",
    ),
    "claude-cli": BackendDef(
        type=BackendType.CLAUDE_CLI,
        default_model="claude-opus-4-7",
    ),
    "gemini-api": BackendDef(
        type=BackendType.GOOGLE_SDK,
        default_model="gemini-2.5-pro",
    ),
    "gemini-cli": BackendDef(
        type=BackendType.GEMINI_CLI,
        default_model="gemini-2.5-pro",
    ),
    # Free tier with generous daily token quota; tool use works.
    "groq": BackendDef(
        type=BackendType.OPENAI_COMPAT,
        base_url="https://api.groq.com/openai/v1",
        api_key_env="GROQ_API_KEY",
        default_model="llama-3.3-70b-versatile",
    ),
    # Any GitHub account → free quota across multiple model families.
    # GitHub Models requires publisher-prefixed model ids ("openai/...").
    "github": BackendDef(
        type=BackendType.OPENAI_COMPAT,
        base_url="https://models.github.ai/inference",
        api_key_env="GITHUB_TOKEN",
        default_model="openai/gpt-4o-mini",
    ),
    # Free tier; very fast inference. Model id needs to be passed
    # explicitly because Cerebras' catalogue rotates.
    "cerebras": BackendDef(
        type=BackendType.OPENAI_COMPAT,
        base_url="https://api.cerebras.ai/v1",
        api_key_env="CEREBRAS_API_KEY",
    ),
    # Hundreds of models including some free tiers; pass --model to
    # pick. e.g. `meta-llama/llama-3.3-70b-instruct:free`.
    "openrouter": BackendDef(
        type=BackendType.OPENAI_COMPAT,
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
    ),
    # La Plateforme free tier; Codestral is Mistral's code-tuned model.
    "mistral": BackendDef(
        type=BackendType.OPENAI_COMPAT,
        base_url="https://api.mistral.ai/v1",
        api_key_env="MISTRAL_API_KEY",
        default_model="codestral-latest",
    ),
    # Local llama.cpp/Ollama; no credentials needed. Pass --model to
    # name a model you've pulled (e.g. `qwen2.5-coder:14b`).
    "ollama": BackendDef(
        type=BackendType.OPENAI_COMPAT,
        base_url="http://localhost:11434/v1",
    ),
}


@dataclass
class ScrConfig:
    """Resolved config: user file + per-repo file merged."""

    backend: str | None = None
    backends: dict[str, BackendDef] = field(default_factory=dict)
    model_default: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    # Where each setting came from, for `scr config show`.
    sources: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(
        cls,
        *,
        user_path: Path | None = None,
        repo_path: Path | None = None,
        cwd: Path | None = None,
    ) -> "ScrConfig":
        """Load and merge config files. Either path can be overridden for tests."""
        cfg = cls(backends=dict(BUILTIN_BACKENDS))

        user = user_path if user_path is not None else default_config_path()
        if user.is_file():
            cfg._merge(_parse(user), source=str(user))

        repo = repo_path if repo_path is not None else find_repo_config_path(cwd)
        if repo is not None and repo.is_file():
            cfg._merge(_parse(repo), source=str(repo))

        # Backend reference must point at a defined backend (or "auto").
        if cfg.backend is not None and cfg.backend != "auto" and cfg.backend not in cfg.backends:
            raise ConfigError(
                f"{cfg.sources.get('backend', '?')}: backend = {cfg.backend!r} "
                f"not one of {sorted(['auto', *cfg.backends.keys()])}"
            )

        return cfg

    def _merge(self, raw: dict[str, Any], *, source: str) -> None:
        backend = raw.get("backend")
        if isinstance(backend, str):
            self.backend = backend
            self.sources["backend"] = source

        # Legacy [model] table: `default` is the global fallback;
        # any other key is sugar for `[backends.<key>] model = ...`.
        model = raw.get("model")
        if isinstance(model, dict):
            for k, v in model.items():
                if not isinstance(v, str):
                    raise ConfigError(
                        f"{source}: model.{k!r} must be a string, got {type(v).__name__}"
                    )
                if k == "default":
                    self.model_default = v
                    self.sources["model.default"] = source
                else:
                    self._set_backend_model(k, v, source=source, source_key=f"model.{k}")

        # New-style [backends.<name>] table.
        backends = raw.get("backends")
        if isinstance(backends, dict):
            for name, body in backends.items():
                if not isinstance(body, dict):
                    raise ConfigError(
                        f"{source}: backends.{name!r} must be a table, got {type(body).__name__}"
                    )
                self._merge_backend(name, body, source=source)

        env = raw.get("env")
        if isinstance(env, dict):
            for k, v in env.items():
                if not isinstance(v, str):
                    raise ConfigError(
                        f"{source}: env.{k!r} must be a string, got {type(v).__name__}"
                    )
                self.env[k] = v
                self.sources[f"env.{k}"] = source

    def _merge_backend(self, name: str, body: dict[str, Any], *, source: str) -> None:
        existing = self.backends.get(name)
        type_raw = body.get("type")
        if type_raw is not None:
            if not isinstance(type_raw, str):
                raise ConfigError(
                    f"{source}: backends.{name}.type must be a string, "
                    f"got {type(type_raw).__name__}"
                )
            try:
                btype = BackendType(type_raw)
            except ValueError:
                valid = sorted(t.value for t in BackendType)
                raise ConfigError(
                    f"{source}: backends.{name}.type = {type_raw!r} "
                    f"not one of {valid}"
                ) from None
        elif existing is not None:
            btype = existing.type
        else:
            raise ConfigError(
                f"{source}: backends.{name} is new — `type` is required "
                f"(one of {sorted(t.value for t in BackendType)})"
            )

        merged = BackendDef(
            type=btype,
            default_model=_pick_str(body, "model", existing.default_model if existing else None),
            base_url=_pick_str(body, "base_url", existing.base_url if existing else None),
            api_key_env=_pick_str(body, "api_key_env", existing.api_key_env if existing else None),
        )
        self.backends[name] = merged
        self.sources[f"backends.{name}"] = source
        for key in ("model", "base_url", "api_key_env"):
            if key in body:
                self.sources[f"backends.{name}.{key}"] = source

    def _set_backend_model(self, name: str, model: str, *, source: str, source_key: str) -> None:
        """Fold a legacy `[model][<name>]` entry into the backends table.

        If <name> isn't a builtin and hasn't been declared in
        `[backends.<name>]` yet, this raises — we can't infer a type.
        """
        existing = self.backends.get(name)
        if existing is None:
            raise ConfigError(
                f"{source}: model.{name!r} refers to an unknown backend; "
                f"declare it with [backends.{name}] first or use one of "
                f"{sorted(self.backends.keys())}"
            )
        self.backends[name] = replace(existing, default_model=model)
        self.sources[source_key] = source

    # -----------------------------------------------------------------
    # Resolution helpers (called from the CLI command bodies)
    # -----------------------------------------------------------------

    def apply_env(self, environ: dict[str, str] | None = None) -> None:
        """Set `[env]` entries on the process environment if not already present.

        `os.environ.setdefault` semantics: the user's shell exports and
        any `.env` they loaded earlier win. The config is the LAST
        fallback before built-in defaults.
        """
        env = environ if environ is not None else os.environ
        for k, v in self.env.items():
            env.setdefault(k, v)

    def resolve_backend(self, cli_value: str | None) -> str:
        """Pick the effective backend.

        Priority: CLI flag > config > "auto".
        """
        if cli_value is not None:
            return cli_value
        return self.backend or "auto"

    def resolve_model(self, *, backend: str, cli_value: str | None) -> str:
        """Pick the effective model.

        Priority: CLI flag > backend's resolved default_model >
        `[model] default` > "claude-opus-4-7" (the historical fallback).
        """
        if cli_value is not None:
            return cli_value
        bdef = self.backends.get(backend)
        if bdef is not None and bdef.default_model is not None:
            return bdef.default_model
        if self.model_default is not None:
            return self.model_default
        return "claude-opus-4-7"


class ConfigError(RuntimeError):
    """Raised when a config file is malformed."""


def _parse(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: invalid TOML: {e}") from e


def _pick_str(body: dict[str, Any], key: str, fallback: str | None) -> str | None:
    if key not in body:
        return fallback
    v = body[key]
    if v is None:
        return None
    if not isinstance(v, str):
        raise ConfigError(f"backend field {key!r} must be a string, got {type(v).__name__}")
    return v


__all__ = ["BackendDef", "BackendType", "BUILTIN_BACKENDS", "ConfigError", "ScrConfig"]
