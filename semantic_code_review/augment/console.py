"""Free-form review console agent.

The console is the first agent in the codebase without a `ToolOutput`
submit tool: it emits prose and calls the same read-only `RepoTools`
the augment passes use, plus a console-only `hunk(id)` diff accessor.
It is wired into the live `scr review` server after augmentation
completes and only for SDK backends (Slice 1 of ADR 0002 — console).

This module owns the agent factory, the compact first-turn seed, and
the turn drivers. The streaming driver (`stream_console_turn`, Slice 2)
drives `Agent.iter` and pumps text deltas + tool-activity out through
caller-supplied callbacks while polling a cancel flag between chunks;
`run_console_turn` is the Slice 1 blocking shape, retained as a thin
no-callback wrapper. CLI-backend support arrives in Slice 5.

Context discipline (ADR 0002): **seed compact, pull on demand.** The
first turn carries the overview JSON + changed-file list + the
deterministic `SymbolDelta` (all bounded); bulk content comes through
tools, including `hunk(id)`. The seed rides the conversation's
`message_history`, so it is paid once, not re-injected per turn.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Callable

from pydantic_ai import Agent
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
)
from pydantic_ai.models import Model

from .agents import Client
from .hunks import overview_to_prompt_json
from .tools import RepoTools, console_tool_functions


log = logging.getLogger(__name__)


CONSOLE_SYSTEM = (
    "You are a code-review console embedded in a live diff viewer. The "
    "reviewer is reading one change (a PR or a local diff) and asks you "
    "free-form questions about it: what calls this, why a guard exists, "
    "what a refactor moved, whether an edge case is handled.\n\n"
    "You have read-only tools over the change's base and head worktrees:\n"
    "  - read_file / read_file_at — file contents at head or any SHA\n"
    "  - grep — search the head worktree\n"
    "  - outline / symbol_at — deterministic tree-sitter symbol structure\n"
    "  - changed_symbols — the structural base->head delta\n"
    "  - list_dir / git_log — directory + history\n"
    "  - hunk(id) — the exact diff text of a hunk, by its 'H<file>_<hunk>' id\n\n"
    "Ground every answer in the code. Prefer calling a tool over guessing; "
    "when you state a fact about the code, cite it as `path:line` so the "
    "reviewer can jump to it. The first message seeds you with the PR "
    "overview, the changed-file list, and the deterministic symbol delta — "
    "reach for tools for anything beyond that.\n\n"
    "Be concise and direct. Answer the question asked; don't pad with "
    "restatements or caveats. If the code doesn't settle the question, say "
    "what you'd need to look at rather than speculating."
)


class ConsoleNotReady(RuntimeError):
    """The run dir doesn't yet hold an `augmented.scr.json`.

    Maps to HTTP 409 at the review-server boundary — augmentation is
    still in flight or was skipped, so there's no diff to ground the
    console against.
    """


class ConsoleCancelled(RuntimeError):
    """The reviewer cancelled an in-flight turn (Stop / Esc).

    Raised by `stream_console_turn` when the cancel flag trips between
    chunks. The background worker catches it and emits a `console-done`
    with `cancelled: true`; the partial turn is discarded (its messages
    never join the conversation history).
    """


def make_console_agent(model: str | Model) -> Agent[RepoTools, str]:
    """Free-form console agent: prose output (no `output_type`), the
    `RepoTools` surface plus `hunk(id)`, and the `CONSOLE_SYSTEM`
    persona. Stateless across turns; the per-turn `RepoTools` is passed
    as `deps=` and the conversation rides `message_history`.
    """
    return Agent(
        model=model,
        deps_type=RepoTools,
        instructions=CONSOLE_SYSTEM,
        tools=console_tool_functions(),
    )


def build_console_seed(diff: Any, *, symbol_delta_json: str | None) -> str:
    """Build the compact first-turn seed string.

    Carries the overview JSON, a one-line-per-file changed-file list,
    and the deterministic `SymbolDelta` JSON when available. Everything
    here is already computed and bounded; bulk content is left to tools.
    """
    files_lines: list[str] = []
    for fp in diff.files:
        role = fp.ann.role.value if getattr(fp.ann, "role", None) else "modified"
        summary = (getattr(fp.ann, "summary", "") or "").strip().replace("\n", " ")
        line = f"- {fp.path} ({role})"
        if summary:
            line += f" — {summary}"
        files_lines.append(line)
    files_block = "\n".join(files_lines) or "(no files)"

    parts = [
        f"# PR overview\n{overview_to_prompt_json(diff)}",
        f"# Changed files\n{files_block}",
    ]
    if symbol_delta_json:
        parts.append(
            "# Structural symbol delta (deterministic tree-sitter base->head)\n"
            f"{symbol_delta_json}"
        )
    return "\n\n".join(parts)


def _prepare_turn(
    client: Client, *, run_dir: Path, question: str, history: list | None,
) -> tuple[Agent[RepoTools, str], str, RepoTools]:
    """Shared per-turn setup: load the sidecar, bind `RepoTools`, build
    the agent, and assemble the prompt (the compact seed prefix on the
    first turn, the bare question thereafter).

    Raises :class:`ConsoleNotReady` if the augmented sidecar isn't on
    disk yet. Returns ``(agent, prompt, repo_tools)`` ready to run or
    iterate.
    """
    sidecar = run_dir / "augmented.scr.json"
    if not sidecar.exists():
        raise ConsoleNotReady("augmented.scr.json missing — augment not complete")

    # Lazy: keep the format machinery off the import path for the
    # agent-factory-only callers.
    from ..format.sidecar import load_sidecar

    diff = load_sidecar(sidecar)
    repo_tools = RepoTools(
        head_worktree=run_dir / "head",
        repo_git=run_dir / "repo.git",
        base_sha=diff.pr.base_sha,
        head_sha=diff.pr.head_sha,
        diff=diff,
    )

    if history:
        prompt: str = question
    else:
        # Best-effort structural seed: a parse failure leaves the seed
        # without the delta rather than failing the turn.
        symbol_delta_json: str | None = None
        try:
            symbol_delta_json = repo_tools.compute_symbol_delta().model_dump_json()
        except Exception:  # noqa: BLE001 — seed is best-effort
            log.warning("console seed: symbol delta failed", exc_info=True)
        seed = build_console_seed(diff, symbol_delta_json=symbol_delta_json)
        prompt = f"{seed}\n\n# Reviewer question\n{question}"

    return make_console_agent(client.model), prompt, repo_tools


def _delta_text(event: Any) -> str:
    """Pull streamed assistant text out of a pydantic-ai stream event.

    A `TextPart` opens with its first chunk in `PartStartEvent`; the
    rest arrive as `TextPartDelta`s. Everything else (tool-call parts,
    thinking deltas) yields the empty string and is skipped by callers.
    """
    if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
        return event.part.content
    if isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
        return event.delta.content_delta
    return ""


def _tool_label(part: Any) -> str:
    """A compact, human-readable label for a tool call — e.g.
    ``grep RepoTools`` or ``read_file src/users.py``.

    Surfaces the tool name plus a representative scalar argument so the
    reviewer sees *what* the console is reaching for, not just *that* it
    is. Args arrive as a dict or a JSON string depending on the backend.
    """
    name = getattr(part, "tool_name", None) or "tool"
    args = getattr(part, "args", None)
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = None
    if isinstance(args, dict):
        for value in args.values():
            text = str(value).strip()
            if text:
                return f"{name} {text[:60]}"
    return name


async def stream_console_turn(
    client: Client,
    *,
    run_dir: Path,
    question: str,
    history: list | None = None,
    on_delta: Callable[[str], None] | None = None,
    on_tool: Callable[[str], None] | None = None,
    cancel: threading.Event | None = None,
) -> tuple[str, list]:
    """Stream one console turn, returning ``(answer, new_history)``.

    Drives the agent via ``Agent.iter`` so assistant text can be pumped
    to ``on_delta`` chunk-by-chunk and each tool invocation announced to
    ``on_tool`` as it fires. Between chunks it polls ``cancel`` (a
    ``threading.Event`` flipped from another thread by ``/console/cancel``)
    and raises :class:`ConsoleCancelled` when set — the partial turn is
    abandoned and its messages never join the returned history.

    `history` is the opaque prior `message_history` (None on the first
    turn); the returned list is the full history to carry forward.
    """
    agent, prompt, repo_tools = _prepare_turn(
        client, run_dir=run_dir, question=question, history=history,
    )

    def cancelled() -> bool:
        return cancel is not None and cancel.is_set()

    abort = False
    async with agent.iter(
        prompt, deps=repo_tools, message_history=history,
    ) as run:
        async for node in run:
            if cancelled():
                abort = True
                break
            if Agent.is_model_request_node(node):
                async with node.stream(run.ctx) as stream:
                    async for event in stream:
                        if cancelled():
                            abort = True
                            break
                        chunk = _delta_text(event)
                        if chunk and on_delta is not None:
                            on_delta(chunk)
                if abort:
                    break
            elif Agent.is_call_tools_node(node):
                async with node.stream(run.ctx) as stream:
                    async for event in stream:
                        if isinstance(event, FunctionToolCallEvent) and on_tool is not None:
                            on_tool(_tool_label(event.part))

    if abort:
        raise ConsoleCancelled("console turn cancelled")
    result = run.result
    # Non-None once the iteration runs to completion (only the cancel
    # path leaves it unset, and that raised above).
    assert result is not None, "agent run finished without a result"
    return result.output, list(result.all_messages())


async def run_console_turn(
    client: Client,
    *,
    run_dir: Path,
    question: str,
    history: list | None = None,
) -> tuple[str, list]:
    """Run one console turn to completion and return (answer, new_history).

    The Slice 1 blocking shape, retained for the non-streaming callers
    and tests: a thin wrapper over :func:`stream_console_turn` with no
    callbacks and no cancel flag, so it accumulates the full answer and
    returns it once the agent finishes.
    """
    return await stream_console_turn(
        client, run_dir=run_dir, question=question, history=history,
    )
