"""The `scr` command-line interface."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import typer

from .cache.store import CacheStore
from .config import ConfigError, ScrConfig
from .fetch import materialize_github_pr_run
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


from .paths import default_runs_root as _default_runs_root


def _version_callback(value: bool) -> None:
    if value:
        from importlib.metadata import version
        typer.echo(version("semantic-code-review"))
        raise typer.Exit()


@app.callback()
def _main(
    version: bool = typer.Option(
        False, "--version", "-V",
        callback=_version_callback,
        is_eager=True,
        help="Print version and exit.",
    ),
) -> None:
    """Semantic Code Review — LLM-augmented PR diff viewer."""


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


# Load the user / per-repo config and apply its `[env]` block. Order:
# shell env > .env > config[env]. Each layer uses `setdefault` so the
# closer one wins. `_CONFIG` itself drives backend/model resolution
# (per-flag, see `_select_client` and command bodies).
# `_select_client` is a thin shim over `backends.get(...).resolve(...)` —
# the dispatch lives in `semantic_code_review.backends`.
try:
    _CONFIG = ScrConfig.load()
except ConfigError as e:
    sys.stderr.write(f"scr: {e}\n")
    sys.stderr.flush()
    raise SystemExit(1)
_CONFIG.apply_env()


def _configure_logging(verbose: bool) -> None:
    # Default is WARNING — quiet by default, with `--verbose` switching
    # to INFO so per-request httpx lines and per-hunk pipeline progress
    # are visible. (Previous default of INFO produced ~50 stderr lines
    # per medium PR; users couldn't see their own output through it.)
    level = logging.INFO if verbose else logging.WARNING
    # force=True so we take over even if a library (anthropic SDK, typer,
    # etc.) already attached a root handler at WARNING — otherwise our
    # progress logs would be silently dropped.
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    # basicConfig sets the *logger* level but leaves the StreamHandler
    # at NOTSET (passes everything). _attach_file_log later raises the
    # package logger to INFO so the trace file captures progress —
    # without an explicit handler level here, those INFO records leak
    # up to the root's stderr handler and undo the quiet default.
    for h in logging.getLogger().handlers:
        h.setLevel(level)


def _select_client(backend: str, *, model: str):
    """Resolve a backend name to a `Client` for the augment pipeline.

    `backend` is "auto" or any name in `_CONFIG.backends` (builtins +
    user-defined `[backends.<name>]` entries). All dispatch lives in
    `semantic_code_review.backends`; this is the CLI's only entry point.
    """
    from . import backends as _backends

    if backend == "auto":
        backend = _backends.resolve_auto(config=_CONFIG)
    return _backends.get(backend, config=_CONFIG).resolve(model=model)


@app.command()
def fetch(
    pr_url: str = typer.Argument(..., help="https://github.com/owner/repo/pull/N"),
    runs_root: Path = typer.Option(
        None, help="Root directory for run artefacts (default: ~/.cache/scr/runs/<repo-fingerprint>/)."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Fetch PR metadata, diff, and base/head worktrees into a run directory."""
    _configure_logging(verbose)
    from .fetch import GhFetchError, preflight_gh

    runs_root = runs_root or _default_runs_root()
    try:
        preflight_gh()
        run_dir = materialize_github_pr_run(pr_url, runs_root)
    except GhFetchError as e:
        typer.echo(f"scr: {e}", err=True)
        raise typer.Exit(code=2)
    typer.echo(f"run directory: {run_dir}")


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
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Augment a fetched run directory with LLM annotations."""
    _configure_logging(verbose)
    # Import inside: anthropic SDK lazy-loaded so strip/lint work without it.
    from .augment.pipeline import augment_run_dir
    from .augment.prompts import PROMPT_VERSION

    backend = _CONFIG.resolve_backend(backend)
    model = _CONFIG.resolve_model(backend=backend, cli_value=model)
    cache = None if no_cache else CacheStore(root=cache_dir, prompt_version=PROMPT_VERSION)
    client = _select_client(backend, model=model)

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
            show_progress=not verbose,
        )
    )
    typer.echo(f"wrote {path}")


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
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Review a local git diff; round-trip reviewer comments to stdout."""
    _configure_logging(verbose)
    from .fetch import EmptyDiff, LocalDiffError
    from .review.runner import ReviewOptions, run_review

    backend = _CONFIG.resolve_backend(backend)
    model = _CONFIG.resolve_model(backend=backend, cli_value=model)
    runs_root = runs_root or _default_runs_root()
    # Resolve the backend up-front so a misconfiguration fails fast, before
    # we spend time building the diff / worktrees.
    client = _select_client(backend, model=model) if augment else None

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
        show_progress=not verbose,
    )
    try:
        code = run_review(opts)
    except EmptyDiff as e:
        # Empty-diff isn't an error — exit cleanly so calling scripts
        # ("review every commit on this branch") don't have to special-
        # case "this commit changed nothing".
        typer.echo(f"scr: {e}", err=True)
        raise typer.Exit(code=0)
    except LocalDiffError as e:
        typer.echo(f"scr: {e}", err=True)
        raise typer.Exit(code=2)
    raise typer.Exit(code=code)


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
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt before posting comments to GitHub."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Review a GitHub PR; round-trip reviewer comments back as a single review."""
    _configure_logging(verbose)
    import json as _json
    from .augment.prompts import PROMPT_VERSION
    from .cache.store import CacheStore
    from .review.github import (
        GhError, list_review_requested_prs, pick_pr_interactive,
    )
    from .review.github_graphql import post_review_via_graphql
    from .review.runner import serve_review
    from .fetch import GhFetchError, preflight_gh

    backend = _CONFIG.resolve_backend(backend)
    model = _CONFIG.resolve_model(backend=backend, cli_value=model)

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

    client = _select_client(backend, model=model) if augment else None

    runs_root = runs_root or _default_runs_root()
    try:
        run_dir = materialize_github_pr_run(pr_url, runs_root)
    except GhFetchError as e:
        typer.echo(f"scr pr: {e}", err=True)
        raise typer.Exit(code=2)

    augment_task = None
    fold_summary_task = None
    if augment:
        from .augment.pipeline import augment_run_dir
        from .review.runner import _build_fold_summary_task

        cache = None if no_cache else CacheStore(root=cache_dir, prompt_version=PROMPT_VERSION)

        async def augment_task(rd, publish):  # noqa: F811 — closes over local config
            await augment_run_dir(
                rd,
                model=model,
                concurrency=concurrency,
                cache=cache,
                client=client,
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
    from .review.comments import format_markdown
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
    from .review.github import comments_to_github
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


runs_app = typer.Typer(help="Inspect or manage scr's per-repo run-artefact directory.")
app.add_typer(runs_app, name="runs")


@runs_app.command("path")
def runs_path() -> None:
    """Print the runs root resolved for the current cwd."""
    typer.echo(str(_default_runs_root()))


config_app = typer.Typer(help="Inspect or edit scr's user/per-repo config.")
app.add_typer(config_app, name="config")


@config_app.command("path")
def config_path() -> None:
    """Print the user-level config path (creates the directory if missing)."""
    from .paths import default_config_path

    typer.echo(str(default_config_path()))


@config_app.command("show")
def config_show() -> None:
    """Print the resolved config (user + per-repo merged) and where each setting came from."""
    from .paths import default_config_path, find_repo_config_path

    user = default_config_path()
    repo = find_repo_config_path()
    typer.echo(f"# user config: {user} ({'present' if user.is_file() else 'absent'})")
    typer.echo(
        f"# per-repo config: {repo or '(none found)'}"
        f"{' (present)' if repo and repo.is_file() else ''}"
    )
    typer.echo("")
    typer.echo(f"backend = {_CONFIG.backend!r} (from {_CONFIG.sources.get('backend', 'default')})")
    if _CONFIG.model_default is not None:
        typer.echo("[model]")
        typer.echo(
            f'  default = {_CONFIG.model_default!r} '
            f'(from {_CONFIG.sources.get("model.default", "?")})'
        )
    typer.echo("[backends]")
    for name in sorted(_CONFIG.backends):
        bdef = _CONFIG.backends[name]
        src = _CONFIG.sources.get(f"backends.{name}", "builtin")
        typer.echo(f"  {name}  type={bdef.type.value}  (from {src})")
        if bdef.default_model is not None:
            typer.echo(f"    model       = {bdef.default_model!r}")
        if bdef.base_url is not None:
            typer.echo(f"    base_url    = {bdef.base_url!r}")
        if bdef.api_key_env is not None:
            typer.echo(f"    api_key_env = {bdef.api_key_env!r}")
        if bdef.api_key_command is not None:
            typer.echo(f"    api_key_command = {list(bdef.api_key_command)!r}")
    if _CONFIG.env:
        typer.echo("[env]")
        for k, v in _CONFIG.env.items():
            applied = "applied" if os.environ.get(k) == v else "overridden by shell/.env"
            typer.echo(f"  {k} = {v!r} (from {_CONFIG.sources.get(f'env.{k}', '?')}, {applied})")


@config_app.command("edit")
def config_edit(
    template: str = typer.Option(
        None, "--template",
        help=(
            "Append a [backends.<name>] block before opening. "
            "<name> is any builtin (run `scr config show` for the list) "
            "or 'openai-compat' for a generic placeholder scaffold."
        ),
    ),
) -> None:
    """Open the user-level config in $EDITOR (or `vi`)."""
    import subprocess as _sp

    from .config import BUILTIN_BACKENDS
    from .config_template import SCAFFOLD_SECTION_NAME, render_backend_template
    from .paths import default_config_path

    path = default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(_CONFIG_TEMPLATE, encoding="utf-8")

    if template is not None:
        valid = sorted([SCAFFOLD_SECTION_NAME, *BUILTIN_BACKENDS])
        if template not in valid:
            raise typer.BadParameter(
                f"unknown template {template!r}; expected one of: "
                + ", ".join(valid)
            )
        existing = path.read_text(encoding="utf-8")
        # Skip+warn if the section already exists; let the user resolve
        # rather than risk clobbering hand-edited overrides.
        if f"[backends.{template}]" in existing:
            typer.echo(
                f"scr: [backends.{template}] already in {path}; "
                "skipping the template append. Edit it directly.",
                err=True,
            )
        else:
            block = render_backend_template(template)
            sep = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
            path.write_text(existing + sep + block, encoding="utf-8")

    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
    _sp.run([editor, str(path)], check=False)


_CONFIG_TEMPLATE = """\
# scr config — non-secret defaults. CLI flags and env vars override.
#
# Do NOT put API keys here. Config files leak too easily (accidental
# commits, dotfile repos, screen-shares). Use a `.env` or your shell.

# Default backend when --backend isn't passed. "auto" picks claude-api
# if ANTHROPIC_API_KEY is set, else claude-cli if `claude` is on PATH.
# Run `scr config show` for the full list of registered backends.
# backend = "claude-api"

# Override a builtin backend's default model, or define a new one.
# [backends.claude-api]
# model = "claude-sonnet-4-7"

# Global model fallback used when the selected backend has no model
# of its own and --model isn't passed.
# [model]
# default = "claude-opus-4-7"

# Environment variables to set if not already in the parent env.
# Useful for non-secrets like GCP project / location.
# [env]
# GOOGLE_CLOUD_PROJECT = "aasgard-dev"
# GOOGLE_CLOUD_LOCATION = "global"
"""


if __name__ == "__main__":
    app()
