"""End-to-end GitHub PR review flow: resolve → fetch → serve → post.

Drives `scr pr`: preflights ``gh``, resolves the PR number (picker or
explicit), materialises a run directory, optionally runs the augment
pipeline, serves the viewer until the reviewer is done. Posting is
confirmed in the **viewer's modal**, not on the terminal — the
reviewer reviews comments inline, clicks Done, ticks/unticks the
final list, and confirms. The server fires the post callback on
their behalf and reports the result back via ``ServeResult.posted``.

The legacy terminal y/N flow lives behind ``--yes`` only as a way to
skip the modal entirely: the server stays out of posting mode and
the CLI posts after the viewer exits.

The flow uses plain ``sys.stderr`` / ``sys.stdout`` for I/O so it's
testable without a Typer dependency. ``cli/pr.py`` is the CLI wrapper
that builds a :class:`PrFlowOptions` from command-line args and calls
:func:`run_pr_flow`.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..augment.agents import Client
from ..fetch import GhFetchError, materialize_github_pr_run, preflight_gh
from .comments import CommentStore, format_markdown
from .github import (
    GhError,
    PostResult,
    comments_to_github,
    list_review_requested_prs,
    pick_pr_interactive,
)
from .github_graphql import post_review_via_graphql
from .runner import (
    _build_console_task,
    _build_fold_summary_task,
    serve_review,
)
from .server import PostCallable


@dataclass(frozen=True)
class PrFlowOptions:
    """All inputs the PR flow needs.

    ``model`` and ``client`` are caller-resolved (typically via the CLI's
    config + backend selection); ``extra_review_prompt`` is the
    already-resolved prompt text (None means none). ``yes`` bypasses the
    in-browser confirmation modal — the viewer's Done button stays a
    plain exit and the CLI posts everything after it returns.
    """

    repo: str
    number: int | None
    runs_root: Path
    augment: bool
    model: str
    concurrency: int
    no_cache: bool
    cache_dir: Path | None
    open_browser: bool
    port: int
    timeout: int
    extra_review_prompt: str | None
    client: Client | None
    yes: bool


def run_pr_flow(opts: PrFlowOptions) -> int:
    """Drive the PR review end-to-end. Returns the exit code.

    Exit codes:
      0 — review completed cleanly (no unresolved local comments).
      1 — graceful user-abort (no PR picked, posting cancelled, etc.).
      2 — error condition: missing ``gh``, fetch failed, post failed,
          or review completed with unresolved local comments.
    """
    try:
        preflight_gh()
    except GhFetchError as e:
        _err(f"scr pr: {e}")
        return 2

    number = opts.number
    if number is None:
        code, picked = _resolve_pr_number(opts.repo)
        if picked is None:
            return code or 1
        number = picked

    pr_url = f"https://github.com/{opts.repo}/pull/{number}"
    try:
        run_dir = materialize_github_pr_run(pr_url, opts.runs_root)
    except GhFetchError as e:
        _err(f"scr pr: {e}")
        return 2

    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    head_sha = meta.get("headRefOid", "")
    if not head_sha:
        _err("scr pr: meta.json is missing headRefOid; can't anchor review")
        return 2

    augment_task, fold_summary_task, console_task = _build_tasks(opts, run_dir)
    if not opts.augment:
        # Mirror cli/review.py's behaviour: copy raw → augmented so render
        # has something to parse when augment is skipped.
        (run_dir / "augmented.diff").write_text(
            (run_dir / "raw.diff").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

    # `--yes` skips the modal entirely — server stays out of posting
    # mode (Done = plain /exit) and the CLI does the post itself after
    # serve_review returns. Default mode wires the callback + meta so
    # the viewer's Done opens the confirm modal.
    post_callback: PostCallable | None = None
    post_meta: dict[str, Any] | None = None
    if not opts.yes:
        post_callback = _build_post_callback(opts.repo, number, run_dir)
        post_meta = {
            "repo": opts.repo,
            "number": number,
            "head_sha": head_sha,
        }

    result = serve_review(
        run_dir,
        augment=augment_task,
        fold_summary=fold_summary_task,
        console=console_task,
        post=post_callback,
        post_meta=post_meta,
        port=opts.port,
        timeout=opts.timeout,
        open_browser=opts.open_browser,
    )

    posted: PostResult | None = result.posted

    # CLI-side fallback for --yes: the server didn't post (we didn't
    # wire it for that), so post everything ourselves now.
    if posted is None and opts.yes:
        mapped = comments_to_github(result.comments)
        if not mapped:
            _err(
                "scr pr: no new local comments to post; "
                f"comments are in {run_dir / 'comments.json'}."
            )
            return 0 if result.clean else 2
        try:
            posted = post_review_via_graphql(opts.repo, number, mapped)
        except GhError as e:
            _err(f"scr pr: posting failed: {e}")
            _err(
                f"comments are still in {run_dir / 'comments.json'} — "
                "re-run with --no-augment to retry."
            )
            return 2

    if posted is not None:
        # Comments are on GitHub; the URL is the artefact. Keep stdout
        # minimal so a slash command (or any downstream LLM) doesn't
        # ingest the comment bodies and treat them as instructions.
        sys.stdout.write(f"# Posted to {posted.review_url}\n")
        word = "comment" if posted.posted == 1 else "comments"
        sys.stdout.write(f"_{posted.posted} {word} posted._\n")
        sys.stdout.flush()
        _err(f"scr pr: posted {posted.posted} comment(s) — {posted.review_url}")
        return 0 if result.clean else 2

    # No post happened — modal cancelled, tab closed, --no-augment with
    # no comments, etc. Dump the markdown so the user / a calling script
    # has a record of what was being reviewed.
    local_comments = [c for c in result.comments if c.source == "local"]
    sys.stdout.write(format_markdown(local_comments, run_slug=run_dir.name))
    sys.stdout.flush()
    return 0 if result.clean else 2


def _resolve_pr_number(repo: str) -> tuple[int | None, int | None]:
    """Pick a PR number when the caller didn't supply one.

    Returns ``(exit_code, number)``: on success ``(None, picked)``; on
    a graceful early exit ``(code, None)`` so the caller can return
    the code.
    """
    try:
        prs = list_review_requested_prs(repo)
    except GhError as e:
        _err(f"scr pr: {e}")
        return 1, None
    if not prs:
        _err(
            f"scr pr: no open PRs in {repo} are requesting your review. "
            "Pass an explicit PR number, or open the list on github.com."
        )
        return 1, None
    if len(prs) == 1:
        _err(f"scr pr: reviewing {repo}#{prs[0].number} — {prs[0].title}")
        return None, prs[0].number
    picked = pick_pr_interactive(repo, prs)
    if picked is None:
        _err("scr pr: no PR selected")
        return 1, None
    return None, picked


def _build_tasks(
    opts: PrFlowOptions, run_dir: Path,
) -> tuple[Callable | None, Callable | None, Callable | None]:
    """Build the augment + fold-summary + console closures, or ``(None,
    None, None)`` when augmentation is skipped (the console grounds its
    answers in the augment sidecar, so it's unavailable without it).
    """
    if not opts.augment:
        return None, None, None

    # Imports inside: anthropic SDK + augment pipeline are lazy-loaded so
    # `--no-augment` runs (and `scr --help`) don't pay the cost.
    from ..augment.pipeline import augment_run_dir
    from ..augment.prompts import PROMPT_VERSION
    from ..cache.store import CacheStore

    cache = None if opts.no_cache else CacheStore(
        root=opts.cache_dir, prompt_version=PROMPT_VERSION,
    )

    async def augment_task(rd: Path, publish: Callable[[str, dict[str, Any]], None]) -> None:
        await augment_run_dir(
            rd,
            model=opts.model,
            concurrency=opts.concurrency,
            cache=cache,
            client=opts.client,
            extra_review_prompt=opts.extra_review_prompt,
            # Page carries the progress display now; suppress the
            # terminal meter to avoid duplicate noise and to keep
            # the listening-URL / warning lines unobstructed.
            show_progress=False,
            on_event=publish,
        )

    fold_summary_task = _build_fold_summary_task(
        client=opts.client, model=opts.model, cache=cache, run_dir=run_dir,
    )
    # Console reuses the augment backend (SDK streams; CLI answers
    # one-shot). When opts.client is None augment defaults to the
    # Anthropic SDK, so mirror that for the console's client.
    console_client = opts.client or Client(model=f"anthropic:{opts.model}")
    console_task = _build_console_task(client=console_client, run_dir=run_dir)
    return augment_task, fold_summary_task, console_task


def _build_post_callback(
    repo: str, number: int, run_dir: Path,
) -> PostCallable:
    """Closure the server fires on /post-review.

    Reads the latest comments off ``comments.json`` (the store mutates
    throughout the session), keeps every local comment whose id is in
    ``selected_ids`` plus every non-local comment (needed for reply-
    parent ``node_id`` lookups in :func:`comments_to_github`), maps,
    and posts via GraphQL. Errors propagate; the server returns 500
    to the modal so the reviewer sees the failure and can retry.
    """
    def post(selected_ids: list[str]) -> PostResult:
        store = CommentStore(run_dir / "comments.json")
        all_comments = store.all()
        selected = set(selected_ids)
        filtered = [
            c for c in all_comments
            if c.source != "local" or c.id in selected
        ]
        mapped = comments_to_github(filtered)
        return post_review_via_graphql(repo, number, mapped)

    return post


def _err(msg: str) -> None:
    """Write ``msg`` to stderr, appending a newline if missing, then flush."""
    if not msg.endswith("\n"):
        msg = msg + "\n"
    sys.stderr.write(msg)
    sys.stderr.flush()


__all__ = ["PrFlowOptions", "run_pr_flow"]
