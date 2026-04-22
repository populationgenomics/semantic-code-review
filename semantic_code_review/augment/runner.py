"""Async orchestrator: tool-use loop, prompt caching, disk cache, backoff."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .tools import RepoTools, dispatch


log = logging.getLogger(__name__)


class ClaudeClient(Protocol):
    async def create_message(self, **kwargs: Any) -> dict: ...


class AnthropicClient:
    """Default adapter over `anthropic.AsyncAnthropic`."""

    def __init__(self, inner: Any | None = None) -> None:
        if inner is None:
            from anthropic import AsyncAnthropic  # lazy import
            inner = AsyncAnthropic()
        self._inner = inner

    async def create_message(self, **kwargs: Any) -> dict:
        msg = await self._inner.messages.create(**kwargs)
        return _message_to_dict(msg)


def _message_to_dict(msg: Any) -> dict:
    return {
        "id": getattr(msg, "id", ""),
        "model": getattr(msg, "model", ""),
        "role": getattr(msg, "role", "assistant"),
        "stop_reason": getattr(msg, "stop_reason", ""),
        "usage": {
            "input_tokens": getattr(msg.usage, "input_tokens", 0),
            "output_tokens": getattr(msg.usage, "output_tokens", 0),
            "cache_creation_input_tokens": getattr(msg.usage, "cache_creation_input_tokens", 0),
            "cache_read_input_tokens": getattr(msg.usage, "cache_read_input_tokens", 0),
        } if getattr(msg, "usage", None) else {},
        "content": [_block_to_dict(b) for b in msg.content],
    }


def _block_to_dict(b: Any) -> dict:
    t = getattr(b, "type", None)
    if t == "text":
        return {"type": "text", "text": b.text}
    if t == "tool_use":
        return {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
    return {"type": t or "unknown"}


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
                        out = dispatch(repo_tools, block["name"], block["input"])
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
