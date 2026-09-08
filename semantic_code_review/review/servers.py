"""Review servers as processes (ADR 0009): probing, stopping, spawning
and the reuse rule.

`scr review` and `scr pr` detach a server per run and record it in the
[[run-directory]]'s `server.json`; once servers are background processes
the CLI owns them. This module is what `detach_server` and the `scr runs`
subcommands share: whether a recorded server is running and which build
it is (`probe`), stopping one and waiting for its record to go (`stop`),
spawning one and waiting for its record to appear (`spawn`,
`await_server_info`), and `clear_for` — the reuse rule that decides
whether a recorded server is reused or replaced.

A recorded pid is signalled only once `/health` (or, for a server older
than the health route, `/data.json`) has answered at the recorded
address: a live pid that answers nothing is a reused pid or a wedged
server, and its record is dropped rather than a stranger killed.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from typing import Any, TextIO

from .. import paths
from . import identity, stream

#: How long `stop` gives a SIGTERM'd server to remove `server.json`
#: before SIGKILL. `serve_review`'s finally runs promptly: the wait is
#: for a blocked `/wait` handler and the socket to close, not for work.
STOP_GRACE = 5.0

#: After SIGKILL, how long to wait for the pid to be gone before
#: removing the record ourselves.
_KILL_GRACE = 2.0

#: How long a health probe waits. Local, so short.
_PROBE_TIMEOUT = 2.0

#: How long `spawn` gives the detached server to bind and write
#: `server.json`. The server starts before augmentation, so this covers
#: interpreter start-up and the SDK import, not an LLM pass.
SERVER_START_TIMEOUT = 60.0

#: The hidden `scr review` / `scr pr` option that turns an invocation
#: into the detached server for an existing run. `detach_server` appends
#: it (with the resolved runs root) to its own argv to spawn the child.
SERVE_RUN_FLAG = "--serve-run"


@dataclasses.dataclass(frozen=True)
class Probe:
    """A recorded server, looked at now: the record, whether its pid is
    alive, and what the server at the recorded address said.

    `health` is `GET /health`'s payload; `{}` when the address answered
    `/data.json` but has no health route (a server older than the build
    identity); None when nothing answered.
    """

    info: stream.ServerInfo
    pid_alive: bool
    health: dict[str, Any] | None

    @property
    def running(self) -> bool:
        """The recorded process is alive and it is the server answering."""
        return self.pid_alive and self.health is not None

    def is_build(self, this: identity.BuildIdentity) -> bool:
        """Whether the record names `this` build. A record without one
        (an older scr) is never this build.
        """
        return self.info.build is not None and self.info.build == this.build


def pid_alive(pid: int) -> bool:
    """Whether a process with `pid` exists. A child of this process that
    has exited is reaped here, so it does not read as alive.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        reaped, _ = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return True
    return reaped != pid


def probe_health(info: stream.ServerInfo, *, timeout: float = _PROBE_TIMEOUT) -> dict[str, Any] | None:
    """Ask the recorded address what it is. The `/health` payload; `{}`
    for a server that answers `/data.json` but not `/health`; None when
    nothing answers.
    """
    try:
        with urllib.request.urlopen(info.url + "/health", timeout=timeout) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code != 404:
            return None
        return {} if stream.server_alive(info, timeout=timeout) else None
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def probe(info: stream.ServerInfo) -> Probe:
    """Look at a recorded server now."""
    alive = pid_alive(info.pid)
    return Probe(info=info, pid_alive=alive, health=probe_health(info) if alive else None)


def stop(run_dir: paths.RunDir, info: stream.ServerInfo, *, grace: float = STOP_GRACE) -> bool:
    """Stop the server recorded for `run_dir`: SIGTERM, wait up to
    `grace` seconds for it to remove `server.json`, then SIGKILL and
    remove the record ourselves. A record whose pid is already gone is
    removed.

    Returns True when a process was stopped, False when none was running.
    """
    if not pid_alive(info.pid):
        run_dir.server_json.unlink(missing_ok=True)
        return False
    try:
        os.kill(info.pid, signal.SIGTERM)
    except ProcessLookupError:
        run_dir.server_json.unlink(missing_ok=True)
        return False
    if _wait_for(lambda: not run_dir.server_json.exists(), timeout=grace):
        _wait_for(lambda: not pid_alive(info.pid), timeout=_KILL_GRACE)
        return True
    with contextlib.suppress(ProcessLookupError):
        os.kill(info.pid, signal.SIGKILL)
    _wait_for(lambda: not pid_alive(info.pid), timeout=_KILL_GRACE)
    run_dir.server_json.unlink(missing_ok=True)
    return True


def clear_for(
    run_dir: paths.RunDir, *, this: identity.BuildIdentity, program: str, err: TextIO, reuse: bool = True
) -> stream.ServerInfo | None:
    """The reuse rule. Look at the server `run_dir`'s record names and
    either hand it back to be reused or clear the way for a new one.

    A record whose server is not running is removed. A running server
    of `this` build is reused (returned). A running server of another
    build — or one older than the build identity — is stopped: a
    server keeps serving the build it started from, so reusing it
    would serve stale code against a new CLI. With `reuse=False` a
    running server of this build is stopped too (`--foreground` takes
    the run over). One line on `err` says what happened; None means the
    way is clear.
    """
    info = stream.read_server_info(run_dir)
    if info is None:
        run_dir.server_json.unlink(missing_ok=True)
        return None
    seen = probe(info)
    if not seen.running:
        run_dir.server_json.unlink(missing_ok=True)
        why = "is gone" if not seen.pid_alive else f"answers nothing at {info.url}"
        err.write(f"{program}: removed a stale server.json (pid {info.pid} {why})\n")
        err.flush()
        return None
    if reuse and seen.is_build(this):
        return info
    if not reuse:
        why = f"pid {info.pid} held this run"
    else:
        why = f"pid {info.pid} was {describe_build(info)}, this is {this.version} at {this.package}"
    stop(run_dir, info)
    err.write(f"{program}: stopped the server holding this run: {why}\n")
    err.flush()
    return None


def describe_build(info: stream.ServerInfo) -> str:
    """`<version> at <package>` for a record, or what an older record lacks."""
    if info.version is None or info.package is None:
        return "an scr without a build identity"
    return f"{info.version} at {info.package}"


def spawn(run_dir: paths.RunDir, argv: Sequence[str], *, cwd: str) -> subprocess.Popen:
    """Start a detached review server: this interpreter re-executing
    `argv` (`--serve-run` included) in `cwd`, in its own session, with
    its stdio in `server.log`. Does not wait for it: see
    `await_server_info`.
    """
    child_argv = [sys.executable, "-m", "semantic_code_review.cli", *argv]
    with run_dir.server_log.open("ab") as log_file:
        return subprocess.Popen(
            child_argv,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            cwd=cwd,
            start_new_session=True,
        )


def await_server_info(
    run_dir: paths.RunDir, child: subprocess.Popen, *, timeout: float = SERVER_START_TIMEOUT
) -> stream.ServerInfo | None:
    """Poll for the child's `server.json`; None if the child exits first
    or the timeout passes.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        info = stream.read_server_info(run_dir)
        if info is not None:
            return info
        if child.poll() is not None:
            return None
        time.sleep(0.05)
    return None


def _wait_for(predicate: Callable[[], bool], *, timeout: float) -> bool:
    deadline = time.time() + timeout
    while True:
        if predicate():
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.05)


__all__ = [
    "SERVER_START_TIMEOUT",
    "SERVE_RUN_FLAG",
    "STOP_GRACE",
    "Probe",
    "await_server_info",
    "clear_for",
    "describe_build",
    "pid_alive",
    "probe",
    "probe_health",
    "spawn",
    "stop",
]
