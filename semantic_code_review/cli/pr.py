"""`scr pr` — review a GitHub PR; post the reviewer's comments back.

This file is the argument-parsing shim. The orchestration lives in
:mod:`semantic_code_review.review.pr_flow` so it stays testable without
a Typer dependency.
"""

from __future__ import annotations

from pathlib import Path

import typer

from ..paths import default_runs_root
from ..review.pr_flow import PrFlowOptions, run_pr_flow
from . import app
from ._shared import (
    configure_logging,
    get_config,
    resolve_extra_review_prompt,
    select_client,
)


@app.command()
def pr(
    repo: str = typer.Argument(..., help="GitHub repo as `owner/name`."),
    number: int = typer.Argument(
        None,
        help=(
            "PR number. Omit to enumerate open PRs requesting your review; "
            "if exactly one matches it's used, otherwise a picker prompts."
        ),
    ),
    runs_root: Path = typer.Option(
        None, help="Root directory for run artefacts (default: ~/.cache/scr/runs/<repo-fingerprint>/)."
    ),
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
            "Runs as a single PR-level LLM call alongside the main "
            "comprehension pass; line-anchored notes merge into the "
            "matching hunk's line_notes. Overrides [augment].extra_prompt."
        ),
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        help=(
            "Skip the in-browser confirmation modal — post every local "
            "comment as soon as the reviewer clicks Done. By default Done "
            "opens a modal listing the comments-to-post with per-row "
            "deselect/delete so the reviewer can prune before sending."
        ),
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Review a GitHub PR; round-trip reviewer comments back as a single review."""
    configure_logging(verbose)

    cfg = get_config()
    backend = cfg.resolve_backend(backend)
    model = cfg.resolve_model(backend=backend, cli_value=model)
    extra_review_prompt = resolve_extra_review_prompt(extra_prompt) if augment else None
    # Resolve the backend up-front so a misconfiguration fails fast,
    # before we spend time on PR resolution and worktree fetch.
    client = select_client(backend, model=model) if augment else None

    opts = PrFlowOptions(
        repo=repo,
        number=number,
        runs_root=runs_root or default_runs_root(),
        augment=augment,
        model=model,
        concurrency=concurrency,
        no_cache=no_cache,
        cache_dir=cache_dir,
        open_browser=not no_open,
        port=port,
        timeout=timeout,
        extra_review_prompt=extra_review_prompt,
        skip_globs=cfg.skip_globs,
        client=client,
        yes=yes,
    )
    raise typer.Exit(code=run_pr_flow(opts))
