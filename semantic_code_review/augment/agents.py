"""Pydantic-ai Agent factories for the SDK-backed passes.

Two factories — one per pass — that wire the right `output_type`,
instructions, and tool set. The Agent itself is stateless across runs;
the per-run `RepoTools` is supplied via `deps=` to `Agent.run`.

Both factories take a fully-qualified pydantic-ai model id (e.g.
`anthropic:claude-opus-4-7` or `google-vertex:gemini-2.5-pro`); the
caller in `cli._select_client` is the single place that decides which
provider an unqualified model name maps to.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic_ai import Agent
from pydantic_ai.output import ToolOutput

from .prompts import HUNK_SYSTEM, OVERVIEW_SYSTEM
from .repo_tool_fns import TOOL_FUNCTIONS
from .schemas import HunkAnnotations, OverviewSubmission
from .tools import RepoTools


# The output-tool names match what `run_agentic` exposes to CLI clients
# (`submit_overview`, `submit_annotations`) so the shared system prompts
# can keep referring to them by name. The CLI path receives the same
# named tool from `prompts.SUBMIT_*_TOOL`; the SDK path receives one
# constructed by pydantic-ai from the Pydantic model.

def make_overview_agent(model_id: str) -> Agent[None, OverviewSubmission]:
    """Agent for the PR-level overview pass.

    No repo tools are registered — the overview pass works purely from
    the diffstat + hunk headers in its prompt. Output is constrained
    via `ToolOutput(OverviewSubmission, name='submit_overview')`, the
    same wire name the CLI path's `submit_overview` tool uses.
    """
    return Agent(
        model=model_id,
        output_type=ToolOutput(OverviewSubmission, name="submit_overview"),
        instructions=OVERVIEW_SYSTEM,
    )


def make_hunk_agent(model_id: str) -> Agent[RepoTools, HunkAnnotations]:
    """Agent for the per-hunk annotation pass.

    Registers the repo tool functions so the model can `read_file`,
    `grep`, etc. against the run's worktree. Output is constrained
    via `ToolOutput(HunkAnnotations, name='submit_annotations')`.
    """
    return Agent(
        model=model_id,
        deps_type=RepoTools,
        output_type=ToolOutput(HunkAnnotations, name="submit_annotations"),
        instructions=HUNK_SYSTEM,
        tools=TOOL_FUNCTIONS,
    )


@dataclass
class SDKBackend:
    """Pipeline-side stand-in for an SDK-driven Agent.

    `pipeline.augment_run_dir` and the per-pass functions in
    `overview.py` / `hunks.py` branch on `isinstance(client, SDKBackend)`
    to pick between the pydantic-ai path (this) and the CLI subprocess
    `ClaudeClient` path. The `model_id` is a fully-qualified
    pydantic-ai model id (e.g. `anthropic:claude-opus-4-7` or
    `google-vertex:gemini-2.5-pro`).

    `repo_tools` is mutated by the pipeline once the run directory is
    known so the per-hunk agent can pass it as `deps` at run time. The
    overview agent doesn't need repo tools.

    `aclose()` is a no-op so `contextlib.aclosing` can drive the
    lifecycle uniformly across SDK and CLI clients in
    `augment_run_dir`. v0.12 collapses this dispatcher.
    """

    model_id: str
    repo_tools: RepoTools | None = None
    is_subprocess_backend: bool = False

    async def aclose(self) -> None:
        return None

