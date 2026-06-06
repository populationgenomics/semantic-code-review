"""`scr strip` — print a plain unified diff with annotations removed."""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from ..format.strip import strip_annotations
from . import app


@app.command()
def strip(
    augmented: Path = typer.Argument(..., help="Path to an augmented.diff file."),
) -> None:
    """Print a plain unified diff (annotations removed) to stdout."""
    text = augmented.read_text(encoding="utf-8")
    sys.stdout.write(strip_annotations(text))
