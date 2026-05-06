"""Typed tool functions shared by the SDK Agent and the MCP server.

Pydantic-ai's `Agent` registers these via `tools=[...]` and introspects
their signatures + Google-style docstrings to build JSON schemas (and
to validate calls). The MCP stdio server (`mcp_server.py`) imports the
same list to keep its `tools/list` and `tools/call` surface in lockstep
without duplicating the schemas.

Each function delegates to a method on `RepoTools`, the data-only
container the Agent passes as `deps`.
"""

from __future__ import annotations

from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.tools import Tool

from .tools import RepoTools


# --- Tool functions ---------------------------------------------------------
# Names are the wire names — `read_file`, `grep`, etc. — both for the
# Agent (pydantic-ai uses the function's __name__) and for MCP (we
# export them under the same names in the schema list below).


async def read_file(
    ctx: RunContext[RepoTools],
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> str:
    """Read a file from the head worktree. Returns up to 20 KB of text.

    Args:
        path: Path relative to repo root.
        start_line: 1-indexed start line (optional).
        end_line: 1-indexed end line inclusive (optional).
    """
    return ctx.deps.read_file(path, start_line=start_line, end_line=end_line)


async def read_file_at(
    ctx: RunContext[RepoTools],
    sha: str,
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> str:
    """Read a file at a specific commit SHA (e.g. the PR base).

    Use for pre-change content.

    Args:
        sha: Commit SHA.
        path: Path relative to repo root.
        start_line: 1-indexed start line (optional).
        end_line: 1-indexed end line inclusive (optional).
    """
    return ctx.deps.read_file_at(sha, path, start_line=start_line, end_line=end_line)


async def grep(
    ctx: RunContext[RepoTools],
    pattern: str,
    path_glob: str | None = None,
    max_hits: int = 50,
) -> str:
    """Search the head worktree with ripgrep.

    Returns path:line:text matches, capped at 50 by default.

    Args:
        pattern: Pattern to search for.
        path_glob: Restrict to matching files (e.g. 'src/**/*.py').
        max_hits: Maximum matches to return.
    """
    return ctx.deps.grep(pattern, path_glob=path_glob, max_hits=max_hits)


async def list_dir(
    ctx: RunContext[RepoTools],
    path: str = "",
) -> str:
    """List a directory in the head worktree (shallow, hidden files skipped).

    Args:
        path: Path relative to repo root (empty for root).
    """
    return ctx.deps.list_dir(path)


async def git_log(
    ctx: RunContext[RepoTools],
    path: str,
    limit: int = 5,
) -> str:
    """Recent commits touching a path (short form).

    Args:
        path: Path relative to repo root.
        limit: Maximum commits to return.
    """
    return ctx.deps.git_log(path, limit=limit)


TOOL_FUNCTIONS: list = [read_file, read_file_at, grep, list_dir, git_log]


# --- MCP-server bridge ------------------------------------------------------
# `tools/list` over MCP wants `inputSchema` (camelCase) where pydantic-ai
# builds a JSON schema. We let pydantic-ai introspect the same functions
# and reshape the result; that way "what the SDK Agent sees" and "what
# `claude -p` / `gemini` see over MCP" are guaranteed to match.


def mcp_tool_schemas() -> list[dict[str, Any]]:
    """MCP `tools/list` payload, derived from `TOOL_FUNCTIONS`."""
    out: list[dict[str, Any]] = []
    for fn in TOOL_FUNCTIONS:
        tool = Tool(fn)
        out.append(
            {
                "name": tool.name,
                "description": tool.description or "",
                "inputSchema": tool.function_schema.json_schema,
            }
        )
    return out


def mcp_dispatch(repo_tools: RepoTools, name: str, args: dict[str, Any]) -> str:
    """Run a tool by name against `repo_tools` for the MCP `tools/call` path.

    Mirrors the names exposed via `mcp_tool_schemas`. Stays in sync with
    the typed functions above because every name resolves to the matching
    `RepoTools` method.
    """
    method = getattr(repo_tools, name, None)
    if not callable(method) or name.startswith("_"):
        return f"error: unknown tool {name!r}"
    try:
        return method(**args)
    except TypeError as e:
        return f"error: bad args for {name}: {e}"
