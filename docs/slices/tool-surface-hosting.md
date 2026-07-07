# Slices — Tool surface & MCP hosting

Bring the `claude-cli` backend to the tool-surface parity SDK backends
already have — live tool visibility and no per-spawn cold start — while
keeping the cross-cutting wins (a read/parse cache, future richer tools)
in the shared `RepoTools` layer so both backends benefit. Design
rationale lives in **ADR 0003**; this plan holds the *how, in order*.

Vertical slices, ordered. The shared-layer slices (1, 4) ship and benefit
SDK backends on their own; the CLI-only slices (2, 3) never block them.
Slice 0 gates the expensive transport rewrite behind a measured payoff.

## Shared currency

The invariant that makes everything here safe: **tools are read-only
against pinned base/head SHAs.** Inputs are immutable, so a `(sha, path)`
cache is never stale and concurrent reads are coordination-free beyond
compute-once-per-key. Hold this line — a mutable tool breaks it and
sends us back to ADR 0003.

Layer split, kept strict:

- **Shared (`RepoTools`)** — cache (Slice 1), richer tools (Slice 4).
  Reaches both backends: the MCP server wraps `RepoTools`; SDK
  function-tools *are* its methods.
- **CLI-path only** — observability (Slice 2), HTTP hosting (Slice 3).
- **SDK backends** — untouched; consume the shared wins directly.

---

## Slice 0 — Measure the per-spawn cost

Gate for Slice 3. Instrument the CLI augment path: wall-time spent on MCP
server process spawn + Python import + tree-sitter re-parse, per hunk,
under real concurrency. A throwaway timing harness is fine.

**Done when:** we have a number for "cold-start overhead × hunks" on a
representative diff, enough to decide whether hosting (Slice 3) pays for
itself.

## Slice 1 — `(sha, path)` cache in `RepoTools` *(shared)*

Memoise source reads and tree-sitter parses (`outline_symbols`) on a
cache the run owns and passes into `RepoTools` (not a module global).
Keyed by `(sha, path)`; immutable inputs mean no invalidation.

- **Benefits now:** SDK augment shares one `RepoTools` across hunks
  (`pipeline.py`), so repeated `outline`/`symbol_at`/reads stop
  re-parsing; the console reuses parses within a turn.
- **CLI:** only helps within a single spawn until Slice 3 makes the
  server long-lived — but it is the prerequisite for that win and is
  independently valuable + low-risk.

**Done when:** repeated tool calls over the same `(sha, path)` parse
once; SDK augment on a multi-hunk file shows fewer parses; behaviour is
otherwise identical.

## Slice 2 — Live tool activity for CLI *(CLI-only)*

Bring `claude-cli` to the `console-tool` parity SDK already has. Form
depends on whether Slice 3 has landed:

- **Interim (pre-hosting):** the stdio MCP server POSTs `{tool, args}` to
  a localhost ingest endpoint on the review server; the server fans it
  out as a `console-tool` frame stamped with the in-flight turn's
  `console_id` (only one turn runs at a time). Ingest URL + token passed
  via `_mcp_config_for` env.
- **Post-hosting:** the in-process handler publishes `console-tool`
  directly — the back-channel is deleted.

**Done when:** a CLI-backed console turn shows tool-activity lines as the
model reads/greps, matching the SDK console.

## Slice 3 — Host the MCP server over HTTP *(CLI-only)*

`scr` hosts one warm MCP server; every `claude -p` connects via
`--mcp-config {type:"http", url, headers}` instead of spawning a stdio
child. Delivers the Slice-0 payoff (no per-hunk spawn), a cache warm
across the session, and in-process observability (subsumes Slice 2's
back-channel).

**Key decision, settled in this slice:** the server is hand-rolled stdio
JSON-RPC today. HTTP means either implementing MCP's Streamable-HTTP
transport (POST + SSE, `Mcp-Session-Id` handshake) by hand, **or**
adopting the `mcp` Python SDK as a dependency. Weigh dep-light
hand-rolling (matches the existing server, more code) against the SDK
(less code, a new runtime dep). Also decide the stdio server's fate:
retire it, or keep it as a fallback.

Cross-cutting concerns: localhost bind + bearer auth; lifecycle owned by
`scr` (start after augment/at serve, stop on teardown); the cache
computes once per key under a lock for the ~8 concurrent augment clients.

**Done when:** a `scr review`/`scr pr` session spawns the MCP server once
(not per hunk/turn), every `claude -p` connects to it, augment shows the
Slice-0 overhead gone, and tool activity is published in-process.

## Slice 4 — Richer tools in `RepoTools` *(shared; future)*

Init-expensive tools that only pay off amortised over a warm server: LSP
find-references, a whole-diff call graph, semantic/embeddings search.
Added as `RepoTools` methods, so both backends expose them with no
transport work (MCP wraps them; SDK function-tools are them).

**Done when:** at least one such tool is callable from both an SDK and a
CLI console turn, grounded in the run's worktrees.

## Not in these slices

- **CLI answer streaming.** Streaming the model's text incrementally
  needs `--output-format stream-json` and a driver read-loop rewrite —
  orthogonal to the tool surface (ADR 0003). The animated pending
  indicator covers the interim liveness cue.
- **Routing SDK backends through MCP.** Rejected in ADR 0003 — they are
  already in-process and streaming.
