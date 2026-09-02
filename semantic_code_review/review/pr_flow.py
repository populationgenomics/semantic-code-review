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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..fetch import GhFetchError, materialize_github_pr_run, preflight_gh
from .comments import CommentStore, format_markdown
from .config import ReviewConfig
from .github import (
    GhError,
    PostResult,
    comments_to_github,
    list_review_requested_prs,
    pick_pr_interactive,
)
from .github_graphql import post_review_via_graphql
from .runner import build_server_tasks, ensure_augmented_diff, serve_review
from .session import PostCallable, PostOutcome


@dataclass(frozen=True)
class PrFlowOptions:
    """All inputs the PR flow needs: which PR, plus the settings every
    review session shares.

    ``yes`` bypasses the in-browser confirmation modal — the viewer's
    Done button stays a plain exit and the CLI posts everything after it
    returns.
    """

    repo: str
    number: int | None
    config: ReviewConfig
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
        run_dir = materialize_github_pr_run(pr_url, opts.config.runs_root)
    except GhFetchError as e:
        _err(f"scr pr: {e}")
        return 2

    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    head_sha = meta.get("headRefOid", "")
    if not head_sha:
        _err("scr pr: meta.json is missing headRefOid; can't anchor review")
        return 2

    tasks = build_server_tasks(run_dir, opts.config)
    if not opts.config.augment:
        ensure_augmented_diff(run_dir)

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
        opts.config,
        tasks,
        post=post_callback,
        post_meta=post_meta,
    )

    posted: PostOutcome | None = result.posted

    # CLI-side fallback for --yes: the server didn't post (we didn't
    # wire it for that), so post everything ourselves now.
    if posted is None and opts.yes:
        mapped = comments_to_github(result.comments)
        if not mapped:
            _err(f"scr pr: no new local comments to post; comments are in {run_dir / 'comments.json'}.")
            return 0 if result.clean else 2
        try:
            posted = post_review_via_graphql(
                opts.repo,
                number,
                mapped,
                diff_text=(run_dir / "raw.diff").read_text(encoding="utf-8"),
            )
            CommentStore(run_dir / "comments.json").mark_posted(posted.posted_node_ids)
        except GhError as e:
            _err(f"scr pr: posting failed: {e}")
            _err(f"comments are still in {run_dir / 'comments.json'} — re-run with --no-augment to retry.")
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


def _build_post_callback(
    repo: str,
    number: int,
    run_dir: Path,
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
        filtered = [c for c in all_comments if c.source != "local" or c.id in selected]
        mapped = comments_to_github(filtered)
        # The raw diff is what GitHub will thread against; anchors are
        # resolved to it before anything is written.
        raw_diff = (run_dir / "raw.diff").read_text(encoding="utf-8")
        result = post_review_via_graphql(repo, number, mapped, diff_text=raw_diff)
        store.mark_posted(result.posted_node_ids)
        return result

    return post


def _err(msg: str) -> None:
    """Write ``msg`` to stderr, appending a newline if missing, then flush."""
    if not msg.endswith("\n"):
        msg = msg + "\n"
    sys.stderr.write(msg)
    sys.stderr.flush()


__all__ = ["PrFlowOptions", "run_pr_flow"]
