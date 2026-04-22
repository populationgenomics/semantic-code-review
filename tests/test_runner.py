"""Tool-use loop: fake client drives repo tools and submits annotations."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from semantic_code_review.augment.runner import AgenticResult, run_agentic
from semantic_code_review.augment.tools import RepoTools


class FakeClient:
    """Script-driven Claude client: each call returns the next canned message."""

    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.script = list(script)
        self.sent: list[dict[str, Any]] = []

    async def create_message(self, **kwargs: Any) -> dict[str, Any]:
        self.sent.append(kwargs)
        if not self.script:
            raise AssertionError("FakeClient script exhausted")
        return self.script.pop(0)


def _msg(content: list[dict[str, Any]], stop: str = "end_turn", usage: dict | None = None) -> dict:
    return {
        "id": "m", "model": "test", "role": "assistant", "stop_reason": stop,
        "usage": usage or {"input_tokens": 10, "output_tokens": 5},
        "content": content,
    }


def _tool_use(name: str, input_: dict, id_: str = "u1") -> dict:
    return {"type": "tool_use", "id": id_, "name": name, "input": input_}


async def test_submit_on_first_call() -> None:
    client = FakeClient([
        _msg([_tool_use("submit_annotations", {"intent": "ok"})]),
    ])
    result = await run_agentic(
        client, model="test", system="sys",
        user_content=[{"type": "text", "text": "hunk"}],
        tools=[], submit_tool_name="submit_annotations",
    )
    assert result.submit_args == {"intent": "ok"}
    assert result.input_tokens == 10 and result.output_tokens == 5


async def test_tool_call_then_submit(tmp_path: Path) -> None:
    (tmp_path / "f.py").write_text("hi\n")
    repo = RepoTools(head_worktree=tmp_path, repo_git=tmp_path, base_sha="x", head_sha="y")
    client = FakeClient([
        _msg([_tool_use("read_file", {"path": "f.py"}, id_="t1")]),
        _msg([_tool_use("submit_annotations", {"intent": "used tool"})]),
    ])
    result = await run_agentic(
        client, model="test", system="sys",
        user_content=[{"type": "text", "text": "hunk"}],
        tools=[], submit_tool_name="submit_annotations", repo_tools=repo,
    )
    assert result.submit_args == {"intent": "used tool"}
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0]["name"] == "read_file"

    # Second call must carry the tool_result.
    second = client.sent[1]
    messages = second["messages"]
    assert messages[-1]["role"] == "user"
    assert messages[-1]["content"][0]["type"] == "tool_result"
    assert "hi" in messages[-1]["content"][0]["content"]


async def test_nudges_on_text_only_response() -> None:
    client = FakeClient([
        _msg([{"type": "text", "text": "Thinking..."}]),
        _msg([_tool_use("submit_annotations", {"intent": "done"})]),
    ])
    result = await run_agentic(
        client, model="test", system="sys",
        user_content=[{"type": "text", "text": "hunk"}],
        tools=[], submit_tool_name="submit_annotations",
    )
    assert result.submit_args == {"intent": "done"}
    # Check nudge message was inserted.
    second = client.sent[1]
    assert any(
        isinstance(m["content"], str) and "submit_annotations" in m["content"]
        for m in second["messages"] if m["role"] == "user"
    )


async def test_raises_if_never_submits() -> None:
    client = FakeClient([
        _msg([{"type": "text", "text": "nope"}]),
    ] * 10)
    with pytest.raises(RuntimeError, match="did not call submit_annotations"):
        await run_agentic(
            client, model="test", system="sys",
            user_content=[{"type": "text", "text": "x"}],
            tools=[], submit_tool_name="submit_annotations", max_iterations=3,
        )


async def test_backoff_on_rate_limit() -> None:
    """Transient rate-limit error is retried."""
    calls = {"n": 0}

    class FlappingClient:
        async def create_message(self, **kwargs: Any) -> dict[str, Any]:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("rate_limit_error: slow down")
            return _msg([_tool_use("submit_annotations", {"intent": "eventually"})])

    # Patch sleep to skip the wait.
    import semantic_code_review.augment.runner as runner_mod
    orig_sleep = asyncio.sleep
    async def fast_sleep(_):
        pass
    runner_mod.asyncio.sleep = fast_sleep  # type: ignore[attr-defined]
    try:
        result = await run_agentic(
            FlappingClient(), model="test", system="sys",
            user_content=[{"type": "text", "text": "x"}],
            tools=[], submit_tool_name="submit_annotations",
        )
    finally:
        runner_mod.asyncio.sleep = orig_sleep  # type: ignore[attr-defined]
    assert result.submit_args == {"intent": "eventually"}
    assert calls["n"] == 2
