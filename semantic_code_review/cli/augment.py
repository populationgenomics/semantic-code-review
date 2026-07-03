"""`scr augment` — run the LLM augmentation pipeline on a fetched run dir."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from ..cache.store import CacheStore
from . import app
from ._shared import (
    configure_logging,
    get_config,
    resolve_extra_review_prompt,
    select_client,
)


@app.command()
def augment(
    run_dir: Path = typer.Argument(..., help="Path to a run directory from 'scr fetch'."),
    model: str = typer.Option(None, help="LLM model id (default from config or 'claude-opus-4-7')."),
    concurrency: int = typer.Option(8, help="Per-hunk call concurrency."),
    max_hunks: int = typer.Option(None, help="Cap hunk calls (smoke tests)."),
    only_files: list[str] = typer.Option(None, help="Restrict to these post-image paths (repeatable)."),
    skip_overview: bool = typer.Option(False, help="Skip the PR-level overview pass."),
    skip_context: bool = typer.Option(False, help="Disable repo tools (no cross-file context)."),
    no_cache: bool = typer.Option(False, help="Disable disk cache of LLM calls."),
    cache_dir: Path = typer.Option(None, help="Cache root (default ~/.cache/scr/v1)."),
    backend: str = typer.Option(
        None, help="LLM backend (default from config or 'auto'); see `scr config show` for registered names."
    ),
    extra_prompt: Path = typer.Option(
        None,
        "--extra-prompt",
        help=(
            "Path to a markdown/text file with an extra review prompt. "
            "Runs as a second per-hunk LLM call alongside the main "
            "comprehension pass; output line-notes merge into the "
            "augmented diff. Overrides [augment].extra_prompt."
        ),
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Augment a fetched run directory with LLM annotations."""
    configure_logging(verbose)
    # Import inside: anthropic SDK lazy-loaded so strip/lint work without it.
    from ..augment.pipeline import augment_run_dir
    from ..augment.prompts import PROMPT_VERSION

    cfg = get_config()
    backend = cfg.resolve_backend(backend)
    model = cfg.resolve_model(backend=backend, cli_value=model)
    cache = None if no_cache else CacheStore(root=cache_dir, prompt_version=PROMPT_VERSION)
    client = select_client(backend, model=model)
    extra_review_prompt = resolve_extra_review_prompt(extra_prompt)

    path = asyncio.run(
        augment_run_dir(
            run_dir,
            model=model,
            concurrency=concurrency,
            max_hunks=max_hunks,
            only_files=list(only_files) if only_files else None,
            skip_overview=skip_overview,
            skip_context=skip_context,
            cache=cache,
            client=client,
            extra_review_prompt=extra_review_prompt,
            show_progress=not verbose,
        )
    )
    typer.echo(f"wrote {path}")
