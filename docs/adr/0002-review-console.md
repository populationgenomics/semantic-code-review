# ADR 0002 — Review console

- Status: Accepted
- Date: 2026-06-30

## Context

The viewer answers "what changed and why" through one-way, precomputed
artefacts: the overview, per-hunk annotations, and on-demand fold
summaries. A reviewer with a *specific* question — "what calls this?",
"why is this guard here?", "draw me the control flow" — has no way to
ask it. They drop to a separate Claude session that lacks the run's
worktrees, annotations, and the exact diff under review.

The same backend that augments the diff already holds everything such a
question needs: the `base/` + `head/` worktrees, the `RepoTools` surface
(read_file, grep, outline, symbol_at, changed_symbols), the overview,
and the structural `SymbolDelta`. A console folds those into an
interactive channel inside the live `scr review` session.

The governing principle: **reuse the proven `/fold-summary` shape, and
add new surface only where a conversation genuinely differs from a
one-shot pass.** A console diverges in exactly two ways — it is
*free-form* (prose, not forced structured output) and *streaming +
multi-turn* — so those are the only two places we build something new.

## Decision

### Lifecycle & state

- **Multi-turn, ephemeral, in-memory.** Conversation `message_history`,
  an in-flight flag, and a cancel `asyncio.Event` live on
  `ServerContext`. Dropped on dismiss; never persisted; excluded from
  the SSE replay buffer (a reload starts fresh). Rejected: persisting
  transcripts or round-tripping them into the Claude session — the value
  is "ask, follow up, done", and persistence raises export/posting
  questions that buy nothing for v1.
- **Live-session only.** Wired into `serve_review` after augment
  completes (worktrees + sidecar must exist) and only when an LLM
  backend is present — the `/fold-summary` gating, verbatim. Absent on
  `--no-augment` and static `scr render` output.

### The agent

- **Free-form, no `output_type`.** A new agent factory: prose output,
  `deps_type=RepoTools`, `tools=TOOL_FUNCTIONS`, and a compact
  `CONSOLE_SYSTEM` persona (grounded Q&A — prefer tools, cite
  files/lines). This is the first agent in the codebase without a
  `ToolOutput` submit tool.
- **Context: seed compact, pull on demand.** The first turn carries the
  overview JSON + changed-file list + `SymbolDelta` (all precomputed,
  bounded); bulk content comes through tools, including a new diff/hunk
  accessor (`hunk(id)`) on `RepoTools`. Rejected: front-loading the full
  augmented diff — it blows the window on large PRs and, because the CLI
  backend replays history as text every turn, is re-paid per turn.

### Transport — split by backend capability

- **SDK backends: stream.** A background worker (its own event loop)
  drives `Agent.iter`, emitting `console-delta` / `console-tool` /
  `console-done` / `console-error` frames over the existing SSE `/events`
  bus, each tagged with a `console_id` and excluded from the replay
  buffer. `POST /console/ask` returns `202`; `POST /console/cancel`
  flips the cancel event the worker checks between chunks. One in-flight
  turn per conversation, under the existing `state_lock`.
- **CLI backends: non-streaming, free-form.** `SubprocessModel.request()`
  grows a branch: when `output_tools` is empty, skip the structured
  submit-tool path, spawn the CLI in plain text mode, and return a
  `TextPart`. Multi-turn continuity rides the existing
  `_flatten_messages` history replay — **no `--resume` / session-id
  coupling**, deliberately, because `claude` and `gemini`/`agy` are
  diverging. CLI turns emit a single `console-done`; the frontend's
  delta-accumulating handler treats this as "zero deltas, one done", so
  the two transports share one render path.
- Rejected for v1: **CLI streaming** via `--output-format stream-json`.
  Per-provider divergence, and the non-streaming path already serves the
  no-API-key (OAuth-`claude`) audience the SDK path would otherwise
  exclude.

### Rendering — fully client-side

- markdown-it + DOMPurify **bundled** into `viewer.js`; the answer buffer
  re-renders on each delta with raw HTML disabled and output sanitized
  (model output is repo-sourced — a malicious repo can prompt-inject
  `<script>`/`<img onerror>`; localhost is not a safe boundary). Vendored
  `hljs` highlights non-mermaid fences.
- **mermaid** is vendored (the `hljs` pattern: `vendor/`, `refresh.sh`,
  `_STATIC_ASSETS`, `VENDOR.md`) and **lazy-loaded** by `<script>`
  injection the first time a `mermaid` fence completes — it is MB-class,
  rarely used, and cannot be code-split out of the IIFE bundle. Rendered
  with `securityLevel: 'strict'`; an invalid diagram degrades to its raw
  source block, never an error box. Rejected: server-side markdown — it
  fights streaming (per-delta re-render or a render-on-done reflow) and
  mermaid is client-side regardless.

### UI

- A **persistent, unobtrusive bottom bar**: the prompt input (left,
  flex) shares the bar with the existing status counts (right).
  Focusable by click or `Ctrl-P` (intercepting the browser Print
  shortcut — acceptable on a dedicated localhost tab). Rejected: a
  summoned/hidden overlay — the always-present line is lower friction.
- Engaged → input auto-grows (1→~6 lines) and a transcript drawer grows
  upward (~50–60vh, then scrolls). `Esc` cancels an in-flight turn, else
  collapses + drops history.
- **Selection-aware, turn-anchored.** On submit, `window.getSelection()`
  is walked up the DOM (reusing `comments.ts`'s
  `.cell-lineno`/`.hunk`/`.file` resolution) and classified code /
  comment / plain; code resolves to `(file, hunk_id, side, range)`. The
  selection is folded once into that turn's user message (for code, the
  enclosing hunk is inlined via the `hunk(id)` accessor) and persists via
  history — never re-injected. A clearable chip in the console area shows
  the live selection (revealed on focus, replaced on re-select, cleared
  on submit). v1 is single-selection **replace**, matching the browser's
  own single-range model.

### No caching, no trace

Console turns are exploratory, history-dependent, and one-off; the
content-hash `CacheStore` the augment/fold passes use would serve stale
answers to re-asked questions. Skip it.

## Consequences

- The CLI driver gains a second mode (free-form text) alongside the
  structured submit-tool path — the riskiest new code, and the reason
  CLI support sequences **last** in the slice plan, against an
  already-proven console.
- A background worker now mutates shared `ServerContext` concurrently
  with request and SSE threads; conversation state and the in-flight
  flag must observe the same `state_lock` discipline the SSE buffer
  already enforces.
- The frontend bundle grows by markdown-it + DOMPurify (~100KB) and gains
  a lazy mermaid dependency; `vendor/` grows by one pinned library.
- The SDK and CLI experiences differ visibly: SDK streams with live tool
  activity, CLI shows a spinner over an opaque tool loop until the whole
  answer lands. Accepted as the cost of covering both backends in v1.

## Backlog (deliberately not v1)

- **CLI streaming + tool-activity** (`--output-format stream-json`),
  once the per-provider envelope divergence is worth absorbing.
- **Persisted / exportable transcripts**, and promotion of a console
  answer into an anchored reviewer comment — weighed once the console is
  felt in use.
- **Multi-selection accumulation** ("compare these two"), after the
  single-selection path is proven.

Scoped into vertical slices in
[`docs/slices/review-console.md`](../slices/review-console.md).
