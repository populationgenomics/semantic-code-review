"""`scr runs` subapp — inspect the per-repo run-artefact directory."""

from __future__ import annotations

import typer

from ..paths import default_runs_root
from . import app

runs_app = typer.Typer(help="Inspect or manage scr's per-repo run-artefact directory.")
app.add_typer(runs_app, name="runs")


@runs_app.command("path")
def runs_path() -> None:
    """Print the runs root resolved for the current cwd."""
    typer.echo(str(default_runs_root()))
