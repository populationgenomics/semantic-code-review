"""HTTP MCP host: serves the RepoTools surface over Streamable-HTTP with
bearer auth, driven by the mcp SDK client (ADR 0003 Slice 3)."""

from __future__ import annotations

import contextlib
import subprocess
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path

import httpx
import mcp
import mcp.types
import pytest
from mcp.client.streamable_http import streamable_http_client

from semantic_code_review.augment import mcp_http_host, source_cache
from semantic_code_review.augment.tools import RepoTools


def _sh(cwd: Path, *args: str) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def _first_text(result: mcp.types.CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, mcp.types.TextContent)
    return block.text


@contextlib.asynccontextmanager
async def _session(url: str, token: str | None) -> AsyncGenerator[mcp.ClientSession]:
    """An initialized MCP client session against `url`, authed with `token`.

    `token=None` sends no Authorization header, so `initialize()` raises —
    the auth-rejection tests wrap this in `pytest.raises`.
    """
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    async with httpx.AsyncClient(headers=headers) as http_client:
        async with streamable_http_client(url, http_client=http_client) as (read, write, _):
            async with mcp.ClientSession(read, write) as session:
                await session.initialize()
                yield session


@pytest.fixture
def repo_tools(tmp_path: Path) -> RepoTools:
    root = tmp_path / "worktree"
    root.mkdir()
    (root / "a.py").write_text("def foo():\n    return 1\n")
    _sh(root, "git", "init", "-q", "-b", "main")
    _sh(root, "git", "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    _sh(root, "git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    return RepoTools(head_worktree=root, repo_git=root, base_sha=head, head_sha=head, cache=source_cache.SourceCache())


@pytest.fixture
def calls() -> list[tuple[str, dict]]:
    return []


@pytest.fixture
def host(repo_tools: RepoTools, calls: list[tuple[str, dict]]) -> Iterator[mcp_http_host.McpHttpHost]:
    h = mcp_http_host.McpHttpHost(repo_tools, on_tool=lambda n, a: calls.append((n, a)))
    h.start()
    try:
        yield h
    finally:
        h.stop()


async def test_lists_the_repo_tool_surface(host: mcp_http_host.McpHttpHost) -> None:
    async with _session(host.url, host.token) as session:
        result = await session.list_tools()
    names = {t.name for t in result.tools}
    assert {"read_file", "outline", "grep"} <= names


async def test_calls_a_tool_and_fires_on_tool(host: mcp_http_host.McpHttpHost, calls: list[tuple[str, dict]]) -> None:
    async with _session(host.url, host.token) as session:
        result = await session.call_tool("read_file", {"path": "a.py"})
    assert "def foo" in _first_text(result)
    assert calls == [("read_file", {"path": "a.py"})]


async def test_outline_returns_json(host: mcp_http_host.McpHttpHost) -> None:
    async with _session(host.url, host.token) as session:
        result = await session.call_tool("outline", {"path": "a.py"})
    assert '"foo"' in _first_text(result)


async def test_rejects_missing_token(host: mcp_http_host.McpHttpHost) -> None:
    with pytest.raises(Exception):  # noqa: B017 — any transport/auth error is a pass
        async with _session(host.url, None):
            pass


async def test_rejects_wrong_token(host: mcp_http_host.McpHttpHost) -> None:
    with pytest.raises(Exception):  # noqa: B017
        async with _session(host.url, "nope"):
            pass


def test_mcp_config_shape(host: mcp_http_host.McpHttpHost) -> None:
    cfg = host.mcp_config()
    assert cfg["type"] == "http"
    assert cfg["url"] == host.url
    assert cfg["headers"]["Authorization"] == f"Bearer {host.token}"


def test_url_before_start_raises(repo_tools: RepoTools) -> None:
    h = mcp_http_host.McpHttpHost(repo_tools)
    with pytest.raises(RuntimeError):
        _ = h.url
