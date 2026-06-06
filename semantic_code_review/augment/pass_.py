"""Shared recipe for a single LLM pass.

The four passes (`overview`, `hunk`, `fold-summary`, `extra-review-pr`)
all wrap the same five-step recipe around a per-pass prompt and apply
step: cache lookup → ``agent.iter()`` driver → trace write → usage
accounting → cache put. This module owns the recipe; each pass file
owns only the prompt assembly, agent construction, and the apply step
that folds the returned payload into an ``AnnotatedDiff``.

Driving via ``agent.iter()`` rather than ``agent.run()`` keeps the
partial message history accessible on the failure path — without it,
a mid-run ``UsageLimitExceeded`` leaves no trace, which is exactly the
case that most needs one.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic_ai import Agent

from ..cache.store import CacheStore
from .agents import Client
from .trace_adapter import (
    submit_args_from_result, write_partial_trace, write_pydantic_ai_trace,
)


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PassMeta:
    """Static identity of a pass.

    ``name`` is the cache-key prefix and the ``pass`` field in cache-hit
    trace markers. ``submit_tool`` and ``tool_names`` are recorded in
    the trace envelope so a trace reader can see what tools were in
    play. ``swallow_errors`` switches the failure policy: when true,
    :func:`run_pass` logs and returns ``None`` instead of re-raising —
    used by the extra-review pass, which is best-effort and must not
    poison the main-pass output.
    """

    name: str
    submit_tool: str
    tool_names: tuple[str, ...] = ()
    swallow_errors: bool = False


async def run_pass(
    meta: PassMeta,
    *,
    client: Client,
    agent: Agent,
    user_content: Any,
    system: str,
    model: str,
    cache_inputs: tuple[Any, ...],
    deps: Any = None,
    model_settings: Any = None,
    cache: CacheStore | None = None,
    trace_path: Path | None = None,
    cache_request: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Run one LLM pass through the shared recipe.

    Returns the response payload as a JSON-shaped dict (the same shape
    persisted in the cache), or ``None`` when ``meta.swallow_errors`` is
    set and the agent run raised. Callers convert the dict into
    domain shapes themselves and fold the result into the diff.

    The cache key is ``(meta.name, model, system, *cache_inputs)`` —
    ``model`` is the user-facing model string so the key stays stable
    across the SDK/CLI driver split for the same logical model. The
    trace envelope records ``str(client.model)`` instead so the
    diagnostic surface carries the precise model identifier.
    """
    key = None
    if cache is not None:
        key = cache.key(meta.name, model, system, *cache_inputs)
        entry = cache.get(key)
        if entry is not None:
            if trace_path is not None:
                _write_cache_hit_marker(trace_path, meta.name, entry)
            return entry["response"]

    async with agent.iter(
        user_content, deps=deps, model_settings=model_settings,
    ) as agent_run:
        try:
            async for _ in agent_run:
                pass
        except BaseException as exc:
            if trace_path is not None:
                write_partial_trace(
                    list(agent_run.all_messages()),
                    trace_path=trace_path,
                    model=str(client.model),
                    system=system,
                    tool_names=list(meta.tool_names),
                    submit_tool=meta.submit_tool,
                    error=exc,
                )
            if meta.swallow_errors:
                log.warning(
                    "%s pass failed: %s: %s",
                    meta.name, type(exc).__name__, exc,
                )
                return None
            raise
        run_result = agent_run.result

    if trace_path is not None:
        write_pydantic_ai_trace(
            run_result,
            trace_path=trace_path,
            model=str(client.model),
            system=system,
            tool_names=list(meta.tool_names),
            submit_tool=meta.submit_tool,
        )

    payload = submit_args_from_result(run_result)

    if cache is not None and key is not None:
        usage = run_result.usage()
        cache.put(
            key,
            request=cache_request or {},
            response=payload,
            tokens_in=usage.input_tokens or 0,
            tokens_out=usage.output_tokens or 0,
        )
    return payload


def _write_cache_hit_marker(
    path: Path, pass_name: str, entry: dict[str, Any],
) -> None:
    """Write the cache-hit envelope at ``path``.

    Mirrors the live-run trace shape just enough that a trace reader
    can identify the pass and see the cached response — the full
    iteration history isn't reconstructible from a cache entry.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"cache_hit": True, "pass": pass_name,
             "response": entry.get("response")},
            indent=2, ensure_ascii=False,
        ),
        encoding="utf-8",
    )


__all__ = ["PassMeta", "run_pass"]
