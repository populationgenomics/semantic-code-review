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

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.usage import UsageLimits

from ..cache.store import CacheKey, CacheStore
from .agents import Client
from .trace_adapter import (
    submit_args_from_result,
    write_partial_trace,
    write_pydantic_ai_trace,
)

log = logging.getLogger(__name__)

#: Anthropic compiles a native-output schema into a grammar on first use
#: and caches it for 24 hours. A run fans out dozens of hunks at once, so
#: the first use is dozens of concurrent compiles of the same uncompiled
#: schema and some lose the race with a 400. It is transient — the same
#: schema succeeds once any one call has warmed the cache — so retry
#: rather than treat it as a failed hunk. Observed: a whole 33-hunk run
#: lost to this on the first use of a changed schema.
_GRAMMAR_TIMEOUT = "Grammar compilation timed out"
_GRAMMAR_RETRIES = 3
_GRAMMAR_BACKOFF_SECONDS = 4.0


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
    agent: Agent[Any, Any],
    user_content: Any,
    system: str,
    model: str,
    cache_inputs: tuple[Any, ...],
    deps: Any = None,
    model_settings: Any = None,
    request_limit: int | None = None,
    cache: CacheStore | None = None,
    trace_path: Path | None = None,
    cache_request: dict[str, Any] | None = None,
    payload_extra: Callable[[], dict[str, Any]] | None = None,
    on_requests: Callable[[int], None] | None = None,
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

    ``payload_extra`` supplies facts about the run that the model did not
    submit — the explainer's prose passes record the files their tool
    loop opened this way. They are merged into the payload *before* it is
    cached, so a cache hit restores them too: provenance that survives
    the prose it belongs to is the only kind worth rendering.

    ``on_requests`` reports the model requests spent, for a caller
    metering a budget across several passes. It is the driver's own
    count — the figure ``UsageLimits`` meters against ``request_limit``
    and the one ``turn_budget.used`` puts in the trace — so a shared
    budget and the ceiling that enforces it cannot disagree. It counts a
    final request that errored and fires on the raising path too. It
    fires once per agent attempt, not once per pass: a grammar retry
    re-drives the whole loop and genuinely re-bills it, so the caller
    adds them up. A cache hit spends nothing and reports ``0``.
    """
    key = None
    if cache is not None:
        key = cache.key(meta.name, model, system, *cache_inputs)
        entry = cache.get(key)
        if entry is not None:
            if trace_path is not None:
                _write_cache_hit_marker(trace_path, meta.name, entry)
            if on_requests is not None:
                on_requests(0)
            return entry["response"]

    # Bound the agentic loop. The CLI drivers get this from `--max-turns`;
    # SDK backends have no equivalent, so without a limit here a pass that
    # cannot answer its question keeps investigating until pydantic-ai's
    # default ceiling — losing the hunk after spending the most on it.
    # `request_limit` lets a pass narrow that to its own budget; the
    # effective figure and the requests actually made go to the trace.
    limit = request_limit if request_limit is not None else client.request_limit
    usage_limits = UsageLimits(request_limit=limit) if limit else None
    # The last attempt is outside the loop so the function has one exit on
    # each path — the retries swallow the grammar error, the final call
    # propagates whatever it raises.
    for attempt in range(_GRAMMAR_RETRIES - 1):
        try:
            return await _drive_agent(
                meta,
                client=client,
                agent=agent,
                user_content=user_content,
                system=system,
                deps=deps,
                model_settings=model_settings,
                usage_limits=usage_limits,
                turn_cap=limit,
                cache=cache,
                key=key,
                trace_path=trace_path,
                cache_request=cache_request,
                payload_extra=payload_extra,
                on_requests=on_requests,
            )
        except Exception as exc:
            if _GRAMMAR_TIMEOUT not in str(exc):
                raise
            log.info(
                "%s pass: schema grammar not compiled yet (attempt %d/%d) — retrying",
                meta.name,
                attempt + 1,
                _GRAMMAR_RETRIES,
            )
            _preserve_attempt_trace(trace_path, attempt + 1)
            await asyncio.sleep(_GRAMMAR_BACKOFF_SECONDS * (attempt + 1))
    return await _drive_agent(
        meta,
        client=client,
        agent=agent,
        user_content=user_content,
        system=system,
        deps=deps,
        model_settings=model_settings,
        usage_limits=usage_limits,
        turn_cap=limit,
        cache=cache,
        key=key,
        trace_path=trace_path,
        cache_request=cache_request,
        payload_extra=payload_extra,
        on_requests=on_requests,
    )


def _preserve_attempt_trace(trace_path: Path | None, attempt: int) -> None:
    """Move a failed attempt's trace aside so the retry doesn't erase it.

    Every attempt drives a fresh `agent.iter`, so a retry re-runs and
    re-bills the whole tool loop, and it writes to the same path. Left
    alone the successful attempt overwrites the failed one and
    `usage.json` — which is derived from these files alone — reports a
    hunk that cost two loops as having cost one.
    """
    if trace_path is None or not trace_path.exists():
        return
    kept = trace_path.with_name(f"{trace_path.stem}-attempt{attempt}{trace_path.suffix}")
    with contextlib.suppress(OSError):
        trace_path.replace(kept)


async def _drive_agent(
    meta: PassMeta,
    *,
    client: Client,
    agent: Agent[Any, Any],
    user_content: Any,
    system: str,
    deps: Any,
    model_settings: Any,
    usage_limits: Any,
    turn_cap: int | None,
    cache: CacheStore | None,
    key: CacheKey | None,
    trace_path: Path | None,
    cache_request: dict[str, Any] | None,
    payload_extra: Callable[[], dict[str, Any]] | None,
    on_requests: Callable[[int], None] | None,
) -> dict[str, Any] | None:
    """One attempt at the agent loop: drive, trace, account, cache."""
    async with agent.iter(
        user_content,
        deps=deps,
        model_settings=model_settings,
        usage_limits=usage_limits,
    ) as agent_run:
        try:
            async for _ in agent_run:
                pass
        except BaseException as exc:
            # Charged before the trace write and before any re-raise: a
            # loop that died at its ceiling spent every request it made,
            # and a budget that only counts successful passes is one a
            # failing pass can spend without limit.
            requests_used = agent_run.usage.requests  # pydantic-ai 2.x: property, not a method
            if on_requests is not None:
                on_requests(requests_used)
            if trace_path is not None:
                write_partial_trace(
                    list(agent_run.all_messages()),
                    trace_path=trace_path,
                    model=str(client.model),
                    system=system,
                    tool_names=list(meta.tool_names),
                    submit_tool=meta.submit_tool,
                    error=exc,
                    turn_cap=turn_cap,
                    requests_used=requests_used,
                )
            if meta.swallow_errors:
                log.warning(
                    "%s pass failed: %s: %s",
                    meta.name,
                    type(exc).__name__,
                    exc,
                )
                return None
            raise
        run_result = agent_run.result

    assert run_result is not None  # the agent run completed without an early return

    # The driver's own count, which is what `UsageLimits` meters against
    # `request_limit` — a caller charging a shared budget and the
    # ceiling that cut the loop off have to be counting the same thing.
    requests_used = run_result.usage.requests
    if on_requests is not None:
        on_requests(requests_used)

    if trace_path is not None:
        write_pydantic_ai_trace(
            run_result,
            trace_path=trace_path,
            model=str(client.model),
            system=system,
            tool_names=list(meta.tool_names),
            submit_tool=meta.submit_tool,
            turn_cap=turn_cap,
            requests_used=requests_used,
        )

    payload = submit_args_from_result(run_result)
    if payload_extra is not None:
        payload = {**payload, **payload_extra()}

    if cache is not None and key is not None:
        usage = run_result.usage  # pydantic-ai 2.x: property, not a method
        cache.put(
            key,
            request=cache_request or {},
            response=payload,
            tokens_in=usage.input_tokens or 0,
            tokens_out=usage.output_tokens or 0,
        )
    return payload


def _write_cache_hit_marker(
    path: Path,
    pass_name: str,
    entry: dict[str, Any],
) -> None:
    """Write the cache-hit envelope at ``path``.

    Mirrors the live-run trace shape just enough that a trace reader
    can identify the pass and see the cached response — the full
    iteration history isn't reconstructible from a cache entry.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"cache_hit": True, "pass": pass_name, "response": entry.get("response")},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


__all__ = ["PassMeta", "run_pass"]
