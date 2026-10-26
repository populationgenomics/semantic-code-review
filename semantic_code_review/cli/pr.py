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
from ..review import runner, servers
from ..review.config import ReviewConfig
from ..review.pr_flow import PrFlowOptions, run_pr_flow, serve_pr_run
from . import app
from ._shared import (
    configure_logging,
    load_config,
    require_viewer_bundle,
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
    foreground: bool = typer.Option(
        False,
        runner.FOREGROUND_FLAG,
        help=(
            "Serve in this process instead of detaching: log to stderr, block until "
            "Ctrl-C or the idle timeout. `viewer:` and `run_id:` are still printed first."
        ),
    ),
    serve_run: str = typer.Option(None, servers.SERVE_RUN_FLAG, hidden=True),
) -> None:
    """Review a GitHub PR in the browser; returns at once with the run id.

    Comments you Send go into your pending review on GitHub as you send
    them; Submit in the viewer publishes it. The review server detaches
    and keeps running until the tab has been gone for the idle timeout.
    """
    configure_logging(verbose or foreground)
    if foreground and serve_run is not None:
        typer.echo(f"scr pr: {runner.FOREGROUND_FLAG} and {servers.SERVE_RUN_FLAG} are mutually exclusive", err=True)
        raise typer.Exit(code=2)

    require_viewer_bundle("scr pr")
    runs_root = runs_root or default_runs_root()
    # The committed file cannot set the backend, model or credentials, so
    # they resolve now, before the PR is fetched: a misconfiguration fails
    # fast.
    cfg = load_config(repo=repo, committed_path=None)
    backend = cfg.resolve_backend(backend)
    model = cfg.resolve_model(backend=backend, cli_value=model)
    client = select_client(cfg, backend, model=model) if augment else None

    def config_for(run_dir: paths.RunDir) -> ReviewConfig:
        """The review settings, reading the repo's committed config from
        the PR's base commit: the branch the PR merges into decides how
        it is reviewed, not the PR, and not whatever the cwd holds.
        """
        settled = load_config(repo=repo, committed_path=paths.committed_config_path(run_dir.base))
        return ReviewConfig(
            runs_root=runs_root,
            augment=augment,
            model=model,
            concurrency=concurrency,
            no_cache=no_cache,
            cache_dir=cache_dir,
            open_browser=not no_open,
            port=port,
            timeout=timeout,
            extra_review_prompt=resolve_extra_review_prompt(settled, extra_prompt) if augment else None,
            skip_globs=settled.skip_globs,
            explainer=settled.explainer,
            explainer_prompt=resolve_explainer_prompt(settled, explainer_prompt) if augment else None,
            client=client,
            debug=debug,
        )

    if serve_run is not None:
        run_dir = paths.RunDir(runs_root / serve_run)
        raise typer.Exit(code=serve_pr_run(run_dir, config_for(run_dir), argv=sys.argv[1:]))

    opts = PrFlowOptions(repo=repo, number=number, runs_root=runs_root, config_for=config_for)
    raise typer.Exit(code=run_pr_flow(opts, argv=sys.argv[1:], foreground=foreground))
