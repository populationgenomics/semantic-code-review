"""The `scr` command-line interface."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import typer

from .cache.store import CacheStore
from .fetch import fetch as fetch_pr
from .format.lint import lint_text
from .format.parse import parse_augmented_diff
from .format.strip import strip_annotations


app = typer.Typer(
    help="Semantic Code Review — LLM-augmented PR diff viewer.",
    # Typer's default rich tracebacks are noisy for end-users. Plain
    # Python tracebacks still print on unexpected errors; expected ones
    # (missing key, claude not logged in) are surfaced as short messages.
    pretty_exceptions_enable=False,
)


DEFAULT_RUNS_ROOT = Path(".scr/runs")


def _load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env loader: KEY=value lines, optional quotes, # comments.

    Also aliases ANTHROPIC_API_TOKEN -> ANTHROPIC_API_KEY because the
    Anthropic SDK reads the KEY form.
    """
    if not path.exists():
        return
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            os.environ.setdefault(key, value)
    except OSError:
        return
    if "ANTHROPIC_API_KEY" not in os.environ and "ANTHROPIC_API_TOKEN" in os.environ:
        os.environ["ANTHROPIC_API_KEY"] = os.environ["ANTHROPIC_API_TOKEN"]


_load_dotenv()


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _select_client(backend: str):  # -> ClaudeClient, but keep import lazy
    """Pick a `ClaudeClient` based on env + explicit choice.

    backend ∈ {"auto","api","cli"}. "auto" picks API if the key is set,
    else CLI if `claude` is on PATH, else raises.
    """
    import shutil as _shutil

    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    has_claude = bool(_shutil.which("claude"))

    if backend == "api":
        if not has_key:
            raise typer.BadParameter(
                "--backend=api but ANTHROPIC_API_KEY is not set "
                "(load a .env or export the variable)."
            )
        from .augment.runner import AnthropicClient
        return AnthropicClient()

    if backend == "cli":
        if not has_claude:
            raise typer.BadParameter(
                "--backend=cli but `claude` is not on PATH "
                "(install Claude Code CLI or set ANTHROPIC_API_KEY)."
            )
        from .augment.claude_cli_client import ClaudeCLIClient
        _warn_cli_fallback()
        return ClaudeCLIClient()

    # auto
    if has_key:
        from .augment.runner import AnthropicClient
        return AnthropicClient()
    if has_claude:
        from .augment.claude_cli_client import ClaudeCLIClient
        _warn_cli_fallback()
        return ClaudeCLIClient()

    raise typer.BadParameter(
        "No Anthropic credentials available: set ANTHROPIC_API_KEY "
        "(or ANTHROPIC_API_TOKEN in .env), or install the `claude` CLI "
        "for subscription-based fallback."
    )


_FALLBACK_WARNED = False


def _warn_cli_fallback() -> None:
    global _FALLBACK_WARNED
    if _FALLBACK_WARNED:
        return
    _FALLBACK_WARNED = True
    sys.stderr.write(
        "scr: no ANTHROPIC_API_KEY; falling back to `claude -p` subprocess. "
        "Note: no prompt caching, reduced concurrency, no in-loop repo tools "
        "(annotation quality will be lower).\n"
    )
    sys.stderr.flush()


@app.command()
def fetch(
    pr_url: str = typer.Argument(..., help="https://github.com/owner/repo/pull/N"),
    runs_root: Path = typer.Option(DEFAULT_RUNS_ROOT, help="Root directory for run artefacts."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Fetch PR metadata, diff, and base/head worktrees into a run directory."""
    _configure_logging(verbose)
    result = fetch_pr(pr_url, runs_root)
    typer.echo(f"run directory: {result.run_dir}")


@app.command()
def augment(
    run_dir: Path = typer.Argument(..., help="Path to a run directory from 'scr fetch'."),
    model: str = typer.Option("claude-opus-4-7", help="Anthropic model id."),
    concurrency: int = typer.Option(8, help="Per-hunk call concurrency."),
    max_hunks: int = typer.Option(None, help="Cap hunk calls (smoke tests)."),
    only_files: list[str] = typer.Option(None, help="Restrict to these post-image paths (repeatable)."),
    skip_overview: bool = typer.Option(False, help="Skip the PR-level overview pass."),
    skip_context: bool = typer.Option(False, help="Disable repo tools (no cross-file context)."),
    no_cache: bool = typer.Option(False, help="Disable disk cache of LLM calls."),
    cache_dir: Path = typer.Option(None, help="Cache root (default ~/.cache/scr/v1)."),
    backend: str = typer.Option(
        "auto", help="LLM backend: auto|api|cli (cli shells out to `claude -p`)."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Augment a fetched run directory with LLM annotations."""
    _configure_logging(verbose)
    # Import inside: anthropic SDK lazy-loaded so strip/lint work without it.
    from .augment.pipeline import augment_run_dir
    from .augment.prompts import PROMPT_VERSION

    cache = None if no_cache else CacheStore(root=cache_dir, prompt_version=PROMPT_VERSION)
    client = _select_client(backend)

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
        )
    )
    typer.echo(f"wrote {path}")


@app.command()
def render(
    run_dir: Path = typer.Argument(...),
    out: Path = typer.Option(None, help="Output HTML path (default <run_dir>/review.html)."),
    offline: bool = typer.Option(False, help="Inline diff2html + highlight.js assets for offline use."),
) -> None:
    """Render an augmented run directory as a self-contained HTML viewer."""
    from .viewer.render_html import render_run_dir

    out_path = out or (run_dir / "review.html")
    render_run_dir(run_dir, out_path, offline=offline)
    typer.echo(f"wrote {out_path}")


@app.command()
def run(
    pr_url: str = typer.Argument(...),
    runs_root: Path = typer.Option(DEFAULT_RUNS_ROOT),
    model: str = typer.Option("claude-opus-4-7"),
    concurrency: int = typer.Option(8),
    no_cache: bool = typer.Option(False),
    offline: bool = typer.Option(False),
    backend: str = typer.Option("auto"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Fetch + augment + render in one shot."""
    _configure_logging(verbose)
    from .augment.pipeline import augment_run_dir
    from .augment.prompts import PROMPT_VERSION
    from .viewer.render_html import render_run_dir

    fetch_result = fetch_pr(pr_url, runs_root)
    cache = None if no_cache else CacheStore(prompt_version=PROMPT_VERSION)
    client = _select_client(backend)
    asyncio.run(
        augment_run_dir(
            fetch_result.run_dir,
            model=model,
            concurrency=concurrency,
            cache=cache,
            client=client,
        )
    )
    out = fetch_result.run_dir / "review.html"
    render_run_dir(fetch_result.run_dir, out, offline=offline)
    typer.echo(f"done: {out}")


@app.command()
def strip(
    augmented: Path = typer.Argument(..., help="Path to an augmented.diff file."),
) -> None:
    """Print a plain unified diff (annotations removed) to stdout."""
    text = augmented.read_text(encoding="utf-8")
    sys.stdout.write(strip_annotations(text))


@app.command()
def lint(
    augmented: Path = typer.Argument(...),
    sidecar: Path = typer.Option(None, help="Optional sidecar JSON to cross-check."),
) -> None:
    """Validate format, smell tags, round-trip, and (optionally) the sidecar."""
    text = augmented.read_text(encoding="utf-8")
    result = lint_text(text, sidecar_path=sidecar)
    for e in result.errors:
        typer.echo(f"error: {e}", err=True)
    for w in result.warnings:
        typer.echo(f"warning: {w}", err=True)
    if not result.ok:
        raise typer.Exit(code=1)
    typer.echo("ok")


@app.command()
def show(
    run_dir: Path = typer.Argument(...),
) -> None:
    """Print the augmented diff of a run directory to stdout."""
    path = run_dir / "augmented.diff"
    sys.stdout.write(path.read_text(encoding="utf-8"))


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
    spec_md: Path = typer.Option(
        None, "--spec", help="Markdown file with the spec/intent for this change."
    ),
    runs_root: Path = typer.Option(DEFAULT_RUNS_ROOT),
    repo_root: Path = typer.Option(None, help="Repo root (defaults to walking up from cwd)."),
    no_staged: bool = typer.Option(False, help="With a single ref: exclude staged changes."),
    no_unstaged: bool = typer.Option(False, help="With a single ref: exclude unstaged changes."),
    augment: bool = typer.Option(True, help="Run the LLM augmentation pass before rendering."),
    model: str = typer.Option("claude-opus-4-7"),
    concurrency: int = typer.Option(8),
    no_cache: bool = typer.Option(False),
    cache_dir: Path = typer.Option(None),
    offline: bool = typer.Option(False, help="Inline highlight.js into the HTML."),
    no_open: bool = typer.Option(False, help="Skip opening the browser (for CI / SSH)."),
    port: int = typer.Option(0, help="Server port (0 = kernel-assigned)."),
    timeout: int = typer.Option(3600, help="Server idle timeout in seconds."),
    backend: str = typer.Option(
        "auto", help="LLM backend: auto|api|cli (cli shells out to `claude -p`)."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Review a local git diff; round-trip reviewer comments to stdout."""
    _configure_logging(verbose)
    from .review.runner import ReviewOptions, run_review

    # Resolve the backend up-front so a misconfiguration fails fast, before
    # we spend time building the diff / worktrees.
    client = _select_client(backend) if augment else None

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
        offline_html=offline,
        open_browser=not no_open,
        port=port,
        timeout=timeout,
        client=client,
    )
    code = run_review(opts)
    raise typer.Exit(code=code)


if __name__ == "__main__":
    app()
