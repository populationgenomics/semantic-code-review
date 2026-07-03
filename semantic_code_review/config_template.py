"""Render `[backends.<name>]` blocks for `scr config edit --template`.

Pulls the per-field doc strings out of `BackendDef`'s Annotated
metadata (via `field_doc`) and emits them as TOML comments above
each line. The lead block is the builtin's `description` (or a
generic hint for the openai-compat scaffold) plus an auth hint
derived from `api_key_env` / `api_key_command`.

Two modes:

- **Builtin override.** Caller passes a known builtin name; we render
  every line commented out with the builtin's actual value, so the
  user uncomments only the fields they want to override.

- **OpenAI-compat scaffold.** Caller passes the literal name
  `"openai-compat"` to get a placeholder block where the four
  required fields (type, base_url, api_key_env, model) are
  uncommented with `<...>`-style placeholders the user fills in.
  The user also renames the `[backends.openai-compat]` heading to
  whatever they prefer.
"""

from __future__ import annotations

import dataclasses
import shlex
import textwrap

from .config import BUILTIN_BACKENDS, BackendDef, BackendType, field_doc

SCAFFOLD_SECTION_NAME = "openai-compat"

# Placeholder values for the openai-compat scaffold. The user
# replaces each before saving.
_SCAFFOLD_PLACEHOLDERS: dict[str, str] = {
    "type": '"openai-compat"',
    "base_url": '"https://api.example.com/v1"',
    "api_key_env": '"EXAMPLE_API_KEY"',
    "default_model": '"<model-id>"',
    "api_key_command": '"<argv-string-or-list>"',
}

# Which scaffold fields are "must fill in" (uncommented) vs "optional
# alternative" (commented). api_key_command is mutually exclusive
# with api_key_env in practice — uncomment whichever you want.
_SCAFFOLD_REQUIRED: frozenset[str] = frozenset(
    {
        "type",
        "base_url",
        "api_key_env",
        "default_model",
    }
)


def render_backend_template(name: str) -> str:
    """Render a TOML `[backends.<name>]` block, ready to append to a config file.

    `name` either matches a builtin (override mode) or equals
    `SCAFFOLD_SECTION_NAME` (scaffold mode). Any other name raises.
    """
    bdef = BUILTIN_BACKENDS.get(name)
    if bdef is None and name != SCAFFOLD_SECTION_NAME:
        raise ValueError(
            f"unknown template {name!r}; expected one of: "
            + ", ".join(sorted([SCAFFOLD_SECTION_NAME, *BUILTIN_BACKENDS]))
        )
    is_scaffold = bdef is None

    out: list[str] = []

    # Lead comments: description (or scaffold hint) then auth hint.
    lead = _lead_comment(bdef)
    if lead:
        out.extend(lead)
        out.append("")

    out.append(f"[backends.{name}]")

    for f in dataclasses.fields(BackendDef):
        if f.name == "description":
            continue  # already rendered as the lead comment block
        out.append("")
        doc = field_doc(f.name)
        if doc:
            out.extend(_wrap_comment(doc))
        toml_key = "model" if f.name == "default_model" else f.name
        commented = _is_commented(f.name, bdef, is_scaffold)
        rendered_value = _render_value(f.name, bdef, is_scaffold)
        prefix = "# " if commented else ""
        out.append(f"{prefix}{toml_key} = {rendered_value}")

    out.append("")
    return "\n".join(out)


def _lead_comment(bdef: BackendDef | None) -> list[str]:
    if bdef is None:
        lines = _wrap_comment(
            "Generic OpenAI-compatible endpoint scaffold. Rename the "
            "section heading, then fill in base_url, an auth field "
            "(api_key_env or api_key_command), and a model id."
        )
        return lines
    lines: list[str] = []
    if bdef.description:
        lines.extend(_wrap_comment(bdef.description))
    auth = _auth_hint(bdef)
    if auth:
        lines.extend(_wrap_comment(auth))
    return lines


def _auth_hint(bdef: BackendDef) -> str:
    if bdef.api_key_env and bdef.api_key_command:
        return f"Auth: set ${bdef.api_key_env} (or rely on the configured api_key_command fallback)."
    if bdef.api_key_env:
        return f"Auth: set ${bdef.api_key_env} (or use api_key_command to fetch from a secret store)."
    if bdef.api_key_command:
        return "Auth: configured via api_key_command."
    return ""


def _is_commented(field_name: str, bdef: BackendDef | None, is_scaffold: bool) -> bool:
    """Builtin lines are always commented (override hint). Scaffold
    lines for required fields are uncommented; optional ones (the
    api_key_command alternative) stay commented.
    """
    if is_scaffold:
        return field_name not in _SCAFFOLD_REQUIRED
    return True


def _render_value(field_name: str, bdef: BackendDef | None, is_scaffold: bool) -> str:
    if is_scaffold:
        return _SCAFFOLD_PLACEHOLDERS.get(field_name, '""')
    assert bdef is not None
    value = getattr(bdef, field_name)
    if value is None:
        return _SCAFFOLD_PLACEHOLDERS.get(field_name, '""')
    if isinstance(value, BackendType):
        return f'"{value.value}"'
    if isinstance(value, str):
        return f'"{value}"'
    if isinstance(value, tuple):
        # Render argv as a shell-quoted string for ergonomic editing.
        return f'"{shlex.join(value)}"'
    return repr(value)


def _wrap_comment(text: str, width: int = 70) -> list[str]:
    return [f"# {line}" for line in textwrap.wrap(text, width=width)]


__all__ = ["SCAFFOLD_SECTION_NAME", "render_backend_template"]
