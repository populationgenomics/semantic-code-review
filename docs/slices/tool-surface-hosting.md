# Slices — Tool surface & MCP hosting

Bring the `claude-cli` backend to the tool-surface parity SDK backends
already have — live tool visibility and no per-spawn cold start — while
keeping the cross-cutting win (a read/parse cache) in the shared
`RepoTools` layer so both backends benefit. Design rationale lives in
**ADR 0003**; this plan holds the *how, in order*. (Further init-expensive
tools the warm host unlocks are an ADR 0003 consequence, not a slice here.)

Vertical slices, ordered. The shared-layer slice (1) ships and benefits
SDK backends on its own; the CLI-only slice (3) never blocks it. Slice 0
gates the expensive transport rewrite behind a measured payoff; Slice 2
was folded into Slice 3.

## Shared currency

The invariant that makes everything here safe: **tools are read-only
against pinned base/head SHAs.** Inputs are immutable, so a `(sha, path)`
cache is never stale and concurrent reads are coordination-free beyond
compute-once-per-key. Hold this line — a mutable tool breaks it and
sends us back to ADR 0003.

Layer split, kept strict:

- **Shared (`RepoTools`)** — the cache (Slice 1). Reaches both backends:
  the MCP server wraps `RepoTools`; SDK function-tools *are* its methods.
  (Richer tools the host unlocks: see ADR 0003 Consequences.)
- **CLI-path only** — HTTP hosting (Slice 3), which subsumes the
  observability originally scoped as Slice 2.
- **SDK backends** — untouched; consume the shared wins directly.

---

## Slice 0 — Measure the per-spawn cost

Gate for Slice 3. Instrument the CLI augment path: wall-time spent on MCP
server process spawn + Python import + tree-sitter re-parse, per hunk,
under real concurrency. A throwaway timing harness is fine.

**Done when:** we have a number for "cold-start overhead × hunks" on a
representative diff, enough to decide whether hosting (Slice 3) pays for
itself.

**Result (measured):** ~765 ms per spawn, ~99% of it Python interpreter
startup + import (tree-sitter, pydantic_ai); the first tree-sitter parse
is ~6 ms — negligible. Under 8-way concurrency (augment's real
concurrency) per-spawn ready time rises to ~990 ms from CPU contention;
wall-clock for a batch of 8 spawns is ~1.24 s. Modelled: a 50-hunk PR
burns ~38 s of serial-equivalent cold-start CPU (~6–8 s wall at
concurrency 8), scaling linearly with hunk count.

Two consequences for the design:

- **The eliminable cost is the import, not the parse.** Hosting's payoff
  is killing the ~765 ms/spawn interpreter+import, not warming the parse
  cache — the cross-spawn parse saving is ~6 ms/file. So Slice 1's cache
  gives the CLI path almost nothing until a warm server exists, and even
  then little; it earns its keep on the SDK path (in-process, no spawn).
- **Not all of it is on the critical path.** The spawn overlaps `claude
  -p`'s own startup and the multi-second per-hunk LLM round-trip, so the
  wall-clock urgency is softer than the CPU number suggests. The clear
  win is reclaimed CPU (8 cores each burning ~1 s per hunk-batch) and
  concurrency headroom.

**Verdict:** cost is real and scales with hunk count — Slice 3 is
justified. (Harness was throwaway; not committed.)

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

## Slice 2 — Live tool activity for CLI *(CLI-only) — SKIPPED*

**Not built. Folded into Slice 3.** The plan kept two forms of CLI tool
visibility: an interim back-channel (the stdio MCP server POSTs
`{tool, args}` to a localhost ingest endpoint, fanned out as a
`console-tool` frame) and a post-hosting form (the in-process handler
publishes `console-tool` directly). Slice 0 and the Slice 3 decision
retire the interim form before it is worth writing:

- **It is throwaway.** Slice 3 hosts the server in-process and publishes
  `console-tool` directly; the back-channel it would replace is deleted
  the moment hosting lands. Building it now is code written to be thrown
  away one slice later.
- **Nothing forces the interim.** Slice 0 cleared the Slice 3 gate, so
  hosting is going ahead directly — there is no prolonged pre-hosting
  window the back-channel needs to cover. The animated pending indicator
  already carries the interim liveness cue (ADR 0003).

So CLI tool visibility ships as part of Slice 3, via the in-process
handler — no separate slice, no back-channel.

**Done when:** covered by Slice 3's done-criteria (tool activity
published in-process).

## Slice 3 — Host the MCP server over HTTP *(CLI-only)*

`scr` hosts one warm MCP server; every `claude -p` connects via
`--mcp-config {type:"http", url, headers}` instead of spawning a stdio
child. Delivers the Slice-0 payoff (no per-hunk spawn), a cache warm
across the session, and in-process observability (subsumes Slice 2's
back-channel).

**Transport (settled in ADR 0003):** build on the `mcp` Python SDK —
hand-rolling MCP's Streamable-HTTP transport (POST + SSE,
`Mcp-Session-Id` handshake) a second time is recurring cost for a spec
we don't own, so the SDK dependency is accepted. The hand-rolled stdio
server (`augment/mcp_server.py`) is retired once this lands, not kept as
a fallback. This slice's work is the implementation: stand up the SDK
server, wire the lifecycle, delete the stdio spawn path and its
`--mcp-config` plumbing.

Cross-cutting concerns: localhost bind + bearer auth; lifecycle owned by
`scr` (start after augment/at serve, stop on teardown); the cache
computes once per key under a lock for the ~8 concurrent augment clients.

**Done when:** a `scr review`/`scr pr` session spawns the MCP server once
(not per hunk/turn), every `claude -p` connects to it, augment shows the
Slice-0 overhead gone, and tool activity is published in-process.

**Status: done.** The host (`augment/mcp_http_host.py`), thread-safe
cache, and endpoint wiring are in — augment starts one host per run and
the console one per turn, both pointing the driver at it and (console)
publishing `console-tool` in-process. Live-validated (`scr review` shows
tools working over HTTP), so the stdio server is fully retired:
`augment/mcp_server.py`, `_mcp_config_for`, `set_repo_tools`/`_repo_tools`
on the CLI driver, and their tests are deleted — no fallback. The live
contract test (`test_claude_cli_live.py`) now drives the HTTP host.

## Not in these slices

- **CLI answer streaming.** Streaming the model's text incrementally
  needs `--output-format stream-json` and a driver read-loop rewrite —
  orthogonal to the tool surface (ADR 0003). The animated pending
  indicator covers the interim liveness cue.
- **Routing SDK backends through MCP.** Rejected in ADR 0003 — they are
  already in-process and streaming.
