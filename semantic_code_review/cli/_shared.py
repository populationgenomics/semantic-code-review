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
from typing import TYPE_CHECKING

import typer

from .. import config, git_ops, paths
from ..config import ConfigError, ScrConfig

if TYPE_CHECKING:
    # Type-only: importing agents eagerly pulls pydantic_ai into every CLI
    # startup. See docs/style/python.md — the sanctioned expensive-import case.
    from ..augment.agents import Client


def load_config(*, repo: str | None, committed_path: Path | None) -> ScrConfig:
    """Load the config for a review of `repo` and apply its `[env]`.

    Loaded per command, not at import, so a malformed config doesn't
    brick commands that never touch it — most importantly `scr config
    edit`, the user's escape hatch for fixing it. A `ConfigError` becomes
    a clean exit so the user sees a short message rather than a
    traceback. Env-var precedence: shell env > .env > config[env]; each
    layer uses `setdefault` so the closer one wins.

    Args:
        repo: The reviewed repo as `owner/name`, or None when it has none.
        committed_path: The reviewed repo's committed `.scr/config.toml`.
    """
    try:
        cfg = ScrConfig.load(repo=repo, committed_path=committed_path)
    except ConfigError as e:
        sys.stderr.write(f"scr: {e}\n")
        sys.stderr.flush()
        raise SystemExit(1) from None
    cfg.apply_env()
    return cfg


def local_repo(start: Path | None) -> tuple[str | None, Path | None]:
    """The GitHub identity and committed config path of the git repo
    containing `start` (cwd when None).

    Returns `(None, None)` outside a git repo. The identity is None when
    the repo has no `origin` or `origin` is not on GitHub: such a repo
    has no `[repos."owner/name"]` entry to select.
    """
    code, top, _ = git_ops.git_capture(start, "rev-parse", "--show-toplevel")
    if code != 0:
        return None, None
    root = Path(top.strip())
    code, url, _ = git_ops.git_capture(root, "remote", "get-url", "origin")
    repo = config.github_repo(url.strip()) if code == 0 else None
    return repo, paths.committed_config_path(root)


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


def select_client(cfg: ScrConfig, backend: str, *, model: str) -> Client:
    """Resolve a backend name to a `Client` for the augment pipeline.

    `backend` is "auto" or any name in `cfg.backends` (builtins +
    user-defined `[backends.<name>]` entries). All dispatch lives in
    `semantic_code_review.backends`; this is the CLI's only entry point.
    """
    import pydantic_ai

    from .. import backends as _backends

    # pydantic-ai prints a first-run banner to a terminal; scr owns its
    # output, and a detached server's log is no place for it either.
    pydantic_ai.BANNER_ENABLED = False
    if backend == "auto":
        backend = _backends.resolve_auto(config=cfg)
    return _backends.get(backend, config=cfg).resolve(model=model)


def _read_prompt_file(cli_path: Path, *, label: str) -> str:
    """Load a prompt body off a CLI-named file.

    A missing, unreadable or empty file exits 2 rather than falling back
    to the config: the user named a file, and quietly reviewing without
    it is a silently different review.
    """
    try:
        text = cli_path.read_text(encoding="utf-8").strip()
    except OSError as e:
        typer.echo(f"scr: {label} {cli_path}: {e}", err=True)
        raise typer.Exit(code=2) from None
    if not text:
        typer.echo(f"scr: {label} {cli_path} is empty", err=True)
        raise typer.Exit(code=2)
    return text


def resolve_extra_review_prompt(cfg: ScrConfig, cli_path: Path | None) -> str | None:
    """CLI --extra-prompt path wins, otherwise the inline config value.

    The CLI flag loads the prompt off the given file. When no CLI flag
    is set, we fall through to the inline ``[augment].extra_prompt``
    string from the config.
    """
    if cli_path is not None:
        return _read_prompt_file(cli_path, label="extra-review prompt")
    return cfg.extra_review_prompt


def resolve_explainer_prompt(cfg: ScrConfig, cli_path: Path | None) -> str | None:
    """CLI --explainer-prompt path wins, otherwise the inline config value.

    Same resolution order as :func:`resolve_extra_review_prompt`, over
    ``[augment].explainer_prompt``. The text is house style for the
    change-explainer document only; it reaches no other pass.

    The flag against a disabled explainer exits 2. ``[augment].explainer
    = false`` lives in a file the user may not have read today, so
    accepting a house style for a document that will never be generated
    is a silent no-op on an explicit request.
    """
    if cli_path is not None:
        if not cfg.explainer:
            typer.echo(
                "scr: --explainer-prompt was given but [augment].explainer = false "
                f"(from {cfg.sources.get('augment.explainer', '?')}) — "
                "no explainer document will be generated",
                err=True,
            )
            raise typer.Exit(code=2)
        return _read_prompt_file(cli_path, label="explainer house-style prompt")
    return cfg.explainer_prompt


__all__ = [
    "configure_logging",
    "load_config",
    "load_dotenv",
    "local_repo",
    "resolve_explainer_prompt",
    "resolve_extra_review_prompt",
    "select_client",
]
