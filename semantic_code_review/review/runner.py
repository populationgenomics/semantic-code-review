"""Orchestrate a ``scr review`` session end-to-end.

Given a git ref/range and optional spec markdown, synthesise a run
directory compatible with the existing augment + render pipeline,
optionally run the LLM augmentation, render the HTML, spawn the
ephemeral server, and drain reviewer comments to stdout when the
viewer signals done.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path

from ..augment.prompts import PROMPT_VERSION
from ..augment.runner import ClaudeClient
from ..cache.store import CacheStore
from ..format.parse import parse_augmented_diff
from ..viewer.build_json import build_viewer_json
from ..viewer.render_html import render_run_dir
from .comments import CommentStore, format_markdown
from .git import LocalDiff, build_local_diff
from .server import ReviewServer


log = logging.getLogger(__name__)


@dataclass
class ReviewOptions:
    spec: str                       # git ref or range, user-supplied
    spec_markdown: Path | None = None
    runs_root: Path = Path(".scr/runs")
    repo_root: Path | None = None
    no_staged: bool = False
    no_unstaged: bool = False
    augment: bool = True
    model: str = "claude-opus-4-7"
    concurrency: int = 8
    no_cache: bool = False
    cache_dir: Path | None = None
    open_browser: bool = True
    port: int = 0
    timeout: int = 3600
    # Optional preselected LLM client. None → augment_run_dir builds an
    # AnthropicClient itself (legacy behavior).
    client: ClaudeClient | None = None


def run_review(opts: ReviewOptions) -> int:
    """Run a full review session. Returns the process exit code."""
    diff = build_local_diff(
        opts.spec,
        repo_root=opts.repo_root,
        no_staged=opts.no_staged,
        no_unstaged=opts.no_unstaged,
    )

    run_dir = opts.runs_root / diff.slug
    run_dir.mkdir(parents=True, exist_ok=True)
    _populate_run_dir(run_dir, diff, spec_md=opts.spec_markdown)

    if opts.augment:
        from ..augment.pipeline import augment_run_dir  # lazy: anthropic SDK

        cache = None if opts.no_cache else CacheStore(
            root=opts.cache_dir, prompt_version=PROMPT_VERSION
        )
        asyncio.run(
            augment_run_dir(
                run_dir,
                model=opts.model,
                concurrency=opts.concurrency,
                cache=cache,
                client=opts.client,
            )
        )
    else:
        # When augment is skipped, copy raw.diff to augmented.diff so render
        # has something to parse. It'll have no annotations.
        (run_dir / "augmented.diff").write_text(
            (run_dir / "raw.diff").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

    result = serve_review(
        run_dir,
        port=opts.port,
        timeout=opts.timeout,
        open_browser=opts.open_browser,
    )
    markdown = format_markdown(result.comments, run_slug=diff.slug)
    sys.stdout.write(markdown)
    sys.stdout.flush()
    return 0 if result.clean else 2


@dataclass
class ServeResult:
    """Outcome of `serve_review`. Returned in addition to the side
    effect of `comments.json` on disk so callers don't have to re-load
    it (and so each caller can decide what to do with the comments —
    `scr review` prints markdown, `scr pr` posts to GitHub)."""
    comments: list  # list[Comment] — kept loose to avoid an import cycle
    clean: bool     # True iff the viewer signalled Done within the timeout


def serve_review(
    run_dir: "Path",
    *,
    port: int = 0,
    timeout: int = 3600,
    open_browser: bool = True,
) -> ServeResult:
    """Render the viewer for a populated run dir, host the back-channel
    server, block on the user clicking Done, and return the comments
    they left.

    Both `cli.review` (local diff) and `cli.pr` (GitHub PR) call this
    with a run dir whose `augmented.diff`, `meta.json`, and worktrees
    are already in place. The function is intentionally diff-source-
    agnostic: it doesn't know whether the run dir came from
    `build_local_diff` or `fetch_pr`.
    """
    html_path = run_dir / "review.html"
    viewer_json = _load_viewer_json(run_dir)
    srv = ReviewServer(
        run_dir=run_dir,
        html_path=html_path,
        viewer_json=viewer_json,
        port=port,
    )
    srv.start()
    try:
        render_run_dir(run_dir, html_path, session_endpoint=srv.url())
        log.info("review server at %s", srv.url())
        sys.stderr.write(f"scr review: listening on {srv.url()}\n")
        sys.stderr.flush()
        if open_browser:
            try:
                webbrowser.open(srv.url())
            except Exception as e:  # noqa: BLE001
                log.warning("could not open browser: %s", e)
        clean = srv.wait_until_done(timeout=timeout)
    finally:
        srv.stop()

    store = CommentStore(run_dir / "comments.json")
    return ServeResult(comments=store.all(), clean=clean)


def _populate_run_dir(run_dir: Path, diff: LocalDiff, *, spec_md: Path | None) -> None:
    (run_dir / "raw.diff").write_text(diff.raw_diff, encoding="utf-8")
    (run_dir / "files.txt").write_text("\n".join(diff.files) + ("\n" if diff.files else ""),
                                       encoding="utf-8")

    spec_text = ""
    if spec_md is not None:
        spec_text = spec_md.read_text(encoding="utf-8")
        (run_dir / "spec.md").write_text(spec_text, encoding="utf-8")

    title = "Local review: " + diff.slug.removeprefix("local-")
    body = ""
    if spec_text:
        body = "# Spec (ground truth)\n\n" + spec_text.strip() + "\n"
    meta = {
        "title": title,
        "body": body,
        "author": {"login": ""},
        "url": "",
        "baseRefOid": diff.base_sha,
        "headRefOid": diff.head_sha,
        "files": [{"path": p} for p in diff.files],
        "number": None,
        "labels": [],
        "additions": 0,
        "deletions": 0,
        "changedFiles": len(diff.files),
        "local": True,
        "mode": diff.mode,
        "head_is_working": diff.head_is_working,
    }
    (run_dir / "meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # Worktrees: for working-state mode, point head/ at the live repo via symlink
    # so RepoTools can grep/read the current state. For committed-only modes,
    # create real detached worktrees so the LLM sees the exact head-sha tree.
    _setup_worktrees(run_dir, diff)


def _setup_worktrees(run_dir: Path, diff: LocalDiff) -> None:
    repo_git_link = run_dir / "repo.git"
    if not repo_git_link.exists():
        _symlink(repo_git_link, diff.repo_git)

    head_link = run_dir / "head"
    base_dir = run_dir / "base"

    if diff.head_is_working:
        # The head "tree" is the live checkout; symlink it so RepoTools.read_file
        # hits the actual files the reviewer is editing.
        if not head_link.exists():
            _symlink(head_link, diff.head_worktree)
    else:
        # Committed-only mode — create a detached worktree at the resolved head SHA.
        if not head_link.exists():
            _git(diff.repo_git.parent, "worktree", "add", "--detach",
                 str(head_link.resolve()), diff.head_sha)

    # Base worktree (always real — we want the LLM to read pre-change code).
    if not base_dir.exists():
        # Strip synthetic suffixes from dirty head; base_sha is always real.
        _git(diff.repo_git.parent, "worktree", "add", "--detach",
             str(base_dir.resolve()), diff.base_sha)


def _symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target.resolve())
    except OSError:
        # Filesystems without symlink support (rare on macOS/Linux): create a
        # marker file with the target path. Tools that rely on the path will
        # fail more loudly and the user can switch modes.
        link.write_text(str(target.resolve()) + "\n", encoding="utf-8")


def _git(cwd: Path, *args: str) -> str:
    import subprocess
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


def _load_viewer_json(run_dir: Path) -> dict:
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    augmented = run_dir / "augmented.diff"
    if not augmented.exists():
        return {"version": "1", "pr": {}, "files": []}
    diff = parse_augmented_diff(augmented.read_text(encoding="utf-8"))
    head_dir = run_dir / "head"
    return build_viewer_json(diff, meta, head_dir=head_dir if head_dir.exists() else None)
