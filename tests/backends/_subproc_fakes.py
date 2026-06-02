"""Shared fakes for CLI-driver subprocess tests.

Not collected by pytest (leading underscore on filename); imported
directly from the test modules in this package.
"""

from __future__ import annotations

import json
from typing import Any

import pytest


class FakeProc:
    """Stands in for an `asyncio.subprocess.Process`.

    Captures stdin bytes from `communicate()` so tests can assert on
    what was piped to the child.
    """

    def __init__(
        self,
        stdout: bytes,
        stderr: bytes = b"",
        returncode: int = 0,
    ) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.stdin_written: bytes | None = None

    async def communicate(self, stdin: bytes | None = None) -> tuple[bytes, bytes]:
        self.stdin_written = stdin
        return self._stdout, self._stderr


def install_fake_subproc(
    monkeypatch: pytest.MonkeyPatch, procs: list[FakeProc]
) -> list[dict[str, Any]]:
    """Replace `asyncio.create_subprocess_exec` with a queue of fakes.

    Returns a list that the patch populates with one
    `{"argv": [...], "kwargs": {...}}` entry per spawn, so tests can
    inspect argv and env after the run completes.
    """
    calls: list[dict[str, Any]] = []
    queue = list(procs)

    async def _fake(*args: str, **kwargs: Any) -> FakeProc:
        calls.append({"argv": list(args), "kwargs": kwargs})
        if not queue:
            raise AssertionError("more subprocess calls than fake procs queued")
        return queue.pop(0)

    import semantic_code_review.backends._cli_driver as mod
    monkeypatch.setattr(mod.asyncio, "create_subprocess_exec", _fake)
    return calls


def claude_envelope(
    structured: Any,
    *,
    is_error: bool = False,
    use_structured_output: bool = True,
    usage: dict | None = None,
) -> bytes:
    """Build a `claude -p --output-format=json` envelope.

    With `--json-schema` active the validated JSON lives in
    `structured_output` and `result` is empty. Set
    `use_structured_output=False` to simulate the pre-schema shape
    that older `claude` versions emit.
    """
    payload: dict[str, Any] = {
        "type": "result",
        "subtype": "success",
        "is_error": is_error,
        "stop_reason": "end_turn",
        "session_id": "sess-abc",
        "usage": usage or {
            "input_tokens": 42,
            "output_tokens": 17,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }
    if is_error:
        payload["result"] = structured if isinstance(structured, str) else json.dumps(structured)
    elif use_structured_output:
        payload["result"] = ""
        payload["structured_output"] = structured
    else:
        payload["result"] = structured if isinstance(structured, str) else json.dumps(structured)
    return (json.dumps(payload) + "\n").encode("utf-8")


def gemini_envelope(
    response: Any,
    *,
    error: dict | str | None = None,
    stats_models: dict | None = None,
) -> bytes:
    payload: dict[str, Any] = {
        "response": response if isinstance(response, str) else json.dumps(response),
        "stats": {
            "models": stats_models or {
                "gemini-2.5-pro": {
                    "tokens": {"input": 42, "candidates": 17, "cached": 0},
                },
            },
        },
        "session_id": "gem-sess-abc",
    }
    if error is not None:
        payload["error"] = error
    return (json.dumps(payload) + "\n").encode("utf-8")
