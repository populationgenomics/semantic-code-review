"""SubprocessModel: request loop, retry feedback, stdin routing.

Plus the pure helpers (`_flatten_messages`, `_extract_json_object`,
`_validate_against_schema`) that the CLI drivers share. Per-driver
specifics live in `test_claude_cli.py`.
"""

from __future__ import annotations

import json

import pytest
from pydantic_ai import Agent
from pydantic_ai.output import ToolOutput

from semantic_code_review.augment.schemas import HunkAnnotations
from semantic_code_review.backends._cli_driver import (
    SubprocessModel,
    _extract_json_object,
    _flatten_messages,
    _Invocation,
    _SchemaValidationError,
    _validate_against_schema,
    _ValidationFailure,
)

from ._subproc_fakes import FakeProc, install_fake_subproc

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_flatten_messages_separates_system_and_user() -> None:
    """SystemPromptParts go to the system channel; user prompts to the prompt body."""
    from datetime import datetime

    from pydantic_ai.messages import (
        ModelRequest,
        SystemPromptPart,
        UserPromptPart,
    )

    ts = datetime(2026, 5, 6)
    req = ModelRequest(
        parts=[
            SystemPromptPart(content="sys", timestamp=ts),
            UserPromptPart(content="hello", timestamp=ts),
        ],
        timestamp=ts,
    )
    sys_text, user_text = _flatten_messages([req])
    assert sys_text == "sys"
    assert "hello" in user_text


def test_extract_json_clean_object() -> None:
    assert _extract_json_object('{"intent": "x"}') == {"intent": "x"}


def test_extract_json_strips_fenced_block() -> None:
    text = '```json\n{"intent": "x"}\n```'
    assert _extract_json_object(text) == {"intent": "x"}


def test_extract_json_finds_balanced_object_among_prose() -> None:
    text = 'Sure! Here is the JSON:\n\n{"intent": "x", "nested": {"k": 1}}\n\nHope this helps.'
    assert _extract_json_object(text) == {"intent": "x", "nested": {"k": 1}}


def test_extract_json_raises_when_no_object() -> None:
    with pytest.raises(ValueError):
        _extract_json_object("just some prose, no JSON here")


def test_extract_json_handles_braces_inside_strings() -> None:
    text = '{"intent": "the } character", "ok": true}'
    assert _extract_json_object(text) == {"intent": "the } character", "ok": True}


def test_validate_against_schema_passes_on_required_present() -> None:
    schema = {"type": "object", "required": ["intent"]}
    _validate_against_schema({"intent": "x"}, schema)


def test_validate_against_schema_rejects_missing_required() -> None:
    schema = {"type": "object", "required": ["intent"]}
    with pytest.raises(_SchemaValidationError, match="missing required"):
        _validate_against_schema({}, schema)


def test_validate_against_schema_rejects_wrong_top_type() -> None:
    schema = {"type": "object"}
    with pytest.raises(_SchemaValidationError, match="object"):
        _validate_against_schema(["array", "instead"], schema)


# ---------------------------------------------------------------------------
# SubprocessModel — base-class request loop / spawn / retry
# ---------------------------------------------------------------------------


class _StubModel(SubprocessModel):
    """Minimal `SubprocessModel` for testing the request loop in isolation.

    The hooks are wired to be inspectable: `_build_invocation` records
    the prior_error each turn, `_envelope_to_structured` returns
    `_ValidationFailure` for envelopes flagged `{retry: True}`. This
    lets one test exercise the retry/feedback path without a real
    subprocess on either side.
    """

    _provider_name = "stub"

    def __init__(self, *, max_validation_retries: int = 0) -> None:
        super().__init__(model="stub-1", max_validation_retries=max_validation_retries)
        self.invocations_seen: list[str | None] = []

    def _build_invocation(self, *, system_text, user_text, schema, submit_tool_name, prior_error):
        self.invocations_seen.append(prior_error)
        return _Invocation(argv=["/usr/bin/true"], env=None, stdin=None)

    def _parse_envelope(self, *, stdout, stderr, returncode):
        return json.loads(stdout.decode("utf-8"))

    def _envelope_to_structured(self, *, envelope, schema, submit_tool_name):
        if envelope.get("retry"):
            raise _ValidationFailure(
                reason=envelope.get("reason", "retry me"),
                detail=envelope.get("detail", ""),
            )
        return envelope.get("structured", {})

    def _validation_exhausted_error(self, last_error, attempts):
        return RuntimeError(f"exhausted: {last_error}")


def _agent(model) -> Agent:  # type: ignore[no-untyped-def]
    return Agent(
        model=model,
        output_type=ToolOutput(HunkAnnotations, name="submit_annotations"),
        instructions="SYS",
    )


async def test_subprocess_model_retries_until_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """The request loop feeds prior-error back into _build_invocation
    and stops retrying once _envelope_to_structured returns a dict."""
    model = _StubModel(max_validation_retries=2)
    bad = FakeProc(json.dumps({"retry": True, "reason": "bad-1"}).encode("utf-8"))
    good = FakeProc(json.dumps({"structured": {"intent": "ok"}}).encode("utf-8"))
    install_fake_subproc(monkeypatch, [bad, good])

    result = await _agent(model).run("USER")
    assert result.output.intent == "ok"
    # First attempt has no prior_error; second sees the first failure.
    assert model.invocations_seen == [None, "bad-1"]


async def test_subprocess_model_raises_after_exhausting_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After max_validation_retries+1 failed attempts, the loop raises
    the subclass's `_validation_exhausted_error`."""
    model = _StubModel(max_validation_retries=1)
    bad1 = FakeProc(json.dumps({"retry": True, "reason": "bad-1"}).encode("utf-8"))
    bad2 = FakeProc(json.dumps({"retry": True, "reason": "bad-2"}).encode("utf-8"))
    install_fake_subproc(monkeypatch, [bad1, bad2])

    with pytest.raises(RuntimeError, match="exhausted: bad-2"):
        await _agent(model).run("USER")
    assert model.invocations_seen == [None, "bad-1"]


async def test_subprocess_model_stdin_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_Invocation.stdin=bytes` writes to the child; stdin=None uses DEVNULL."""

    class _StdinModel(_StubModel):
        def __init__(self, stdin_bytes: bytes | None) -> None:
            super().__init__()
            self._stdin_bytes = stdin_bytes

        def _build_invocation(self, *, system_text, user_text, schema, submit_tool_name, prior_error):
            self.invocations_seen.append(prior_error)
            return _Invocation(argv=["/usr/bin/true"], stdin=self._stdin_bytes)

    # With bytes: child receives them via communicate().
    fed = _StdinModel(stdin_bytes=b"prompt-payload")
    proc_fed = FakeProc(json.dumps({"structured": {"intent": "ok"}}).encode("utf-8"))
    install_fake_subproc(monkeypatch, [proc_fed])
    await _agent(fed).run("USER")
    assert proc_fed.stdin_written == b"prompt-payload"

    # With None: child gets DEVNULL — communicate() receives None.
    devnull = _StdinModel(stdin_bytes=None)
    proc_dn = FakeProc(json.dumps({"structured": {"intent": "ok"}}).encode("utf-8"))
    install_fake_subproc(monkeypatch, [proc_dn])
    await _agent(devnull).run("USER")
    assert proc_dn.stdin_written is None
