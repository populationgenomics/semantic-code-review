"""Cross-command helpers — config, backend resolution, prompt files, logging.

Kept private to the `cli` package. Commands import from here so each
per-command module stays focused on argument parsing + delegating to
the relevant subsystem.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import typer

from ..config import ConfigError, ScrConfig


# Lazily-loaded user/per-repo config. Loading is deferred to the first
# `get_config()` call so a malformed config doesn't brick commands that
# never touch it — most importantly `scr config edit`, which is the
# user's escape hatch for fixing the very config that's broken. Also
# means `scr --help` and `scr --version` work in any state.
_CONFIG_CACHE: ScrConfig | None = None


def get_config() -> ScrConfig:
    """Return the resolved config, loading + applying `[env]` on first use.

    Subsequent calls return the cached instance. A `ConfigError` from
    `ScrConfig.load()` is converted to a clean exit at the CLI boundary
    so the user sees a short message rather than a Python traceback.
    Order of env-var precedence: shell env > .env > config[env]; each
    layer uses `setdefault` so the closer one wins.
    """
    global _CONFIG_CACHE
    if _CONFIG_CACHE is None:
        try:
            cfg = ScrConfig.load()
        except ConfigError as e:
            sys.stderr.write(f"scr: {e}\n")
            sys.stderr.flush()
            raise SystemExit(1)
        cfg.apply_env()
        _CONFIG_CACHE = cfg
    return _CONFIG_CACHE


def _reset_config_cache() -> None:
    """Drop the cached config so the next `get_config()` re-reads from disk.

    Tests use this when they monkey-patch `XDG_CONFIG_HOME` or
    `SCR_REPO_ROOT` and want the change reflected in the next command.
    """
    global _CONFIG_CACHE
    _CONFIG_CACHE = None


def load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env loader: KEY=value lines, optional quotes, # comments.

    Also aliases ANTHROPIC_API_TOKEN -> ANTHROPIC_API_KEY because the
    Anthropic SDK reads the KEY form.
    """
    if not path.exists():
        return
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            os.environ.setdefault(key, value)
    except OSError:
        return
    if "ANTHROPIC_API_KEY" not in os.environ and "ANTHROPIC_API_TOKEN" in os.environ:
        os.environ["ANTHROPIC_API_KEY"] = os.environ["ANTHROPIC_API_TOKEN"]


def configure_logging(verbose: bool) -> None:
    # Default is WARNING — quiet by default, with `--verbose` switching
    # to INFO so per-request httpx lines and per-hunk pipeline progress
    # are visible. (Previous default of INFO produced ~50 stderr lines
    # per medium PR; users couldn't see their own output through it.)
    level = logging.INFO if verbose else logging.WARNING
    # force=True so we take over even if a library (anthropic SDK, typer,
    # etc.) already attached a root handler at WARNING — otherwise our
    # progress logs would be silently dropped.
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    # basicConfig sets the *logger* level but leaves the StreamHandler
    # at NOTSET (passes everything). _attach_file_log later raises the
    # package logger to INFO so the trace file captures progress —
    # without an explicit handler level here, those INFO records leak
    # up to the root's stderr handler and undo the quiet default.
    for h in logging.getLogger().handlers:
        h.setLevel(level)


def select_client(backend: str, *, model: str):
    """Resolve a backend name to a `Client` for the augment pipeline.

    `backend` is "auto" or any name in `get_config().backends` (builtins +
    user-defined `[backends.<name>]` entries). All dispatch lives in
    `semantic_code_review.backends`; this is the CLI's only entry point.
    """
    from .. import backends as _backends

    cfg = get_config()
    if backend == "auto":
        backend = _backends.resolve_auto(config=cfg)
    return _backends.get(backend, config=cfg).resolve(model=model)


def resolve_extra_review_prompt(cli_path: Path | None) -> str | None:
    """CLI --extra-prompt path wins, otherwise the inline config value.

    The CLI flag loads the prompt off the given file (a missing /
    unreadable / empty file is a hard error — the user asked for an
    extra-review pass and we shouldn't silently degrade). When no CLI
    flag is set, we fall through to the inline ``[augment].extra_prompt``
    string from the config.
    """
    if cli_path is not None:
        try:
            text = cli_path.read_text(encoding="utf-8").strip()
        except OSError as e:
            typer.echo(f"scr: extra-review prompt {cli_path}: {e}", err=True)
            raise typer.Exit(code=2)
        if not text:
            typer.echo(f"scr: extra-review prompt {cli_path} is empty", err=True)
            raise typer.Exit(code=2)
        return text
    return get_config().extra_review_prompt


__all__ = [
    "configure_logging",
    "get_config",
    "load_dotenv",
    "resolve_extra_review_prompt",
    "select_client",
    "_reset_config_cache",
]
