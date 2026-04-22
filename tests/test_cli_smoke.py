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
