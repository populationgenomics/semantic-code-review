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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import git_ops
from ..augment.agents import Client
from ..augment.prompts import PROMPT_VERSION
from ..cache.store import CacheStore
from ..format.parse import parse_augmented_diff
from ..paths import default_runs_root as _default_runs_root
from ..viewer.build_json import build_pending_viewer_json, build_viewer_json
from ..viewer.render_html import render_run_dir
from .comments import CommentStore, format_markdown
from .git import LocalDiff, build_local_diff
from .server import ReviewServer


log = logging.getLogger(__name__)


#: Signature of the augment callable accepted by ``serve_review``. The
#: second argument is the publisher bound to the live review server's
#: SSE channel; pass it through to ``augment_run_dir(on_event=...)`` so
#: the pipeline can stream overview / per-hunk events to the page.
AugmentCallable = Callable[
    [Path, Callable[[str, dict], None]],
    Awaitable[None],
]


#: Signature of the on-demand fold-summary callable accepted by
#: ``serve_review``. The closure does the actual LLM call (with the
#: backend that augment_run_dir is wired against) given the file
#: identifiers + line ranges the server resolved from the request.
#: Wired up only when an LLM backend is available (i.e.
#: ``opts.augment is True``); ``--no-augment`` reviews leave this
#: at ``None`` and the route returns 409 unconditionally.
FoldSummaryCallable = Callable[
    # (file_path, file_summary, overview_json, context, right_range, left_range)
    [str, str, str, str, "tuple[int, int] | None", "tuple[int, int] | None"],
    Awaitable[str],
]


@dataclass
class ReviewOptions:
    spec: str                       # git ref or range, user-supplied
    spec_markdown: Path | None = None
    runs_root: Path = field(default_factory=_default_runs_root)
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
    # Optional preselected backend handle. None → augment_run_dir
    # defaults to a `Client` for the Anthropic SDK path.
    client: Client | None = None
    show_progress: bool = True


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

    augment_task: AugmentCallable | None = None
    fold_summary_task: FoldSummaryCallable | None = None
    if opts.augment:
        from ..augment.pipeline import augment_run_dir  # lazy: anthropic SDK

        cache = None if opts.no_cache else CacheStore(
            root=opts.cache_dir, prompt_version=PROMPT_VERSION
        )

        async def augment_task(rd: Path, publish) -> None:  # noqa: F811 — closes over opts
            await augment_run_dir(
                rd,
                model=opts.model,
                concurrency=opts.concurrency,
                cache=cache,
                client=opts.client,
                # The page now carries the progress display, so silence
                # the terminal meter — its redraw line would just fight
                # the listening-URL / per-hunk warning log lines.
                show_progress=False,
                on_event=publish,
            )

        fold_summary_task = _build_fold_summary_task(
            client=opts.client, model=opts.model, cache=cache, run_dir=run_dir,
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
        augment=augment_task,
        fold_summary=fold_summary_task,
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
    augment: AugmentCallable | None = None,
    fold_summary: FoldSummaryCallable | None = None,
    port: int = 0,
    timeout: int = 3600,
    open_browser: bool = True,
) -> ServeResult:
    """Render the viewer for a populated run dir, host the back-channel
    server, block on the user clicking Done, and return the comments
    they left.

    Both `cli.review` (local diff) and `cli.pr` (GitHub PR) call this
    with a run dir whose `meta.json`, `raw.diff`, and worktrees are
    already in place. If ``augment`` is supplied, the server starts
    immediately with a pending viewer (file/hunk structure visible,
    no annotations yet); the augmentation coroutine then runs while
    the page is live, and a ``reload`` SSE event flushes the completed
    state to any connected clients. If ``augment`` is None, the run
    dir is expected to already contain ``augmented.diff`` (the caller
    skipped augmentation upstream).
    """
    html_path = run_dir / "review.html"
    if augment is not None:
        # Pre-augment: render the file/hunk skeleton so the page is
        # responsive while the LLM pass runs. The viewer JS sees
        # `pending: true` and shows "analysing…" placeholders.
        viewer_json = build_pending_viewer_json(run_dir)
    else:
        viewer_json = _load_viewer_json(run_dir)
    srv = ReviewServer(
        run_dir=run_dir,
        html_path=html_path,
        viewer_json=viewer_json,
        port=port,
    )
    srv.start()
    try:
        render_run_dir(
            run_dir, html_path,
            session_endpoint=srv.url(),
            override_data=viewer_json if augment is not None else None,
        )
        log.info("review server at %s", srv.url())
        sys.stderr.write(f"scr review: listening on {srv.url()}\n")
        sys.stderr.flush()
        if open_browser:
            try:
                webbrowser.open(srv.url())
            except Exception as e:  # noqa: BLE001
                log.warning("could not open browser: %s", e)

        if augment is not None:
            # Run augmentation while the server is live, streaming each
            # overview / per-hunk completion to the page via SSE. After
            # the pass returns, refresh `/data.json` and the on-disk
            # HTML so a fresh tab opened post-augment also sees the
            # full state, then publish `done` so connected viewers can
            # finalise any still-pending placeholders.
            augment_error: BaseException | None = None
            try:
                asyncio.run(augment(run_dir, srv.publish))
            except BaseException as e:  # noqa: BLE001
                augment_error = e
                log.exception("augmentation failed; page stays on pending view")
                sys.stderr.write(f"scr review: augment failed: {e}\n")
            if (run_dir / "augmented.diff").exists():
                final_json = _load_viewer_json(run_dir)
                render_run_dir(run_dir, html_path, session_endpoint=srv.url())
                srv.update_viewer_json(final_json)
                # Augmentation has emitted a sidecar, so the /fold-summary
                # route can now resolve hunk_ids. Bind the summariser here
                # rather than at start() to prevent races against a tab
                # that opens before augmentation lands.
                if fold_summary is not None:
                    srv.set_fold_summariser(fold_summary)
                srv.publish("done", {"reason": "augment-complete"})
            if augment_error is not None and not isinstance(augment_error, Exception):
                # KeyboardInterrupt / SystemExit shouldn't be swallowed —
                # re-raise after the page has its latest state pushed.
                raise augment_error

        clean = srv.wait_until_done(timeout=timeout)
    finally:
        srv.stop()

    store = CommentStore(run_dir / "comments.json")
    return ServeResult(comments=store.all(), clean=clean)


def _build_fold_summary_task(
    *, client: Client | None, model: str, cache: CacheStore | None,
    run_dir: Path,
) -> FoldSummaryCallable:
    """Construct the FoldSummaryCallable that ``serve_review`` installs
    onto the review server once augmentation completes. The closure
    captures the LLM backend + cache + run_dir so the server module
    stays independent of the augment-side machinery.
    """
    # Lazy import: keeps the SDK / pydantic-ai dep out of the
    # `--no-augment` path.
    from ..augment.fold_summary import summarise_fold

    async def task(
        file_path: str, file_summary: str, overview_json: str,
        context: str,
        right_range: "tuple[int, int] | None",
        left_range: "tuple[int, int] | None",
    ) -> str:
        # client is None only when augment is False; in that path
        # serve_review never wires this task up, so a None here would
        # be a wiring bug — fail loudly.
        assert client is not None, "fold-summary task called without an LLM backend"
        return await summarise_fold(
            client,
            run_dir=run_dir,
            file_path=file_path,
            file_summary=file_summary,
            overview_json=overview_json,
            context=context,  # type: ignore[arg-type]
            right_range=right_range,
            left_range=left_range,
            model=model, cache=cache,
        )

    return task


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
            git_ops.worktree_add(
                diff.repo_git.parent, head_link.resolve(), diff.head_sha,
            )

    # Base worktree (always real — we want the LLM to read pre-change code).
    if not base_dir.exists():
        # Strip synthetic suffixes from dirty head; base_sha is always real.
        git_ops.worktree_add(
            diff.repo_git.parent, base_dir.resolve(), diff.base_sha,
        )


def _symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target.resolve())
    except OSError:
        # Filesystems without symlink support (rare on macOS/Linux): create a
        # marker file with the target path. Tools that rely on the path will
        # fail more loudly and the user can switch modes.
        link.write_text(str(target.resolve()) + "\n", encoding="utf-8")


def _load_viewer_json(run_dir: Path) -> dict:
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    augmented = run_dir / "augmented.diff"
    if not augmented.exists():
        return {"version": "1", "pr": {}, "files": []}
    diff = parse_augmented_diff(augmented.read_text(encoding="utf-8"))
    head_dir = run_dir / "head"
    return build_viewer_json(diff, meta, head_dir=head_dir if head_dir.exists() else None)
