"""`scr comment` subapp — Claude's replies into a live review (ADR 0009).

`scr comment reply <run_id> <comment_id> [BODY]` adds a `claude`-authored
entry under a reviewer comment; `scr comment resolve` / `unresolve` set
the thread's resolution. All three reach the detached review server
through the run's `server.json`; with no live server they exit 2 — the
review has ended and there is no thread to write into.
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from .. import paths
from ..review import stream
from . import app

comment_app = typer.Typer(help="Reply into a live review's comment threads, or resolve them.")
app.add_typer(comment_app, name="comment")

_RUN_ID = typer.Argument(..., help="The run id `scr review` printed (`run_id: <slug>`).")
_COMMENT_ID = typer.Argument(..., help="The comment id as the batch names it (`## <id> — …`).")
_RUNS_ROOT = typer.Option(
    None, help="Root directory for run artefacts (default: ~/.cache/scr/runs/<repo-fingerprint>/)."
)


def _run_dir(runs_root: Path | None, run_id: str) -> paths.RunDir:
    return paths.RunDir((runs_root or paths.default_runs_root()) / run_id)


def _exit_on(e: Exception) -> typer.Exit:
    typer.echo(f"scr comment: {e}", err=True)
    return typer.Exit(code=2)


@comment_app.command("reply")
def comment_reply(
    run_id: str = _RUN_ID,
    comment_id: str = _COMMENT_ID,
    body: str = typer.Argument(None, help="The reply. Omit to read it from stdin."),
    runs_root: Path = _RUNS_ROOT,
) -> None:
    """Add a reply from Claude under a reviewer's comment; the viewer shows it live."""
    text = sys.stdin.read() if body is None else body
    if not text.strip():
        typer.echo("scr comment: the reply is empty", err=True)
        raise typer.Exit(code=2)
    try:
        saved = stream.reply(_run_dir(runs_root, run_id), comment_id, text)
    except (stream.NoServer, stream.ServerRefused) as e:
        raise _exit_on(e) from None
    typer.echo(f"replied as {saved['id']} under {comment_id}")


@comment_app.command("resolve")
def comment_resolve(
    run_id: str = _RUN_ID,
    comment_id: str = _COMMENT_ID,
    runs_root: Path = _RUNS_ROOT,
) -> None:
    """Mark the thread holding a comment resolved."""
    _set_resolved(runs_root, run_id, comment_id, resolved=True)


@comment_app.command("unresolve")
def comment_unresolve(
    run_id: str = _RUN_ID,
    comment_id: str = _COMMENT_ID,
    runs_root: Path = _RUNS_ROOT,
) -> None:
    """Reopen the thread holding a comment."""
    _set_resolved(runs_root, run_id, comment_id, resolved=False)


def _set_resolved(runs_root: Path | None, run_id: str, comment_id: str, *, resolved: bool) -> None:
    try:
        result = stream.set_resolved(_run_dir(runs_root, run_id), comment_id, resolved)
    except (stream.NoServer, stream.ServerRefused) as e:
        raise _exit_on(e) from None
    word = "resolved" if resolved else "reopened"
    typer.echo(f"{word} the thread of {comment_id} ({len(result['comment_ids'])} comment(s) changed)")
