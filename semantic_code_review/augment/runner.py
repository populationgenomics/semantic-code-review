"""Async tool-use loop for the CLI subprocess backends.

`run_agentic` drives `claude -p` and `gemini -p` style clients that
expose a `create_message(...)` Anthropic-shaped wire surface. The SDK
backends (Anthropic API, Vertex / AI Studio) are now driven by
pydantic-ai through `agents.py`; they don't go through here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .repo_tool_fns import mcp_dispatch
from .tools import RepoTools


log = logging.getLogger(__name__)


class ClaudeClient(Protocol):
    """Wire surface the CLI subprocess clients implement.

    The SDK backends do NOT implement this — they go through the
    pydantic-ai `Agent` path via `agents.py`. Callers branch on
    `isinstance(client, SDKBackend)` in the pipeline.
    """

    async def create_message(self, **kwargs: Any) -> dict: ...
    # All clients implement aclose so callers can drive the lifecycle
    # uniformly via `contextlib.aclosing` without duck-typing checks.
    async def aclose(self) -> None: ...


@dataclass
class AgenticResult:
    submit_args: dict[str, Any]
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0


async def run_agentic(
    client: ClaudeClient,
    *,
    model: str,
    system: str,
    user_content: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    submit_tool_name: str,
    repo_tools: RepoTools | None = None,
    max_iterations: int = 20,
    trace_path: Path | None = None,
) -> AgenticResult:
    """Run a Claude tool-use loop until the model calls `submit_tool_name`.

    If `trace_path` is provided, write a complete per-iteration trace
    (system prompt, every message sent, every raw response, every tool
    call with full result) as JSON at the given path.
    """
    sys_block = {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_content}]
    result = AgenticResult(submit_args={})

    trace: dict[str, Any] | None = None
    if trace_path is not None:
        trace = {
            "model": model,
            "system": system,
            "tools": [t["name"] for t in tools],
            "submit_tool": submit_tool_name,
            "iterations": [],
        }

    def _record_iteration(sent: list[dict[str, Any]], response: dict[str, Any],
                          tool_results: list[dict[str, Any]]) -> None:
        if trace is None:
            return
        trace["iterations"].append(
            {
                "messages_sent": sent,
                "response": response,
                "tool_results": tool_results,
            }
        )

    try:
        for _ in range(max_iterations):
            sent = [dict(m) for m in messages]
            response = await _call_with_backoff(
                client,
                model=model,
                max_tokens=4096,
                system=[sys_block],
                tools=tools,
                messages=messages,
            )
            usage = response.get("usage", {})
            result.input_tokens += usage.get("input_tokens", 0)
            result.output_tokens += usage.get("output_tokens", 0)
            result.cache_read_tokens += usage.get("cache_read_input_tokens", 0)

            for block in response["content"]:
                if block["type"] == "tool_use" and block["name"] == submit_tool_name:
                    result.submit_args = block["input"]
                    _record_iteration(sent, response, [])
                    return result

            messages.append({"role": "assistant", "content": response["content"]})
            tool_results: list[dict[str, Any]] = []
            for block in response["content"]:
                if block["type"] != "tool_use":
                    continue
                if repo_tools is None:
                    out = "error: no repo tools configured for this pass"
                else:
                    try:
                        out = mcp_dispatch(repo_tools, block["name"], block["input"])
                    except Exception as e:  # noqa: BLE001
                        out = f"error: tool {block['name']} raised: {e}"
                result.tool_calls.append(
                    {"name": block["name"], "input": block["input"], "result_len": len(out)}
                )
                tool_results.append(
                    {"type": "tool_result", "tool_use_id": block["id"], "content": out}
                )

            _record_iteration(sent, response, tool_results)

            if not tool_results:
                messages.append(
                    {"role": "user", "content": f"Call {submit_tool_name} now to finalise."}
                )
                continue
            messages.append({"role": "user", "content": tool_results})

        raise RuntimeError(
            f"agent did not call {submit_tool_name} within {max_iterations} iterations"
        )
    finally:
        if trace is not None and trace_path is not None:
            trace["result"] = {
                "submit_args": result.submit_args,
                "tool_calls": result.tool_calls,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "cache_read_tokens": result.cache_read_tokens,
            }
            trace_path.parent.mkdir(parents=True, exist_ok=True)
            trace_path.write_text(json.dumps(trace, indent=2, ensure_ascii=False), encoding="utf-8")


async def _call_with_backoff(
    client: ClaudeClient, *, max_retries: int = 5, **kwargs: Any
) -> dict[str, Any]:
    delay = 1.0
    for attempt in range(max_retries):
        try:
            return await client.create_message(**kwargs)
        except Exception as e:  # noqa: BLE001
            msg = str(e).lower()
            retryable = "rate" in msg or "429" in msg or "overloaded" in msg or "503" in msg
            if not retryable or attempt == max_retries - 1:
                raise
            sleep = delay + random.random()
            log.warning("Claude call failed (%s); retrying in %.1fs", e, sleep)
            await asyncio.sleep(sleep)
            delay = min(delay * 2, 30.0)
    raise RuntimeError("unreachable")
