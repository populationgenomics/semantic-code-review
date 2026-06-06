"""`scr show` — print the augmented diff of a run directory to stdout."""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from . import app


@app.command()
def show(
    run_dir: Path = typer.Argument(...),
) -> None:
    """Print the augmented diff of a run directory to stdout."""
    path = run_dir / "augmented.diff"
    sys.stdout.write(path.read_text(encoding="utf-8"))
