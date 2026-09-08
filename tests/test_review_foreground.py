"""`scr review --foreground`: the server runs in the invoking process.

No detach: `server.json` is still written (so `--wait` and `scr comment`
reach it) and removed on exit; stdout begins with the `viewer:` /
`run_id:` lines; Ctrl-C ends the session cleanly with exit 0. These run
the real CLI in a subprocess on a tmp runs root.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from typer.testing import CliRunner

from semantic_code_review import paths
from semantic_code_review.cli import app
from semantic_code_review.review import runner, servers, stream
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
def env(tmp_path: Path) -> dict[str, str]:
    """The child's environment: off the developer's config and cache."""
    return {
        **os.environ,
        "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
        "XDG_CACHE_HOME": str(tmp_path / "xdg-cache"),
    }


def _wait_until(predicate, *, what: str, timeout: float) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _foreground(repo: Path, runs_root: Path, *, idle_timeout: int, env: dict[str, str]) -> subprocess.Popen:
    argv = [
        sys.executable,
        "-m",
        "semantic_code_review.cli",
        "review",
        "HEAD~1..HEAD",
        "--no-augment",
        "--no-open",
        "--foreground",
        "--timeout",
        str(idle_timeout),
        "--repo-root",
        str(repo),
        "--runs-root",
        str(runs_root),
    ]
    return subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)


def _the_run_dir(runs_root: Path) -> paths.RunDir:
    runs = [p for p in runs_root.iterdir() if p.is_dir()]
    assert len(runs) == 1, runs
    return paths.RunDir(runs[0])


def _await_record(runs_root: Path) -> tuple[paths.RunDir, stream.ServerInfo]:
    _wait_until(lambda: runs_root.is_dir() and any(runs_root.glob("*/server.json")), what="server.json", timeout=30)
    run_dir = _the_run_dir(runs_root)
    info = stream.read_server_info(run_dir)
    assert info is not None
    return run_dir, info


def test_a_foreground_run_serves_in_process_until_idle(repo: Path, tmp_path: Path, env: dict[str, str]) -> None:
    runs_root = tmp_path / "runs"
    with _foreground(repo, runs_root, idle_timeout=2, env=env) as proc:
        run_dir, info = _await_record(runs_root)
        # The record names this process, not a child, and the argv it
        # records is the detached form: a restart brings it back detached.
        assert info.pid == proc.pid
        assert info.argv is not None and "--foreground" not in info.argv
        assert info.argv[-2:] == ("--serve-run", run_dir.slug) and "--runs-root" in info.argv
        with urllib.request.urlopen(info.url + "/health", timeout=5) as r:
            health = json.load(r)
        assert health["pid"] == proc.pid and health["run_id"] == run_dir.slug and health["counterpart"] == "claude"

        out, err = proc.communicate(timeout=30)

    assert proc.returncode == 0, err
    lines = out.splitlines()
    assert lines[0].startswith("viewer: http://127.0.0.1:") and lines[1] == f"run_id: {run_dir.slug}"
    assert not run_dir.server_json.exists()
    assert not stream.server_alive(info)
    # --foreground logs at INFO to stderr, and the idle shutdown is named there.
    assert "INFO" in err and "review server at" in err
    assert "idle timeout" in err


def test_ctrl_c_ends_a_foreground_run_cleanly(repo: Path, tmp_path: Path, env: dict[str, str]) -> None:
    runs_root = tmp_path / "runs"
    with _foreground(repo, runs_root, idle_timeout=60, env=env) as proc:
        run_dir, info = _await_record(runs_root)
        assert stream.server_alive(info)

        proc.send_signal(signal.SIGINT)
        out, err = proc.communicate(timeout=30)

    assert proc.returncode == 0, err
    assert out.splitlines()[1] == f"run_id: {run_dir.slug}"
    assert "interrupted; the server has stopped" in err
    assert not run_dir.server_json.exists()
    assert not stream.server_alive(info)


def test_a_foreground_run_takes_over_from_a_detached_server(
    repo: Path, tmp_path: Path, env: dict[str, str], capsys
) -> None:
    """This process was asked to be the server: a live one holding the
    run — whatever its build — is stopped first."""
    runs_root = tmp_path / "runs"
    opts = runner.ReviewOptions(
        spec="HEAD~1..HEAD",
        repo_root=repo,
        config=ReviewConfig(runs_root=runs_root, augment=False, open_browser=False, timeout=60),
    )
    argv = ["review", "HEAD~1..HEAD", "--no-augment", "--no-open", "--timeout", "60", "--repo-root", str(repo)]
    assert runner.run_review(opts, argv=argv) == 0
    capsys.readouterr()
    run_dir = _the_run_dir(runs_root)
    detached = stream.read_server_info(run_dir)
    assert detached is not None

    with _foreground(repo, runs_root, idle_timeout=2, env=env) as proc:
        _wait_until(
            lambda: (i := stream.read_server_info(run_dir)) is not None and i.pid == proc.pid,
            what="the foreground server's record",
            timeout=30,
        )
        assert not servers.pid_alive(detached.pid)
        out, err = proc.communicate(timeout=30)

    assert proc.returncode == 0, err
    assert f"stopped the server holding this run: pid {detached.pid} held this run" in err
    assert out.splitlines()[1] == f"run_id: {run_dir.slug}"


def test_foreground_and_serve_run_are_mutually_exclusive() -> None:
    result = CliRunner().invoke(app, ["review", "--foreground", "--serve-run", "x"])
    assert result.exit_code == 2
    assert "mutually exclusive" in result.stderr
    result = CliRunner().invoke(app, ["pr", "o/r", "7", "--foreground", "--serve-run", "x"])
    assert result.exit_code == 2
    assert "mutually exclusive" in result.stderr
