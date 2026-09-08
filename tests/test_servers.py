"""`review/servers.py`: the process primitives behind the reuse rule and
`scr runs` — pid liveness, the health probe, stop with escalation."""

from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from semantic_code_review import paths
from semantic_code_review.review import identity, servers, stream


def _record(port: int, pid: int, **extra: object) -> stream.ServerInfo:
    data = {"port": port, "pid": pid, "started_at": 0.0, "url": f"http://127.0.0.1:{port}", **extra}
    info = stream.ServerInfo.from_json(data)
    assert info is not None
    return info


def test_pid_alive_reaps_an_exited_child() -> None:
    with subprocess.Popen([sys.executable, "-c", "pass"]) as child:
        child.communicate()
    # Exited and reaped by Popen: gone.
    assert not servers.pid_alive(child.pid)
    assert servers.pid_alive(os.getpid())


def test_pid_alive_does_not_count_a_zombie() -> None:
    """A child that exited before anyone waited on it is a zombie:
    `kill -0` succeeds, but it is not a server."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    deadline = time.time() + 5
    while time.time() < deadline and child.poll() is None and servers.pid_alive(child.pid):
        # poll() reaps; race it with pid_alive, which must also reap.
        time.sleep(0.01)
    assert not servers.pid_alive(child.pid)


class _OldServer(http.server.BaseHTTPRequestHandler):
    """A server from before the health route: `/data.json` yes, `/health` no."""

    def do_GET(self) -> None:
        body = b"{}" if self.path == "/data.json" else b'{"error": "not found"}'
        self.send_response(200 if self.path == "/data.json" else 404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        pass


@pytest.fixture
def old_server():
    httpd = http.server.HTTPServer(("127.0.0.1", 0), _OldServer)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def test_probe_health_falls_back_to_data_json_for_an_older_server(old_server: int) -> None:
    info = _record(old_server, os.getpid())
    assert servers.probe_health(info) == {}
    seen = servers.probe(info)
    assert seen.running and not seen.is_build(identity.this_build())


def test_probe_health_is_none_when_nothing_answers() -> None:
    assert servers.probe_health(_record(1, os.getpid())) is None
    seen = servers.probe(_record(1, os.getpid()))
    assert seen.pid_alive and not seen.running


def test_probe_skips_the_network_for_a_dead_pid(monkeypatch) -> None:
    monkeypatch.setattr(servers, "probe_health", lambda *_a, **_k: pytest.fail("probed a dead pid"))
    with subprocess.Popen([sys.executable, "-c", "pass"]) as child:
        child.communicate()
    seen = servers.probe(_record(1, child.pid))
    assert not seen.pid_alive and seen.health is None


def test_stop_removes_a_record_whose_pid_is_gone(tmp_path: Path) -> None:
    run_dir = paths.RunDir(tmp_path).create()
    with subprocess.Popen([sys.executable, "-c", "pass"]) as child:
        child.communicate()
    run_dir.server_json.write_text("{}", encoding="utf-8")

    assert servers.stop(run_dir, _record(1, child.pid)) is False
    assert not run_dir.server_json.exists()


def test_stop_escalates_to_sigkill_when_sigterm_is_ignored(tmp_path: Path) -> None:
    """A wedged server that neither exits nor removes its record on
    SIGTERM is killed and the record removed for it."""
    run_dir = paths.RunDir(tmp_path).create()
    child = subprocess.Popen(
        [sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"],
    )
    try:
        run_dir.server_json.write_text("{}", encoding="utf-8")
        started = time.time()

        assert servers.stop(run_dir, _record(1, child.pid), grace=0.5) is True

        assert time.time() - started < 5
        # pid_alive reaped it (so Popen sees no status); it is gone either way.
        assert not servers.pid_alive(child.pid)
        assert child.wait(timeout=5) is not None
        assert not run_dir.server_json.exists()
    finally:
        if child.poll() is None:
            child.kill()


def test_stop_waits_for_a_server_that_removes_its_record(tmp_path: Path) -> None:
    run_dir = paths.RunDir(tmp_path).create()
    run_dir.server_json.write_text("{}", encoding="utf-8")
    script = (
        "import signal, sys, time, os\n"
        f"signal.signal(signal.SIGTERM, lambda *_: (os.unlink({str(run_dir.server_json)!r}), sys.exit(0)))\n"
        "time.sleep(60)\n"
    )
    child = subprocess.Popen([sys.executable, "-c", script])
    try:
        time.sleep(0.3)  # let the handler install
        assert servers.stop(run_dir, _record(1, child.pid), grace=5.0) is True
        assert not servers.pid_alive(child.pid)
        assert not run_dir.server_json.exists()
    finally:
        if child.poll() is None:
            child.kill()


def test_describe_build() -> None:
    assert servers.describe_build(_record(1, 1)) == "an scr without a build identity"
    assert servers.describe_build(_record(1, 1, version="0.34.0", build="abc", package="/p")) == "0.34.0 at /p"


def test_clear_for_with_no_record_is_a_clear_way(tmp_path: Path, capsys) -> None:
    run_dir = paths.RunDir(tmp_path).create()
    assert servers.clear_for(run_dir, this=identity.this_build(), program="t", err=sys.stderr) is None
    run_dir.server_json.write_text("not json", encoding="utf-8")
    assert servers.clear_for(run_dir, this=identity.this_build(), program="t", err=sys.stderr) is None
    assert not run_dir.server_json.exists()
    assert capsys.readouterr().err == ""


def test_server_info_round_trips_and_tolerates_an_older_record() -> None:
    full = stream.ServerInfo(
        url="http://127.0.0.1:1",
        port=1,
        pid=2,
        started_at=3.0,
        version="0.34.0",
        build="abc",
        package="/p",
        counterpart="claude",
        cwd="/w",
        argv=("review", "--serve-run", "x"),
    )
    assert stream.ServerInfo.from_json(json.loads(json.dumps(full.to_json()))) == full
    old = stream.ServerInfo.from_json({"url": "http://127.0.0.1:1", "port": 1, "pid": 2, "started_at": 3.0})
    assert old is not None and old.build is None and old.argv is None
    assert stream.ServerInfo.from_json({"url": "u"}) is None
    assert stream.ServerInfo.from_json([]) is None
