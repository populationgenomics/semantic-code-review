"""Minimal stdio MCP server exposing `RepoTools` to `claude -p`.

Used by `ClaudeCLIClient` when a RepoTools instance is available: we
inject this server via `claude -p --mcp-config ... --strict-mcp-config`
so the agentic loop can still `read_file`, `grep`, `list_dir`,
`read_file_at`, and `git_log` against the run's worktrees — matching
the behaviour of the API backend's in-process tool loop.

Protocol: JSON-RPC 2.0 over newline-delimited JSON on stdio, per the
MCP stdio transport spec. Only a subset is implemented:

  - initialize               (request)
  - notifications/initialized (notification, no response)
  - tools/list               (request)
  - tools/call               (request)

Anything else returns a JSON-RPC method-not-found error. No resources,
prompts, or sampling — we don't need them for read-only repo access.

Run directly::

    python -m semantic_code_review.augment.mcp_server \
        --head-worktree <path> --repo-git <path> \
        --base-sha <sha> --head-sha <sha>
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from .repo_tool_fns import mcp_dispatch, mcp_tool_schemas
from .tools import RepoTools


log = logging.getLogger("scr.mcp_server")


PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "scr"
SERVER_VERSION = "0.1.0"


def _make_error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _make_result(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _handle(req: dict[str, Any], repo_tools: RepoTools) -> dict[str, Any] | None:
    """Return a response dict, or None for notifications."""
    method = req.get("method", "")
    req_id = req.get("id")
    params = req.get("params") or {}
    is_notification = "id" not in req

    if method == "initialize":
        return _make_result(req_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })

    if method == "notifications/initialized":
        # Notification: no response.
        return None

    if method == "tools/list":
        return _make_result(req_id, {"tools": mcp_tool_schemas()})

    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments") or {}
        try:
            output = mcp_dispatch(repo_tools, name, args)
            return _make_result(req_id, {
                "content": [{"type": "text", "text": output}],
                "isError": output.startswith("error:"),
            })
        except Exception as e:  # noqa: BLE001
            return _make_result(req_id, {
                "content": [{"type": "text", "text": f"error: {e}"}],
                "isError": True,
            })

    if is_notification:
        # Drop unknown notifications silently.
        return None

    return _make_error(req_id, -32601, f"method not found: {method}")


def serve(repo_tools: RepoTools, stdin: Any = None, stdout: Any = None) -> None:
    """Loop over newline-delimited JSON-RPC on stdio until EOF."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for raw in stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            log.warning("dropping malformed JSON-RPC line: %s", e)
            continue
        try:
            response = _handle(req, repo_tools)
        except Exception as e:  # noqa: BLE001
            log.exception("handler crashed on request %s", req.get("method"))
            response = _make_error(req.get("id"), -32603, f"internal error: {e}")
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--head-worktree", required=True, type=Path)
    parser.add_argument("--repo-git", required=True, type=Path)
    parser.add_argument("--base-sha", default="")
    parser.add_argument("--head-sha", default="")
    parser.add_argument("--log-file", type=Path, default=None)
    args = parser.parse_args(argv)

    if args.log_file is not None:
        logging.basicConfig(
            filename=str(args.log_file), level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )

    repo_tools = RepoTools(
        head_worktree=args.head_worktree,
        repo_git=args.repo_git,
        base_sha=args.base_sha,
        head_sha=args.head_sha,
    )
    serve(repo_tools)
    return 0


if __name__ == "__main__":
    sys.exit(main())
