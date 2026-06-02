# CONTEXT — semantic-code-review

A glossary of domain terms used across the codebase. Each entry pins a
concept that recurs in source, tests, and docs so we can talk about it
without re-inventing vocabulary.

This file grows incrementally — add an entry when a refactor needs a
term, not all at once. Terms not yet listed but recurring in code
include: **hunk**, **fold**, **pass** (overview / hunk / fold-summary),
**run directory**, **augmented diff**, **annotation**. Pin these the
next time a refactor brushes against them.

## Terms

**Backend**
A registered LLM provider that the CLI resolves a name to. Each backend
is a `Backend` subclass under `semantic_code_review/backends/`; the
registry (`backends/__init__.py`) maps `BackendType → Backend`. The
backend owns credential resolution and constructs the `Client` that
the augment pipeline drives.

**Client**
The handle the augment pipeline drives. Wraps either a pydantic-ai
model id string (for SDK backends) or a `pydantic_ai.models.Model`
instance (for CLI subprocess backends). Constructed by
`Backend.resolve(model=...)`. Defined in `augment/agents.py`.

**CLI driver**
A concrete `pydantic_ai.Model` subclass we author to wrap a specific
third-party LLM CLI. Two today: `ClaudeCLIModel` (wraps `claude -p`)
and `GeminiCLIModel` (wraps `gemini -p`). Each spawns the CLI on every
`request()`, parses its envelope, and returns a synthetic
`ModelResponse`; the multi-turn tool-call loop runs inside the
subprocess via MCP, not in pydantic-ai.

CLI drivers share `SubprocessModel` (in `backends/_cli_driver.py`) as
a base — not itself a driver, just the scaffolding they extend. Each
driver lives in its per-backend file alongside the `Backend` adapter
that constructs it.

Distinct from the `Model` subclasses pydantic-ai ships
(`AnthropicModel`, `GoogleModel`, …), which we instantiate but do not
author. pydantic-ai itself has no word for this distinction —
"`Model`" covers both — but our tree splits along it: drivers are
ours, other `Model`s come from pydantic-ai.
