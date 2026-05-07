"""Template rendering for `scr config edit --template <name>`."""

from __future__ import annotations

import tomllib

import pytest

from semantic_code_review.config import BUILTIN_BACKENDS, ScrConfig
from semantic_code_review.config_template import (
    SCAFFOLD_SECTION_NAME,
    render_backend_template,
)


def test_render_builtin_emits_section_header() -> None:
    block = render_backend_template("groq")
    assert "[backends.groq]" in block


def test_render_builtin_lead_uses_description() -> None:
    block = render_backend_template("groq")
    expected = BUILTIN_BACKENDS["groq"].description.split(".")[0]
    assert expected in block


def test_render_builtin_emits_per_field_doc_comments() -> None:
    block = render_backend_template("groq")
    # Fields that are set on the builtin should appear with their value.
    assert '# base_url = "https://api.groq.com/openai/v1"' in block
    assert '# api_key_env = "GROQ_API_KEY"' in block
    assert '# model = "llama-3.3-70b-versatile"' in block
    # Doc comments above each line.
    assert "Endpoint URL" in block
    assert "Env var holding the bearer" in block


def test_render_builtin_emits_auth_hint() -> None:
    block = render_backend_template("groq")
    assert "$GROQ_API_KEY" in block


def test_render_builtin_renders_argv_command_as_shell_string() -> None:
    """github builtin's api_key_command tuple should round-trip as
    a shell-quoted string, ergonomic for editing."""
    block = render_backend_template("github")
    assert '# api_key_command = "gh auth token"' in block


def test_render_scaffold_uses_uncommented_required_fields() -> None:
    block = render_backend_template(SCAFFOLD_SECTION_NAME)
    # Required scaffold fields are uncommented placeholders.
    assert 'type = "openai-compat"' in block
    assert 'base_url = "https://api.example.com/v1"' in block
    assert 'api_key_env = "EXAMPLE_API_KEY"' in block
    assert 'model = "<model-id>"' in block
    # api_key_command is the alternative — kept commented.
    assert '# api_key_command =' in block


def test_render_unknown_template_raises() -> None:
    with pytest.raises(ValueError, match="unknown template"):
        render_backend_template("not-a-thing")


def test_render_output_is_valid_toml_when_uncommented() -> None:
    """A user uncommenting every line of a builtin override should
    produce parseable TOML — sanity check on the line shape."""
    block = render_backend_template("groq")
    # Strip the leading "# " from every override line.
    body = "\n".join(
        line[2:] if line.startswith("# ") and "=" in line else line
        for line in block.splitlines()
    )
    # Comments + section header + uncommented overrides.
    parsed = tomllib.loads(body)
    assert "backends" in parsed
    assert parsed["backends"]["groq"]["base_url"] == "https://api.groq.com/openai/v1"


def test_appended_template_still_loads_via_scrconfig(tmp_path) -> None:
    """End-to-end: append a builtin template, uncomment a model
    override, and verify ScrConfig.load picks it up."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '# header\nbackend = "claude-api"\n', encoding="utf-8"
    )
    block = render_backend_template("groq")
    # Simulate user uncommenting the model line.
    block = block.replace(
        '# model = "llama-3.3-70b-versatile"',
        'model = "llama-3.3-70b-versatile"',
    )
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + "\n" + block,
        encoding="utf-8",
    )
    cfg = ScrConfig.load(user_path=config_path, repo_path=None)
    assert cfg.backends["groq"].default_model == "llama-3.3-70b-versatile"
