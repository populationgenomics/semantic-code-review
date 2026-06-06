"""`scr pr` — review a GitHub PR; post the reviewer's comments back."""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from ..fetch import GhFetchError, materialize_github_pr_run, preflight_gh
from ..paths import default_runs_root
from . import app
from ._shared import (
    configure_logging, get_config, resolve_extra_review_prompt, select_client,
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
        None, "--extra-prompt",
        help=(
            "Path to a markdown/text file with an extra review prompt. "
            "Runs as a single PR-level LLM call alongside the main "
            "comprehension pass; line-anchored notes merge into the "
            "matching hunk's line_notes. Overrides [augment].extra_prompt."
        ),
    ),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt before posting comments to GitHub."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Review a GitHub PR; round-trip reviewer comments back as a single review."""
    configure_logging(verbose)
    import json as _json
    from ..augment.prompts import PROMPT_VERSION
    from ..cache.store import CacheStore
    from ..review.github import (
        GhError, list_review_requested_prs, pick_pr_interactive,
    )
    from ..review.github_graphql import post_review_via_graphql
    from ..review.runner import serve_review

    cfg = get_config()
    backend = cfg.resolve_backend(backend)
    model = cfg.resolve_model(backend=backend, cli_value=model)

    # Preflight `gh` once — both PR resolution and the post step need
    # it, and a missing-tool / too-old error here is more informative
    # than the same error fired from a downstream subprocess.
    try:
        preflight_gh()
    except GhFetchError as e:
        typer.echo(f"scr pr: {e}", err=True)
        raise typer.Exit(code=2)

    # Resolve the PR.
    if number is None:
        try:
            prs = list_review_requested_prs(repo)
        except GhError as e:
            typer.echo(f"scr pr: {e}", err=True)
            raise typer.Exit(code=1)
        if not prs:
            typer.echo(
                f"scr pr: no open PRs in {repo} are requesting your review. "
                "Pass an explicit PR number, or open the list on github.com.",
                err=True,
            )
            raise typer.Exit(code=1)
        if len(prs) == 1:
            number = prs[0].number
            sys.stderr.write(f"scr pr: reviewing {repo}#{number} — {prs[0].title}\n")
            sys.stderr.flush()
        else:
            picked = pick_pr_interactive(repo, prs)
            if picked is None:
                typer.echo("scr pr: no PR selected", err=True)
                raise typer.Exit(code=1)
            number = picked

    pr_url = f"https://github.com/{repo}/pull/{number}"

    extra_review_prompt = resolve_extra_review_prompt(extra_prompt) if augment else None
    client = select_client(backend, model=model) if augment else None

    runs_root = runs_root or default_runs_root()
    try:
        run_dir = materialize_github_pr_run(pr_url, runs_root)
    except GhFetchError as e:
        typer.echo(f"scr pr: {e}", err=True)
        raise typer.Exit(code=2)

    augment_task = None
    fold_summary_task = None
    if augment:
        from ..augment.pipeline import augment_run_dir
        from ..review.runner import _build_fold_summary_task

        cache = None if no_cache else CacheStore(root=cache_dir, prompt_version=PROMPT_VERSION)

        async def augment_task(rd, publish):  # noqa: F811 — closes over local config
            await augment_run_dir(
                rd,
                model=model,
                concurrency=concurrency,
                cache=cache,
                client=client,
                extra_review_prompt=extra_review_prompt,
                # Page carries the progress display now; suppress the
                # terminal meter to avoid duplicate noise and to keep
                # the listening-URL / warning lines unobstructed.
                show_progress=False,
                on_event=publish,
            )

        fold_summary_task = _build_fold_summary_task(
            client=client, model=model, cache=cache, run_dir=run_dir,
        )
    else:
        # Mirror cli.review's behaviour: copy raw → augmented so render has
        # something to parse when augment is skipped.
        (run_dir / "augmented.diff").write_text(
            (run_dir / "raw.diff").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

    result = serve_review(
        run_dir,
        augment=augment_task,
        fold_summary=fold_summary_task,
        port=port,
        timeout=timeout,
        open_browser=not no_open,
    )

    # Markdown to stdout for parity with `scr review` (the slash-command
    # downstream expects to read it). Only the *new* (session-local)
    # comments belong in the markdown — re-printing every ingested
    # upstream comment would drown the reviewer's actual notes.
    from ..review.comments import format_markdown
    local_comments = [c for c in result.comments if c.source == "local"]
    sys.stdout.write(format_markdown(local_comments, run_slug=run_dir.name))
    sys.stdout.flush()

    # Need the head SHA from meta.json so GitHub anchors the review at
    # the commit the reviewer actually saw.
    meta = _json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    head_sha = meta.get("headRefOid", "")
    if not head_sha:
        typer.echo("scr pr: meta.json is missing headRefOid; can't anchor review", err=True)
        raise typer.Exit(code=2)

    # Map + filter once: ingested comments drop out, local replies to
    # ingested threads become reply entries. The prompt + the post both
    # work off this filtered list so the count we promise matches what
    # we actually send.
    from ..review.github import comments_to_github
    mapped = comments_to_github(result.comments)
    if not mapped:
        sys.stderr.write(
            "scr pr: no new local comments to post; "
            f"comments are in {run_dir / 'comments.json'}.\n"
        )
        raise typer.Exit(code=0 if result.clean else 2)
    n_threads = sum(1 for c in mapped if not c.is_reply)
    n_replies = len(mapped) - n_threads
    descr_parts: list[str] = []
    if n_threads:
        descr_parts.append(f"{n_threads} new thread{'s' if n_threads != 1 else ''}")
    if n_replies:
        descr_parts.append(f"{n_replies} repl{'ies' if n_replies != 1 else 'y'}")
    descr = " + ".join(descr_parts)

    if not yes:
        sys.stderr.write(
            f"\nAbout to post {descr} as a COMMENT review on "
            f"{repo}#{number} (commit {head_sha[:8]}…).\n"
            f"Continue? [y/N] "
        )
        sys.stderr.flush()
        answer = (sys.stdin.readline() or "").strip().lower()
        if answer != "y":
            sys.stderr.write(
                "scr pr: aborted; comments are still in "
                f"{run_dir / 'comments.json'} — re-run with --no-augment "
                "to retry.\n"
            )
            raise typer.Exit(code=1)

    try:
        post = post_review_via_graphql(repo, number, mapped)
    except GhError as e:
        typer.echo(f"scr pr: posting failed: {e}", err=True)
        sys.stderr.write(
            f"comments are still in {run_dir / 'comments.json'} — "
            "re-run with --no-augment to retry.\n"
        )
        raise typer.Exit(code=2)

    sys.stderr.write(
        f"scr pr: posted {post.posted} comment(s) — {post.review_url}\n"
    )
    raise typer.Exit(code=0 if result.clean else 2)
