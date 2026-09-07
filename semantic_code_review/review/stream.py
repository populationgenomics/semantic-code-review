"""Reaching a detached review server: `server.json` and the client side
of the stream to Claude (ADR 0009).

A running server records `{port, pid, started_at, url}` in the
[[run-directory]]'s `server.json` and removes it on exit. Anything that
wants the server — `scr review` deciding whether one already holds the
run, `scr review --wait`, `scr comment` — reads it here.
"""

from __future__ import annotations

import dataclasses
import json
import urllib.error
import urllib.request

from .. import paths


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
