"""Minimal MCP stdio server: handshake + tools/list + tools/call round-trips."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from semantic_code_review.augment.mcp_server import serve
from semantic_code_review.augment.tools import RepoTools


def _rt(head: Path) -> RepoTools:
    return RepoTools(
        head_worktree=head, repo_git=head, base_sha="x", head_sha="y",
    )


def _request(method: str, *, id_: int | None = 1, params: dict | None = None) -> str:
    req = {"jsonrpc": "2.0", "method": method}
    if id_ is not None:
        req["id"] = id_
    if params is not None:
        req["params"] = params
    return json.dumps(req) + "\n"


def _run(rt: RepoTools, input_text: str) -> list[dict]:
    stdin = io.StringIO(input_text)
    stdout = io.StringIO()
    serve(rt, stdin=stdin, stdout=stdout)
    lines = [ln for ln in stdout.getvalue().splitlines() if ln.strip()]
    return [json.loads(ln) for ln in lines]


def test_initialize_returns_capabilities(tmp_path: Path) -> None:
    rt = _rt(tmp_path)
    responses = _run(rt, _request("initialize", id_=1))
    assert len(responses) == 1
    r = responses[0]
    assert r["id"] == 1
    assert "protocolVersion" in r["result"]
    assert r["result"]["serverInfo"]["name"] == "scr"
    assert r["result"]["capabilities"].get("tools") is not None


def test_initialized_notification_produces_no_response(tmp_path: Path) -> None:
    rt = _rt(tmp_path)
    responses = _run(
        rt, _request("notifications/initialized", id_=None),
    )
    assert responses == []


def test_tools_list_exposes_repo_tools(tmp_path: Path) -> None:
    rt = _rt(tmp_path)
    responses = _run(rt, _request("tools/list", id_=2))
    assert len(responses) == 1
    names = {t["name"] for t in responses[0]["result"]["tools"]}
    assert names == {"read_file", "read_file_at", "outline", "symbol_at", "changed_symbols", "grep", "list_dir", "git_log"}
    for t in responses[0]["result"]["tools"]:
        assert "inputSchema" in t  # MCP uses camelCase


def test_tools_call_read_file_roundtrip(tmp_path: Path) -> None:
    (tmp_path / "hello.txt").write_text("HELLO\nworld\n", encoding="utf-8")
    rt = _rt(tmp_path)
    responses = _run(rt, _request(
        "tools/call", id_=3,
        params={"name": "read_file", "arguments": {"path": "hello.txt"}},
    ))
    assert len(responses) == 1
    r = responses[0]
    assert r["id"] == 3
    assert r["result"]["isError"] is False
    assert "HELLO" in r["result"]["content"][0]["text"]


def test_tools_call_error_surface(tmp_path: Path) -> None:
    rt = _rt(tmp_path)
    responses = _run(rt, _request(
        "tools/call", id_=4,
        params={"name": "read_file", "arguments": {"path": "does-not-exist"}},
    ))
    assert responses[0]["result"]["isError"] is True
    assert "error:" in responses[0]["result"]["content"][0]["text"]


def test_unknown_method_returns_jsonrpc_error(tmp_path: Path) -> None:
    rt = _rt(tmp_path)
    responses = _run(rt, _request("sampling/create", id_=5))
    assert responses[0]["error"]["code"] == -32601


def test_malformed_line_is_skipped(tmp_path: Path) -> None:
    rt = _rt(tmp_path)
    responses = _run(
        rt,
        "{not-json\n" + _request("tools/list", id_=7),
    )
    assert len(responses) == 1
    assert responses[0]["id"] == 7
