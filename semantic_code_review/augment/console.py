"""Free-form review console agent.

The console is the first agent in the codebase without a `ToolOutput`
submit tool: it emits prose and calls the same read-only `RepoTools`
the augment passes use, plus a console-only `hunk(id)` diff accessor.
It is wired into the live `scr review` server after augmentation
completes, for both SDK and CLI subprocess backends (ADR 0002 —
console).

This module owns the agent factory, the compact first-turn seed, and
the turn drivers. The streaming driver (`stream_console_turn`, Slice 2)
drives `Agent.iter` and pumps text deltas + tool-activity out through
caller-supplied callbacks while polling a cancel flag between chunks.
CLI subprocess backends (Slice 5) can't stream, so `stream_console_turn`
detects them and falls back to a one-shot `Agent.run`
(`_run_console_turn_oneshot`): one `console-done` with the whole answer,
no intermediate deltas. `run_console_turn` is the Slice 1 blocking
shape, retained as a thin no-callback wrapper.

Context discipline (ADR 0002): **seed compact, pull on demand.** The
first turn carries the overview JSON + changed-file list + the
deterministic `SymbolDelta` + the change-explainer document's section
list when one exists (all bounded); bulk content comes through tools,
including `hunk(id)` and `section(id)`. The seed rides the
conversation's `message_history`, so it is paid once, not re-injected
per turn.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
)
from pydantic_ai.models import Model

from .. import errors, paths
from . import explainer_schema, mcp_http_host, source_cache
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
    "  - hunk(id) — the exact diff text of a hunk, by its 'H<file>_<hunk>' id\n"
    "  - section(id) — one section of the change-explainer document, by its id\n\n"
    "Ground every answer in the code. Prefer calling a tool over guessing; "
    "when you state a fact about the code, cite it as `path:line` so the "
    "reviewer can jump to it. The first message seeds you with the PR "
    "overview, the changed-file list, and the deterministic symbol delta — "
    "reach for tools for anything beyond that.\n\n"
    "If the reviewer has generated a change-explainer document, the seed "
    "also lists its sections — ids, titles and the references each one "
    "anchors to, but no prose. Call section(id) for a body. The document is "
    "generated, so treat its claims as something to check against the code, "
    "not as ground truth.\n\n"
    "Be concise and direct. Answer the question asked; don't pad with "
    "restatements or caveats. If the code doesn't settle the question, say "
    "what you'd need to look at rather than speculating.\n\n"
    "The reviewer may pin a selection to a question — highlighted code "
    "(with its enclosing hunk inlined), a comment, or a claim from the "
    "change-explainer document (with the section's prose and the hunks it "
    "anchors to inlined). When a turn carries a '# Reviewer selection' "
    "block, treat it as the subject of 'this' in the question and ground "
    "your answer in it.\n\n"
    "Your answers render as GitHub-flavoured markdown: use code spans for "
    "identifiers and `path:line` citations, fenced code blocks (with a "
    "language) for snippets, and lists or short headings to structure a "
    "longer answer. Don't emit raw HTML. When — and only when — a diagram "
    "genuinely clarifies the answer (a control- or data-flow, a call graph, "
    "a state machine, a sequence of calls across files), emit it as a "
    "```mermaid``` fenced block; for a plain factual answer, prose is "
    "better than a diagram. Keep diagrams small and label nodes with the "
    "real symbol names."
)


class ConsoleNotReady(errors.ScrError):
    """The run dir doesn't yet hold an `augmented.scr.json`.

    Augmentation is still in flight or was skipped, so there's no diff
    to ground the console against. Reaches the reviewer as a
    `console-error` frame, not a status: the route has already answered
    202 by the time the turn runs.
    """

    status = 409


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


def build_console_seed(
    diff: Any,
    *,
    symbol_delta_json: str | None,
    explainer: explainer_schema.ExplainerDocument | None,
) -> str:
    """Build the compact first-turn seed string.

    Carries the overview JSON, a one-line-per-file changed-file list,
    the deterministic `SymbolDelta` JSON when available, and — when the
    reviewer has generated one — the change-explainer document's section
    list. Everything here is already computed and bounded; bulk content
    is left to tools. The document's *prose* is deliberately not seeded:
    it is unbounded, and `section(id)` pulls a body on demand.
    """
    files_lines: list[str] = []
    for i, fp in enumerate(diff.files):
        role = fp.ann.role.value if getattr(fp.ann, "role", None) else "modified"
        summary = (getattr(fp.ann, "summary", "") or "").strip().replace("\n", " ")
        # The `F<i>` id is what the explainer's references address; without
        # it a seeded reference names nothing the model can act on.
        line = f"- F{i} {fp.path} ({role})"
        if summary:
            line += f" — {summary}"
        files_lines.append(line)
    files_block = "\n".join(files_lines) or "(no files)"

    parts = [
        f"# PR overview\n{overview_to_prompt_json(diff)}",
        f"# Changed files\n{files_block}",
    ]
    if symbol_delta_json:
        parts.append(f"# Structural symbol delta (deterministic tree-sitter base->head)\n{symbol_delta_json}")
    if explainer is not None:
        parts.append(_explainer_section_list(explainer))
    return "\n\n".join(parts)


def _explainer_section_list(doc: explainer_schema.ExplainerDocument) -> str:
    """The document's section tree as one bounded block: ids, titles,
    states and references, no bodies.

    Indentation carries the tree; the ids are what `section(id)` takes.
    """
    lines = [
        "# Change-explainer document",
        "A generated document about this change. Titles and references only —"
        " call section(id) for a body. Its claims are generated: check them"
        " against the code.",
        f"verdict: {doc.verdict}",
    ]
    if doc.verdict_note:
        lines.append(f"note: {doc.verdict_note}")

    def emit(sections: list[explainer_schema.Section], depth: int) -> None:
        for section in sections:
            refs = ", ".join(r.id for r in section.refs)
            line = f"{'  ' * depth}- {section.id}: {section.title} [{section.state}]"
            if refs:
                line += f" — refs: {refs}"
            lines.append(line)
            emit(section.subsections, depth + 1)

    emit(doc.sections, 0)
    return "\n".join(lines)


#: Defensive cap on the reviewer-supplied selection text folded into a
#: turn. The browser only ever sends what's visibly selected, but the
#: payload is untrusted, so bound it rather than trust the client.
_SELECTION_CAP = 4000

#: How many of a section's anchored hunks an explainer selection inlines.
#: A Code subsection can anchor to a dozen; past a handful the block stops
#: being context and starts being the diff, and `hunk(id)` covers the rest.
_SELECTION_HUNK_CAP = 8


def _format_selection(selection: Any, repo_tools: RepoTools) -> str:
    """Render a reviewer's pinned selection as a prompt block, or ``""``.

    The block names what was highlighted and quotes it, then adds the
    context that kind of selection can supply: a code selection inlines
    its enclosing hunk, an explainer selection inlines the section's own
    prose *and* the hunks that section anchors to (the claim and the code
    the claim is about). Comment/plain selections carry just the quoted
    text. An absent/empty/non-dict selection yields ``""``.

    Wire shape (from the viewer's `console_selection.ts`):
    ``{selection_text, selection_kind, file?, side?, hunk_id?,
    line_range?, section_id?}``.
    """
    if not isinstance(selection, dict):
        return ""
    text = str(selection.get("selection_text") or "").strip()
    if not text:
        return ""
    if len(text) > _SELECTION_CAP:
        text = text[:_SELECTION_CAP] + "\n…(truncated)"
    kind = str(selection.get("selection_kind") or "plain")

    parts = [
        f"# Reviewer selection ({kind}){_selection_where(kind, selection)}",
        "The reviewer highlighted this and is asking about it:",
        f"```\n{text}\n```",
    ]
    if kind == "code":
        parts += _code_context(selection, repo_tools)
    elif kind == "explainer":
        parts += _explainer_context(selection, repo_tools)
    return "\n".join(parts)


def _selection_where(kind: str, selection: dict) -> str:
    """The ` in …` clause of the selection header, or ``""``."""
    if kind == "explainer":
        section_id = selection.get("section_id")
        return f" in section `{section_id}`" if section_id else ""
    file = selection.get("file")
    if kind != "code" or not file:
        return ""
    where = f" in `{file}`"
    rng = selection.get("line_range")
    side = selection.get("side")
    if isinstance(rng, (list, tuple)) and len(rng) == 2:
        lo, hi = rng
        span = f"line {lo}" if lo == hi else f"lines {lo}–{hi}"
        side_txt = f", {side} side" if side in ("old", "new") else ""
        where += f" ({span}{side_txt})"
    return where


def _code_context(selection: dict, repo_tools: RepoTools) -> list[str]:
    """The enclosing hunk of a code selection, as prompt lines."""
    hunk_id = selection.get("hunk_id")
    if not hunk_id:
        return []
    hunk_text = repo_tools.hunk(str(hunk_id))
    # A bad/absent hunk id degrades to text-only — never surface the
    # accessor's error string into the prompt.
    if hunk_text.startswith("error:"):
        return []
    return ["Enclosing hunk:", f"```diff\n{hunk_text}\n```"]


def _explainer_context(selection: dict, repo_tools: RepoTools) -> list[str]:
    """The selected section's prose and its anchored hunks, as prompt lines.

    A `pending` section renders as the accessor's "not written yet" —
    the reviewer can still select the heading, and the hunks it anchors
    to are the answerable part. A selection that landed in the document
    but outside any section carries no ``section_id`` and gets nothing.
    """
    section_id = selection.get("section_id")
    if not section_id:
        return []
    rendered = repo_tools.section(str(section_id))
    if rendered.startswith("error:"):
        return []
    out = ["Section:", f"```\n{rendered}\n```"]

    doc = repo_tools.explainer
    if doc is None:
        return out
    section = explainer_schema.find_section(doc, str(section_id))
    if section is None:
        return out
    hunk_refs = [r for r in section.refs if r.kind == "hunk"]
    blocks = [t for t in (repo_tools.hunk(r.id) for r in hunk_refs[:_SELECTION_HUNK_CAP]) if not t.startswith("error:")]
    if blocks:
        joined = "\n\n".join(blocks)
        out += ["Hunks this section anchors to:", f"```diff\n{joined}\n```"]
    omitted = len(hunk_refs) - _SELECTION_HUNK_CAP
    if omitted > 0:
        out.append(f"({omitted} further anchored hunks omitted — call hunk(id) for them.)")
    return out


def _load_explainer(run_dir: paths.RunDir, diff: Any) -> explainer_schema.ExplainerDocument | None:
    """The run's change-explainer document, or None when there isn't one.

    Absent (nobody pressed the button) and stale (the SHAs moved) are
    both ordinary. Corruption is not, but it belongs to the explainer's
    own routes, which raise on it — a console turn about something else
    entirely should not die because the document is unreadable, so it is
    logged and the turn proceeds without it.
    """
    try:
        return explainer_schema.load_explainer(
            run_dir,
            base_sha=diff.pr.base_sha,
            head_sha=diff.pr.head_sha,
        )
    except explainer_schema.ExplainerCorrupt:
        log.warning("console: explainer.json is unreadable — the turn goes without it", exc_info=True)
        return None


def _prepare_turn(
    client: Client,
    *,
    run_dir: paths.RunDir,
    question: str,
    history: list | None,
    selection: Any = None,
) -> tuple[Agent[RepoTools, str], str, RepoTools]:
    """Shared per-turn setup: load the sidecar, bind `RepoTools`, build
    the agent, and assemble the prompt (the compact seed prefix on the
    first turn, the bare question thereafter).

    When ``selection`` is present it is folded once into this turn's user
    message, just ahead of the question — turn-anchored, so it rides the
    conversation history and is never re-injected on later turns.

    Raises :class:`ConsoleNotReady` if the augmented sidecar isn't on
    disk yet. Returns ``(agent, prompt, repo_tools)`` ready to run or
    iterate.
    """
    sidecar = run_dir.sidecar
    if not sidecar.exists():
        raise ConsoleNotReady("augmented.scr.json missing — augment not complete")

    # Lazy: keep the format machinery off the import path for the
    # agent-factory-only callers.
    from ..format.sidecar import load_sidecar

    diff = load_sidecar(sidecar)
    repo_tools = RepoTools(
        head_worktree=run_dir.head,
        repo_git=run_dir.repo_git,
        base_sha=diff.pr.base_sha,
        head_sha=diff.pr.head_sha,
        diff=diff,
        explainer=_load_explainer(run_dir, diff),
        cache=source_cache.SourceCache(),
    )

    selection_block = _format_selection(selection, repo_tools)
    question_section = f"# Reviewer question\n{question}"
    if selection_block:
        question_section = f"{selection_block}\n\n{question_section}"

    if history:
        prompt = question_section
    else:
        # Best-effort structural seed: a parse failure leaves the seed
        # without the delta rather than failing the turn.
        symbol_delta_json: str | None = None
        try:
            symbol_delta_json = repo_tools.compute_symbol_delta().model_dump_json()
        except Exception:  # noqa: BLE001 — seed is best-effort
            log.warning("console seed: symbol delta failed", exc_info=True)
        seed = build_console_seed(
            diff,
            symbol_delta_json=symbol_delta_json,
            explainer=repo_tools.explainer,
        )
        prompt = f"{seed}\n\n{question_section}"

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


def _tool_label_from_call(name: str, args: dict[str, Any]) -> str:
    """`_tool_label` for the hosted MCP handler.

    The handler hands us `(name, args)` directly, not a pydantic event part.
    """
    if isinstance(args, dict):
        for value in args.values():
            text = str(value).strip()
            if text:
                return f"{name} {text[:60]}"
    return name


async def _run_console_turn_oneshot(
    client: Client,
    agent: Agent[RepoTools, str],
    prompt: str,
    repo_tools: RepoTools,
    *,
    session_id: str | None,
    cancel: threading.Event | None,
    on_tool: Callable[[str], None] | None = None,
) -> tuple[str, str | None]:
    """Run one console turn to completion via `Agent.run` (CLI backends).

    CLI drivers can't stream, and — unlike SDK backends — don't carry the
    conversation as pydantic `message_history`. Each turn resumes the
    driver's persisted `claude -p` session (``session_id``, None on the
    first turn) so the subprocess restores its own tool-loop context —
    MCP reads, prior answers — instead of re-deriving it from a lossy text
    replay (the prior turns' internal tool calls never surface to
    pydantic-ai). The compact seed is likewise paid once, into the session.

    The CLI driver exposes the worktree to the subprocess through MCP, not
    pydantic-ai `deps`, so `RepoTools` is bound onto the client's inner
    Model for the duration of the call (and unbound after, which also
    cleans up the temp MCP-config file). `deps=` is still passed so the
    agent's tool surface validates; `message_history` is `None` because the
    CLI session, not pydantic-ai, holds the history. Cancel is best-effort:
    the subprocess runs as one opaque shot, so we honour the flag before
    and after rather than mid-flight.

    Returns ``(answer, session_id)`` — the session id to thread into the
    next turn.
    """
    if cancel is not None and cancel.is_set():
        raise ConsoleCancelled("console turn cancelled")
    client.set_console_session(session_id)

    # One warm HTTP MCP host for this turn (ADR 0003 Slice 3): the driver
    # connects `claude -p` to it instead of spawning a stdio child, and the
    # handler's on_tool fires as the model reads/greps — the console-tool
    # visibility the CLI path otherwise lacks (this folds in Slice 2).
    host_on_tool: mcp_http_host.OnToolCall | None = None
    if on_tool is not None:
        publish = on_tool

        def _publish_tool(name: str, args: dict[str, Any]) -> None:
            publish(_tool_label_from_call(name, args))

        host_on_tool = _publish_tool

    with mcp_http_host.McpHttpHost(repo_tools, on_tool=host_on_tool) as host:
        client.set_mcp_endpoint(host.mcp_config())
        try:
            result = await agent.run(prompt, deps=repo_tools, message_history=None)
        finally:
            client.set_mcp_endpoint(None)
    if cancel is not None and cancel.is_set():
        raise ConsoleCancelled("console turn cancelled")
    return result.output, client.last_console_session_id


async def stream_console_turn(
    client: Client,
    *,
    run_dir: paths.RunDir,
    question: str,
    history: Any = None,
    on_delta: Callable[[str], None] | None = None,
    on_tool: Callable[[str], None] | None = None,
    cancel: threading.Event | None = None,
    selection: Any = None,
) -> tuple[str, Any]:
    """Stream one console turn, returning ``(answer, new_history)``.

    Drives the agent via ``Agent.iter`` so assistant text can be pumped
    to ``on_delta`` chunk-by-chunk and each tool invocation announced to
    ``on_tool`` as it fires. Between chunks it polls ``cancel`` (a
    ``threading.Event`` flipped from another thread by ``/console/cancel``)
    and raises :class:`ConsoleCancelled` when set — the partial turn is
    abandoned and its messages never join the returned history.

    `history` is the opaque continuation token from the prior turn (None
    on the first), backend-shaped: pydantic `message_history` for SDK
    backends, the `claude -p` session id (a str) for CLI subprocess
    backends. The returned value is the token to carry into the next turn;
    the caller holds it verbatim and never inspects it. `selection`, when
    present, is the reviewer's pinned selection, folded once into this
    turn's user message (see :func:`_format_selection`).
    """
    agent, prompt, repo_tools = _prepare_turn(
        client,
        run_dir=run_dir,
        question=question,
        history=history,
        selection=selection,
    )

    # CLI backends (Slice 5) can't stream: the subprocess runs its own
    # opaque tool loop and returns the whole answer at once, and
    # `Agent.iter`'s per-node streaming would hit the driver's
    # unimplemented `request_stream`. Run one-shot via `Agent.run`
    # instead — the worker emits a single `console-done` with the full
    # text, which the frontend renders identically to "zero deltas, one
    # done". `on_delta`/`on_tool` simply never fire.
    #
    # For subprocess backends `history` is the prior turn's `claude -p`
    # session id (a str), not pydantic messages: the CLI session holds the
    # conversation, so continuity is a resume, not a replay.
    if client.is_subprocess_backend:
        return await _run_console_turn_oneshot(
            client,
            agent,
            prompt,
            repo_tools,
            session_id=history,
            cancel=cancel,
            on_tool=on_tool,
        )

    def cancelled() -> bool:
        return cancel is not None and cancel.is_set()

    abort = False
    async with agent.iter(
        prompt,
        deps=repo_tools,
        message_history=history,
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
    run_dir: paths.RunDir,
    question: str,
    history: Any = None,
    selection: Any = None,
) -> tuple[str, Any]:
    """Run one console turn to completion and return (answer, new_history).

    The Slice 1 blocking shape, retained for the non-streaming callers
    and tests: a thin wrapper over :func:`stream_console_turn` with no
    callbacks and no cancel flag, so it accumulates the full answer and
    returns it once the agent finishes.
    """
    return await stream_console_turn(
        client,
        run_dir=run_dir,
        question=question,
        history=history,
        selection=selection,
    )
