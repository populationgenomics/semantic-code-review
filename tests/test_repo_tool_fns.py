"""Direct tests for the introspected tool surface and the MCP bridge."""

from __future__ import annotations

import inspect
import subprocess
from pathlib import Path

import pytest

from semantic_code_review.augment.tools import (
    TOOL_FUNCTIONS,
    RepoTools,
    mcp_dispatch,
    mcp_tool_schemas,
)


def _sh(cwd: Path, *args: str) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> RepoTools:
    root = tmp_path / "wt"
    root.mkdir()
    (root / "a.py").write_text("def foo():\n    return 1\n")
    (root / "b.py").write_text("y = 1\n")
    _sh(root, "git", "init", "-q", "-b", "main")
    _sh(root, "git", "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    _sh(root, "git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    return RepoTools(head_worktree=root, repo_git=root, base_sha=head, head_sha=head)


# ---------------------------------------------------------------------------
# Schema generation: source-of-truth check
# ---------------------------------------------------------------------------

def test_schemas_cover_every_tool_function() -> None:
    schemas = mcp_tool_schemas()
    schema_names = [s["name"] for s in schemas]
    fn_names = [fn.__name__ for fn in TOOL_FUNCTIONS]
    assert schema_names == fn_names


def test_each_schema_uses_input_schema_camelcase() -> None:
    """MCP wants `inputSchema`, not Anthropic-style `input_schema`."""
    schemas = mcp_tool_schemas()
    for s in schemas:
        assert "inputSchema" in s
        assert "input_schema" not in s


def test_pydantic_ai_and_mcp_surfaces_match() -> None:
    """The two surfaces are derived from the same `RepoTools` methods.

    Same names, same parameter shapes (modulo the `RunContext` injected
    on the pydantic-ai side, which doesn't appear in the MCP schema).
    """
    schemas = {s["name"]: s for s in mcp_tool_schemas()}
    fns = {fn.__name__: fn for fn in TOOL_FUNCTIONS}

    assert set(schemas) == set(fns)

    for name, fn in fns.items():
        sig = inspect.signature(fn)
        # Drop the RunContext parameter; everything else should match the schema.
        fn_params = [p for p in sig.parameters.values() if p.name != "ctx"]
        schema_props = schemas[name]["inputSchema"].get("properties", {})
        schema_required = set(schemas[name]["inputSchema"].get("required", []))

        assert {p.name for p in fn_params} == set(schema_props), name
        # Required params on each side must match: positional-with-no-default
        # in the function ↔ listed in `required` in the JSON schema.
        fn_required = {
            p.name for p in fn_params if p.default is inspect.Parameter.empty
        }
        assert fn_required == schema_required, name


def test_surface_matches_marked_repo_tools_methods() -> None:
    """Every exported function corresponds to an `@_tool`-marked method."""
    from semantic_code_review.augment.tools import _TOOL_EXPORT_ATTR

    marked = {
        name
        for name, attr in vars(RepoTools).items()
        if callable(attr) and getattr(attr, _TOOL_EXPORT_ATTR, False)
    }
    assert {fn.__name__ for fn in TOOL_FUNCTIONS} == marked
    assert {s["name"] for s in mcp_tool_schemas()} == marked


def test_read_file_schema_marks_path_required() -> None:
    schemas = {s["name"]: s for s in mcp_tool_schemas()}
    rf = schemas["read_file"]
    assert "path" in rf["inputSchema"]["required"]
    # Optional kwargs must NOT be required.
    assert "start_line" not in rf["inputSchema"]["required"]


# ---------------------------------------------------------------------------
# mcp_dispatch routes by name
# ---------------------------------------------------------------------------

def test_dispatch_read_file(repo: RepoTools) -> None:
    out = mcp_dispatch(repo, "read_file", {"path": "a.py"})
    assert "def foo" in out


def test_dispatch_unknown_tool_returns_error(repo: RepoTools) -> None:
    out = mcp_dispatch(repo, "nope", {})
    assert out.startswith("error: unknown tool")


def test_dispatch_rejects_dunder_attrs(repo: RepoTools) -> None:
    """Defends against name-injection — `_is_inside`, `__class__` etc."""
    out = mcp_dispatch(repo, "__class__", {})
    assert out.startswith("error: unknown tool")


def test_dispatch_bad_args_surfaces_error(repo: RepoTools) -> None:
    out = mcp_dispatch(repo, "read_file", {"bogus_kwarg": 1})
    assert out.startswith("error: bad args")


def test_dispatch_grep(repo: RepoTools) -> None:
    out = mcp_dispatch(repo, "grep", {"pattern": "foo"})
    assert "a.py" in out


def test_dispatch_list_dir_default(repo: RepoTools) -> None:
    out = mcp_dispatch(repo, "list_dir", {})
    assert "a.py" in out
