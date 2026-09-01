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
    # Drop the cached config so the XDG monkeypatch takes effect — the
    # CLI lazily caches on first access, and an earlier test in this
    # module may already have populated it from the developer's real
    # ~/.config/scr/config.toml.
    from semantic_code_review import cli as cli_module

    cli_module._reset_config_cache()
    runner = CliRunner()
    result = runner.invoke(app, ["config", "show"])
    assert result.exit_code == 0, result.stdout
    assert "absent" in result.stdout
    assert "backend = None" in result.stdout


def test_config_edit_template_appends_block(tmp_path: Path, monkeypatch) -> None:
    """`scr config edit --template groq` appends a [backends.groq] block.

    EDITOR is set to a no-op so the editor never actually opens.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("EDITOR", "true")  # `true` exits 0, prints nothing
    runner = CliRunner()
    result = runner.invoke(app, ["config", "edit", "--template", "groq"])
    assert result.exit_code == 0, result.stdout

    cfg_path = tmp_path / "xdg" / "scr" / "config.toml"
    body = cfg_path.read_text(encoding="utf-8")
    assert "[backends.groq]" in body
    assert "$GROQ_API_KEY" in body  # auth hint


def test_config_edit_template_skips_existing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("EDITOR", "true")
    cfg_path = tmp_path / "xdg" / "scr" / "config.toml"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text(
        '[backends.groq]\nmodel = "llama-3.3-70b-versatile"\n',
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(app, ["config", "edit", "--template", "groq"])
    assert result.exit_code == 0, result.stdout
    # Existing block not duplicated; warning surfaced.
    body = cfg_path.read_text(encoding="utf-8")
    assert body.count("[backends.groq]") == 1
    assert "already in" in (result.stderr or "")


def test_config_edit_unknown_template_errors(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("EDITOR", "true")
    runner = CliRunner()
    result = runner.invoke(app, ["config", "edit", "--template", "carrier-pigeon"])
    assert result.exit_code != 0
    assert "unknown template" in (result.stderr or "") + result.stdout


def test_config_edit_template_openai_compat_scaffold(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("EDITOR", "true")
    runner = CliRunner()
    result = runner.invoke(app, ["config", "edit", "--template", "openai-compat"])
    assert result.exit_code == 0, result.stdout
    body = (tmp_path / "xdg" / "scr" / "config.toml").read_text(encoding="utf-8")
    assert "[backends.openai-compat]" in body
    # Scaffold marks the four required fields uncommented.
    assert 'type = "openai-compat"' in body
    assert 'base_url = "https://api.example.com/v1"' in body


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


# ---------------------------------------------------------------------------
# --explainer-prompt / [augment].explainer_prompt
# ---------------------------------------------------------------------------


def _isolated_config(tmp_path: Path, monkeypatch, body: str) -> None:
    """Point the CLI's config loader at a config file holding `body`.

    The per-repo lookup is stubbed out rather than redirected: it walks
    up to the filesystem root, so a `.scr/config.toml` anywhere above the
    checkout would otherwise merge into these assertions.
    """
    xdg = tmp_path / "xdg"
    (xdg / "scr").mkdir(parents=True)
    (xdg / "scr" / "config.toml").write_text(body, encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))

    from semantic_code_review import cli as cli_module
    from semantic_code_review import config as config_module

    monkeypatch.setattr(config_module, "find_repo_config_path", lambda *_a, **_k: None)
    cli_module._reset_config_cache()


def test_config_show_reports_the_explainer_house_style(tmp_path: Path, monkeypatch) -> None:
    """Reported as a line count and its first line, like extra_prompt —
    a multi-paragraph body printed whole crowds out every other setting.
    """
    _isolated_config(
        tmp_path,
        monkeypatch,
        '[augment]\nexplainer_prompt = """\nName the dataset in every example.\nBe terse.\n"""\n',
    )
    result = CliRunner().invoke(app, ["config", "show"])
    assert result.exit_code == 0, result.stdout
    assert "explainer_prompt = <2-line prompt: 'Name the dataset in every example.'…>" in result.stdout


def test_the_flag_loads_the_house_style_and_beats_the_config(tmp_path: Path, monkeypatch) -> None:
    from semantic_code_review.cli._shared import resolve_explainer_prompt

    _isolated_config(tmp_path, monkeypatch, '[augment]\nexplainer_prompt = "from the config"\n')
    assert resolve_explainer_prompt(None) == "from the config"

    note = tmp_path / "house.md"
    note.write_text("from the file\n", encoding="utf-8")
    assert resolve_explainer_prompt(note) == "from the file"


def test_a_missing_or_empty_house_style_file_is_a_hard_error(tmp_path: Path, monkeypatch) -> None:
    """The user named a file. Reviewing without it is a silently
    different document, so it exits rather than falling back."""
    import pytest
    import typer

    from semantic_code_review.cli._shared import resolve_explainer_prompt

    _isolated_config(tmp_path, monkeypatch, '[augment]\nexplainer_prompt = "from the config"\n')

    with pytest.raises(typer.Exit) as missing:
        resolve_explainer_prompt(tmp_path / "absent.md")
    assert missing.value.exit_code == 2

    empty = tmp_path / "empty.md"
    empty.write_text("   \n", encoding="utf-8")
    with pytest.raises(typer.Exit) as blank:
        resolve_explainer_prompt(empty)
    assert blank.value.exit_code == 2


def test_the_flag_against_a_disabled_explainer_exits(tmp_path: Path, monkeypatch) -> None:
    """`explainer = false` lives in a file the user may not have read
    today; accepting house style for a document that will never be
    generated is a silent no-op on an explicit request."""
    import pytest
    import typer

    from semantic_code_review.cli._shared import resolve_explainer_prompt

    _isolated_config(tmp_path, monkeypatch, "[augment]\nexplainer = false\n")
    note = tmp_path / "house.md"
    note.write_text("name the dataset\n", encoding="utf-8")

    with pytest.raises(typer.Exit) as off:
        resolve_explainer_prompt(note)
    assert off.value.exit_code == 2
    # The inline config value is not an explicit request, so it stays silent.
    assert resolve_explainer_prompt(None) is None


def test_the_plugin_and_the_package_agree_on_the_version() -> None:
    """`bin/scr` installs `semantic-code-review==` the plugin tree's own
    `pyproject.toml` version, so the two version fields are one number
    wearing two hats. Nothing checked them: `release.yml` validates the
    release tag against `pyproject` alone, and `plugin.json` had drifted
    two releases behind before anything noticed.
    """
    import json
    import tomllib

    root = Path(__file__).resolve().parent.parent
    pkg = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    plugin = json.loads((root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]
    assert plugin == pkg, (
        f"plugin.json says {plugin!r} and pyproject.toml says {pkg!r}; "
        "bin/scr installs the pyproject version from PyPI, so a plugin "
        "advertising a different one is advertising a version it will not run"
    )
