"""`scr runs ps | stop | restart | logs`: the CLI owns the detached review
servers. Real children on tmp runs roots, as in test_review_detach.py."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest
from typer.testing import CliRunner

from semantic_code_review import paths
from semantic_code_review.cli import app
from semantic_code_review.review import identity, runner, servers, stream
from semantic_code_review.review.config import ReviewConfig


def _sh(cwd: Path, *args: str) -> None:
    subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "r"
    root.mkdir()
    _sh(root, "git", "init", "-q", "-b", "main")
    (root / "a.py").write_text("x = 1\n")
    _sh(root, "git", "add", "a.py")
    _sh(root, "git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "add a")
    (root / "a.py").write_text("x = 2\n")
    _sh(root, "git", "add", "a.py")
    _sh(root, "git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "bump a")
    return root


@pytest.fixture
def cache(tmp_path: Path, monkeypatch) -> Path:
    """An XDG cache of our own: `scr runs` without `--runs-root` walks
    `<cache>/scr/runs/*`, and the children stay off the real config."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    return tmp_path / "xdg-cache" / "scr" / "runs"


def _wait_until(predicate, *, what: str, timeout: float) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _start(repo: Path, runs_root: Path, *, idle_timeout: int) -> paths.RunDir:
    """A detached server for the repo's last commit, as `scr review` starts one."""
    argv = [
        "review",
        "HEAD~1..HEAD",
        "--no-augment",
        "--no-open",
        "--timeout",
        str(idle_timeout),
        "--repo-root",
        str(repo),
    ]
    opts = runner.ReviewOptions(
        spec="HEAD~1..HEAD",
        repo_root=repo,
        config=ReviewConfig(runs_root=runs_root, augment=False, open_browser=False, timeout=idle_timeout),
    )
    assert runner.run_review(opts, argv=argv) == 0
    runs = [p for p in runs_root.iterdir() if p.is_dir()]
    assert len(runs) == 1, runs
    return paths.RunDir(runs[0])


def _info(run_dir: paths.RunDir) -> stream.ServerInfo:
    info = stream.read_server_info(run_dir)
    assert info is not None
    return info


def _stop_quietly(run_dir: paths.RunDir) -> None:
    info = stream.read_server_info(run_dir)
    if info is not None:
        servers.stop(run_dir, info)


def _rows(stdout: str) -> dict[str, dict[str, str]]:
    """`ps` output as {slug: {column: cell}}."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    header = lines[0].split()
    rows = {}
    for line in lines[1:]:
        cells = line.split()
        assert len(cells) == len(header), line
        rows[cells[0]] = dict(zip(header, cells, strict=True))
    return rows


# --- ps -------------------------------------------------------------------


def test_ps_lists_every_repo_root_with_state_and_live_counts(repo: Path, cache: Path, capsys) -> None:
    live_root = cache / "aaaa0000aaaa0000"
    stale_root = cache / "bbbb0000bbbb0000"
    run_dir = _start(repo, live_root, idle_timeout=60)
    capsys.readouterr()
    info = _info(run_dir)
    stale = paths.RunDir(stale_root / "local-old-run").create()
    stale.server_json.write_text(
        json.dumps({"port": 1, "pid": _dead_pid(), "started_at": 0.0, "url": "http://127.0.0.1:1"}), encoding="utf-8"
    )
    # A tab and a listener, so the live counts have something to count.
    tab = urllib.request.urlopen(info.url + "/events", timeout=5)
    waiter = threading.Thread(target=lambda: urllib.request.urlopen(info.url + "/wait?timeout=3", timeout=10).read())
    waiter.start()
    _wait_until(lambda: servers.probe_health(info)["listening"], what="the listener", timeout=5)  # type: ignore[index]
    try:
        result = CliRunner().invoke(app, ["runs", "ps"])
    finally:
        tab.close()
        waiter.join(timeout=10)

    assert result.exit_code == 0, result.output
    rows = _rows(result.stdout)
    this = identity.this_build()
    assert set(rows) == {run_dir.slug, "local-old-run"}
    live = rows[run_dir.slug]
    assert live["STATE"] == "up" and live["URL"] == info.url and live["PID"] == str(info.pid)
    assert live["BUILD"] == f"{this.version}/{this.build}" and live["COUNTERPART"] == "claude"
    assert live["WAIT"] == "yes" and live["TABS"] == "1"
    assert live["UP"].endswith("s")
    dead = rows["local-old-run"]
    assert dead["STATE"] == "stale" and dead["BUILD"] == "-/-" and dead["WAIT"] == "-" and dead["TABS"] == "-"
    assert stale.server_json.exists()  # reported, not removed
    _stop_quietly(run_dir)


def test_ps_marks_another_build_and_prunes_stale_records(repo: Path, cache: Path, capsys) -> None:
    root = cache / "aaaa0000aaaa0000"
    run_dir = _start(repo, root, idle_timeout=60)
    capsys.readouterr()
    info = _info(run_dir)
    run_dir.server_json.write_text(
        json.dumps({**info.to_json(), "build": "0000deadbeef", "version": "0.1.0"}), encoding="utf-8"
    )
    stale = paths.RunDir(root / "local-old-run").create()
    stale.server_json.write_text(
        json.dumps({"port": 1, "pid": _dead_pid(), "started_at": 0.0, "url": "http://127.0.0.1:1"}), encoding="utf-8"
    )

    result = CliRunner().invoke(app, ["runs", "ps", "--prune", "--runs-root", str(root)])

    assert result.exit_code == 0, result.output
    rows = _rows(result.stdout)
    assert rows[run_dir.slug]["STATE"] == "other-build"
    assert rows[run_dir.slug]["BUILD"] == "0.1.0/0000deadbeef"
    assert rows["local-old-run"]["STATE"] == "pruned"
    assert not stale.server_json.exists()
    assert run_dir.server_json.exists()
    _stop_quietly(run_dir)


def test_ps_here_reads_the_current_repos_root(cache: Path, monkeypatch) -> None:
    (cache / "elsewhere" / "run").mkdir(parents=True)
    (cache / "elsewhere" / "run" / "server.json").write_text(
        json.dumps({"port": 1, "pid": 1, "started_at": 0.0, "url": "http://127.0.0.1:1"}), encoding="utf-8"
    )
    result = CliRunner().invoke(app, ["runs", "ps", "--here"])
    assert result.exit_code == 0
    assert "no review servers recorded under" in result.stderr
    assert str(paths.default_runs_root()) in result.stderr
    result = CliRunner().invoke(app, ["runs", "ps"])
    assert "run " in result.stdout and "stale" in result.stdout


def test_ps_with_nothing_recorded(cache: Path) -> None:
    result = CliRunner().invoke(app, ["runs", "ps"])
    assert result.exit_code == 0
    assert result.stdout == ""
    assert "no review servers recorded" in result.stderr


def test_uptime_format() -> None:
    from semantic_code_review.cli.runs_cmd import _uptime

    assert _uptime(5) == "5s"
    assert _uptime(65) == "1m05s"
    assert _uptime(3600 * 2 + 60 * 13) == "2h13m"
    assert _uptime(86400 * 3 + 3600 * 2) == "3d2h"


# --- stop -----------------------------------------------------------------


def test_stop_ends_the_server_and_reports_a_second_stop(repo: Path, cache: Path, capsys) -> None:
    root = cache / "aaaa0000aaaa0000"
    run_dir = _start(repo, root, idle_timeout=60)
    capsys.readouterr()
    info = _info(run_dir)

    result = CliRunner().invoke(app, ["runs", "stop", run_dir.slug])

    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == f"stopped {run_dir.slug} (pid {info.pid})"
    assert not run_dir.server_json.exists()
    assert not servers.pid_alive(info.pid)
    assert not stream.server_alive(info)

    again = CliRunner().invoke(app, ["runs", "stop", run_dir.slug])
    assert again.exit_code == 1
    assert "not running" in again.stderr


def test_stop_removes_a_stale_record_and_exits_1(cache: Path) -> None:
    run_dir = paths.RunDir(cache / "aaaa" / "local-old-run").create()
    run_dir.server_json.write_text(
        json.dumps({"port": 1, "pid": _dead_pid(), "started_at": 0.0, "url": "http://127.0.0.1:1"}), encoding="utf-8"
    )
    result = CliRunner().invoke(app, ["runs", "stop", "local-old-run"])
    assert result.exit_code == 1
    assert "record removed" in result.stderr
    assert not run_dir.server_json.exists()


def test_stop_unknown_run_id_exits_2(cache: Path) -> None:
    result = CliRunner().invoke(app, ["runs", "stop", "nope"])
    assert result.exit_code == 2
    assert "unknown run id 'nope'" in result.stderr
    result = CliRunner().invoke(app, ["runs", "stop"])
    assert result.exit_code == 2 and "give a run id or --all" in result.stderr


def test_stop_all(repo: Path, cache: Path, capsys) -> None:
    root = cache / "aaaa0000aaaa0000"
    run_dir = _start(repo, root, idle_timeout=60)
    capsys.readouterr()
    info = _info(run_dir)

    result = CliRunner().invoke(app, ["runs", "stop", "--all"])

    assert result.exit_code == 0, result.output
    assert f"stopped {run_dir.slug}" in result.stdout
    _wait_until(lambda: not servers.pid_alive(info.pid), what="the child to go", timeout=5)
    assert CliRunner().invoke(app, ["runs", "stop", "--all"]).exit_code == 1


# --- restart --------------------------------------------------------------


def test_restart_brings_a_new_server_up_from_the_recorded_argv(repo: Path, cache: Path, capsys) -> None:
    root = cache / "aaaa0000aaaa0000"
    run_dir = _start(repo, root, idle_timeout=60)
    capsys.readouterr()
    first = _info(run_dir)

    result = CliRunner().invoke(app, ["runs", "restart", run_dir.slug])

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[-1] == f"run_id: {run_dir.slug}"
    assert result.stdout.splitlines()[-2].startswith("viewer: http://127.0.0.1:")
    assert f"stopped pid {first.pid}" in result.stderr
    second = _info(run_dir)
    assert second.pid != first.pid
    assert not servers.pid_alive(first.pid)
    with urllib.request.urlopen(second.url + "/health", timeout=5) as r:
        health = json.load(r)
    assert health["pid"] == second.pid and health["run_id"] == run_dir.slug
    # The same arguments, the same working directory.
    assert second.argv == first.argv and second.cwd == first.cwd
    _stop_quietly(run_dir)


def test_restart_refuses_a_record_that_cannot_be_restarted(repo: Path, cache: Path, capsys) -> None:
    root = cache / "aaaa0000aaaa0000"
    run_dir = _start(repo, root, idle_timeout=60)
    capsys.readouterr()
    info = _info(run_dir)
    try:
        # No argv: an older scr's record.
        run_dir.server_json.write_text(
            json.dumps({"port": info.port, "pid": info.pid, "started_at": info.started_at, "url": info.url}),
            encoding="utf-8",
        )
        result = CliRunner().invoke(app, ["runs", "restart", run_dir.slug])
        assert result.exit_code == 2 and "no argv" in result.stderr

        # An argv that serves another run.
        wrong = [a if a != run_dir.slug else "some-other-run" for a in info.argv or ()]
        run_dir.server_json.write_text(json.dumps({**info.to_json(), "argv": wrong}), encoding="utf-8")
        result = CliRunner().invoke(app, ["runs", "restart", run_dir.slug])
        assert result.exit_code == 2 and f"does not serve {run_dir.slug}" in result.stderr

        # A working directory that has since been deleted.
        run_dir.server_json.write_text(json.dumps({**info.to_json(), "cwd": "/no/such/worktree"}), encoding="utf-8")
        result = CliRunner().invoke(app, ["runs", "restart", run_dir.slug])
        assert result.exit_code == 2 and "/no/such/worktree, which no longer exists" in result.stderr

        # Refusals leave the server alone.
        assert servers.pid_alive(info.pid) and stream.server_alive(info)
    finally:
        run_dir.server_json.write_text(json.dumps(info.to_json()), encoding="utf-8")
        _stop_quietly(run_dir)


def test_restart_without_a_record_exits_2(cache: Path) -> None:
    paths.RunDir(cache / "aaaa" / "local-run").create()
    result = CliRunner().invoke(app, ["runs", "restart", "local-run"])
    assert result.exit_code == 2
    assert "no server is recorded" in result.stderr


# --- logs -----------------------------------------------------------------


def test_logs_prints_the_server_log_and_follows_until_the_server_exits(repo: Path, cache: Path, capsys) -> None:
    root = cache / "aaaa0000aaaa0000"
    run_dir = _start(repo, root, idle_timeout=2)
    capsys.readouterr()
    info = _info(run_dir)

    result = CliRunner().invoke(app, ["runs", "logs", run_dir.slug])
    assert result.exit_code == 0, result.output
    assert f"listening on {info.url}" in result.stdout
    assert "idle timeout" not in result.stdout

    followed = CliRunner().invoke(app, ["runs", "logs", "-f", run_dir.slug])
    assert followed.exit_code == 0, followed.output
    assert f"listening on {info.url}" in followed.stdout
    assert "idle timeout" in followed.stdout
    assert not run_dir.server_json.exists()


def test_logs_without_a_log_exits_1(cache: Path) -> None:
    paths.RunDir(cache / "aaaa" / "local-run").create()
    result = CliRunner().invoke(app, ["runs", "logs", "local-run"])
    assert result.exit_code == 1
    assert "has no server.log" in result.stderr


# --- run id resolution ------------------------------------------------------


def test_a_run_id_in_several_repos_needs_a_runs_root(cache: Path) -> None:
    for fp in ("aaaa", "bbbb"):
        paths.RunDir(cache / fp / "local-run").create()
    result = CliRunner().invoke(app, ["runs", "logs", "local-run"])
    assert result.exit_code == 2
    assert "names a run in several repos" in result.stderr
    (cache / "aaaa" / "local-run" / "server.log").write_text("hello\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["runs", "logs", "local-run", "--runs-root", str(cache / "aaaa")])
    assert result.exit_code == 0 and result.stdout == "hello\n"


def _dead_pid() -> int:
    with subprocess.Popen([sys.executable, "-c", "pass"]) as proc:
        proc.wait()
    assert not servers.pid_alive(proc.pid)
    return proc.pid
