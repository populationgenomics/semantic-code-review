"""`scr review` detaches: the server outlives the CLI and records itself.

`run_review` materialises the run, spawns the server as a detached child
(`python -m semantic_code_review.cli review … --serve-run <slug>`), and
returns once `server.json` appears. The child serves until idle, then
removes the record. These spawn a real child on a tmp run dir.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from typer.testing import CliRunner

from semantic_code_review import paths
from semantic_code_review.cli import app
from semantic_code_review.review import identity, runner, stream
from semantic_code_review.review.config import ReviewConfig


def _sh(cwd: Path, *args: str) -> str:
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True)
    return r.stdout


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
def isolated_config(tmp_path: Path, monkeypatch) -> None:
    """Keep the child off the developer's real config and prefs."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))


def _wait_until(predicate, *, what: str, timeout: float) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _run_review(repo: Path, runs_root: Path, *, idle_timeout: int, extra_argv: list[str] | None = None) -> int:
    argv = [
        "review",
        "HEAD~1..HEAD",
        "--no-augment",
        "--no-open",
        "--timeout",
        str(idle_timeout),
        "--repo-root",
        str(repo),
        *(extra_argv or []),
    ]
    opts = runner.ReviewOptions(
        spec="HEAD~1..HEAD",
        repo_root=repo,
        config=ReviewConfig(runs_root=runs_root, augment=False, open_browser=False, timeout=idle_timeout),
    )
    return runner.run_review(opts, argv=argv)


def _the_run_dir(runs_root: Path) -> paths.RunDir:
    runs = [p for p in runs_root.iterdir() if p.is_dir()]
    assert len(runs) == 1, runs
    return paths.RunDir(runs[0])


@pytest.mark.usefixtures("isolated_config")
def test_run_review_returns_with_the_run_id_while_the_server_lives_on(repo: Path, tmp_path: Path, capsys) -> None:
    runs_root = tmp_path / "runs"

    code = _run_review(repo, runs_root, idle_timeout=2)

    assert code == 0
    out = capsys.readouterr().out.splitlines()
    run_dir = _the_run_dir(runs_root)
    assert out[-1] == f"run_id: {run_dir.slug}"
    assert out[-2].startswith("viewer: http://127.0.0.1:")

    info = stream.read_server_info(run_dir)
    assert info is not None
    record = json.loads(run_dir.server_json.read_text(encoding="utf-8"))
    assert set(record) == {
        "port",
        "pid",
        "started_at",
        "url",
        "version",
        "build",
        "package",
        "counterpart",
        "cwd",
        "argv",
    }
    assert info.pid != 0 and info.url.endswith(f":{info.port}")
    # The server is another process, reachable, and serving this run for Claude.
    with urllib.request.urlopen(info.url + "/data.json", timeout=5) as r:
        data = json.load(r)
    assert data["run_id"] == run_dir.slug
    assert data["counterpart"] == "claude"
    # The child is this build, started from here, and knows how to come back.
    this = identity.this_build()
    assert (info.version, info.build, info.package) == (this.version, this.build, this.package)
    assert info.counterpart == "claude" and info.cwd == os.getcwd()
    assert info.argv is not None and info.argv[-2:] == ("--serve-run", run_dir.slug)
    assert "--runs-root" in info.argv and "--no-augment" in info.argv
    # /health says the same, plus what the server is doing now.
    with urllib.request.urlopen(info.url + "/health", timeout=5) as r:
        health = json.load(r)
    assert health["build"] == this.build and health["run_id"] == run_dir.slug
    assert health["listening"] is False and health["viewers"] == 0

    # No viewer, no --wait: the idle clock runs out and the record goes.
    _wait_until(lambda: not run_dir.server_json.exists(), what="idle shutdown", timeout=15)
    assert not stream.server_alive(info)
    assert "idle timeout" in run_dir.server_log.read_text(encoding="utf-8")


@pytest.mark.usefixtures("isolated_config")
def test_a_second_run_review_reuses_the_live_server(repo: Path, tmp_path: Path, capsys) -> None:
    runs_root = tmp_path / "runs"
    assert _run_review(repo, runs_root, idle_timeout=3) == 0
    run_dir = _the_run_dir(runs_root)
    first = stream.read_server_info(run_dir)
    assert first is not None
    capsys.readouterr()

    assert _run_review(repo, runs_root, idle_timeout=3) == 0

    captured = capsys.readouterr()
    assert captured.out.splitlines()[-1] == f"run_id: {run_dir.slug}"
    assert "already holds this run" in captured.err
    assert stream.read_server_info(run_dir) == first
    _wait_until(lambda: not run_dir.server_json.exists(), what="idle shutdown", timeout=15)


@pytest.mark.usefixtures("isolated_config")
def test_a_sigterm_ends_the_session_cleanly(repo: Path, tmp_path: Path) -> None:
    """A `kill <pid>` of the recorded server takes the record with it, so
    the next `--wait` reads `ended` rather than a stale record."""
    import signal

    runs_root = tmp_path / "runs"
    assert _run_review(repo, runs_root, idle_timeout=60) == 0
    run_dir = _the_run_dir(runs_root)
    info = stream.read_server_info(run_dir)
    assert info is not None

    os.kill(info.pid, signal.SIGTERM)

    _wait_until(lambda: not run_dir.server_json.exists(), what="server.json removed on SIGTERM", timeout=10)
    _wait_until(lambda: not stream.server_alive(info), what="the server to stop answering", timeout=10)


@pytest.mark.usefixtures("isolated_config")
def test_a_stale_server_json_is_replaced(repo: Path, tmp_path: Path) -> None:
    """A record a killed server left behind must not be reused."""
    from semantic_code_review.fetch import materialize_local_diff_run

    runs_root = tmp_path / "runs"
    run_dir = materialize_local_diff_run("HEAD~1..HEAD", runs_root, repo_root=repo)
    run_dir.server_json.write_text(
        json.dumps({"port": 1, "pid": 1, "started_at": 0.0, "url": "http://127.0.0.1:1"}), encoding="utf-8"
    )

    assert _run_review(repo, runs_root, idle_timeout=2) == 0

    info = stream.read_server_info(run_dir)
    assert info is not None and info.port != 1
    _wait_until(lambda: not run_dir.server_json.exists(), what="idle shutdown", timeout=15)


@pytest.mark.usefixtures("isolated_config")
def test_a_child_that_dies_is_reported_with_its_log(repo: Path, tmp_path: Path, capsys) -> None:
    runs_root = tmp_path / "runs"

    code = _run_review(repo, runs_root, idle_timeout=2, extra_argv=["--no-such-option"])

    assert code == 2
    captured = capsys.readouterr()
    run_dir = _the_run_dir(runs_root)
    assert "run_id:" not in captured.out
    assert "did not start" in captured.err
    assert str(run_dir.server_log) in captured.err
    # The child's own complaint is in the log the message points at. Click
    # renders it in a box whose wrapping depends on the terminal width, so
    # compare letters only.
    letters = re.sub(r"[^a-z]", "", run_dir.server_log.read_text(encoding="utf-8", errors="replace").lower())
    assert "nosuchoption" in letters
    assert not run_dir.server_json.exists()


def test_serve_run_refuses_a_directory_that_is_not_a_run(tmp_path: Path, capsys) -> None:
    code = runner.serve_run(paths.RunDir(tmp_path / "nope"), ReviewConfig(augment=False, open_browser=False), argv=())
    assert code == 2
    assert "not a run directory" in capsys.readouterr().err


def test_review_without_a_spec_exits_2() -> None:
    result = CliRunner().invoke(app, ["review"])
    assert result.exit_code == 2
    assert "give a git ref" in (result.stderr or "") + result.stdout


def test_the_child_is_the_same_interpreter() -> None:
    """`-m semantic_code_review.cli` has to resolve: the child is spawned
    with it rather than with whatever `scr` is on PATH."""
    r = subprocess.run(
        [sys.executable, "-m", "semantic_code_review.cli", "--version"], capture_output=True, text=True, check=False
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip()
