"""`ClaudeClient` implementation that shells out to `claude -p`.

Used when no `ANTHROPIC_API_KEY` is set but the `claude` CLI is on PATH
(i.e. the user has a Claude Code subscription). Each `create_message`
call spawns a `claude -p` subprocess with `--json-schema` set to the
expected submit-tool schema, parses the structured result, and returns
a dict shaped like `runner._message_to_dict` so the rest of the
pipeline is unchanged.

If a `RepoTools` has been attached (via `set_repo_tools`), a stdio MCP
server is also injected via `--mcp-config`, so the model can explore
the worktree during the call — matching the behaviour of the API
backend's in-process tool loop. Without `RepoTools`, the client runs
single-shot and the model answers from the hunk text alone.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from .tools import RepoTools


log = logging.getLogger(__name__)


class ClaudeCLINotFound(RuntimeError):
    pass


class ClaudeCLIError(RuntimeError):
    """Raised for non-zero exits from `claude -p`.

    The string form includes common transient markers ("rate",
    "overloaded") when stderr suggests them, so the existing
    `_call_with_backoff` retry predicate picks them up.
    """


class ClaudeCLIClient:
    """Protocol impl over `claude -p`. Pure single-shot in v1."""

    # Used by pipeline.py to detect CLI mode and drop concurrency.
    is_subprocess_backend = True

    def __init__(
        self,
        *,
        claude_path: str | None = None,
        fallback_model: str | None = "claude-sonnet-4-6",
        max_turns_single_shot: int = 1,
        max_turns_with_mcp: int = 20,
    ) -> None:
        resolved = claude_path or shutil.which("claude")
        if not resolved:
            raise ClaudeCLINotFound("`claude` not on PATH")
        self._claude = resolved
        self._fallback_model = fallback_model
        self._max_turns_single_shot = max_turns_single_shot
        self._max_turns_with_mcp = max_turns_with_mcp
        self._repo_tools: RepoTools | None = None
        self._mcp_config_path: Path | None = None

    def set_repo_tools(self, repo_tools: RepoTools | None) -> None:
        """Bind a `RepoTools` so subsequent calls get MCP-backed repo access.

        Called by the augment pipeline once it has resolved the run dir
        (head worktree + repo.git + SHAs). Calling with None disables MCP
        injection and reverts to single-shot mode.
        """
        self._repo_tools = repo_tools
        # Invalidate any prior config file; it will be rewritten on next
        # create_message if MCP is still active.
        if self._mcp_config_path is not None and self._mcp_config_path.exists():
            try:
                self._mcp_config_path.unlink()
            except OSError:
                pass
        self._mcp_config_path = None

    def _ensure_mcp_config(self) -> Path:
        if self._mcp_config_path is not None and self._mcp_config_path.exists():
            return self._mcp_config_path
        assert self._repo_tools is not None
        rt = self._repo_tools
        # Ensure the child python can import `semantic_code_review` even
        # when launched with an unrelated cwd: prepend our package root to
        # PYTHONPATH. In the installed plugin this is redundant (the venv
        # has the package on site-packages), but it's cheap insurance for
        # dev environments and editable checkouts.
        import semantic_code_review as _pkg
        pkg_root = str(Path(_pkg.__file__).resolve().parent.parent)
        existing_pp = os.environ.get("PYTHONPATH", "")
        pythonpath = (
            f"{pkg_root}{os.pathsep}{existing_pp}" if existing_pp else pkg_root
        )
        # `claude -p` launches stdio MCP servers as subprocesses itself; it
        # owns stdin/stdout of the child. We just name the entrypoint.
        config = {
            "mcpServers": {
                "scr": {
                    "type": "stdio",
                    "command": sys.executable,
                    "args": [
                        "-m", "semantic_code_review.augment.mcp_server",
                        "--head-worktree", str(rt.head_worktree),
                        "--repo-git", str(rt.repo_git),
                        "--base-sha", rt.base_sha,
                        "--head-sha", rt.head_sha,
                    ],
                    "env": {"PYTHONPATH": pythonpath},
                }
            }
        }
        fd, path = tempfile.mkstemp(prefix="scr-mcp-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(config, fh)
        self._mcp_config_path = Path(path)
        return self._mcp_config_path

    async def aclose(self) -> None:
        if self._mcp_config_path is not None and self._mcp_config_path.exists():
            try:
                self._mcp_config_path.unlink()
            except OSError:
                pass
        self._mcp_config_path = None

    async def create_message(self, **kwargs: Any) -> dict:
        model: str = kwargs["model"]
        system_blocks: list[dict[str, Any]] = kwargs.get("system", []) or []
        tools: list[dict[str, Any]] = kwargs.get("tools", []) or []
        messages: list[dict[str, Any]] = kwargs.get("messages", []) or []

        submit_tool = _pick_submit_tool(tools)
        if submit_tool is None:
            raise ClaudeCLIError(
                "ClaudeCLIClient requires a `submit_*` tool in `tools` "
                "to drive --json-schema output"
            )

        system_text = _flatten_system(system_blocks)
        prompt = _serialize_messages(messages, submit_tool)
        schema_json = json.dumps(submit_tool["input_schema"], ensure_ascii=False)

        mcp_active = self._repo_tools is not None
        max_turns = (
            self._max_turns_with_mcp if mcp_active else self._max_turns_single_shot
        )

        argv = [
            self._claude, "-p",
            "--model", model,
            "--system-prompt", system_text,
            "--json-schema", schema_json,
            "--tools", "",
            "--bare",
            "--no-session-persistence",
            "--setting-sources", "",
            "--permission-mode", "bypassPermissions",
            "--output-format", "json",
            "--max-turns", str(max_turns),
        ]
        if self._fallback_model:
            argv += ["--fallback-model", self._fallback_model]
        if mcp_active:
            argv += [
                "--mcp-config", str(self._ensure_mcp_config()),
                "--strict-mcp-config",
            ]

        log.debug("claude -p invocation: %s", " ".join(argv[:7] + ["<prompt>"]))

        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate(prompt.encode("utf-8"))

        # `claude -p` frequently writes the real failure into the stdout
        # envelope (is_error=true, result=<message>) and exits non-zero
        # with empty stderr — e.g. when not logged in. So we parse stdout
        # first regardless of exit code, and only fall back to stderr if
        # stdout isn't a usable envelope.
        envelope: dict[str, Any] | None = None
        try:
            envelope = _parse_envelope(stdout, stderr)
        except ClaudeCLIError:
            if proc.returncode != 0:
                raise ClaudeCLIError(
                    f"claude -p exited {proc.returncode}: "
                    f"{_tail(stderr.decode('utf-8', errors='replace')) or '<no stderr>'}"
                )
            raise

        if envelope.get("is_error") or proc.returncode != 0:
            message = (envelope.get("result") or "").strip()
            if "not logged in" in message.lower() or "please run /login" in message.lower():
                raise ClaudeCLIError(
                    "claude is not logged in; run `claude /login` (or "
                    "`claude setup-token` for a long-lived token) before "
                    "using --backend=cli."
                )
            raise ClaudeCLIError(
                f"claude -p returned error (exit {proc.returncode}): {message or '<empty>'}"
            )

        result_text = envelope.get("result") or ""
        try:
            structured = json.loads(result_text)
        except json.JSONDecodeError as e:
            raise ClaudeCLIError(
                f"claude -p result is not valid JSON (schema mode): {e}; "
                f"result[:200]={result_text[:200]!r}"
            ) from e

        return _synthesize_tool_use_message(
            envelope=envelope,
            model=model,
            submit_tool_name=submit_tool["name"],
            submit_input=structured,
        )


def _pick_submit_tool(tools: list[dict[str, Any]]) -> dict[str, Any] | None:
    for t in reversed(tools):
        name = t.get("name", "")
        if name.startswith("submit_"):
            return t
    return None


def _flatten_system(system_blocks: list[dict[str, Any]]) -> str:
    out: list[str] = []
    for b in system_blocks:
        if isinstance(b, dict) and b.get("type") == "text":
            out.append(b.get("text", ""))
        elif isinstance(b, str):
            out.append(b)
    return "\n\n".join(x for x in out if x)


def _serialize_messages(messages: list[dict[str, Any]], submit_tool: dict[str, Any]) -> str:
    """Flatten a tool-use message history into a single prompt.

    In single-shot mode we never actually round-trip tool_use results,
    so the history is effectively the initial user content. But the
    serializer is written to handle assistant/tool_result turns too in
    case the caller ever replays them.
    """
    parts: list[str] = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content")
        if isinstance(content, str):
            parts.append(f"# {role}\n{content}")
            continue
        if not isinstance(content, list):
            continue
        chunks: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            t = block.get("type")
            if t == "text":
                chunks.append(block.get("text", ""))
            elif t == "tool_use":
                chunks.append(
                    f"[tool_use: {block.get('name')} "
                    f"input={json.dumps(block.get('input', {}), ensure_ascii=False)}]"
                )
            elif t == "tool_result":
                chunks.append(
                    f"[tool_result {block.get('tool_use_id','')}]:\n"
                    f"{block.get('content','')}"
                )
        if chunks:
            parts.append(f"# {role}\n" + "\n\n".join(chunks))

    parts.append(
        f"# Task\nReply with a single JSON object matching the schema for "
        f"`{submit_tool['name']}`. Do not include any prose or code fences."
    )
    return "\n\n".join(parts)


def _parse_envelope(stdout: bytes, stderr: bytes) -> dict[str, Any]:
    text = stdout.decode("utf-8", errors="replace").strip()
    if not text:
        raise ClaudeCLIError(
            f"claude -p produced empty stdout; stderr="
            f"{_tail(stderr.decode('utf-8', errors='replace'))}"
        )
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ClaudeCLIError(
            f"claude -p envelope not JSON: {e}; stdout[:200]={text[:200]!r}"
        ) from e


def _synthesize_tool_use_message(
    *,
    envelope: dict[str, Any],
    model: str,
    submit_tool_name: str,
    submit_input: dict[str, Any],
) -> dict[str, Any]:
    usage_src = envelope.get("usage") or {}
    return {
        "id": envelope.get("session_id", ""),
        "model": model,
        "role": "assistant",
        "stop_reason": envelope.get("stop_reason", "tool_use"),
        "usage": {
            "input_tokens": int(usage_src.get("input_tokens", 0) or 0),
            "output_tokens": int(usage_src.get("output_tokens", 0) or 0),
            "cache_creation_input_tokens": int(
                usage_src.get("cache_creation_input_tokens", 0) or 0
            ),
            "cache_read_input_tokens": int(
                usage_src.get("cache_read_input_tokens", 0) or 0
            ),
        },
        "content": [
            {
                "type": "tool_use",
                "id": f"scr-cli-{uuid.uuid4().hex[:12]}",
                "name": submit_tool_name,
                "input": submit_input,
            },
        ],
    }


def _tail(text: str, n: int = 400) -> str:
    t = text.strip()
    if len(t) <= n:
        return t
    return "..." + t[-n:]


__all__ = [
    "ClaudeCLIClient",
    "ClaudeCLIError",
    "ClaudeCLINotFound",
]
