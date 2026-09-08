"""`scr runs` subapp — the run-artefact directory and the review servers
holding runs in it (ADR 0009).

`ps` lists every recorded server, `stop` / `restart` manage one, `logs`
prints its `server.log`; `path` prints the runs root. The process work
is `review/servers.py`; this module resolves run ids and formats.
"""

from __future__ import annotations

import pathlib
import sys
import time

import typer

from .. import paths
from ..review import identity, servers, stream
from . import app

runs_app = typer.Typer(help="Inspect the run-artefact directory and manage the review servers holding runs.")
app.add_typer(runs_app, name="runs")

_RUNS_ROOT = typer.Option(
    None, help="Root directory for run artefacts (default: ~/.cache/scr/runs/<repo-fingerprint>/)."
)

#: How often `logs -f` looks for new output.
_FOLLOW_POLL = 0.2


@runs_app.command("path")
def runs_path() -> None:
    """Print the runs root resolved for the current cwd."""
    typer.echo(str(paths.default_runs_root()))


# --- ps -------------------------------------------------------------------

_COLUMNS = ("RUN", "STATE", "URL", "PID", "UP", "BUILD", "COUNTERPART", "WAIT", "TABS")


@runs_app.command("ps")
def runs_ps(
    here: bool = typer.Option(False, "--here", help="Only the current repo's runs root, not every repo's."),
    prune: bool = typer.Option(False, "--prune", help="Remove the records of servers that are gone."),
    runs_root: pathlib.Path = _RUNS_ROOT,
) -> None:
    """List the review servers recorded under the runs roots, one line each.

    STATE is `up`, `other-build` (up, but not the build this CLI is —
    `scr review` / `scr pr` would restart it) or `stale` (its process is
    gone; `--prune` removes the record). WAIT says whether a `--wait` is
    listening; TABS how many viewer tabs are open.
    """
    if runs_root is not None:
        roots = [runs_root]
    elif here:
        roots = [paths.default_runs_root()]
    else:
        roots = _all_roots()
    records = servers.recorded(roots)
    if not records:
        where = ", ".join(str(r) for r in roots) or str(paths.runs_cache_root())
        typer.echo(f"scr runs ps: no review servers recorded under {where}", err=True)
        return
    this = identity.this_build()
    rows = [_ps_row(r, this=this, prune=prune) for r in records]
    widths = [max(len(col), *(len(row[i]) for row in rows)) for i, col in enumerate(_COLUMNS)]
    for row in (_COLUMNS, *rows):
        typer.echo("  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip())


def _ps_row(r: servers.Recorded, *, this: identity.BuildIdentity, prune: bool) -> tuple[str, ...]:
    info = r.info
    seen = servers.probe(info)
    build = f"{info.version or '-'}/{info.build or '-'}"
    if not seen.running:
        if prune:
            r.run_dir.server_json.unlink(missing_ok=True)
        state = "pruned" if prune else "stale"
        return (r.run_dir.slug, state, info.url, str(info.pid), "-", build, info.counterpart or "-", "-", "-")
    assert seen.health is not None  # running implies an answer
    state = "up" if seen.is_build(this) else "other-build"
    listening = seen.health.get("listening")
    viewers = seen.health.get("viewers")
    return (
        r.run_dir.slug,
        state,
        info.url,
        str(info.pid),
        _uptime(time.time() - info.started_at),
        build,
        info.counterpart or "-",
        "-" if listening is None else ("yes" if listening else "no"),
        "-" if viewers is None else str(viewers),
    )


def _uptime(seconds: float) -> str:
    s = max(0, int(seconds))
    days, s = divmod(s, 86400)
    hours, s = divmod(s, 3600)
    minutes, s = divmod(s, 60)
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{s:02d}s"
    return f"{s}s"


# --- stop / restart / logs -------------------------------------------------


@runs_app.command("stop")
def runs_stop(
    run_id: str = typer.Argument(None, help="The run id `scr review` / `scr pr` printed (`run_id: <slug>`)."),
    stop_all: bool = typer.Option(False, "--all", help="Stop every recorded server under the runs roots."),
    runs_root: pathlib.Path = _RUNS_ROOT,
) -> None:
    """Stop the review server holding a run.

    SIGTERM, then SIGKILL if it has not removed its record within a few
    seconds. Exit 0 when a server was stopped, 1 when none was running
    (a stale record is removed), 2 for an unknown run id.
    """
    if stop_all == (run_id is not None):
        typer.echo("scr runs stop: give a run id or --all", err=True)
        raise typer.Exit(code=2)
    if stop_all:
        roots = [runs_root] if runs_root is not None else _all_roots()
        targets = [(r.run_dir, r.info) for r in servers.recorded(roots)]
    else:
        run_dir = _locate(run_id, runs_root, program="scr runs stop")
        info = stream.read_server_info(run_dir)
        if info is None:
            typer.echo(f"{run_dir.slug}: not running", err=True)
            raise typer.Exit(code=1)
        targets = [(run_dir, info)]
    stopped = 0
    for run_dir, info in targets:
        if servers.stop(run_dir, info):
            typer.echo(f"stopped {run_dir.slug} (pid {info.pid})")
            stopped += 1
        else:
            typer.echo(f"{run_dir.slug}: not running (pid {info.pid} is gone; record removed)", err=True)
    raise typer.Exit(code=0 if stopped else 1)


@runs_app.command("restart")
def runs_restart(
    run_id: str = typer.Argument(..., help="The run id `scr review` / `scr pr` printed (`run_id: <slug>`)."),
    runs_root: pathlib.Path = _RUNS_ROOT,
) -> None:
    """Stop the server holding a run and start it again from this build,
    with the arguments and working directory it was started with.

    Prints `viewer: <url>` and `run_id: <slug>` like `scr review`. Exit 2
    when the record cannot be restarted (no record, or its argv or cwd
    no longer resolve) or the new server does not start.
    """
    run_dir = _locate(run_id, runs_root, program="scr runs restart")
    info = stream.read_server_info(run_dir)
    if info is None:
        typer.echo(f"scr runs restart: no server is recorded for {run_id}; start it with scr review / scr pr", err=True)
        raise typer.Exit(code=2)
    try:
        argv, cwd = servers.restart_argv(run_dir, info)
    except servers.CannotRestart as e:
        typer.echo(f"scr runs restart: {e}", err=True)
        raise typer.Exit(code=2) from None
    if servers.stop(run_dir, info):
        typer.echo(f"scr runs restart: stopped pid {info.pid} ({servers.describe_build(info)})", err=True)
    child = servers.spawn(run_dir, argv, cwd=cwd)
    started = servers.await_server_info(run_dir, child)
    if started is None:
        typer.echo(f"scr runs restart: the review server did not start; its log is {run_dir.server_log}", err=True)
        raise typer.Exit(code=2)
    typer.echo(f"viewer: {started.url}")
    typer.echo(f"run_id: {run_dir.slug}")


@runs_app.command("logs")
def runs_logs(
    run_id: str = typer.Argument(..., help="The run id `scr review` / `scr pr` printed (`run_id: <slug>`)."),
    follow: bool = typer.Option(False, "--follow", "-f", help="Keep printing as the server writes, until it exits."),
    runs_root: pathlib.Path = _RUNS_ROOT,
) -> None:
    """Print the server's log (`server.log` in the run directory)."""
    run_dir = _locate(run_id, runs_root, program="scr runs logs")
    if not run_dir.server_log.exists():
        typer.echo(f"scr runs logs: {run_dir.slug} has no server.log", err=True)
        raise typer.Exit(code=1)
    with run_dir.server_log.open("rb") as f:
        _copy(f.read())
        if not follow:
            return
        try:
            while True:
                chunk = f.read()
                if chunk:
                    _copy(chunk)
                elif not run_dir.server_json.exists():
                    # The server has gone; one last read catches its last line.
                    _copy(f.read())
                    return
                else:
                    time.sleep(_FOLLOW_POLL)
        except KeyboardInterrupt:
            return


def _copy(chunk: bytes) -> None:
    sys.stdout.buffer.write(chunk)
    sys.stdout.buffer.flush()


def _all_roots() -> list[pathlib.Path]:
    cache = paths.runs_cache_root()
    return sorted(p for p in cache.iterdir() if p.is_dir()) if cache.is_dir() else []


def _locate(run_id: str, runs_root: pathlib.Path | None, *, program: str) -> paths.RunDir:
    """The run directory for `run_id`: under `runs_root` when given, else
    the current repo's root, else the one repo root under the cache
    that has it. Exits 2 when none or several do.
    """
    if runs_root is not None:
        candidates = [runs_root / run_id]
    else:
        here = paths.default_runs_root() / run_id
        candidates = [here] if here.is_dir() else [root / run_id for root in _all_roots()]
    found = [paths.RunDir(p) for p in candidates if p.is_dir()]
    if len(found) == 1:
        return found[0]
    if not found:
        typer.echo(f"{program}: unknown run id {run_id!r}", err=True)
        raise typer.Exit(code=2)
    listed = ", ".join(str(rd.path) for rd in found)
    typer.echo(f"{program}: {run_id!r} names a run in several repos ({listed}); pass --runs-root", err=True)
    raise typer.Exit(code=2)
