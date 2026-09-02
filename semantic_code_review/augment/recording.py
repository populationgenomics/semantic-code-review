"""The cross-cutting envelope around one agent run: ceiling + trace.

Two callers drive an agent: `pass_.run_pass` (structured output, cached,
one shot) and `console.stream_console_turn` (free-form prose, streamed,
multi-turn). What they share is not the output shape but the accounting
around it — bound the loop with a request ceiling, then record what the
run spent where `usage.py` can find it. That is this module; `run_pass`
adds the cache and the submit-tool extraction on top.

A run whose spend leaves no trace is a run `usage.json` reports as free,
so the recorder charges and traces on *every* exit — completion, error,
and the console's cancellation — not only the successful one.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic_ai.usage import UsageLimits

from .agents import Client
from .trace_adapter import write_partial_trace, write_pydantic_ai_trace


@dataclasses.dataclass(frozen=True)
class RunRecorder:
    """Bounds one agent run and records what it cost.

    `system`, `tool_names` and `submit_tool` are the trace envelope's
    identity fields; a free-form caller passes `submit_tool=""` because
    it has no structured-output sink. `trace_path` of None disables the
    trace write (callers that keep no run directory); the request
    ceiling still applies.

    `request_limit` overrides the backend's own `client.request_limit`
    for callers metering a narrower budget.
    """

    client: Client
    system: str
    trace_path: Path | None
    submit_tool: str = ""
    tool_names: tuple[str, ...] = ()
    request_limit: int | None = None
    on_requests: Callable[[int], None] | None = None

    @property
    def turn_cap(self) -> int | None:
        """Requests this run is allowed, or None for pydantic-ai's default.

        The CLI drivers get this from `--max-turns`; SDK backends have no
        equivalent, so without a limit here a loop that cannot answer its
        question keeps investigating until pydantic-ai's default ceiling
        — losing the answer after spending the most on it.
        """
        return self.request_limit if self.request_limit is not None else self.client.request_limit

    @property
    def usage_limits(self) -> UsageLimits | None:
        """`UsageLimits` for `agent.iter`, or None when uncapped."""
        cap = self.turn_cap
        return UsageLimits(request_limit=cap) if cap else None

    def record(self, run: Any, *, error: BaseException | None = None) -> None:
        """Charge the requests `run` made and write its trace.

        `run` is an `AgentRun` (mid-flight: the error and cancellation
        paths, where there is no result yet) or an `AgentRunResult`.
        Both carry `usage` and `all_messages()`; only the latter carries
        `output`, and a free-form run's `str` output contributes no
        submit args.

        Charging precedes the trace write, and both precede any re-raise
        by the caller: a loop that died at its ceiling spent every
        request it made, and a budget that only counts successful runs is
        one a failing run can spend without limit.
        """
        requests_used = run.usage.requests  # pydantic-ai 2.x: property, not a method
        if self.on_requests is not None:
            self.on_requests(requests_used)
        if self.trace_path is None:
            return
        if error is None:
            write_pydantic_ai_trace(
                run,
                trace_path=self.trace_path,
                model=str(self.client.model),
                system=self.system,
                tool_names=list(self.tool_names),
                submit_tool=self.submit_tool,
                turn_cap=self.turn_cap,
                requests_used=requests_used,
            )
            return
        write_partial_trace(
            list(run.all_messages()),
            trace_path=self.trace_path,
            model=str(self.client.model),
            system=self.system,
            tool_names=list(self.tool_names),
            submit_tool=self.submit_tool,
            error=error,
            turn_cap=self.turn_cap,
            requests_used=requests_used,
        )


__all__ = ["RunRecorder"]
