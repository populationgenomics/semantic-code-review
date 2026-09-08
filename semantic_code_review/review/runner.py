"""Orchestrate a review session end-to-end.

Two processes share the work (ADR 0009). The one the user invoked
materialises the [[run-directory]] — `run_review` from a git ref/range
and optional spec markdown, `pr_flow.run_pr_flow` from a GitHub PR —
then re-executes itself detached as the review server
(`detach_server` → `serve_run`) and returns once that child has written
`server.json`, printing the viewer's URL and the run id. The child
augments, serves the viewer, opens the browser, and lives until the tab
has been gone for the idle period; `scr review --wait <run_id>` reaches
it through `server.json`. `serve_review` is the serving core, with
Claude or GitHub as the [[counterpart]].
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import threading
import webbrowser
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import paths
from ..augment.agents import Client
from ..augment.prompts import PROMPT_VERSION
from ..cache.store import CacheStore
from ..fetch import materialize_local_diff_run
from ..format.parse import parse_augmented_diff
from ..viewer.build_json import build_pending_viewer_json, build_viewer_json
from . import identity, servers
from .comments import Comment, CommentStore
from .config import ReviewConfig
from .pending_review import ReviewSink
from .server import ReviewServer
from .session import (
    ConsoleCallable,
    Counterpart,
    ExplainerCallable,
    ExplainerSectionCallable,
    FoldSummaryCallable,
    ServerTasks,
)

log = logging.getLogger(__name__)


@dataclass
class ReviewOptions:
    """Everything `scr review` needs: where the diff comes from, plus the
    settings every review session shares.
    """

    spec: str  # git ref or range, user-supplied
    config: ReviewConfig
    # Optional second endpoint (two-endpoint form). When set, `spec` and
    # `spec_right` are the left/right endpoints — two refs or two rev:path.
    spec_right: str | None = None
    spec_markdown: Path | None = None
    repo_root: Path | None = None
    no_staged: bool = False
    no_unstaged: bool = False


def build_server_tasks(run_dir: paths.RunDir, cfg: ReviewConfig) -> ServerTasks:
    """Build the augment + fold-summary + console + explainer closures plus
    a debug-sink binder, or an all-`None` bundle when augmentation is
    skipped (the console grounds its answers in the augment sidecar, so
    it's unavailable without it).

    The binder, present only in `--debug`, lets the server route the CLI
    driver's per-spawn records to its SSE fan-out (see `serve_review`).
    """
    if not cfg.augment:
        return ServerTasks()

    from ..augment.pipeline import augment_run_dir  # lazy: anthropic SDK

    augment_cfg = cfg.for_augment()
    cache = None if cfg.no_cache else CacheStore(root=cfg.cache_dir, prompt_version=PROMPT_VERSION)

    async def augment_task(rd: paths.RunDir, publish: Callable[..., None]) -> None:
        await augment_run_dir(
            rd,
            augment_cfg,
            client=cfg.client,
            cache=cache,
            on_event=publish,
        )

    # The console reuses the augment backend — SDK backends stream
    # token-by-token, CLI subprocess backends answer one-shot per turn
    # (ADR 0002, Slice 5). When cfg.client is None the augment path
    # defaults to the Anthropic SDK, so we mirror that to construct
    # the console's client.
    console_client = cfg.client or Client(model=f"anthropic:{cfg.model}")
    bind_debug_sink: Callable[[Callable[[dict], None]], None] | None = None
    # In --debug, surface the client driver's per-spawn records in the
    # viewer drawer. The augment pass shares this client, so its spawns
    # flow too; set_debug_sink no-ops on the SDK string-model path.
    if cfg.debug:
        bind_debug_sink = lambda sink, c=console_client: c.set_debug_sink(sink)  # noqa: E731

    # Both explainer generators or neither: a skeleton without the
    # per-section pass yields a document whose every prose section
    # refuses, and the refusal a bare `None` produces reads "augmentation
    # still in progress" long after augmentation has finished.
    explainer_task: ExplainerCallable | None = None
    explainer_section_task: ExplainerSectionCallable | None = None
    if cfg.explainer:
        explainer_task = _build_explainer_task(
            client=cfg.client,
            model=cfg.model,
            cache=cache,
            run_dir=run_dir,
            house_style=cfg.explainer_prompt,
        )
        explainer_section_task = _build_explainer_section_task(
            client=cfg.client,
            model=cfg.model,
            cache=cache,
            run_dir=run_dir,
            house_style=cfg.explainer_prompt,
        )

    return ServerTasks(
        augment=augment_task,
        fold_summary=_build_fold_summary_task(
            client=cfg.client,
            model=cfg.model,
            cache=cache,
            run_dir=run_dir,
        ),
        console=_build_console_task(client=console_client, run_dir=run_dir),
        explainer=explainer_task,
        explainer_section=explainer_section_task,
        bind_debug_sink=bind_debug_sink,
    )


def run_review(opts: ReviewOptions, *, argv: Sequence[str]) -> int:
    """Materialise the run from a git ref/range, detach the review server,
    print the run id. See `detach_server` for the child and the exit code.
    """
    cfg = opts.config
    run_dir = materialize_local_diff_run(
        opts.spec,
        cfg.runs_root,
        right=opts.spec_right,
        repo_root=opts.repo_root,
        no_staged=opts.no_staged,
        no_unstaged=opts.no_unstaged,
        spec_md_path=opts.spec_markdown,
    )
    return detach_server(run_dir, cfg, argv=argv, program="scr review")


def detach_server(run_dir: paths.RunDir, cfg: ReviewConfig, *, argv: Sequence[str], program: str) -> int:
    """Spawn the review server for a materialised run as a detached
    child, wait until it is reachable, print the viewer's URL and the run
    id, return.

    `argv` is this invocation's own arguments after the program name
    (`sys.argv[1:]`); the child is the same interpreter re-executing them
    with `--runs-root <resolved>` and `--serve-run <slug>` appended, so
    every option the user gave — backend, model, `--no-open`,
    `--timeout` — reaches the server without being re-encoded. Stdout
    ends with `run_id: <slug>`; `viewer: <url>` precedes it. `program`
    names the command in what goes to stderr.

    A server already holding the run is reused when it is this build
    (`servers.clear_for`); one of another build is stopped first, and a
    stale record removed.

    Returns the process exit code: 0 once the server is reachable, 2 when
    the child exits before writing `server.json` (its log is printed).
    """
    existing = servers.clear_for(run_dir, this=identity.this_build(), program=program, err=sys.stderr)
    if existing is not None:
        sys.stderr.write(f"{program}: a server already holds this run at {existing.url}\n")
        sys.stderr.flush()
        _print_run_id(run_dir, existing.url)
        return 0

    child = servers.spawn(run_dir, serve_argv(argv, cfg, run_dir), cwd=os.getcwd())
    info = servers.await_server_info(run_dir, child)
    if info is None:
        sys.stderr.write(f"{program}: the review server did not start; its log is {run_dir.server_log}:\n")
        sys.stderr.write(_tail(run_dir.server_log))
        sys.stderr.flush()
        return 2
    _print_run_id(run_dir, info.url)
    return 0


def serve_argv(argv: Sequence[str], cfg: ReviewConfig, run_dir: paths.RunDir) -> list[str]:
    """The arguments that make an invocation's `argv` the server for
    `run_dir`: `--runs-root <resolved> --serve-run <slug>` appended. What
    the detached child runs, and what `server.json` records for `scr
    runs restart`.
    """
    return [*argv, "--runs-root", str(cfg.runs_root), servers.SERVE_RUN_FLAG, run_dir.slug]


def serve_run(
    run_dir: paths.RunDir, cfg: ReviewConfig, *, argv: Sequence[str], github: ReviewSink | None = None
) -> int:
    """The detached server: serve an already-materialised run until the
    tab has been gone for the idle period. What `--serve-run` runs.

    With `github` the counterpart is GitHub (`scr pr`): the run's pending
    review is resumed before serving and Sends deliver into it. Without,
    Claude is: the comments reach it through `scr review --wait`.

    `argv` is this process's own arguments after the program name
    (`sys.argv[1:]`), recorded in `server.json` for `scr runs restart`.

    Prints nothing on stdout — nothing here is for the caller's process.
    """
    if not run_dir.meta.exists():
        sys.stderr.write(f"scr: {run_dir.path} is not a run directory\n")
        return 2
    # A SIGTERM (a `kill` of the pid in server.json) ends the session the
    # way the idle clock does: `serve_review`'s finally stops the server
    # and removes the record, so the next `--wait` reads `ended` rather
    # than meeting a stale record.
    signal.signal(signal.SIGTERM, _exit_on_sigterm)
    tasks = build_server_tasks(run_dir, cfg)
    if not cfg.augment:
        ensure_augmented_diff(run_dir)
    serve_review(
        run_dir, cfg, tasks, counterpart="github" if github is not None else "claude", argv=argv, github=github
    )
    return 0


def _exit_on_sigterm(signum: int, frame: object) -> None:
    raise SystemExit(0)


def _print_run_id(run_dir: paths.RunDir, url: str) -> None:
    """The two stdout lines a caller reads: the viewer's URL, then the run
    id last, as `run_id: <slug>`.
    """
    sys.stdout.write(f"viewer: {url}\n")
    sys.stdout.write(f"run_id: {run_dir.slug}\n")
    sys.stdout.flush()


def _tail(path: Path, lines: int = 20) -> str:
    try:
        return "".join(path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)[-lines:])
    except OSError:
        return ""


@dataclass
class ServeResult:
    """Outcome of `serve_review`: the comments as the session left them,
    and whether it ended on `/exit` (True) or the idle clock (False).
    """

    comments: list[Comment]
    clean: bool


def ensure_augmented_diff(run_dir: paths.RunDir) -> None:
    """Give the renderer an ``augmented.diff`` to parse without spending
    an augmentation pass.

    The ``--no-augment`` path calls this: an absent one is filled with
    ``raw.diff``, annotation-free. An existing one is left alone. Run
    dirs are keyed by head SHA, so re-running ``--no-augment`` on a PR
    lands in the run dir a paid-for pass already augmented; overwriting
    it would drop those annotations and desync the text form from
    ``augmented.scr.json``.
    """
    if run_dir.augmented.exists():
        return
    run_dir.augmented.write_text(run_dir.raw_diff.read_text(encoding="utf-8"), encoding="utf-8")


def serve_review(
    run_dir: paths.RunDir,
    cfg: ReviewConfig,
    tasks: ServerTasks,
    *,
    counterpart: Counterpart,
    argv: Sequence[str],
    github: ReviewSink | None = None,
    on_ready: Callable[[str], None] | None = None,
) -> ServeResult:
    """Render the viewer for a populated run dir, host the back-channel
    server, block until the session ends, and return the comments left.

    The session ends once the server has sat idle — no request, no open
    viewer, no `--wait` attached — for `cfg.timeout` seconds (or on
    `POST /exit`). While it runs, `server.json` in the run dir says how
    to reach it, which build it is and how to start it again (`argv`
    is what `scr runs restart` re-executes; see `ReviewServer`); it is
    removed on the way out.

    `github` is the [[pending-review]] when the counterpart is GitHub; an
    existing one is resumed before the server binds, so the first
    `/comments` a tab fetches already holds what it carried.

    `serve_run` calls this with a run dir whose `meta.json`, `raw.diff`,
    and worktrees are already in place. If ``tasks.augment`` is supplied, the server starts
    immediately with a pending viewer (file/hunk structure visible,
    no annotations yet); the augmentation coroutine then runs while
    the page is live, publishing per-hunk SSE events as completions
    land. After the pass finishes, `ReviewSession.attach` swaps the
    `/data.json` payload to the augmented state and unlocks the tasks
    that read the sidecar, and a `done` event flushes any still-pending
    placeholders. If it is None, the run dir
    is expected to already contain ``augmented.diff`` (the caller
    skipped augmentation upstream).
    """
    augment = tasks.augment
    if augment is not None:
        # Pre-augment: a file/hunk skeleton so the page is responsive
        # while the LLM pass runs. The viewer JS sees `pending: true`
        # and shows "analysing…" placeholders for each hunk.
        viewer_json = build_pending_viewer_json(run_dir, skip_globs=cfg.skip_globs)
    else:
        viewer_json = _load_viewer_json(run_dir)
    srv = ReviewServer(
        run_dir=run_dir,
        viewer_json=viewer_json,
        counterpart=counterpart,
        argv=argv,
        github=github,
        port=cfg.port,
        debug=cfg.debug,
        # Known at construction, not at wire-up: the viewer needs to
        # decide whether to mount the overview-mode button on its first
        # /data.json, well before augmentation has finished.
        explainer=tasks.explainer is not None,
    )
    if github is not None:
        srv.session.resume_pending_review()
    srv.start()
    try:
        log.info("review server at %s", srv.url())
        sys.stderr.write(f"scr review: listening on {srv.url()}\n")
        sys.stderr.flush()
        _write_server_json(run_dir, srv)
        # Route the CLI backend's per-spawn debug records to the viewer's
        # drawer. Bound before augmentation so its spawns are captured too.
        if cfg.debug and tasks.bind_debug_sink is not None:
            tasks.bind_debug_sink(lambda record: srv.publish("debug-log", record))
        if on_ready is not None:
            on_ready(srv.url())
        if cfg.open_browser:
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
            if run_dir.augmented.exists():
                # The sidecar is on disk: the session can serve the
                # augmented view and every task that needs to resolve
                # the diff. Handed over here rather than at start() so a
                # tab that opens mid-pass sees them refuse rather than
                # resolve half a diff.
                srv.attach(tasks, _load_viewer_json(run_dir))
                srv.publish("done", {"reason": "augment-complete"})
            if augment_error is not None and not isinstance(augment_error, Exception):
                # KeyboardInterrupt / SystemExit shouldn't be swallowed —
                # re-raise after the page has its latest state pushed.
                raise augment_error

        # Idle seconds, not a session lifetime: the server shuts down once
        # it has gone that long with neither a request nor a connected
        # viewer.
        clean = srv.wait_until_done(timeout=cfg.timeout)
        if not clean:
            # Both CLI entry points come through here, so the line lands
            # once, ahead of whatever they print about the comments.
            sys.stderr.write(
                f"scr review: idle timeout — {cfg.timeout}s with no request and no open viewer; shutting down.\n"
            )
            sys.stderr.flush()
    finally:
        srv.stop()
        run_dir.server_json.unlink(missing_ok=True)

    store = CommentStore(run_dir.comments)
    return ServeResult(comments=store.all(), clean=clean)


def _write_server_json(run_dir: paths.RunDir, srv: ReviewServer) -> None:
    """Record the server (`stream.ServerInfo`). Written whole then
    renamed, so a reader never sees a torn record.
    """
    tmp = run_dir.server_json.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(srv.info.to_json(), indent=2), encoding="utf-8")
    os.replace(tmp, run_dir.server_json)


def _build_fold_summary_task(
    *,
    client: Client | None,
    model: str,
    cache: CacheStore | None,
    run_dir: paths.RunDir,
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


def _build_explainer_task(
    *,
    client: Client | None,
    model: str,
    cache: CacheStore | None,
    run_dir: paths.RunDir,
    house_style: str | None,
) -> ExplainerCallable:
    """Construct the change-explainer generator ``serve_review`` installs
    once augmentation completes. Captures the LLM backend + cache +
    run_dir so the server module stays independent of the augment-side
    machinery.

    ``house_style`` is the reviewed repo's ``[augment].explainer_prompt``
    text (or ``--explainer-prompt``), appended to the explainer passes'
    guidance. It has no default: ``scr review`` and ``scr pr`` build
    their bundles independently, and that is how the per-section
    generator came to be missing from the PR path — an omission here is
    a ``TypeError`` rather than a silently unstyled document.
    """
    # Lazy import: keeps pydantic-ai off the `--no-augment` path.
    from ..augment.explainer import document_to_payload, generate_explainer_skeleton

    async def task() -> dict:
        # client is None only when augment is False, and serve_review
        # never wires this task up in that case — a None here is a
        # wiring bug, so fail loudly.
        assert client is not None, "explainer task called without an LLM backend"
        doc = await generate_explainer_skeleton(
            client,
            run_dir=run_dir,
            model=model,
            house_style=house_style,
            cache=cache,
            trace_dir=run_dir.trace,
        )
        return document_to_payload(doc)

    return task


def _build_explainer_section_task(
    *,
    client: Client | None,
    model: str,
    cache: CacheStore | None,
    run_dir: paths.RunDir,
    house_style: str | None,
) -> ExplainerSectionCallable:
    """Construct the per-section prose generator ``serve_review``
    installs alongside the skeleton one. Same capture, same lazy import,
    same no-default ``house_style``; the section id is the only
    per-call input.
    """
    # Lazy import: keeps pydantic-ai off the `--no-augment` path.
    from ..augment.explainer import document_to_payload
    from ..augment.explainer_section import generate_explainer_section

    async def task(section_id: str) -> dict:
        # As with the skeleton task: serve_review never wires this up
        # without a backend, so a None here is a wiring bug.
        assert client is not None, "explainer section task called without an LLM backend"
        doc = await generate_explainer_section(
            client,
            run_dir=run_dir,
            section_id=section_id,
            model=model,
            house_style=house_style,
            cache=cache,
            trace_dir=run_dir.trace,
        )
        return document_to_payload(doc)

    return task


def _build_console_task(
    *,
    client: Client,
    run_dir: paths.RunDir,
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


def _load_viewer_json(run_dir: paths.RunDir) -> dict:
    meta = json.loads(run_dir.meta.read_text(encoding="utf-8"))
    if not run_dir.augmented.exists():
        return {"version": "1", "pr": {}, "files": []}
    diff = parse_augmented_diff(run_dir.augmented.read_text(encoding="utf-8"))
    head_dir = run_dir.head
    base_dir = run_dir.base
    return build_viewer_json(
        diff,
        meta,
        head_dir=head_dir if head_dir.exists() else None,
        base_dir=base_dir if base_dir.exists() else None,
    )
