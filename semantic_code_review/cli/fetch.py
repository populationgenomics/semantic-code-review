"""`scr fetch` — materialise a GitHub PR into a run directory."""

from __future__ import annotations

from pathlib import Path

import typer

from ..fetch import GhFetchError, materialize_github_pr_run, preflight_gh
from ..paths import default_runs_root
from . import app
from ._shared import configure_logging


@app.command()
def fetch(
    pr_url: str = typer.Argument(..., help="https://github.com/owner/repo/pull/N"),
    runs_root: Path = typer.Option(
        None, help="Root directory for run artefacts (default: ~/.cache/scr/runs/<repo-fingerprint>/)."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Fetch PR metadata, diff, and base/head worktrees into a run directory."""
    configure_logging(verbose)

    runs_root = runs_root or default_runs_root()
    try:
        preflight_gh()
        run_dir = materialize_github_pr_run(pr_url, runs_root)
    except GhFetchError as e:
        typer.echo(f"scr: {e}", err=True)
        raise typer.Exit(code=2)
    typer.echo(f"run directory: {run_dir}")
