"""`scr config` subapp — inspect and edit the scr config files."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

import typer

from .. import git_ops
from . import app
from ._shared import get_config

config_app = typer.Typer(help="Inspect or edit scr's user/per-repo config.")
app.add_typer(config_app, name="config")


@config_app.command("path")
def config_path() -> None:
    """Print the user-level config path (creates the directory if missing)."""
    from ..paths import default_config_path

    typer.echo(str(default_config_path()))


@config_app.command("show")
def config_show() -> None:
    """Print the resolved config (user + per-repo merged) and where each setting came from."""
    from ..paths import default_config_path, find_repo_config_path

    cfg = get_config()
    user = default_config_path()
    repo = find_repo_config_path()
    typer.echo(f"# user config: {user} ({'present' if user.is_file() else 'absent'})")
    typer.echo(f"# per-repo config: {repo or '(none found)'}{' (present)' if repo and repo.is_file() else ''}")
    typer.echo("")
    typer.echo(f"backend = {cfg.backend!r} (from {cfg.sources.get('backend', 'default')})")
    if cfg.model_default is not None:
        typer.echo("[model]")
        typer.echo(f"  default = {cfg.model_default!r} (from {cfg.sources.get('model.default', '?')})")
    typer.echo("[backends]")
    for name in sorted(cfg.backends):
        bdef = cfg.backends[name]
        src = cfg.sources.get(f"backends.{name}", "builtin")
        typer.echo(f"  {name}  type={bdef.type.value}  (from {src})")
        if bdef.default_model is not None:
            typer.echo(f"    model       = {bdef.default_model!r}")
        if bdef.base_url is not None:
            typer.echo(f"    base_url    = {bdef.base_url!r}")
        if bdef.api_key_env is not None:
            typer.echo(f"    api_key_env = {bdef.api_key_env!r}")
        if bdef.api_key_command is not None:
            typer.echo(f"    api_key_command = {list(bdef.api_key_command)!r}")
    if cfg.env:
        typer.echo("[env]")
        for k, v in cfg.env.items():
            applied = "applied" if os.environ.get(k) == v else "overridden by shell/.env"
            typer.echo(f"  {k} = {v!r} (from {cfg.sources.get(f'env.{k}', '?')}, {applied})")
    if cfg.extra_review_prompt is not None:
        # Show line count + a leading snippet rather than the whole
        # body — extra-review prompts are typically multi-paragraph
        # and would crowd the resolved-config display.
        prompt = cfg.extra_review_prompt
        lines = prompt.count("\n") + 1
        first_line = prompt.split("\n", 1)[0][:80]
        typer.echo("[augment]")
        typer.echo(
            f"  extra_prompt = <{lines}-line prompt: {first_line!r}…> "
            f"(from {cfg.sources.get('augment.extra_prompt', '?')})"
        )


@config_app.command("edit")
def config_edit(
    subject: str = typer.Argument(
        "config",
        help=(
            "What to edit. 'config' (default) opens the whole TOML file. "
            "'prompt' extracts [augment].extra_prompt into a tempfile so "
            "you can edit it as plain markdown without TOML triple-quote "
            "escaping; the result is spliced back on save."
        ),
    ),
    scope: str = typer.Option(
        "user",
        "--scope",
        help=(
            "Which config to edit. 'user' (default) = "
            "~/.config/scr/config.toml. 'repo' = <repo_root>/.scr/config.toml "
            "for the current repo."
        ),
    ),
    template: str = typer.Option(
        None,
        "--template",
        help=(
            "(config subject only) Append a [backends.<name>] block "
            "before opening. <name> is any builtin (run `scr config show` "
            "for the list) or 'openai-compat' for a generic placeholder."
        ),
    ),
) -> None:
    """Open the scr config in $EDITOR (or `vi`).

    By default edits the whole user-global config file. Two args narrow
    that: `prompt` round-trips just the extra-review prompt through a
    markdown tempfile; `--scope=repo` targets the per-repo override
    instead of the user-global file.
    """
    if scope not in ("user", "repo"):
        raise typer.BadParameter(f"--scope must be 'user' or 'repo', got {scope!r}")
    if subject not in ("config", "prompt"):
        raise typer.BadParameter(f"subject must be 'config' or 'prompt', got {subject!r}")

    path = _resolve_config_edit_path(scope)
    if subject == "prompt":
        if template is not None:
            raise typer.BadParameter("--template only applies to the default 'config' subject")
        _edit_inline_prompt(path)
        return

    _edit_full_config(path, template)


def _resolve_config_edit_path(scope: str) -> Path:
    """Locate the config file for ``scope``, creating parent dirs as needed."""
    from ..paths import default_config_path

    if scope == "user":
        return default_config_path()
    # scope == "repo"
    try:
        root_text = git_ops.git(None, "rev-parse", "--show-toplevel").strip()
    except git_ops.GitError as e:
        typer.echo(f"scr: --scope=repo needs a git repo: {e}", err=True)
        raise typer.Exit(code=2)
    return Path(root_text) / ".scr" / "config.toml"


def _edit_full_config(path: Path, template: str | None) -> None:
    """Open the whole config file in ``$EDITOR``, with the optional
    backend-template append from the legacy ``--template`` flag.
    """
    import subprocess as _sp

    from ..config import BUILTIN_BACKENDS
    from ..config_template import SCAFFOLD_SECTION_NAME, render_backend_template

    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(_CONFIG_TEMPLATE, encoding="utf-8")

    if template is not None:
        valid = sorted([SCAFFOLD_SECTION_NAME, *BUILTIN_BACKENDS])
        if template not in valid:
            raise typer.BadParameter(f"unknown template {template!r}; expected one of: " + ", ".join(valid))
        existing = path.read_text(encoding="utf-8")
        # Skip+warn if the section already exists; let the user resolve
        # rather than risk clobbering hand-edited overrides.
        if f"[backends.{template}]" in existing:
            typer.echo(
                f"scr: [backends.{template}] already in {path}; skipping the template append. Edit it directly.",
                err=True,
            )
        else:
            block = render_backend_template(template)
            sep = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
            path.write_text(existing + sep + block, encoding="utf-8")

    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
    _sp.run([editor, str(path)], check=False)


def _edit_inline_prompt(path: Path) -> None:
    """Extract `[augment].extra_prompt` into a tempfile, open $EDITOR
    on it as markdown, then splice the result back into the config.

    Keeps the round-trip atomic — if the user clears the file we
    remove the assignment; if they cancel without changing anything,
    the existing prompt is left alone. The rest of the config file
    (other sections, comments) is preserved by editing-as-text rather
    than re-serialising through tomllib.
    """
    import subprocess as _sp
    import tempfile

    from ..config import ScrConfig, write_inline_extra_prompt

    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(_CONFIG_TEMPLATE, encoding="utf-8")

    # Read the current prompt via the standard config loader so the
    # tempfile starts with what `scr config show` would say. Empty
    # body when no prompt is configured at this scope yet.
    cfg = ScrConfig.load(user_path=path, repo_path=None)
    current = cfg.extra_review_prompt or ""

    # Initial contents: current prompt if set, else a placeholder
    # comment-block. We compare the post-edit content against this
    # initial text (not against ``current``) so the placeholder
    # isn't mistakenly written back as the new prompt when the editor
    # exits without changes.
    placeholder = (
        "# scr extra-review prompt — replace this comment block with\n"
        "# the prompt body. Saving an empty file removes\n"
        "# [augment].extra_prompt from the config.\n"
    )
    initial = (current + ("\n" if not current.endswith("\n") else "")) if current else placeholder

    # Tempfile gets a .md suffix so editor markdown plugins activate.
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".md",
        prefix="scr-extra-prompt-",
        delete=False,
        encoding="utf-8",
    ) as fh:
        fh.write(initial)
        tmp_path = Path(fh.name)

    try:
        editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
        _sp.run([editor, str(tmp_path)], check=False)
        edited_raw = tmp_path.read_text(encoding="utf-8")
        if edited_raw == initial:
            typer.echo(f"scr: no changes; {path} unchanged.")
            return
        edited = edited_raw.strip()
        write_inline_extra_prompt(path, edited)
        if edited:
            typer.echo(f"scr: updated [augment].extra_prompt in {path}.")
        else:
            typer.echo(f"scr: cleared [augment].extra_prompt in {path}.")
    finally:
        with contextlib.suppress(OSError):
            tmp_path.unlink()


_CONFIG_TEMPLATE = """\
# scr config — non-secret defaults. CLI flags and env vars override.
#
# Do NOT put API keys here. Config files leak too easily (accidental
# commits, dotfile repos, screen-shares). Use a `.env` or your shell.

# Default backend when --backend isn't passed. "auto" picks claude-api
# if ANTHROPIC_API_KEY is set, else claude-cli if `claude` is on PATH.
# Run `scr config show` for the full list of registered backends.
# backend = "claude-api"

# Override a builtin backend's default model, or define a new one.
# [backends.claude-api]
# model = "claude-sonnet-4-7"

# Global model fallback used when the selected backend has no model
# of its own and --model isn't passed.
# [model]
# default = "claude-opus-4-7"

# Environment variables to set if not already in the parent env.
# Useful for non-secrets like GCP project / location.
# [env]
# GOOGLE_CLOUD_PROJECT = "aasgard-dev"
# GOOGLE_CLOUD_LOCATION = "global"
"""
