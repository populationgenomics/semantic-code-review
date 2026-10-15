"""`scr pr` reads the repo's committed config from the PR's base worktree."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from semantic_code_review import paths
from semantic_code_review.cli import app
from semantic_code_review.cli import pr as pr_cmd
from semantic_code_review.review import pr_flow


def _committed(root: Path, body: str) -> None:
    path = paths.committed_config_path(root)
    path.parent.mkdir(parents=True)
    path.write_text(body, encoding="utf-8")


@pytest.fixture
def captured(tmp_path: Path, monkeypatch) -> list[pr_flow.PrFlowOptions]:
    """Run `scr pr` from a cwd checkout of the same repo, with the flow
    replaced by one that records its options."""
    xdg = tmp_path / "xdg"
    (xdg / "scr").mkdir(parents=True)
    (xdg / "scr" / "config.toml").write_text(
        '[augment]\nskip_globs = ["user/**"]\n[repos."acme/widgets".augment]\nskip_globs = ["entry/**"]\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    cwd = tmp_path / "checkout"
    cwd.mkdir()
    subprocess.run(["git", "init", "-q", str(cwd)], check=True)
    subprocess.run(["git", "-C", str(cwd), "remote", "add", "origin", "git@github.com:acme/widgets.git"], check=True)
    _committed(cwd, '[augment]\nskip_globs = ["cwd/**"]\n')
    monkeypatch.chdir(cwd)

    seen: list[pr_flow.PrFlowOptions] = []

    def fake_flow(opts: pr_flow.PrFlowOptions, **_kw: object) -> int:
        seen.append(opts)
        return 0

    monkeypatch.setattr(pr_cmd, "run_pr_flow", fake_flow)
    result = CliRunner().invoke(app, ["pr", "acme/widgets", "7", "--no-augment", "--runs-root", str(tmp_path / "runs")])
    assert result.exit_code == 0, result.output
    return seen


def test_the_committed_config_comes_from_the_base_worktree(captured, tmp_path: Path) -> None:
    run_dir = paths.RunDir(tmp_path / "runs" / "acme-widgets-7")
    _committed(run_dir.base, '[augment]\nskip_globs = ["base/**"]\n')

    (opts,) = captured
    assert opts.config_for(run_dir).skip_globs == ("user/**", "base/**", "entry/**")


def test_a_base_without_a_committed_config_adds_nothing(captured, tmp_path: Path) -> None:
    run_dir = paths.RunDir(tmp_path / "runs" / "acme-widgets-7")
    run_dir.base.mkdir(parents=True)

    (opts,) = captured
    assert opts.config_for(run_dir).skip_globs == ("user/**", "entry/**")


def test_a_base_committing_a_machine_setting_fails_the_review(captured, tmp_path: Path, capsys) -> None:
    run_dir = paths.RunDir(tmp_path / "runs" / "acme-widgets-7")
    _committed(run_dir.base, 'backend = "groq"\n')

    (opts,) = captured
    with pytest.raises(SystemExit) as e:
        opts.config_for(run_dir)
    assert e.value.code == 1
    assert '[repos."acme/widgets"]' in capsys.readouterr().err
