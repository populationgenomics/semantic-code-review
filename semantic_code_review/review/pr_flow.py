"""The `scr pr` command: resolve a PR, materialise its run, detach the
review server.

Preflights ``gh``, resolves the PR number (picker or explicit),
materialises the [[run-directory]] and hands it to `runner.detach_server`,
which spawns the server as a detached child and prints the viewer's URL
and the run id (ADR 0009). The child — `scr pr … --serve-run <slug>` —
serves with GitHub as the [[counterpart]]: Sends go into the reviewer's
[[pending-review]] and Submit publishes it, all from the browser. Nothing
comes back to this process: it has returned by then.

The flow uses plain ``sys.stderr`` / ``sys.stdout`` for I/O so it's
testable without a Typer dependency. ``cli/pr.py`` is the CLI wrapper
that builds a :class:`PrFlowOptions` from command-line args and calls
:func:`run_pr_flow`, or `serve_pr_run` for the child.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass

from .. import paths
from ..fetch import GhFetchError, materialize_github_pr_run, preflight_gh
from . import pending_review, runner
from .config import ReviewConfig
from .github import GhError, list_review_requested_prs, pick_pr_interactive


@dataclass(frozen=True)
class PrFlowOptions:
    """All inputs the PR flow needs: which PR, plus the settings every
    review session shares.
    """

    repo: str
    number: int | None
    config: ReviewConfig


def run_pr_flow(opts: PrFlowOptions, *, argv: Sequence[str], foreground: bool = False) -> int:
    """Resolve the PR, materialise its run, serve it — detached, or with
    `foreground` in this process until the session ends. Returns the
    exit code.

    `argv` is this invocation's own arguments (`sys.argv[1:]`), which the
    child re-executes with `--serve-run` appended; it reads the PR off
    the run's `meta.json`, so a number the picker chose need not be in it.

    Exit codes:
      0 — the server is reachable; stdout ends `viewer: <url>`, `run_id: <slug>`
          (with `foreground`, begins with them, and the session has ended).
      1 — graceful user-abort: no PR picked.
      2 — error condition: missing ``gh``, fetch failed, or the server
          did not start (its log is printed).
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

    meta = json.loads(run_dir.meta.read_text(encoding="utf-8"))
    if not meta.get("headRefOid"):
        _err("scr pr: meta.json is missing headRefOid; can't anchor review")
        return 2
    if foreground:
        return runner.serve_foreground(
            run_dir, opts.config, argv=argv, program="scr pr", github=pending_review.for_run(run_dir)
        )
    return runner.detach_server(run_dir, opts.config, argv=argv, program="scr pr")


def serve_pr_run(run_dir: paths.RunDir, cfg: ReviewConfig, *, argv: Sequence[str]) -> int:
    """The detached server for a PR run: what `scr pr --serve-run` runs.
    GitHub is the counterpart; the pending review is read off the run's
    `meta.json`. `argv` is this process's own arguments, recorded for
    `scr runs restart`.
    """
    if not run_dir.meta.exists():
        _err(f"scr pr: {run_dir.path} is not a run directory")
        return 2
    try:
        sink = pending_review.for_run(run_dir)
    except ValueError as e:
        _err(f"scr pr: {e}")
        return 2
    return runner.serve_run(run_dir, cfg, argv=argv, github=sink)


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


def _err(msg: str) -> None:
    """Write ``msg`` to stderr, appending a newline if missing, then flush."""
    if not msg.endswith("\n"):
        msg = msg + "\n"
    sys.stderr.write(msg)
    sys.stderr.flush()


__all__ = ["PrFlowOptions", "run_pr_flow", "serve_pr_run"]
