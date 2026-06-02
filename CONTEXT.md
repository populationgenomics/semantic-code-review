# CONTEXT — semantic-code-review

A glossary of domain terms used across the codebase. Each entry pins a
concept that recurs in source, tests, and docs so we can talk about it
without re-inventing vocabulary.

This file grows incrementally — add an entry when a refactor needs a
term, not all at once. Terms not yet listed but recurring in code
include: **hunk**, **fold**, **pass** (overview / hunk / fold-summary),
**annotation**, **viewer JSON**. Pin these the next time a refactor
brushes against them.

## Terms

**Run directory**
The per-review on-disk state, one directory per (repo, slug). Default
location is `~/.cache/scr/runs/<sha256-of-git-common-dir>/<run-slug>/`;
overridable with `--runs-root`. Contents:

- `meta.json` — PR-shaped metadata (title, body, base/head SHAs, file
  list, mode).
- `raw.diff` — the unified diff before any LLM augmentation.
- `augmented.diff` + `augmented.scr.json` — the [[augmented-diff]]
  artefacts emitted by the augment pipeline (paired; same data, two
  shapes).
- `base/` and `head/` — git worktrees pinned to the diff's endpoints
  so `RepoTools` (the MCP-exposed read_file / grep) can resolve paths
  during the LLM passes.
- `comments.json` — reviewer comments persisted by the back-channel
  HTTP server; populated only when `scr review` is the entry point.

Each subsystem under `fetch/`, `review/`, `augment/`, and `viewer/`
takes a `run_dir: Path` and operates inside it. The implicit contract
is "everything I need to do my job lives under this one path" — the
deepening opportunity flagged as candidate #5 is to make the act of
*producing* a run directory itself a named seam (a "run source") so
local-diff and GitHub-PR inputs share a shape.

**Augmented diff**
The output of the augment pipeline, kept on disk in two paired forms:

- `augmented.diff` — the unified diff with LLM annotations encoded as
  line-prefix metadata (`# intent: …`, `# refs: …`, `# fold: …`, etc).
  Grammar lives in `format/parse.py` ↔ `format/emit.py`. The text form
  is what the HTML viewer ultimately renders.
- `augmented.scr.json` — the same content as a Pydantic-shaped JSON
  sidecar (an `AnnotatedDiff` tree of `AnnotatedFile` → `AnnotatedHunk`
  → annotations). Round-tripped by `format/sidecar.py`. Used when code
  needs to manipulate annotations structurally (e.g. the fold-summary
  pass writing a new `FoldDescription` back into the tree).

The two are kept in sync — any code that mutates one rewrites the
other. The sidecar is the canonical structural shape; the unified-diff
form is the canonical wire shape.

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
