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
import threading
import webbrowser
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..augment.agents import Client
from ..augment.prompts import PROMPT_VERSION
from ..cache.store import CacheStore
from ..fetch import materialize_local_diff_run
from ..format.parse import parse_augmented_diff
from ..paths import default_runs_root as _default_runs_root
from ..viewer.build_json import build_pending_viewer_json, build_viewer_json
from .comments import CommentStore, format_markdown
from .github import PostResult
from .server import PostCallable, ReviewServer

log = logging.getLogger(__name__)


#: Signature of the augment callable accepted by ``serve_review``. The
#: second argument is the publisher bound to the live review server's
#: SSE channel; pass it through to ``augment_run_dir(on_event=...)`` so
#: the pipeline can stream overview / per-hunk events to the page.
AugmentCallable = Callable[
    [Path, Callable[[str, dict], None]],
    Coroutine[Any, Any, None],
]


#: Signature of the on-demand fold-summary callable accepted by
#: ``serve_review``. The closure resolves the sidecar, calls the LLM
#: against the addressed file, persists the new ``FoldDescription``,
#: and returns the broadcast payload (the dict the server fans out as
#: an SSE event and sends back to the requesting tab). Wired up only
#: when an LLM backend is available (``opts.augment is True``);
#: ``--no-augment`` reviews leave this at ``None`` and the route
#: returns 409 unconditionally.
FoldSummaryCallable = Callable[
    # (file_idx, context, right_range, left_range, qualified_name, kind)
    [
        int,
        str,
        "tuple[int, int] | None",
        "tuple[int, int] | None",
        "str | None",
        "str | None",
    ],
    Coroutine[Any, Any, dict],
]


#: Signature of the streaming console turn driver accepted by
#: ``serve_review``. Called as ``(question, history, on_delta, on_tool,
#: cancel)`` and awaited to ``(answer_text, new_history)``:
#: ``on_delta(str)`` / ``on_tool(str)`` stream text and tool activity,
#: ``cancel`` is the ``threading.Event`` the driver polls between
#: chunks. Wired only when augmentation runs on an SDK backend;
#: ``--no-augment`` and CLI-subprocess reviews leave this ``None`` and
#: /console/ask 409s (CLI support is Slice 5).
ConsoleCallable = Callable[
    [
        str,
        "list | None",
        "Callable[[str], None]",
        "Callable[[str], None]",
        "threading.Event",
    ],
    Coroutine[Any, Any, "tuple[str, list]"],
]


@dataclass
class ReviewOptions:
    spec: str  # git ref or range, user-supplied
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
    # Optional file-loaded text for the extra-review pass. When set,
    # each hunk gets a second LLM call with this as the system prompt;
    # the returned line-anchored notes merge into hunk.line_notes.
    extra_review_prompt: str | None = None
    # Extra file globs to skip in the LLM passes (config [augment].skip_globs).
    skip_globs: tuple[str, ...] = ()
    show_progress: bool = True
    # `--debug` / SCR_DEBUG: surface each CLI-backend subprocess spawn (raw
    # argv + envelope) in the viewer's debug drawer.
    debug: bool = False


def run_review(opts: ReviewOptions) -> int:
    """Run a full review session. Returns the process exit code."""
    run_dir = materialize_local_diff_run(
        opts.spec,
        opts.runs_root,
        repo_root=opts.repo_root,
        no_staged=opts.no_staged,
        no_unstaged=opts.no_unstaged,
        spec_md_path=opts.spec_markdown,
    )

    augment_task: AugmentCallable | None = None
    fold_summary_task: FoldSummaryCallable | None = None
    console_task: ConsoleCallable | None = None
    bind_debug_sink: Callable[[Callable[[dict], None]], None] | None = None
    if opts.augment:
        from ..augment.pipeline import augment_run_dir  # lazy: anthropic SDK

        cache = None if opts.no_cache else CacheStore(root=opts.cache_dir, prompt_version=PROMPT_VERSION)

        async def _run_augment(rd: Path, publish: Callable[..., None]) -> None:
            await augment_run_dir(
                rd,
                model=opts.model,
                concurrency=opts.concurrency,
                cache=cache,
                client=opts.client,
                extra_review_prompt=opts.extra_review_prompt,
                skip_globs=opts.skip_globs,
                # The page now carries the progress display, so silence
                # the terminal meter — its redraw line would just fight
                # the listening-URL / per-hunk warning log lines.
                show_progress=False,
                on_event=publish,
            )

        augment_task = _run_augment

        fold_summary_task = _build_fold_summary_task(
            client=opts.client,
            model=opts.model,
            cache=cache,
            run_dir=run_dir,
        )

        # The console reuses the augment backend — SDK backends stream
        # token-by-token, CLI subprocess backends answer one-shot per turn
        # (ADR 0002, Slice 5). When opts.client is None the augment path
        # defaults to the Anthropic SDK, so we mirror that to construct
        # the console's client.
        console_client = opts.client or Client(model=f"anthropic:{opts.model}")
        console_task = _build_console_task(
            client=console_client,
            run_dir=run_dir,
        )
        # In --debug, surface the client driver's per-spawn records in the
        # viewer drawer. The augment pass shares this client, so its spawns
        # flow too; set_debug_sink no-ops on the SDK string-model path.
        if opts.debug:
            bind_debug_sink = lambda sink, c=console_client: c.set_debug_sink(sink)  # noqa: E731
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
        skip_globs=opts.skip_globs,
        fold_summary=fold_summary_task,
        console=console_task,
        port=opts.port,
        timeout=opts.timeout,
        open_browser=opts.open_browser,
        debug=opts.debug,
        bind_debug_sink=bind_debug_sink,
    )
    # The markdown dump is the reviewer's "new notes" feed — ingested
    # upstream comments are already on GitHub and would crowd it out.
    local_comments = [c for c in result.comments if c.source == "local"]
    markdown = format_markdown(local_comments, run_slug=run_dir.name)
    sys.stdout.write(markdown)
    sys.stdout.flush()
    return 0 if result.clean else 2


@dataclass
class ServeResult:
    """Outcome of `serve_review`. Returned in addition to the side
    effect of `comments.json` on disk so callers don't have to re-load
    it (and so each caller can decide what to do with the comments —
    `scr review` prints markdown, `scr pr` posts to GitHub).

    ``posted`` is set when the viewer's confirmation modal fired a
    successful /post-review (only possible when the caller supplied a
    ``post`` callback to ``serve_review``). None means "no post
    happened" — cancelled, no postable comments, or the caller wasn't
    in posting mode at all.
    """

    comments: list  # list[Comment] — kept loose to avoid an import cycle
    clean: bool  # True iff the viewer signalled Done within the timeout
    posted: PostResult | None = None


def serve_review(
    run_dir: Path,
    *,
    augment: AugmentCallable | None = None,
    skip_globs: tuple[str, ...] = (),
    fold_summary: FoldSummaryCallable | None = None,
    console: ConsoleCallable | None = None,
    post: PostCallable | None = None,
    post_meta: dict | None = None,
    port: int = 0,
    timeout: int = 3600,
    open_browser: bool = True,
    on_ready: Callable[[str], None] | None = None,
    debug: bool = False,
    bind_debug_sink: Callable[[Callable[[dict], None]], None] | None = None,
) -> ServeResult:
    """Render the viewer for a populated run dir, host the back-channel
    server, block on the user clicking Done, and return the comments
    they left.

    Both `cli.review` (local diff) and `cli.pr` (GitHub PR) call this
    with a run dir whose `meta.json`, `raw.diff`, and worktrees are
    already in place. If ``augment`` is supplied, the server starts
    immediately with a pending viewer (file/hunk structure visible,
    no annotations yet); the augmentation coroutine then runs while
    the page is live, publishing per-hunk SSE events as completions
    land. After the pass finishes, `update_viewer_json` swaps the
    `/data.json` payload to the augmented state and a `done` event
    flushes any still-pending placeholders. If ``augment`` is None,
    the run dir is expected to already contain ``augmented.diff``
    (the caller skipped augmentation upstream).
    """
    if augment is not None:
        # Pre-augment: a file/hunk skeleton so the page is responsive
        # while the LLM pass runs. The viewer JS sees `pending: true`
        # and shows "analysing…" placeholders for each hunk.
        viewer_json = build_pending_viewer_json(run_dir, skip_globs=skip_globs)
    else:
        viewer_json = _load_viewer_json(run_dir)
    srv = ReviewServer(
        run_dir=run_dir,
        viewer_json=viewer_json,
        port=port,
        post_callback=post,
        post_meta=post_meta,
        debug=debug,
    )
    srv.start()
    try:
        log.info("review server at %s", srv.url())
        sys.stderr.write(f"scr review: listening on {srv.url()}\n")
        sys.stderr.flush()
        # Route the CLI backend's per-spawn debug records to the viewer's
        # drawer. Bound before augmentation so its spawns are captured too.
        if debug and bind_debug_sink is not None:
            bind_debug_sink(lambda record: srv.publish("debug-log", record))
        if on_ready is not None:
            on_ready(srv.url())
        if open_browser:
            try:
                webbrowser.open(srv.url())
            except Exception as e:  # noqa: BLE001
                log.warning("could not open browser: %s", e)

        if augment is not None:
            # Run augmentation while the server is live, streaming each
            # overview / per-hunk completion to the page via SSE. After
            # the pass returns, swap `/data.json` to the augmented state
            # so any tab opened post-augment (or a manual reload) sees
            # the final view, then publish `done` so connected viewers
            # can finalise any still-pending placeholders.
            augment_error: BaseException | None = None
            try:
                asyncio.run(augment(run_dir, srv.publish))
            except BaseException as e:
                augment_error = e
                log.exception("augmentation failed; page stays on pending view")
                sys.stderr.write(f"scr review: augment failed: {e}\n")
            if (run_dir / "augmented.diff").exists():
                final_json = _load_viewer_json(run_dir)
                srv.update_viewer_json(final_json)
                # Augmentation has emitted a sidecar, so the /fold-summary
                # route can now resolve hunk_ids. Bind the summariser here
                # rather than at start() to prevent races against a tab
                # that opens before augmentation lands.
                if fold_summary is not None:
                    srv.set_fold_summariser(fold_summary)
                # Same gate as the fold summariser: the console needs the
                # sidecar on disk to ground its answers, so bind it here
                # rather than at start(). Unset for --no-augment / CLI
                # backends, where /console/ask stays 409.
                if console is not None:
                    srv.set_console_asker(console)
                srv.publish("done", {"reason": "augment-complete"})
            if augment_error is not None and not isinstance(augment_error, Exception):
                # KeyboardInterrupt / SystemExit shouldn't be swallowed —
                # re-raise after the page has its latest state pushed.
                raise augment_error

        clean = srv.wait_until_done(timeout=timeout)
    finally:
        srv.stop()

    store = CommentStore(run_dir / "comments.json")
    return ServeResult(
        comments=store.all(),
        clean=clean,
        posted=srv.ctx.posted_result,
    )


def _build_fold_summary_task(
    *,
    client: Client | None,
    model: str,
    cache: CacheStore | None,
    run_dir: Path,
) -> FoldSummaryCallable:
    """Construct the FoldSummaryCallable that ``serve_review`` installs
    onto the review server once augmentation completes. The closure
    captures the LLM backend + cache + run_dir so the server module
    stays independent of the augment-side machinery.
    """
    # Lazy import: keeps the SDK / pydantic-ai dep out of the
    # `--no-augment` path.
    from ..augment.fold_summary import apply_fold_summary_to_run

    async def task(
        file_idx: int,
        context: str,
        right_range: tuple[int, int] | None,
        left_range: tuple[int, int] | None,
        qualified_name: str | None = None,
        kind: str | None = None,
    ) -> dict:
        # client is None only when augment is False; in that path
        # serve_review never wires this task up, so a None here would
        # be a wiring bug — fail loudly.
        assert client is not None, "fold-summary task called without an LLM backend"
        return await apply_fold_summary_to_run(
            client,
            run_dir=run_dir,
            file_idx=file_idx,
            context=context,  # type: ignore[arg-type]
            right_range=right_range,
            left_range=left_range,
            qualified_name=qualified_name,
            kind=kind,
            model=model,
            cache=cache,
        )

    return task


def _build_console_task(
    *,
    client: Client,
    run_dir: Path,
) -> ConsoleCallable:
    """Construct the console turn driver ``serve_review`` installs once
    augmentation completes. Captures the LLM backend + run_dir so the
    server module stays independent of the augment-side machinery.
    """
    # Lazy import: keeps pydantic-ai off the `--no-augment` path.
    from ..augment.console import stream_console_turn

    async def task(
        question: str,
        history: list | None,
        on_delta: Callable[[str], None],
        on_tool: Callable[[str], None],
        cancel: threading.Event,
        selection: Any = None,
    ) -> tuple[str, list]:
        return await stream_console_turn(
            client,
            run_dir=run_dir,
            question=question,
            history=history,
            on_delta=on_delta,
            on_tool=on_tool,
            cancel=cancel,
            selection=selection,
        )

    return task


def _load_viewer_json(run_dir: Path) -> dict:
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    augmented = run_dir / "augmented.diff"
    if not augmented.exists():
        return {"version": "1", "pr": {}, "files": []}
    diff = parse_augmented_diff(augmented.read_text(encoding="utf-8"))
    head_dir = run_dir / "head"
    base_dir = run_dir / "base"
    return build_viewer_json(
        diff,
        meta,
        head_dir=head_dir if head_dir.exists() else None,
        base_dir=base_dir if base_dir.exists() else None,
    )
