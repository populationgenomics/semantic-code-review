"""Reaching a detached review server: `server.json` and the client side
of the stream to Claude (ADR 0009).

A running server records `{port, pid, started_at, url}` in the
[[run-directory]]'s `server.json` and removes it on exit. Anything that
wants the server — `scr review` deciding whether one already holds the
run, `scr review --wait`, `scr comment` — reads it here.

`run_wait` is `scr review --wait <run_id>`: one long-poll of `GET /wait`,
printing a [[batch]], `nothing yet`, or `ended` with the remaining
drafts. The first stdout line is the machine-readable outcome —
`status: batch` | `status: nothing-yet` | `status: ended` — and the exit
code is 0 for all three; 2 means the run id is unknown or the server
misbehaved.
"""

from __future__ import annotations

import dataclasses
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, TextIO

from .. import paths
from . import comments

#: How long `--wait` blocks by default: under the Bash tool's 600 s cap,
#: with room for the HTTP round trip.
WAIT_TIMEOUT = 540

#: Slack the client allows past the server-side timeout before it treats
#: the socket as dead.
_WAIT_SLACK = 30.0

#: How long the client gives an `ended` server to remove `server.json`
#: and finish flushing the store before reading the drafts off disk.
_ENDED_GRACE = 5.0


@dataclasses.dataclass(frozen=True)
class ServerInfo:
    """What `server.json` says about the server holding a run."""

    url: str
    port: int
    pid: int
    started_at: float

    @classmethod
    def from_json(cls, data: object) -> ServerInfo | None:
        """The record, or None when `data` is not one (a torn write, a
        hand-edited file).
        """
        if not isinstance(data, dict):
            return None
        try:
            return cls(
                url=str(data["url"]),
                port=int(data["port"]),
                pid=int(data["pid"]),
                started_at=float(data["started_at"]),
            )
        except (KeyError, TypeError, ValueError):
            return None


def read_server_info(run_dir: paths.RunDir) -> ServerInfo | None:
    """The server holding `run_dir`, per its `server.json`; None when the
    file is absent or not a record. Says nothing about liveness — see
    `server_alive`.
    """
    try:
        raw = run_dir.server_json.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return ServerInfo.from_json(data)


def server_alive(info: ServerInfo, *, timeout: float = 2.0) -> bool:
    """True when the recorded address answers. A `server.json` a killed
    server left behind fails this.
    """
    try:
        with urllib.request.urlopen(info.url + "/data.json", timeout=timeout) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


class ServerRefused(Exception):
    """The server answered `/wait` with an error status."""


def run_wait(run_dir: paths.RunDir, *, timeout: float, out: TextIO | None = None, err: TextIO | None = None) -> int:
    """`scr review --wait`: block for the next batch and print the outcome.

    Returns the exit code. 0 with a first line of `status: batch`,
    `status: nothing-yet` or `status: ended`; 2 when `run_dir` is not a
    run, or the server is alive but `/wait` failed. `out` / `err` default
    to the process streams as they are at call time.
    """
    return _run_wait(
        run_dir,
        timeout=timeout,
        out=sys.stdout if out is None else out,
        err=sys.stderr if err is None else err,
    )


def _run_wait(run_dir: paths.RunDir, *, timeout: float, out: TextIO, err: TextIO) -> int:
    if not run_dir.meta.exists():
        err.write(f"scr review --wait: unknown run id {run_dir.slug!r} (no run at {run_dir.path})\n")
        return 2
    info = read_server_info(run_dir)
    if info is None:
        return _print_ended(run_dir, out)
    try:
        response = _poll_wait(info, timeout=timeout)
    except ServerRefused as e:
        err.write(f"scr review --wait: {e}\n")
        return 2
    except OSError as e:
        if server_alive(info):
            err.write(f"scr review --wait: the server at {info.url} is up but /wait failed: {e}\n")
            return 2
        # The record outlived its server: a killed process, not an ended
        # session — but the drafts are the same either way.
        run_dir.server_json.unlink(missing_ok=True)
        return _print_ended(run_dir, out)

    status = response.get("status")
    if status == "batch":
        out.write("status: batch\n")
        out.write(comments.format_batch_markdown(int(response["batch_no"]), response["entries"], run_slug=run_dir.slug))
        out.flush()
        return 0
    if status == "nothing-yet":
        out.write("status: nothing-yet\n")
        out.write(f"Nothing sent in the last {int(timeout)}s; the review is still open.\n")
        out.flush()
        return 0
    if status == "ended":
        _await_server_gone(run_dir)
        return _print_ended(run_dir, out)
    err.write(f"scr review --wait: unexpected /wait response: {response!r}\n")
    return 2


def _poll_wait(info: ServerInfo, *, timeout: float) -> dict[str, Any]:
    """One `GET /wait`. Raises OSError (a `URLError` is one) when the
    server cannot be reached, `ServerRefused` on an error status.
    """
    url = f"{info.url}/wait?{urllib.parse.urlencode({'timeout': timeout})}"
    try:
        with urllib.request.urlopen(url, timeout=timeout + _WAIT_SLACK) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise ServerRefused(f"/wait answered {e.code}: {e.read().decode('utf-8', 'replace')}") from e


def _await_server_gone(run_dir: paths.RunDir) -> None:
    deadline = time.time() + _ENDED_GRACE
    while time.time() < deadline and run_dir.server_json.exists():
        time.sleep(0.05)


def _print_ended(run_dir: paths.RunDir, out: TextIO) -> int:
    """The end of the review: today's list of what the counterpart never
    received, marked delivered by writing the store directly — the
    server is gone.
    """
    remaining = comments.CommentStore(run_dir.comments).deliver_remaining()
    out.write("status: ended\n")
    out.write(
        comments.format_markdown(
            remaining,
            run_slug=run_dir.slug,
            heading=f"# Review ended — remaining comments for {run_dir.slug}",
        )
    )
    out.flush()
    return 0


# --- the reply channel: scr comment -----------------------------------------


class NoServer(Exception):
    """No live server holds the run: the review has ended (or never ran)."""


def _require_server(run_dir: paths.RunDir) -> ServerInfo:
    if not run_dir.meta.exists():
        raise NoServer(f"unknown run id {run_dir.slug!r} (no run at {run_dir.path})")
    info = read_server_info(run_dir)
    if info is None:
        raise NoServer(f"no server holds {run_dir.slug}; the review has ended")
    if not server_alive(info):
        run_dir.server_json.unlink(missing_ok=True)
        raise NoServer(f"the server recorded for {run_dir.slug} is gone; the review has ended")
    return info


def _post(info: ServerInfo, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    """One POST to the server. Raises `ServerRefused` with the server's
    own message on an error status.
    """
    req = urllib.request.Request(
        info.url + path,
        method="POST",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            message = json.loads(e.read().decode("utf-8")).get("error", "")
        except ValueError:
            message = ""
        raise ServerRefused(f"{path} answered {e.code}: {message}") from e


def reply(run_dir: paths.RunDir, comment_id: str, body: str) -> dict[str, Any]:
    """Add Claude's reply under `comment_id`; the viewer shows it live.

    Raises:
        NoServer: no live server holds the run.
        ServerRefused: the server refused (unknown comment, empty body).
    """
    payload = {"source": "claude", "in_reply_to_id": comment_id, "body": body}
    return _post(_require_server(run_dir), "/comments", payload)


def set_resolved(run_dir: paths.RunDir, comment_id: str, resolved: bool) -> dict[str, Any]:
    """Resolve or reopen the thread holding `comment_id`.

    Raises:
        NoServer: no live server holds the run.
        ServerRefused: the server refused (unknown comment, ingested thread).
    """
    action = "resolve" if resolved else "unresolve"
    return _post(_require_server(run_dir), f"/comments/{urllib.parse.quote(comment_id, safe='')}/{action}", {})
