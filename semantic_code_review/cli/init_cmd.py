"""`scr init` — interactive first-run setup wizard.

Detects which backends are usable right now (mirroring the signals
`resolve_auto` / `resolve_api_key` use), lets the user pick a default
backend + model, writes the non-secret choices to the user (or per-repo)
config, and guides credential setup.

Credentials never go into the TOML config. When the chosen backend has
no detected credential the wizard offers alternatives: set the env var
yourself (instruct-only), configure a command that fetches the key
(`api_key_command` — a command is not a secret, so it *is* written to
config), or drop the key into a gitignored `.env`.
"""

from __future__ import annotations

import dataclasses
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import typer

from .. import git_ops
from ..config import BackendDef, BackendType, ScrConfig
from ..paths import default_config_path
from . import app


@app.command("init")
def init(
    scope: str = typer.Option(
        "user",
        "--scope",
        help="Where to write the config: 'user' (~/.config/scr/config.toml) or 'repo' (<repo>/.scr/config.toml).",
    ),
) -> None:
    """Interactively configure scr: pick a backend, model, and credential source."""
    if scope not in ("user", "repo"):
        raise typer.BadParameter(f"--scope must be 'user' or 'repo', got {scope!r}")

    cfg = ScrConfig.load()
    path = _resolve_config_path(scope)

    typer.echo("scr init — set up a default backend.\n")
    name = _choose_backend(cfg)
    bdef = cfg.backends[name]
    model_override = _choose_model(name, bdef)
    api_key_command = _credential_step(name, bdef)

    warning = _write_config(
        path,
        backend=name,
        model_override=model_override,
        api_key_command=api_key_command,
    )
    typer.echo(f"\nscr: wrote {path}")
    if warning:
        typer.echo(f"scr: {warning}", err=True)

    _print_summary(name, bdef)


# --- detection -------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _Status:
    """A backend's readiness for the current environment."""

    state: str  # "ready" | "local" | "setup"
    detail: str

    @property
    def marker(self) -> str:
        return {"ready": "✓", "local": "○", "setup": "·"}[self.state]


def _env_present(var: str | None) -> bool:
    return bool(var) and bool(os.environ.get(var or "", "").strip())


def _command_yields_key(argv: tuple[str, ...]) -> bool:
    """True if ``argv`` runs, exits 0, and prints non-empty output.

    Non-raising: any failure (missing binary, non-zero exit, timeout)
    is a plain False so detection never aborts the wizard.
    """
    try:
        proc = subprocess.run(list(argv), capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and bool((proc.stdout or "").strip())


def _detect(name: str, bdef: BackendDef) -> _Status:
    """Report whether ``bdef`` can satisfy a run right now, and how."""
    if bdef.type is BackendType.CLAUDE_CLI:
        if shutil.which("claude"):
            return _Status("ready", "`claude` on PATH (uses your Claude Code subscription)")
        return _Status("setup", "install the `claude` CLI, then run `claude` once to log in")

    # Gemini reaches Vertex AI via Application Default Credentials when a
    # project is set, even without an API key.
    if name == "gemini-api" and os.environ.get("GOOGLE_CLOUD_PROJECT"):
        return _Status("ready", "GOOGLE_CLOUD_PROJECT set (Vertex AI via ADC)")

    if _env_present(bdef.api_key_env):
        return _Status("ready", f"${bdef.api_key_env} set")
    if bdef.api_key_command and _command_yields_key(bdef.api_key_command):
        return _Status("ready", f"`{shlex.join(bdef.api_key_command)}` yields a key")
    if not bdef.api_key_env and not bdef.api_key_command:
        return _Status("local", "local endpoint — no key, but the server must be running with a model pulled")
    return _Status("setup", f"set ${bdef.api_key_env}" if bdef.api_key_env else "credential required")


def _ordered_backends(cfg: ScrConfig) -> list[tuple[str, BackendDef, _Status]]:
    """All backends with detection, ready ones first then by name."""
    rows = [(name, bdef, _detect(name, bdef)) for name, bdef in cfg.backends.items()]
    rank = {"ready": 0, "local": 1, "setup": 2}
    rows.sort(key=lambda r: (rank[r[2].state], r[0]))
    return rows


# --- prompts ---------------------------------------------------------------


def _choose_backend(cfg: ScrConfig) -> str:
    """Numbered menu (ready first) with detection + description. Default is
    the current config backend, else the first ready one, else the first row.
    """
    rows = _ordered_backends(cfg)
    typer.echo("Backends (✓ ready · ○ local · · needs a credential):\n")
    for i, (name, bdef, status) in enumerate(rows, start=1):
        typer.echo(f"  [{i}] {status.marker} {name:<12} {status.detail}")
        if bdef.description:
            typer.echo(f"         {bdef.description}")
    default_idx = _default_backend_index(cfg, rows)
    choice = typer.prompt("\nDefault backend", default=str(default_idx))
    idx = _parse_choice(choice, len(rows))
    return rows[idx - 1][0]


def _default_backend_index(cfg: ScrConfig, rows: list[tuple[str, BackendDef, _Status]]) -> int:
    names = [r[0] for r in rows]
    if cfg.backend and cfg.backend in names:
        return names.index(cfg.backend) + 1
    for i, (_name, _bdef, status) in enumerate(rows, start=1):
        if status.state == "ready":
            return i
    return 1


def _parse_choice(raw: str, count: int) -> int:
    try:
        idx = int(raw.strip())
    except ValueError:
        raise typer.BadParameter(f"expected a number 1–{count}, got {raw!r}") from None
    if not 1 <= idx <= count:
        raise typer.BadParameter(f"choice {idx} out of range 1–{count}")
    return idx


def _choose_model(name: str, bdef: BackendDef) -> str | None:
    """Prompt for a model. Returns an override only when the user picks
    something other than the backend's builtin default (blank keeps it).
    """
    default = bdef.default_model or ""
    prompt = f"Model for {name}" + (f" [{default}]" if default else " (required — this backend pins no default)")
    entered = typer.prompt(prompt, default=default, show_default=False).strip()
    if not entered:
        return None
    return entered if entered != (bdef.default_model or None) else None


def _credential_step(name: str, bdef: BackendDef) -> tuple[str, ...] | None:
    """Guide credential setup for the chosen backend.

    Returns an `api_key_command` tuple to persist in `[backends.<name>]`
    when the user opts for the fetch-command route, else None.
    """
    status = _detect(name, bdef)
    if status.state == "ready":
        typer.echo(f"\nscr: credential ready — {status.detail}.")
        return None
    if status.state == "local":
        typer.echo(f"\nscr: {status.detail}.")
        return None
    if bdef.type is BackendType.CLAUDE_CLI:
        typer.echo(f"\nscr: {status.detail}.")
        return None

    var = bdef.api_key_env or "API_KEY"
    typer.echo(f"\n{name} needs a credential (${var}). Choose how to provide it:")
    typer.echo("  [1] I'll set the env var myself (in .env or my shell)")
    typer.echo("  [2] Run a command to fetch it (stored in config; the key is not)")
    typer.echo("  [3] Paste it now into a gitignored .env")
    choice = typer.prompt("Credential source", default="1").strip()

    if choice == "2":
        return _configure_api_key_command(name, var)
    if choice == "3":
        _write_env_key(var)
        return None
    typer.echo(f"\nAdd this to a .env in the repo you review from, or export it in your shell:\n    {var}=<your-key>")
    return None


def _configure_api_key_command(name: str, var: str) -> tuple[str, ...] | None:
    """Prompt for a key-fetch command, validate it, return its argv."""
    raw = typer.prompt("Command to fetch the key (e.g. `gh auth token`)").strip()
    try:
        argv = tuple(shlex.split(raw))
    except ValueError as e:
        typer.echo(f"scr: unbalanced quotes in command: {e}", err=True)
        raise typer.Exit(code=2) from None
    if not argv:
        typer.echo("scr: empty command", err=True)
        raise typer.Exit(code=2)
    if _command_yields_key(argv):
        typer.echo(f"scr: command produced a key — storing api_key_command for {name}.")
    else:
        typer.echo(
            f"scr: warning: `{shlex.join(argv)}` did not produce a key just now; storing it anyway "
            f"(it may need auth first, or ${var} set as a fallback).",
            err=True,
        )
    return argv


def _write_env_key(var: str) -> None:
    """Append ``var`` to ./.env (hidden prompt) and ensure .env is gitignored."""
    value = typer.prompt(f"{var}", hide_input=True).strip()
    if not value:
        typer.echo("scr: no value entered; skipping .env write.", err=True)
        return
    env_path = Path(".env")
    existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    sep = "" if (not existing or existing.endswith("\n")) else "\n"
    env_path.write_text(f"{existing}{sep}{var}={value}\n", encoding="utf-8")
    _ensure_gitignored(".env")
    typer.echo(f"scr: wrote {var} to {env_path} (keep it out of version control).")


def _ensure_gitignored(entry: str) -> None:
    """Add ``entry`` to the repo's root .gitignore if not already covered."""
    try:
        root = Path(git_ops.git(None, "rev-parse", "--show-toplevel").strip())
    except git_ops.GitError:
        return
    gi = root / ".gitignore"
    lines = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
    if entry in {line.strip() for line in lines}:
        return
    sep = "" if (not gi.exists() or gi.read_text(encoding="utf-8").endswith("\n")) else "\n"
    with gi.open("a", encoding="utf-8") as fh:
        fh.write(f"{sep}{entry}\n")
    typer.echo(f"scr: added {entry} to {gi}.")


# --- config writing --------------------------------------------------------

_FRESH_HEADER = """\
# scr config — non-secret defaults. CLI flags and env vars override.
# Do NOT put API keys here; use a .env or your shell (or api_key_command).
"""


def _resolve_config_path(scope: str) -> Path:
    if scope == "user":
        return default_config_path()
    try:
        root = git_ops.git(None, "rev-parse", "--show-toplevel").strip()
    except git_ops.GitError as e:
        typer.echo(f"scr: --scope=repo needs a git repo: {e}", err=True)
        raise typer.Exit(code=2) from None
    return Path(root) / ".scr" / "config.toml"


def _write_config(
    path: Path,
    *,
    backend: str,
    model_override: str | None,
    api_key_command: tuple[str, ...] | None,
) -> str | None:
    """Write the chosen backend/model to ``path``, preserving any existing
    file. Returns a warning string when a `[backends.<name>]` section
    already exists (left untouched) so the caller can surface it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else _FRESH_HEADER
    text = _set_backend_line(text, backend)

    warning: str | None = None
    if model_override or api_key_command:
        if re.search(rf"^\[backends\.{re.escape(backend)}\]", text, re.MULTILINE):
            warning = (
                f"[backends.{backend}] already exists in the config; left as-is. "
                f"Set model/api_key_command there with `scr config edit`."
            )
        else:
            text = text.rstrip("\n") + "\n\n" + _render_backends_block(backend, model_override, api_key_command)

    path.write_text(text, encoding="utf-8")
    return warning


def _set_backend_line(text: str, backend: str) -> str:
    """Set the top-level `backend = "..."`, replacing an existing (or
    commented-template) assignment, else inserting one near the top.
    """
    line = f'backend = "{backend}"'
    pattern = re.compile(r"^[ \t]*#?[ \t]*backend[ \t]*=.*$", re.MULTILINE)
    if pattern.search(text):
        return pattern.sub(line, text, count=1)
    sep = "" if text.endswith("\n") or not text else "\n"
    return f"{text}{sep}{line}\n"


def _render_backends_block(
    backend: str,
    model_override: str | None,
    api_key_command: tuple[str, ...] | None,
) -> str:
    out = [f"[backends.{backend}]"]
    if model_override:
        out.append(f'model = "{model_override}"')
    if api_key_command:
        out.append(f'api_key_command = "{shlex.join(api_key_command)}"')
    return "\n".join(out) + "\n"


# --- summary ---------------------------------------------------------------


def _print_summary(name: str, bdef: BackendDef) -> None:
    status = _detect(name, bdef)
    typer.echo(f"\nDefault backend: {name}  ({status.marker} {status.detail})")
    if status.state == "ready":
        typer.echo("You're set. Try:  scr review HEAD~1")
    else:
        typer.echo("Once the credential above is in place, try:  scr review HEAD~1")
    typer.echo("Inspect anytime with:  scr config show")
