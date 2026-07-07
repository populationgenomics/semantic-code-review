"""Pydantic-ai Agent factories for both SDK and CLI backends.

Two factories — one per pass — that wire the right `output_type`,
instructions, and tool set. The Agent itself is stateless across runs;
the per-run `RepoTools` is supplied via `deps=` to `Agent.run`.

`model` accepts either a fully-qualified pydantic-ai model id string
(e.g. `anthropic:claude-opus-4-7` or `google-vertex:gemini-2.5-pro`)
*or* a `pydantic_ai.models.Model` instance — the CLI driver in
`backends/claude_cli.py` is a Model subclass that wraps the
`claude -p` client.
`cli._select_client` is the single place that decides which form an
unqualified model name maps to.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.output import ToolOutput

from .prompts import HUNK_SYSTEM, OVERVIEW_SYSTEM
from .schemas import HunkAnnotations, OverviewSubmission
from .tools import TOOL_FUNCTIONS, RepoTools


def make_overview_agent(model: str | Model) -> Agent[None, OverviewSubmission]:
    """Agent for the PR-level overview pass.

    No repo tools are registered — the overview pass works purely from
    the diffstat + hunk headers in its prompt. Output is constrained
    via `ToolOutput(OverviewSubmission, name='submit_overview')`.
    """
    return Agent(
        model=model,
        output_type=ToolOutput(OverviewSubmission, name="submit_overview"),
        instructions=OVERVIEW_SYSTEM,
    )


def make_hunk_agent(model: str | Model) -> Agent[RepoTools, HunkAnnotations]:
    """Agent for the per-hunk annotation pass.

    Registers the repo tool functions so the SDK Agent can `read_file`,
    `grep`, etc. against the run's worktree. CLI backends ignore
    `function_tools` — they expose the same tools to the underlying
    subprocess via the MCP server. Output is constrained via
    `ToolOutput(HunkAnnotations, name='submit_annotations')`.
    """
    return Agent(
        model=model,
        deps_type=RepoTools,
        output_type=ToolOutput(HunkAnnotations, name="submit_annotations"),
        instructions=HUNK_SYSTEM,
        tools=TOOL_FUNCTIONS,
    )


@dataclass
class Client:
    """Pipeline-side handle for an LLM backend.

    Holds either a pydantic-ai model id string (for SDK backends) or
    a `pydantic_ai.models.Model` instance (for CLI subprocess backends —
    the CLI drivers under `backends/`). The pipeline calls
    `make_*_agent(client.model)` to build pass-specific agents.

    `set_mcp_endpoint` proxies to the inner CLI Model when present so the
    subprocess reaches tools through the run's hosted MCP server; SDK
    string models have no MCP concept here — the SDK Agent receives
    `deps=repo_tools` at `Agent.run` call time instead.

    `aclose()` is delegated to the inner Model. SDK string models have
    no per-run resources to release; the no-op fallthrough is fine.
    """

    model: str | Model
    is_subprocess_backend: bool = False

    def set_mcp_endpoint(self, config: dict[str, Any] | None) -> None:
        """Point a subprocess backend at the run's hosted HTTP MCP server.

        `config` is the run's `McpHttpHost.mcp_config()` (or None to clear).
        Proxies to the inner CLI Model (ADR 0003 Slice 3). SDK string models
        have no subprocess to point — the call no-ops.
        """
        if isinstance(self.model, Model):
            setter = getattr(self.model, "set_mcp_endpoint", None)
            if callable(setter):
                setter(config)

    def set_console_session(self, session_id: str | None) -> None:
        """Resume the CLI's console session next turn (subprocess only).

        The console calls this before a subprocess turn so the driver adds
        `--resume <id>` and the model's tool-loop context carries across
        turns. SDK string models have no session concept — the call no-ops,
        and the console threads pydantic `message_history` for them instead.
        """
        if isinstance(self.model, Model):
            setter = getattr(self.model, "set_console_session", None)
            if callable(setter):
                setter(session_id)

    @property
    def last_console_session_id(self) -> str | None:
        """Session id of the most recent subprocess console turn, else None."""
        if isinstance(self.model, Model):
            return getattr(self.model, "last_console_session_id", None)
        return None

    def set_debug_sink(self, sink: Callable[[dict[str, Any]], None] | None) -> None:
        """Route the CLI driver's per-spawn debug records to `sink`.

        Bound by the review server in `--debug` mode. Only subprocess
        backends emit records; SDK string models have no subprocess envelope,
        so the call no-ops.
        """
        if isinstance(self.model, Model):
            setter = getattr(self.model, "set_debug_sink", None)
            if callable(setter):
                setter(sink)

    async def aclose(self) -> None:
        if isinstance(self.model, Model):
            close = getattr(self.model, "aclose", None)
            if callable(close):
                # Dynamic probe: getattr erases the type, so pyright can't see the coroutine.
                await close()  # pyright: ignore[reportGeneralTypeIssues]
