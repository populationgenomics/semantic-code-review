"""CLI smoke tests for offline commands: strip, lint, show."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from semantic_code_review.cli import app


FIXTURE = Path(__file__).parent / "fixtures" / "sample.augmented.diff"


def test_strip_prints_clean_diff() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["strip", str(FIXTURE)])
    assert result.exit_code == 0
    # No annotation lines should remain.
    assert "#scr:" not in result.stdout
    assert "#scr>" not in result.stdout
    assert "diff --git" in result.stdout


def test_lint_ok_on_fixture() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["lint", str(FIXTURE)])
    assert result.exit_code == 0, result.stdout + "\n" + (result.stderr or "")


def test_version_flag_prints_pyproject_version() -> None:
    from importlib.metadata import version as pkg_version
    runner = CliRunner()
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == pkg_version("semantic-code-review")


def test_config_path_prints_xdg_path(tmp_path: Path, monkeypatch) -> None:
    """`scr config path` should reflect $XDG_CONFIG_HOME when set."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    runner = CliRunner()
    result = runner.invoke(app, ["config", "path"])
    assert result.exit_code == 0, result.stdout
    assert result.stdout.strip().endswith("/xdg/scr/config.toml")


def test_config_show_reports_absent_user_config(tmp_path: Path, monkeypatch) -> None:
    """When no config files exist, `scr config show` still runs cleanly."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    runner = CliRunner()
    result = runner.invoke(app, ["config", "show"])
    assert result.exit_code == 0, result.stdout
    assert "absent" in result.stdout
    assert "backend = None" in result.stdout


def test_lint_fails_on_bad_smell(tmp_path: Path) -> None:
    p = tmp_path / "bad.diff"
    p.write_text(FIXTURE.read_text().replace("string-sql", "made-up-smell"))
    runner = CliRunner()
    result = runner.invoke(app, ["lint", str(p)])
    assert result.exit_code == 1
    combined = result.stdout + (getattr(result, "stderr", "") or "")
    assert "made-up-smell" in combined


def test_show_prints_augmented(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / "augmented.diff").write_text(FIXTURE.read_text())
    runner = CliRunner()
    result = runner.invoke(app, ["show", str(run)])
    assert result.exit_code == 0
    assert "scr-version" in result.stdout


def test_render_runs_against_fixture(tmp_path: Path) -> None:
    """Render works with just augmented.diff present (sidecar fallback)."""
    run = tmp_path / "run"
    run.mkdir()
    (run / "augmented.diff").write_text(FIXTURE.read_text())
    out = tmp_path / "review.html"
    runner = CliRunner()
    result = runner.invoke(app, ["render", str(run), "--out", str(out)])
    assert result.exit_code == 0, result.stdout + "\n" + (getattr(result, "stderr", "") or "")
    assert out.exists()
    html = out.read_text()
    assert "Pagination is introduced" in html
    assert "string-sql" in html
    assert 'data-fold="segments"' in html
