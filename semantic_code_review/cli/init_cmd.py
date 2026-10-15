"""`scr init` — interactive first-run setup wizard.

Detects which backends are usable right now (mirroring the signals
`resolve_auto` / `resolve_api_key` use), lets the user pick a default
backend, guides credential setup, then picks a model — live-listing the
backend's models once the credential resolves. Writes the choices to the
user config: its top level, or with `--scope=repo` the
`[repos."owner/name"]` entry for the cwd checkout's GitHub repo.

Credential setup is a set of composable *sources* (see `credentials.py`);
each backend accepts a subset. A key can go into a gitignored `.env`
(0600), a fetch command (`api_key_command`, stored in config — a command
is not a secret), an env var the user sets, or the user config's `[env]`
table. All secret/config writes are 0600 (see `paths.write_private_file`).
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shlex
import shutil
import subprocess
import tomllib
from pathlib import Path

import typer

from .. import git_ops, paths
from ..config import BackendDef, BackendType, ScrConfig
from . import app, credentials, prompt
from ._shared import local_repo


@app.command("init")
def init(
    scope: str = typer.Option(
        "user",
        "--scope",
        help=(
            "Where to write the choices: 'user' (your defaults) or 'repo' (your settings for the "
            'cwd checkout\'s GitHub repo, as [repos."owner/name"]). Both are in ~/.config/scr/config.toml.'
        ),
    ),
) -> None:
    """Interactively configure scr: pick a backend, model, and credential source."""
    if scope not in ("user", "repo"):
        raise typer.BadParameter(f"--scope must be 'user' or 'repo', got {scope!r}")

    path = paths.default_config_path()
    reviewed = _scope_repo(path) if scope == "repo" else None
    cfg = ScrConfig.load(repo=reviewed)

    typer.echo("scr init — set up a default backend.\n")
    name = _choose_backend(cfg)
    bdef = cfg.backends[name]
    # Credential before model: a resolved key lets the model step list the
    # backend's actual models (and doubles as a live credential check).
    cred = _credential_step(name, bdef)
    model_override = _choose_model(name, bdef)

    keys = () if reviewed is None else ("repos", reviewed)
    warning = _write_config(
        path,
        keys=keys,
        backend=name,
        model_override=model_override,
        api_key_command=cred.api_key_command,
        config_env=cred.config_env,
    )
    typer.echo(f"\nscr: wrote {path}" + (f" [{_table(keys)}]" if keys else ""))
    if warning:
        typer.echo(f"scr: {warning}", err=True)

    _print_summary(name, bdef)


def _scope_repo(path: Path) -> str:
    """The cwd checkout's `owner/name`, spelt as an existing `[repos]`
    entry in `path` spells it (entries match case-insensitively).
    """
    reviewed, _ = local_repo(None)
    if reviewed is None:
        typer.echo("scr: --scope=repo needs a checkout whose origin is a GitHub repo", err=True)
        raise typer.Exit(code=2)
    if path.is_file():
        repos = tomllib.loads(path.read_text(encoding="utf-8")).get("repos", {})
        return next((k for k in repos if k.lower() == reviewed.lower()), reviewed)
    return reviewed


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


def _command_key_value(argv: tuple[str, ...]) -> str | None:
    """The key ``argv`` prints, or None. Non-raising: any failure (missing
    binary, non-zero exit, timeout, empty output) is None so detection
    never aborts the wizard.
    """
    try:
        proc = subprocess.run(list(argv), capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    out = (proc.stdout or "").strip()
    return out if (proc.returncode == 0 and out) else None


def _command_yields_key(argv: tuple[str, ...]) -> bool:
    return _command_key_value(argv) is not None


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
    """Arrow-key select (ready first) with detection marker + description.
    Default is the current config backend, else the first ready one.
    """
    rows = _ordered_backends(cfg)
    choices: list[tuple[str, str]] = []
    for name, bdef, status in rows:
        label = f"{status.marker} {name:<12} {status.detail}"
        if bdef.description:
            label += f"\n     {bdef.description}"
        choices.append((label, name))
    return prompt.select(
        "Default backend  (✓ ready · ○ local · · needs a credential)",
        choices,
        default=_default_backend(cfg, rows),
    )


def _default_backend(cfg: ScrConfig, rows: list[tuple[str, BackendDef, _Status]]) -> str:
    names = [r[0] for r in rows]
    if cfg.backend and cfg.backend in names:
        return cfg.backend
    for name, _bdef, status in rows:
        if status.state == "ready":
            return name
    return names[0]


def _choose_model(name: str, bdef: BackendDef) -> str | None:
    """Pick a model, live-listing the backend's models when the credential
    resolves. Returns an override only when the choice differs from the
    backend's builtin default (matching the default keeps it implicit).
    """
    default = bdef.default_model or ""
    key = os.environ.get(bdef.api_key_env) if bdef.api_key_env else None
    models = credentials.list_models(bdef, key)

    if models:
        other = "\x00other"  # sentinel value distinct from any real model id
        choices = [(m, m) for m in models]
        choices.append(("other — type a model id", other))
        picked = prompt.select(f"Model for {name}", choices, default=default if default in models else models[0])
        if picked == other:
            picked = prompt.text(f"Model id for {name}", default=default)
    else:
        label = f"Model for {name}" + ("" if default else " (required — this backend pins no default)")
        picked = prompt.text(label, default=default)

    picked = picked.strip()
    if not picked or picked == (bdef.default_model or ""):
        return None
    return picked


@dataclasses.dataclass
class _Credential:
    """What the credential step wants persisted. `api_key_command` goes
    under `[backends.<name>]`; `config_env` is a `(VAR, value)` written to
    the `[env]` table (the config-key source). Both may be None.
    """

    api_key_command: tuple[str, ...] | None = None
    config_env: tuple[str, str] | None = None


def _credential_step(name: str, bdef: BackendDef) -> _Credential:
    """Guide credential setup by offering the backend's allowed sources.

    Whichever source is chosen, its key (when we can obtain it) is applied
    to this process's environment so the following model step can list the
    backend's models live.
    """
    status = _detect(name, bdef)
    if status.state == "ready":
        typer.echo(f"\nscr: credential ready — {status.detail}.")
        return _Credential()

    ids = credentials.allowed_source_ids(name, bdef)
    if status.state == "local" or ids == ["none"]:
        typer.echo(f"\nscr: {status.detail}.")
        return _Credential()

    var = bdef.api_key_env or "API_KEY"
    choices = [(credentials.SOURCES[i].label.format(var=var), i) for i in ids]
    source = prompt.select(f"\n{name} needs a credential (${var}). How will you provide it?", choices, default=ids[0])

    if source == "command":
        return _Credential(api_key_command=_configure_api_key_command(name, var))
    if source == "dotenv":
        value = _acquire_key(var)
        if value:
            _write_env_key(var, value)
        return _Credential()
    if source == "config":
        value = _acquire_key(var)
        return _Credential(config_env=(var, value)) if value else _Credential()
    if source == "vertex":
        typer.echo(
            "\nUsing Vertex AI: set GOOGLE_CLOUD_PROJECT and authenticate with\n"
            "    gcloud auth application-default login"
        )
        return _Credential()
    # "env"
    typer.echo(f"\nExport it in your shell, or add it to a .env in the repo you review from:\n    {var}=<your-key>")
    return _Credential()


def _acquire_key(var: str) -> str | None:
    """Prompt for a key (hidden), apply it to this process's env so the
    model step can list live, and return it (None when left blank).
    """
    value = prompt.password(f"Paste {var}")
    if not value:
        typer.echo("scr: no value entered; skipping.", err=True)
        return None
    os.environ[var] = value
    return value


def _configure_api_key_command(name: str, var: str) -> tuple[str, ...] | None:
    """Prompt for a key-fetch command, validate it, return its argv."""
    raw = prompt.text("Command to fetch the key (e.g. `gh auth token`)")
    try:
        argv = tuple(shlex.split(raw))
    except ValueError as e:
        typer.echo(f"scr: unbalanced quotes in command: {e}", err=True)
        raise typer.Exit(code=2) from None
    if not argv:
        typer.echo("scr: empty command", err=True)
        raise typer.Exit(code=2)
    value = _command_key_value(argv)
    if value:
        os.environ[var] = value  # so the model step can list live
        typer.echo(f"scr: command produced a key — storing api_key_command for {name}.")
    else:
        typer.echo(
            f"scr: warning: `{shlex.join(argv)}` did not produce a key just now; storing it anyway "
            f"(it may need auth first, or ${var} set as a fallback).",
            err=True,
        )
    return argv


def _write_env_key(var: str, value: str) -> None:
    """Append ``var=value`` to ./.env (0600) and ensure .env is gitignored."""
    env_path = Path(".env")
    existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    sep = "" if (not existing or existing.endswith("\n")) else "\n"
    paths.write_private_file(env_path, f"{existing}{sep}{var}={value}\n")
    _ensure_gitignored(".env")
    typer.echo(f"scr: wrote {var} to {env_path} (mode 0600; keep it out of version control).")


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
# scr config — CLI flags and env vars override these. This file is 0600, so
# a key may sit in [env]; a repo's committed .scr/config.toml cannot hold one.
"""

# Any table header, `[x]` or `[[x]]`.
_HEADER = re.compile(r"^[ \t]*\[.*$", re.MULTILINE)


def _write_config(
    path: Path,
    *,
    keys: tuple[str, ...] = (),
    backend: str,
    model_override: str | None,
    api_key_command: tuple[str, ...] | None,
    config_env: tuple[str, str] | None = None,
) -> str | None:
    """Write the chosen backend/model into the table at `keys` of ``path``
    (the top level when empty), preserving the rest of the file. Returns a
    warning string when a `backends.<name>` table already exists there
    (left untouched) so the caller can surface it.

    The file is edited as text, so the result is parsed before it is
    written: an edit that does not land where intended (say, the table is
    spelt with other quoting) exits rather than writing a different
    config.
    """
    paths.ensure_private_dir(path.parent)
    text = path.read_text(encoding="utf-8") if path.exists() else _FRESH_HEADER
    table = _table(keys)
    text = _set_key(text, table, "backend", json.dumps(backend), uncomment=not keys)
    if config_env is not None:
        text = _set_key(text, _table((*keys, "env")), config_env[0], json.dumps(config_env[1]))

    warning: str | None = None
    block = _table((*keys, "backends", backend))
    if model_override or api_key_command:
        if _table_span(text, block) is not None:
            warning = (
                f"[{block}] already exists in the config; left as-is. "
                "Set model/api_key_command there with `scr config edit`."
            )
        else:
            text = text.rstrip("\n") + "\n\n" + _render_backends_block(block, model_override, api_key_command)

    _check_written(path, text, keys, backend=backend, config_env=config_env)
    paths.write_private_file(path, text)
    return warning


def _table(keys: tuple[str, ...]) -> str:
    """The TOML table name for a key path; keys that are not bare are quoted."""
    return ".".join(k if re.fullmatch(r"[A-Za-z0-9_-]+", k) else json.dumps(k) for k in keys)


def _table_span(text: str, table: str) -> tuple[int, int] | None:
    """The body of `[table]` as `(start, end)` offsets, or None when the
    file has no such header. `""` is the top level: everything before the
    first header.
    """
    if not table:
        first = _HEADER.search(text)
        return 0, first.start() if first else len(text)
    header = re.search(rf"^[ \t]*\[{re.escape(table)}\][ \t]*(?:#.*)?(?:\n|$)", text, re.MULTILINE)
    if header is None:
        return None
    following = _HEADER.search(text, header.end())
    return header.end(), following.start() if following else len(text)


def _set_key(text: str, table: str, key: str, value: str, *, uncomment: bool = False) -> str:
    """Set `key = value` in `[table]`: replace an existing assignment
    (with `uncomment`, a commented-out one too), else add one at the end
    of the table's body, creating the table at the end of the file when
    it has none.
    """
    line = f"{key} = {value}"
    span = _table_span(text, table)
    if span is None:
        sep = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
        return f"{text}{sep}[{table}]\n{line}\n"
    start, end = span
    body = text[start:end]
    comment = "#?[ \\t]*" if uncomment else ""
    assign = re.compile(rf"^[ \t]*{comment}{re.escape(key)}[ \t]*=.*$", re.MULTILINE)
    if assign.search(body):
        body = assign.sub(lambda _m: line, body, count=1)
    else:
        kept = body.rstrip("\n")
        body = f"{kept}{chr(10) if kept else ''}{line}\n" + ("\n" if end < len(text) else "")
    return text[:start] + body + text[end:]


def _check_written(
    path: Path, text: str, keys: tuple[str, ...], *, backend: str, config_env: tuple[str, str] | None
) -> None:
    """Exit unless `text` parses with the choices at `keys`."""
    try:
        node = tomllib.loads(text)
        for k in keys:
            node = node[k]
        landed = node["backend"] == backend and (config_env is None or node["env"][config_env[0]] == config_env[1])
    except (tomllib.TOMLDecodeError, KeyError, TypeError) as e:
        landed, why = False, f"{type(e).__name__}: {e}"
    else:
        why = "the values did not land where intended"
    if not landed:
        where = f"[{_table(keys)}]" if keys else "its top level"
        typer.echo(f"scr: could not edit {path} safely ({why}); set {where} by hand with `scr config edit`", err=True)
        raise typer.Exit(code=2)


def _render_backends_block(
    table: str,
    model_override: str | None,
    api_key_command: tuple[str, ...] | None,
) -> str:
    out = [f"[{table}]"]
    if model_override:
        out.append(f"model = {json.dumps(model_override)}")
    if api_key_command:
        out.append(f"api_key_command = {json.dumps(shlex.join(api_key_command))}")
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
