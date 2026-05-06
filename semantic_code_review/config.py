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
    default      = "claude-opus-4-7"
    "gemini-api" = "gemini-3-pro"

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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .paths import default_config_path, find_repo_config_path


VALID_BACKENDS = frozenset({"auto", "claude-api", "claude-cli", "gemini-api", "gemini-cli"})


@dataclass
class ScrConfig:
    """Resolved config: user file + per-repo file merged."""

    backend: str | None = None
    model: dict[str, str] = field(default_factory=dict)
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
        cfg = cls()

        user = user_path if user_path is not None else default_config_path()
        if user.is_file():
            cfg._merge(_parse(user), source=str(user))

        repo = repo_path if repo_path is not None else find_repo_config_path(cwd)
        if repo is not None and repo.is_file():
            cfg._merge(_parse(repo), source=str(repo))

        return cfg

    def _merge(self, raw: dict[str, Any], *, source: str) -> None:
        backend = raw.get("backend")
        if isinstance(backend, str):
            if backend not in VALID_BACKENDS:
                raise ConfigError(
                    f"{source}: backend = {backend!r} not one of "
                    f"{sorted(VALID_BACKENDS)}"
                )
            self.backend = backend
            self.sources["backend"] = source

        model = raw.get("model")
        if isinstance(model, dict):
            for k, v in model.items():
                if not isinstance(v, str):
                    raise ConfigError(
                        f"{source}: model.{k!r} must be a string, got {type(v).__name__}"
                    )
                self.model[k] = v
                self.sources[f"model.{k}"] = source

        env = raw.get("env")
        if isinstance(env, dict):
            for k, v in env.items():
                if not isinstance(v, str):
                    raise ConfigError(
                        f"{source}: env.{k!r} must be a string, got {type(v).__name__}"
                    )
                self.env[k] = v
                self.sources[f"env.{k}"] = source

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

        Priority: CLI flag > config[model][backend] > config[model][default]
        > "claude-opus-4-7".

        The hardcoded fallback matches the historical default; users
        who want a different default can put it in `[model] default`
        rather than passing `--model` every time.
        """
        if cli_value is not None:
            return cli_value
        return (
            self.model.get(backend)
            or self.model.get("default")
            or "claude-opus-4-7"
        )


class ConfigError(RuntimeError):
    """Raised when a config file is malformed."""


def _parse(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: invalid TOML: {e}") from e


__all__ = ["ConfigError", "ScrConfig", "VALID_BACKENDS"]
