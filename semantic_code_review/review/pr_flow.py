"""End-to-end GitHub PR review flow: resolve → fetch → serve → post.

Drives `scr pr`: preflights ``gh``, resolves the PR number (picker or
explicit), materialises a run directory, optionally runs the augment
pipeline, serves the viewer until the reviewer hits Done, then posts
the accumulated comments back to GitHub as a single review.

The flow uses plain ``sys.stderr`` / ``sys.stdin`` / ``sys.stdout`` for
I/O so it's testable without a Typer dependency. ``cli/pr.py`` is the
CLI wrapper that builds a :class:`PrFlowOptions` from command-line
args and calls :func:`run_pr_flow`.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..augment.agents import Client
from ..fetch import GhFetchError, materialize_github_pr_run, preflight_gh
from .comments import format_markdown
from .github import (
    GhError, comments_to_github, list_review_requested_prs, pick_pr_interactive,
)
from .github_graphql import post_review_via_graphql
from .runner import _build_fold_summary_task, serve_review


@dataclass(frozen=True)
class PrFlowOptions:
    """All inputs the PR flow needs.

    ``model`` and ``client`` are caller-resolved (typically via the CLI's
    config + backend selection); ``extra_review_prompt`` is the
    already-resolved prompt text (None means none). ``yes`` skips the
    confirmation prompt before posting.
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
      1 — graceful user-abort (no PR picked, post cancelled, etc.).
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

    augment_task, fold_summary_task = _build_tasks(opts, run_dir)
    if not opts.augment:
        # Mirror cli/review.py's behaviour: copy raw → augmented so render
        # has something to parse when augment is skipped.
        (run_dir / "augmented.diff").write_text(
            (run_dir / "raw.diff").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

    result = serve_review(
        run_dir,
        augment=augment_task,
        fold_summary=fold_summary_task,
        port=opts.port,
        timeout=opts.timeout,
        open_browser=opts.open_browser,
    )

    # Markdown to stdout for parity with `scr review` (the slash-command
    # downstream expects to read it). Only the *new* (session-local)
    # comments belong in the markdown — re-printing every ingested
    # upstream comment would drown the reviewer's actual notes.
    local_comments = [c for c in result.comments if c.source == "local"]
    sys.stdout.write(format_markdown(local_comments, run_slug=run_dir.name))
    sys.stdout.flush()

    # Need the head SHA from meta.json so GitHub anchors the review at
    # the commit the reviewer actually saw.
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    head_sha = meta.get("headRefOid", "")
    if not head_sha:
        _err("scr pr: meta.json is missing headRefOid; can't anchor review")
        return 2

    # Map + filter once: ingested comments drop out, local replies to
    # ingested threads become reply entries. The prompt + the post both
    # work off this filtered list so the count we promise matches what
    # we actually send.
    mapped = comments_to_github(result.comments)
    if not mapped:
        _err(
            "scr pr: no new local comments to post; "
            f"comments are in {run_dir / 'comments.json'}."
        )
        return 0 if result.clean else 2

    if not opts.yes and not _confirm_post(opts.repo, number, mapped, head_sha, run_dir):
        return 1

    try:
        post = post_review_via_graphql(opts.repo, number, mapped)
    except GhError as e:
        _err(f"scr pr: posting failed: {e}")
        _err(
            f"comments are still in {run_dir / 'comments.json'} — "
            "re-run with --no-augment to retry."
        )
        return 2

    _err(f"scr pr: posted {post.posted} comment(s) — {post.review_url}")
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
) -> tuple[Callable | None, Callable | None]:
    """Build the augment + fold-summary closures, or ``(None, None)``."""
    if not opts.augment:
        return None, None

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
    return augment_task, fold_summary_task


def _confirm_post(
    repo: str, number: int, mapped: list, head_sha: str, run_dir: Path,
) -> bool:
    """Interactive y/N prompt. Returns ``True`` iff the user confirmed."""
    n_threads = sum(1 for c in mapped if not c.is_reply)
    n_replies = len(mapped) - n_threads
    descr_parts: list[str] = []
    if n_threads:
        descr_parts.append(f"{n_threads} new thread{'s' if n_threads != 1 else ''}")
    if n_replies:
        descr_parts.append(f"{n_replies} repl{'ies' if n_replies != 1 else 'y'}")
    descr = " + ".join(descr_parts)

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
        return False
    return True


def _err(msg: str) -> None:
    """Write ``msg`` to stderr, appending a newline if missing, then flush."""
    if not msg.endswith("\n"):
        msg = msg + "\n"
    sys.stderr.write(msg)
    sys.stderr.flush()


__all__ = ["PrFlowOptions", "run_pr_flow"]
