"""`scr review` — review a local git diff in the browser."""

from __future__ import annotations

from pathlib import Path

import typer

from ..fetch import EmptyDiff, LocalDiffError
from ..paths import default_runs_root
from ..review.runner import ReviewOptions, run_review
from . import app
from ._shared import (
    configure_logging,
    get_config,
    resolve_extra_review_prompt,
    select_client,
)


@app.command()
def review(
    spec: str = typer.Argument(
        ...,
        help=(
            "Git ref (e.g. 'main') or range ('main..HEAD', 'HEAD~3...HEAD'). "
            "Single ref diffs against current working state; range is "
            "committed-only."
        ),
    ),
    spec_md: Path = typer.Option(None, "--spec", help="Markdown file with the spec/intent for this change."),
    runs_root: Path = typer.Option(
        None, help="Root directory for run artefacts (default: ~/.cache/scr/runs/<repo-fingerprint>/)."
    ),
    repo_root: Path = typer.Option(None, help="Repo root (defaults to walking up from cwd)."),
    no_staged: bool = typer.Option(False, help="With a single ref: exclude staged changes."),
    no_unstaged: bool = typer.Option(False, help="With a single ref: exclude unstaged changes."),
    augment: bool = typer.Option(True, help="Run the LLM augmentation pass before rendering."),
    model: str = typer.Option(None),
    concurrency: int = typer.Option(8),
    no_cache: bool = typer.Option(False),
    cache_dir: Path = typer.Option(None),
    no_open: bool = typer.Option(False, help="Skip opening the browser (for CI / SSH)."),
    port: int = typer.Option(0, help="Server port (0 = kernel-assigned)."),
    timeout: int = typer.Option(3600, help="Server idle timeout in seconds."),
    backend: str = typer.Option(
        None, help="LLM backend (default from config or 'auto'); see `scr config show` for registered names."
    ),
    extra_prompt: Path = typer.Option(
        None,
        "--extra-prompt",
        help=(
            "Path to a markdown/text file with an extra review prompt. "
            "Runs as a second per-hunk LLM call alongside the main "
            "comprehension pass; produces line-anchored notes that "
            "the reviewer can promote to comments. Overrides "
            "[augment].extra_prompt in the config."
        ),
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Review a local git diff; round-trip reviewer comments to stdout."""
    configure_logging(verbose)

    cfg = get_config()
    backend = cfg.resolve_backend(backend)
    model = cfg.resolve_model(backend=backend, cli_value=model)
    runs_root = runs_root or default_runs_root()
    extra_review_prompt = resolve_extra_review_prompt(extra_prompt) if augment else None
    # Resolve the backend up-front so a misconfiguration fails fast, before
    # we spend time building the diff / worktrees.
    client = select_client(backend, model=model) if augment else None

    opts = ReviewOptions(
        spec=spec,
        spec_markdown=spec_md,
        runs_root=runs_root,
        repo_root=repo_root,
        no_staged=no_staged,
        no_unstaged=no_unstaged,
        augment=augment,
        model=model,
        concurrency=concurrency,
        no_cache=no_cache,
        cache_dir=cache_dir,
        open_browser=not no_open,
        port=port,
        timeout=timeout,
        client=client,
        extra_review_prompt=extra_review_prompt,
        show_progress=not verbose,
    )
    try:
        code = run_review(opts)
    except EmptyDiff as e:
        # Empty-diff isn't an error — exit cleanly so calling scripts
        # ("review every commit on this branch") don't have to special-
        # case "this commit changed nothing".
        typer.echo(f"scr: {e}", err=True)
        raise typer.Exit(code=0) from None
    except LocalDiffError as e:
        typer.echo(f"scr: {e}", err=True)
        raise typer.Exit(code=2) from None
    raise typer.Exit(code=code)
