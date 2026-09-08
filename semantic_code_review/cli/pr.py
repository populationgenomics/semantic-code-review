"""`scr pr` — review a GitHub PR in the browser; Submit posts from there.

This file is the argument-parsing shim. The orchestration lives in
:mod:`semantic_code_review.review.pr_flow` so it stays testable without
a Typer dependency. Two modes (ADR 0009): `scr pr <repo> [<number>]`
materialises the run and detaches the server, printing the run id;
`scr pr … --serve-run <slug>` is the detached server itself (hidden —
`run_pr_flow` spawns it).
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from .. import paths
from ..paths import default_runs_root
from ..review import servers
from ..review.config import ReviewConfig
from ..review.pr_flow import PrFlowOptions, run_pr_flow, serve_pr_run
from . import app
from ._shared import (
    configure_logging,
    get_config,
    resolve_explainer_prompt,
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
            "matching hunk's spans. Overrides [augment].extra_prompt."
        ),
    ),
    explainer_prompt: Path = typer.Option(
        None,
        "--explainer-prompt",
        help=(
            "Path to a markdown/text file of house style for the change "
            "explainer — how a document about a change should read in "
            "this repo. Appended to the explainer passes' guidance and "
            "nothing else; the per-hunk annotations are unaffected. "
            "Overrides [augment].explainer_prompt."
        ),
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    debug: bool = typer.Option(
        False,
        "--debug",
        envvar="SCR_DEBUG",
        help="Surface each CLI-backend subprocess spawn (raw argv + envelope) in the viewer's debug drawer.",
    ),
    serve_run: str = typer.Option(None, servers.SERVE_RUN_FLAG, hidden=True),
) -> None:
    """Review a GitHub PR in the browser; returns at once with the run id.

    Comments you Send go into your pending review on GitHub as you send
    them; Submit in the viewer publishes it. The review server detaches
    and keeps running until the tab has been gone for the idle timeout.
    """
    configure_logging(verbose)

    cfg = get_config()
    backend = cfg.resolve_backend(backend)
    model = cfg.resolve_model(backend=backend, cli_value=model)
    extra_review_prompt = resolve_extra_review_prompt(extra_prompt) if augment else None
    house_style = resolve_explainer_prompt(explainer_prompt) if augment else None
    # Resolve the backend up-front so a misconfiguration fails fast,
    # before we spend time on PR resolution and worktree fetch.
    client = select_client(backend, model=model) if augment else None

    review_cfg = ReviewConfig(
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
        explainer=cfg.explainer,
        explainer_prompt=house_style,
        client=client,
        debug=debug,
    )
    if serve_run is not None:
        raise typer.Exit(
            code=serve_pr_run(paths.RunDir(review_cfg.runs_root / serve_run), review_cfg, argv=sys.argv[1:])
        )

    opts = PrFlowOptions(repo=repo, number=number, config=review_cfg)
    raise typer.Exit(code=run_pr_flow(opts, argv=sys.argv[1:]))
