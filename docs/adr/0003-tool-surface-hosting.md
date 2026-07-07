# ADR 0003 — Tool surface: shared cache, long-lived MCP host

- Status: Proposed
- Date: 2026-07-07

## Context

Two backend models reach the same read-only tools by different routes:

- **SDK backends** (`anthropic:`, `google-vertex:`, `openai:`) run the
  tool loop **in-process** via pydantic-ai. Tools are the `RepoTools`
  methods, registered as `TOOL_FUNCTIONS` and passed as `deps=`. The
  console already streams both text deltas and tool activity
  (`FunctionToolCallEvent` → `console-tool`).
- **CLI backend** (`claude-cli`) runs the loop **inside `claude -p`**, a
  subprocess. It reaches tools through a per-spawn **stdio MCP server**
  (`augment/mcp_server.py`, hand-rolled JSON-RPC-over-stdio, a thin
  wrapper over the same `RepoTools`). `claude` owns that server's
  lifecycle, so it is spawned fresh on **every** request — per console
  turn and per hunk during augment. Consequences: it cold-starts a
  Python interpreter + re-imports tree-sitter + re-parses on each spawn,
  and its tool calls are invisible to `scr` (the driver sees only the
  final envelope).

So the CLI backend lacks what SDK backends already have — live tool
visibility — and pays a per-spawn cost SDK never pays. We also want room
for richer, init-expensive tools (LSP, call graph) without forking their
implementation per backend.

## Decision

**Cross-cutting tool concerns live in the shared `RepoTools` layer;
transport stays per-backend.** The MCP server is a thin wrapper over
`RepoTools` and SDK function-tools *are* `RepoTools` methods, so anything
added there reaches both backends with no transport coupling. Two things
go in this layer:

- A **`(sha, path)` read/parse cache** — memoise source reads and
  tree-sitter parses (`outline_symbols`).
- **Future richer tools** (LSP find-references, whole-diff call graph,
  semantic search) as `RepoTools` methods.

**This is safe because tools are read-only against pinned base/head
SHAs.** Inputs are immutable, so the cache is never stale and concurrent
reads need no coordination beyond compute-once-per-key. *Invariant to
preserve: no mutable/stateful tool without revisiting this decision — it
is what keeps shared state correct.*

**Host the MCP server long-lived over HTTP — CLI-path only.** `scr`
hosts one warm server; each `claude -p` connects via
`--mcp-config {type:"http", url, headers}` instead of spawning a stdio
child (`claude` supports `--transport http`/`sse`, verified). This:

- eliminates the per-spawn cold start (the augment cost above),
- lets the Slice-1 cache stay warm across the whole session, and
- makes tool calls observable **in-process** — the handler publishes
  `console-tool` directly, no back-channel, no parsing of `claude`'s
  output.

**Do not route SDK backends through the MCP server.** They already call
`RepoTools` in-process and already stream tool activity; an HTTP hop
would add latency and serialization for no observability gain. SDK
backends are untouched by this work — they consume the shared-layer
wins (cache, new tools) directly.

**Answer streaming for the CLI console is out of scope here.** Streaming
the model's *text* incrementally requires `--output-format stream-json`
and a driver read-loop change; it is orthogonal to the tool surface.
This ADR covers tools + hosting only. (A pending-indicator animation
already covers the "is it working" cue in the interim.)

## Consequences

- The CLI backend reaches SDK parity on tool visibility, and augment
  drops N process spawns — **gated on measuring that per-spawn cost
  first**, so the transport rewrite is only undertaken if the payoff is
  real.
- New surface: a localhost HTTP endpoint + bearer auth, and an MCP
  server lifecycle `scr` now owns (start/stop, teardown). Augment runs
  ~8 `claude -p` clients concurrently against the one server, so the
  cache computes once per key under a lock; reads are otherwise safe.
- **Biggest cost lives in the hosting slice:** the MCP server is
  hand-rolled stdio JSON-RPC, so HTTP means implementing MCP's
  Streamable-HTTP transport (POST + SSE, session handshake) by hand
  **or** adopting the `mcp` Python SDK — a new dependency in a codebase
  that deliberately hand-rolls to stay dep-light. That trade-off is
  settled in the slice plan, not here.
- The shared cache (Slice 1) ships and benefits SDK regardless of
  whether hosting (Slice 3) is ever built — the slices are ordered so
  the shared wins never depend on the CLI-only ones.

## Rejected alternatives

- **Route SDK backends through the hosted MCP server for uniformity.**
  Strictly worse: an HTTP hop + serialization replacing direct in-process
  calls, with no observability gain (pydantic-ai already emits the
  events).
- **A per-spawn back-channel** (the stdio grandchild POSTs tool activity
  to the review server) as the permanent design. It works and is a valid
  interim (Slice 2), but hosting subsumes it *and* delivers the perf +
  cache wins, so it is not the end state.
- **A module-level parse cache.** Violates the no-global-mutable-state
  rule; the cache is owned by the run / hosted server and passed
  explicitly.
