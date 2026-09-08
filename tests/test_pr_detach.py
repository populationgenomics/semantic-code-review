"""`scr pr` detaches like `scr review`: the server outlives the CLI, with
GitHub as the counterpart.

`run_pr_flow` resolves and materialises the PR run (faked here — no
network), spawns the server as a detached child (`python -m
semantic_code_review.cli pr … --serve-run <slug>`) and returns once
`server.json` appears. The child reads the PR off `meta.json`, looks for
a pending review through `gh` (a fake on PATH here), and serves until
idle.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import time
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from semantic_code_review import paths
from semantic_code_review.cli import app
from semantic_code_review.review import pr_flow, stream
from semantic_code_review.review.config import ReviewConfig

_STATE = {
    "data": {
        "viewer": {"login": "me"},
        "repository": {"pullRequest": {"id": "PR_1", "reviews": {"nodes": []}}},
    }
}


@pytest.fixture
def fake_gh(tmp_path: Path, monkeypatch) -> Path:
    """A `gh` on PATH that answers `api graphql` with a PR holding no
    pending review, and records every invocation."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "gh-calls.log"
    script = bin_dir / "gh"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"open({str(calls)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        "if '--version' in sys.argv:\n"
        "    print('gh version 2.60.0 (2026-01-01)')\n"
        "elif 'graphql' in sys.argv:\n"
        f"    print(json.dumps({json.dumps(_STATE)}))\n"
        "else:\n"
        "    sys.exit(1)\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    return calls


def _fake_materialize(runs_root: Path) -> paths.RunDir:
    """What `materialize_github_pr_run` leaves for a PR: metadata naming
    the PR, and the raw diff."""
    run_dir = paths.RunDir(runs_root / "o-r-pr7-abc12345").create()
    meta = {
        "title": "t",
        "url": "https://github.com/o/r/pull/7",
        "number": 7,
        "baseRefOid": "b" * 40,
        "headRefOid": "abc12345" + "0" * 32,
    }
    run_dir.meta.write_text(json.dumps(meta), encoding="utf-8")
    diff = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
    run_dir.raw_diff.write_text(diff, encoding="utf-8")
    return run_dir


def _wait_until(predicate, *, what: str, timeout: float) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _run_pr(runs_root: Path, *, idle_timeout: int) -> int:
    argv = ["pr", "o/r", "--no-augment", "--no-open", "--timeout", str(idle_timeout)]
    opts = pr_flow.PrFlowOptions(
        repo="o/r",
        number=7,
        config=ReviewConfig(runs_root=runs_root, augment=False, open_browser=False, timeout=idle_timeout),
    )
    with (
        patch.object(pr_flow, "preflight_gh", lambda: None),
        patch.object(pr_flow, "materialize_github_pr_run", lambda _url, root: _fake_materialize(root)),
    ):
        return pr_flow.run_pr_flow(opts, argv=argv)


def test_scr_pr_returns_with_the_run_id_while_the_server_serves_github_mode(
    fake_gh: Path, tmp_path: Path, capsys
) -> None:
    runs_root = tmp_path / "runs"

    code = _run_pr(runs_root, idle_timeout=2)

    assert code == 0
    out = capsys.readouterr().out.splitlines()
    run_dir = paths.RunDir(runs_root / "o-r-pr7-abc12345")
    assert out[-1] == f"run_id: {run_dir.slug}"
    assert out[-2].startswith("viewer: http://127.0.0.1:")
    info = stream.read_server_info(run_dir)
    assert info is not None
    with urllib.request.urlopen(info.url + "/data.json", timeout=5) as r:
        data = json.load(r)
    assert data["counterpart"] == "github"
    assert data["pending_review"] == {"unsent": [], "submitted_url": None, "submitted_from": None, "unanchored": 0}
    # The child looked for a pending review through gh before binding.
    assert "api graphql" in fake_gh.read_text(encoding="utf-8")

    _wait_until(lambda: not run_dir.server_json.exists(), what="idle shutdown", timeout=15)
    assert not stream.server_alive(info)


def test_scr_pr_reuses_a_live_server(fake_gh: Path, tmp_path: Path, capsys) -> None:
    runs_root = tmp_path / "runs"
    assert _run_pr(runs_root, idle_timeout=3) == 0
    first = stream.read_server_info(paths.RunDir(runs_root / "o-r-pr7-abc12345"))
    capsys.readouterr()

    assert _run_pr(runs_root, idle_timeout=3) == 0

    captured = capsys.readouterr()
    assert "scr pr: a server already holds this run" in captured.err
    assert stream.read_server_info(paths.RunDir(runs_root / "o-r-pr7-abc12345")) == first
    _wait_until(lambda: not (runs_root / "o-r-pr7-abc12345" / "server.json").exists(), what="idle shutdown", timeout=15)


def test_serve_pr_run_refuses_a_run_that_is_not_a_pr(tmp_path: Path, capsys) -> None:
    run_dir = paths.RunDir(tmp_path / "local-run").create()
    run_dir.meta.write_text(json.dumps({"title": "t", "headRefOid": "abc"}), encoding="utf-8")
    assert pr_flow.serve_pr_run(run_dir, ReviewConfig(augment=False, open_browser=False)) == 2
    assert "no url" in capsys.readouterr().err
    assert pr_flow.serve_pr_run(paths.RunDir(tmp_path / "nope"), ReviewConfig(augment=False)) == 2


def test_yes_is_no_longer_an_option() -> None:
    result = CliRunner().invoke(app, ["pr", "o/r", "7", "--yes"])
    assert result.exit_code == 2
    # Click boxes and colours the complaint at the terminal's width, so
    # compare letters only.
    letters = re.sub(r"[^a-z]", "", ((result.stderr or "") + result.stdout).lower())
    assert "nosuchoption" in letters and "yes" in letters
