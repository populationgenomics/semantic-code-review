"""`scr review` — review a local git diff in the browser.

One command, three modes (ADR 0009): `scr review <spec>` materialises the
run and detaches the server, printing the run id; `scr review --serve-run
<slug>` is the detached server itself (hidden — `run_review` spawns it);
`scr review --wait <run_id>` is Claude's end of the stream.
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from .. import paths
from ..fetch import EmptyDiff, LocalDiffError
from ..paths import default_runs_root
from ..review import runner, stream
from ..review.config import ReviewConfig
from . import app
from ._shared import (
    configure_logging,
    get_config,
    resolve_explainer_prompt,
    resolve_extra_review_prompt,
    select_client,
)


@app.command()
def review(
    spec: str = typer.Argument(
        None,
        metavar="[SPEC]",
        help=(
            "Git ref (e.g. 'main') or range ('main..HEAD', 'HEAD~3...HEAD'). "
            "Single ref diffs against current working state; range is "
            "committed-only. Give a second endpoint for a two-endpoint diff."
        ),
    ),
    right: str = typer.Argument(
        None,
        metavar="[RIGHT]",
        help=(
            "Optional second endpoint. With SPEC, diffs left vs right: "
            "two refs ('e4e8f74 HEAD') for a whole-tree diff, or two "
            "'rev:path' blobs ('A:old.py B:new.py') for a single-file "
            "diff (cross-path renders as a rename). Both must be the "
            "same kind."
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
    explainer_prompt: Path = typer.Option(
        None,
        "--explainer-prompt",
        help=(
            "Path to a markdown/text file of house style for the change "
            "explainer — how a document about a change should read in "
            "this repo. Appended to the explainer passes' guidance and "
            "nothing else; the per-hunk annotations are unaffected. "
            "Overrides [augment].explainer_prompt in the config."
        ),
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    debug: bool = typer.Option(
        False,
        "--debug",
        envvar="SCR_DEBUG",
        help="Surface each CLI-backend subprocess spawn (raw argv + envelope) in the viewer's debug drawer.",
    ),
    wait: str = typer.Option(
        None,
        "--wait",
        metavar="RUN_ID",
        help=(
            "Claude's end of the review: block for the next batch of comments the "
            "reviewer sends from the viewer of RUN_ID. Prints `status: batch`, "
            "`status: nothing-yet` or `status: ended` as the first line; exit 0 for "
            "all three, 2 for an unknown run id."
        ),
    ),
    wait_timeout: int = typer.Option(
        stream.WAIT_TIMEOUT,
        "--wait-timeout",
        help="Seconds a --wait blocks before answering `nothing-yet`.",
    ),
    serve_run: str = typer.Option(None, runner.SERVE_RUN_FLAG, hidden=True),
) -> None:
    """Review a local git diff in the browser; returns at once with the run id.

    The review server detaches and keeps running until the tab has been
    gone for the idle timeout. Comments the reviewer sends reach Claude
    through `scr review --wait <run_id>`.
    """
    configure_logging(verbose)

    runs_root = runs_root or default_runs_root()
    if wait is not None:
        raise typer.Exit(code=stream.run_wait(paths.RunDir(runs_root / wait), timeout=wait_timeout))
    if spec is None and serve_run is None:
        typer.echo("scr review: give a git ref or range to review", err=True)
        raise typer.Exit(code=2)

    cfg = get_config()
    backend = cfg.resolve_backend(backend)
    model = cfg.resolve_model(backend=backend, cli_value=model)
    extra_review_prompt = resolve_extra_review_prompt(extra_prompt) if augment else None
    house_style = resolve_explainer_prompt(explainer_prompt) if augment else None
    # Resolve the backend up-front so a misconfiguration fails fast, before
    # we spend time building the diff / worktrees.
    client = select_client(backend, model=model) if augment else None

    review_cfg = ReviewConfig(
        runs_root=runs_root,
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
        skip_globs=cfg.skip_globs,
        explainer=cfg.explainer,
        explainer_prompt=house_style,
        debug=debug,
    )
    if serve_run is not None:
        raise typer.Exit(code=runner.serve_run(paths.RunDir(runs_root / serve_run), review_cfg, argv=sys.argv[1:]))

    opts = runner.ReviewOptions(
        spec=spec,
        spec_right=right,
        spec_markdown=spec_md,
        repo_root=repo_root,
        no_staged=no_staged,
        no_unstaged=no_unstaged,
        config=review_cfg,
    )
    try:
        code = runner.run_review(opts, argv=sys.argv[1:])
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
