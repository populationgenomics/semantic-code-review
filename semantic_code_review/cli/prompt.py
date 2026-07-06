"""Interactive prompt seam for the wizard.

One thin layer over `questionary` (arrow-key select / text / password /
confirm) with a plain-text fallback when stdin isn't a TTY — piped input,
CI, dumb terminals — where a full-screen prompt would hang or crash.

It is also the test seam: every `scr init` interaction goes through these
four functions, so tests inject scripted answers by monkeypatching them
instead of driving `prompt_toolkit` over a pseudo-terminal.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

import questionary
import typer


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _abort_if_none(value: object) -> object:
    # questionary returns None when the user hits Ctrl-C / EOF.
    if value is None:
        raise typer.Abort
    return value


def select(message: str, choices: Sequence[tuple[str, str]], *, default: str | None = None) -> str:
    """Pick one value. `choices` is a sequence of (label, value) pairs;
    returns the chosen value. Falls back to a numbered menu off a TTY.
    """
    values = [v for _label, v in choices]
    if _interactive():
        opts = [questionary.Choice(title=label, value=value) for label, value in choices]
        default_opt = next((o for o in opts if o.value == default), None)
        return str(_abort_if_none(questionary.select(message, choices=opts, default=default_opt).ask()))
    # Non-TTY: numbered list, echoed to stderr so it never pollutes stdout.
    typer.echo(f"{message}", err=True)
    for i, (label, _v) in enumerate(choices, start=1):
        typer.echo(f"  [{i}] {label}", err=True)
    default_idx = (values.index(default) + 1) if default in values else 1
    raw = typer.prompt("Choice", default=str(default_idx))
    try:
        idx = int(raw.strip())
    except ValueError:
        raise typer.BadParameter(f"expected 1–{len(values)}, got {raw!r}") from None
    if not 1 <= idx <= len(values):
        raise typer.BadParameter(f"choice {idx} out of range 1–{len(values)}")
    return values[idx - 1]


def text(message: str, *, default: str = "") -> str:
    if _interactive():
        return str(_abort_if_none(questionary.text(message, default=default).ask())).strip()
    return typer.prompt(message, default=default, show_default=bool(default)).strip()


def password(message: str) -> str:
    if _interactive():
        return str(_abort_if_none(questionary.password(message).ask())).strip()
    return typer.prompt(message, hide_input=True).strip()


def confirm(message: str, *, default: bool = True) -> bool:
    if _interactive():
        return bool(_abort_if_none(questionary.confirm(message, default=default).ask()))
    return typer.confirm(message, default=default)
