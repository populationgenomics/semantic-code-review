"""The `scr` command-line interface."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import typer

from .cache.store import CacheStore
from .config import BackendType, ConfigError, ScrConfig
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
try:
    _CONFIG = ScrConfig.load()
except ConfigError as e:
    sys.stderr.write(f"scr: {e}\n")
    sys.stderr.flush()
    raise SystemExit(1)
_CONFIG.apply_env()


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    # force=True so we take over even if a library (anthropic SDK, typer,
    # etc.) already attached a root handler at WARNING — otherwise our
    # INFO+ progress logs would be silently dropped.
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )


_DEFAULT_GEMINI_API_MODEL = "gemini-2.5-pro"


def _backend_choices() -> str:
    return ", ".join(sorted(["auto", *_CONFIG.backends.keys()]))


def _select_client(backend: str, *, model: str):
    """Pick a backend handle based on env + explicit choice.

    Returns a `Backend` regardless of the path: SDK backends carry a
    pydantic-ai model id string, CLI backends carry a `Model` subclass
    that wraps the `claude -p` / `gemini -p` subprocess client. The
    pipeline calls `make_*_agent(backend.model)` either way.

    `backend` is "auto" or any name in `_CONFIG.backends` (builtins +
    user-defined `[backends.<name>]` entries). "auto" picks claude-api
    if `ANTHROPIC_API_KEY` is set, else claude-cli if `claude` is on
    PATH, else raises. Both Gemini backends are opt-in only.
    """
    if backend == "auto":
        backend = _resolve_auto_backend()

    bdef = _CONFIG.backends.get(backend)
    if bdef is None:
        raise typer.BadParameter(
            f"unknown backend {backend!r}; expected one of: {_backend_choices()}."
        )

    btype = bdef.type
    if btype is BackendType.ANTHROPIC_SDK:
        return _make_anthropic_sdk_backend(backend, model)
    if btype is BackendType.CLAUDE_CLI:
        return _make_claude_cli_backend(backend, model)
    if btype is BackendType.GOOGLE_SDK:
        return _make_google_sdk_backend(backend, model, bdef.default_model)
    if btype is BackendType.GEMINI_CLI:
        return _make_gemini_cli_backend(backend, model, bdef.default_model)
    if btype is BackendType.OPENAI_COMPAT:
        raise typer.BadParameter(
            f"--backend={backend} (type=openai-compat) is not yet "
            "wired up in this build."
        )
    raise typer.BadParameter(f"backend {backend!r} has unknown type {btype!r}")


def _resolve_auto_backend() -> str:
    import shutil as _shutil

    if os.environ.get("ANTHROPIC_API_KEY"):
        return "claude-api"
    if _shutil.which("claude"):
        return "claude-cli"
    raise typer.BadParameter(
        "No Anthropic credentials available: set ANTHROPIC_API_KEY "
        "(or ANTHROPIC_API_TOKEN in .env), install the `claude` CLI "
        "for subscription-based fallback, or pass --backend=gemini-cli "
        "(CLI subprocess) / --backend=gemini-api (Google SDK) to opt "
        "into a Gemini backend."
    )


def _make_anthropic_sdk_backend(name: str, model: str):
    from .augment.agents import Backend

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise typer.BadParameter(
            f"--backend={name} but ANTHROPIC_API_KEY is not set "
            "(load a .env or export the variable)."
        )
    return Backend(model=f"anthropic:{model}")


def _make_claude_cli_backend(name: str, model: str):
    import shutil as _shutil

    from .augment.agents import Backend
    from .augment.cli_models import ClaudeCLIModel

    if not _shutil.which("claude"):
        raise typer.BadParameter(
            f"--backend={name} but `claude` is not on PATH "
            "(install Claude Code CLI or set ANTHROPIC_API_KEY)."
        )
    _warn_cli_fallback()
    return Backend(model=ClaudeCLIModel(model=model), is_subprocess_backend=True)


def _make_google_sdk_backend(name: str, model: str, default_model: str | None):
    from .augment.agents import Backend

    if not (
        os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
    ):
        raise typer.BadParameter(
            f"--backend={name} but no Gemini credentials found. "
            "Set GEMINI_API_KEY (AI Studio), GOOGLE_API_KEY, or "
            "GOOGLE_CLOUD_PROJECT (Vertex via ADC)."
        )
    gem_model = _coerce_gemini_model(model, default_model)
    # GOOGLE_CLOUD_PROJECT triggers Vertex via ADC; otherwise the
    # API-key path (AI Studio) wins.
    if os.environ.get("GOOGLE_CLOUD_PROJECT"):
        return Backend(model=f"google-vertex:{gem_model}")
    return Backend(model=f"google-gla:{gem_model}")


def _make_gemini_cli_backend(name: str, model: str, default_model: str | None):
    import shutil as _shutil

    from .augment.agents import Backend
    from .augment.cli_models import GeminiCLIModel

    if not _shutil.which("gemini"):
        raise typer.BadParameter(
            f"--backend={name} but `gemini` is not on PATH "
            "(install via `npm install -g @google/gemini-cli`)."
        )
    if not (
        os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
        or (Path.home() / ".gemini" / "oauth_creds.json").exists()
    ):
        raise typer.BadParameter(
            f"--backend={name} but no Gemini credentials found. Set "
            "GEMINI_API_KEY (AI Studio) or GOOGLE_API_KEY (Vertex), "
            "or run `gemini` once interactively to complete the "
            "OAuth flow."
        )
    _warn_gemini_fallback()
    gem_model = _coerce_gemini_model(model, default_model)
    return Backend(
        model=GeminiCLIModel(model=gem_model),
        is_subprocess_backend=True,
    )


def _coerce_gemini_model(model: str, default_model: str | None) -> str:
    """If a Gemini backend gets a Claude model id (because `[model] default`
    was set globally for an Anthropic config), substitute the backend's
    own default. Avoids surprising the user with a 404 on the wrong vendor.
    """
    if model.startswith("claude"):
        return default_model or _DEFAULT_GEMINI_API_MODEL
    return model


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


_GEMINI_WARNED = False


def _warn_gemini_fallback() -> None:
    global _GEMINI_WARNED
    if _GEMINI_WARNED:
        return
    _GEMINI_WARNED = True
    sys.stderr.write(
        "scr: using `gemini -p` subprocess backend. Note: no prompt caching, "
        "no JSON-schema-constrained output (we validate client-side and retry "
        "once on failure), reduced concurrency.\n"
    )
    sys.stderr.flush()


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
    runs_root = runs_root or _default_runs_root()
    result = fetch_pr(pr_url, runs_root)
    typer.echo(f"run directory: {result.run_dir}")


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
        )
    )
    typer.echo(f"wrote {path}")


@app.command()
def render(
    run_dir: Path = typer.Argument(...),
    out: Path = typer.Option(None, help="Output HTML path (default <run_dir>/review.html)."),
) -> None:
    """Render an augmented run directory as a self-contained HTML viewer."""
    from .viewer.render_html import render_run_dir

    out_path = out or (run_dir / "review.html")
    render_run_dir(run_dir, out_path)
    typer.echo(f"wrote {out_path}")


@app.command()
def run(
    pr_url: str = typer.Argument(...),
    runs_root: Path = typer.Option(
        None, help="Root directory for run artefacts (default: ~/.cache/scr/runs/<repo-fingerprint>/)."
    ),
    model: str = typer.Option(None),
    concurrency: int = typer.Option(8),
    no_cache: bool = typer.Option(False),
    backend: str = typer.Option(None),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Fetch + augment + render in one shot."""
    _configure_logging(verbose)
    from .augment.pipeline import augment_run_dir
    from .augment.prompts import PROMPT_VERSION
    from .viewer.render_html import render_run_dir

    backend = _CONFIG.resolve_backend(backend)
    model = _CONFIG.resolve_model(backend=backend, cli_value=model)
    runs_root = runs_root or _default_runs_root()
    fetch_result = fetch_pr(pr_url, runs_root)
    cache = None if no_cache else CacheStore(prompt_version=PROMPT_VERSION)
    client = _select_client(backend, model=model)
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
    render_run_dir(fetch_result.run_dir, out)
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
    )
    code = run_review(opts)
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
    import asyncio
    import json as _json
    from .augment.prompts import PROMPT_VERSION
    from .cache.store import CacheStore
    from .review.github import (
        GhError, list_review_requested_prs, pick_pr_interactive, post_inline_review, require_gh,
    )
    from .review.runner import serve_review
    from .fetch import fetch as fetch_pr

    backend = _CONFIG.resolve_backend(backend)
    model = _CONFIG.resolve_model(backend=backend, cli_value=model)

    # Preflight `gh` once — both PR resolution and the post step need it,
    # and a missing-tool error here is more informative than the same
    # error fired from inside fetch_pr.
    try:
        require_gh()
    except GhError as e:
        typer.echo(f"scr pr: {e}", err=True)
        raise typer.Exit(code=1)

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
    fetch_result = fetch_pr(pr_url, runs_root)
    run_dir = fetch_result.run_dir

    if augment:
        from .augment.pipeline import augment_run_dir

        cache = None if no_cache else CacheStore(root=cache_dir, prompt_version=PROMPT_VERSION)
        asyncio.run(
            augment_run_dir(
                run_dir,
                model=model,
                concurrency=concurrency,
                cache=cache,
                client=client,
            )
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
        port=port,
        timeout=timeout,
        open_browser=not no_open,
    )

    # Markdown to stdout for parity with `scr review` (the slash-command
    # downstream expects to read it). GitHub posting is in addition to,
    # not instead of, this.
    from .review.comments import format_markdown
    sys.stdout.write(format_markdown(result.comments, run_slug=run_dir.name))
    sys.stdout.flush()

    if not result.comments:
        # Nothing to post; exit clean.
        raise typer.Exit(code=0 if result.clean else 2)

    # Need the head SHA from meta.json so GitHub anchors the review at
    # the commit the reviewer actually saw.
    meta = _json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    head_sha = meta.get("headRefOid", "")
    if not head_sha:
        typer.echo("scr pr: meta.json is missing headRefOid; can't anchor review", err=True)
        raise typer.Exit(code=2)

    if not yes:
        sys.stderr.write(
            f"\nAbout to post {len(result.comments)} inline comment(s) as a "
            f"COMMENT review on {repo}#{number} (commit {head_sha[:8]}…).\n"
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
        post = post_inline_review(repo, number, head_sha, result.comments)
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
    if _CONFIG.env:
        typer.echo("[env]")
        for k, v in _CONFIG.env.items():
            applied = "applied" if os.environ.get(k) == v else "overridden by shell/.env"
            typer.echo(f"  {k} = {v!r} (from {_CONFIG.sources.get(f'env.{k}', '?')}, {applied})")


@config_app.command("edit")
def config_edit() -> None:
    """Open the user-level config in $EDITOR (or `vi`)."""
    import subprocess as _sp

    from .paths import default_config_path

    path = default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(_CONFIG_TEMPLATE, encoding="utf-8")
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
