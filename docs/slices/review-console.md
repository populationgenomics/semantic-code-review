# Slices — Review console

A bottom-anchored, multi-turn console in the live `scr review` viewer:
the reviewer asks free-form questions about the change under review, the
model answers with rendered markdown (including inline mermaid
diagrams), and it retains the same `RepoTools` surface the augment
passes used. Design rationale lives in **ADR 0002 (console)**; this plan
holds the *how, in order*.

Vertical slices, ordered. Each ends in something that ships and is
exercisable on its own; later slices add richness (streaming, rendering,
selection, CLI) but never block earlier ones from landing.

## Background — the fold-summary pattern as template

The console reuses the machinery `/fold-summary` already proved:

- An async LLM closure **wired into `serve_review` only after augment
  completes** (the sidecar/worktrees must exist), and **only when an LLM
  backend is present** — `--no-augment` reviews leave it unset and the
  route 409s.
- Results fanned out over the existing SSE `/events` bus
  (`_ctx_publish`), which already has subscriber fan-out, a reconnect
  replay buffer, and `state_lock` discipline.
- `RepoTools` bound to the run's `head/` + `base/` worktrees, exposed to
  SDK agents via `TOOL_FUNCTIONS` + `deps=` and to CLI subprocesses via
  the stdio MCP server.

Two things are genuinely new and carry the risk:

1. **A free-form agent.** Every existing pass uses
   `output_type=ToolOutput(...)` — forced structured output. The console
   agent has *no* `output_type`: it emits prose and calls tools. The CLI
   driver (`SubprocessModel.request`) is hardwired to the structured
   submit-tool path and must grow a free-form branch (Slice 5).
2. **A streaming, cancellable, long-lived turn.** `/fold-summary` runs
   `asyncio.run` on the handler thread and returns one result. A
   streaming console turn runs on a **background worker** and pumps
   deltas onto the SSE bus while the POST returns `202` (Slice 2).

## Shared currency

Per-session **conversation state** on `ServerContext`: an in-memory
pydantic-ai `message_history`, an in-flight flag, and a cancel
`asyncio.Event`. It is **ephemeral** — never persisted, dropped on
dismiss, excluded from the SSE replay buffer (a reload starts fresh).
Every console SSE frame is tagged with a `console_id` so other tabs can
ignore streams that aren't theirs.

The governing rule for context: **seed compact, pull on demand.** The
agent starts with the overview JSON + changed-file list + `SymbolDelta`
(all already computed, all bounded) and reaches for tools — including a
new diff/hunk accessor — for bulk content. This keeps each turn's
payload small, which matters because the CLI backend replays the whole
history as text every turn.

---

## Slice 1 — End-to-end one-shot console (SDK, plain text) ✅ done

The tracer bullet: a working text Q&A console, thinnest everything.

- **Server:** a free-form console agent factory (no `output_type`,
  `deps_type=RepoTools`, `tools=TOOL_FUNCTIONS`, a compact `CONSOLE_SYSTEM`
  persona) plus a diff/hunk accessor tool (`hunk(id)`) on `RepoTools`.
  Seed the first turn with overview + changed-files + `SymbolDelta`.
- A `POST /console/ask` route that runs the agent to completion
  (blocking `asyncio.run`, exactly the `/fold-summary` shape) and returns
  the full answer text. Conversation `message_history` kept in memory on
  `ServerContext`; appended each turn.
- Wired into `serve_review` after augment, gated on
  `augment and not client.is_subprocess_backend` (SDK only for now).
- **Frontend:** the persistent unobtrusive bottom bar — prompt input
  (left) sharing the bar with the existing status counts (right);
  focusable by click or `Ctrl-P` (intercept browser Print). On submit,
  POST and render the answer as **plain text** in a transcript drawer
  that grows upward (to ~50–60vh, then scrolls). `Esc` collapses + drops
  history.

**Done when:** in an SDK-backed `scr review`, the reviewer focuses the
bar, asks a question, and a tool-grounded plain-text answer appears in
the drawer; follow-ups retain context; `Esc` clears the conversation;
the bar is absent on `--no-augment` / static `render`.

## Slice 2 — Streaming over SSE, with cancel (SDK) ✅ done

Upgrade the transport from blocking-response to live streaming.

- **Server:** replace the blocking run with a **background worker** (own
  event loop) driven by `Agent.iter`. Emit `console-delta` (text chunk),
  `console-tool` (activity, e.g. "grep `RepoTools`"), `console-done`, and
  `console-error` frames via `_ctx_publish`, each tagged `console_id` and
  **excluded from the replay buffer**. `POST /console/ask` now returns
  `202`. Add `POST /console/cancel` flipping an `asyncio.Event` the
  worker checks between chunks; one in-flight turn per conversation,
  guarded by `state_lock`.
- **Frontend:** drive the transcript off the SSE stream instead of the
  POST body — accumulate deltas, render progressively, surface tool
  activity, show a Stop affordance. `Esc` cancels an in-flight turn
  before it collapses the drawer. Answer is still plain text (rendering
  lands in Slice 3).

**Done when:** an SDK answer streams token-by-token with visible tool
activity, `Esc`/Stop aborts a turn mid-flight and leaves the
conversation usable, and a mid-turn reload starts the console fresh.

## Slice 3 — Markdown + mermaid rendering

Make answers render correctly, including inline diagrams.

- **Frontend bundle:** add markdown-it + DOMPurify as npm deps bundled
  into `viewer.js`; render the accumulated answer buffer on each delta
  with raw HTML disabled and the output sanitized. Reuse the vendored
  `hljs` for non-mermaid code fences.
- **mermaid:** vendor `mermaid.min.js` (`vendor/`, `refresh.sh`,
  `_STATIC_ASSETS`, `VENDOR.md`), lazy-load it via `<script>` injection
  the first time a ```` ```mermaid ```` fence **completes** (can't
  code-split an IIFE). Render with `securityLevel: 'strict'`; an invalid
  diagram falls back to its raw source code block, never a red error.
- **Prompt:** `CONSOLE_SYSTEM` gains the explicit-but-conditional mermaid
  affordance ("emit a `mermaid` block when a diagram genuinely clarifies")
  and markdown discipline.

**Done when:** a prose answer renders as formatted markdown with
highlighted code, a question that warrants a diagram produces a rendered
inline mermaid SVG, a deliberately malformed diagram degrades to source,
and `<script>`-laden model output is neutralised.

## Slice 4 — Selection-aware context

"Ask about *this*" — bind the reviewer's selection to the turn.

- **Frontend:** on submit, read `window.getSelection()`, walk the anchor
  node up the DOM (reusing the `.cell-lineno`/`.hunk`/`.file` resolution
  from `comments.ts`), and classify code / comment / plain. For code,
  resolve `(file, side, line)` + `.closest(".hunk")` → `hunk_id`. Send
  `{selection_text, selection_kind, file?, hunk_id?, line_range?}`. Show
  a clearable selection **chip** in the console area: revealed on focus
  if a selection exists, replaced on re-select, cleared on submit.
- **Server:** fold the selection into that turn's user message
  (turn-anchored — one copy, persists via history, never re-injected).
  For `kind: "code"`, inline the enclosing hunk via the `hunk(id)`
  accessor from Slice 1.

**Done when:** selecting code (or a comment) and asking a question routes
the highlighted text — plus, for code, its enclosing hunk — into the
prompt; the chip reflects the live selection and clears on submit; a
prompt with no selection behaves exactly as Slices 1–3.

## Slice 5 — CLI backend support (non-streaming)

Un-gate the console for the OAuth-`claude` / `gemini` CLI backends.

- **Driver:** a free-form branch in `SubprocessModel.request()` — when
  `model_request_parameters.output_tools` is empty, skip `_output_tool`,
  spawn the CLI in plain text mode, parse the envelope's text result, and
  return a `TextPart`. Multi-turn continuity comes from the existing
  `_flatten_messages` replay; no `--resume` / session-id reliance (keeps
  `claude` and `gemini`/`agy` divergence out of scope).
- **Wiring:** drop the `not is_subprocess_backend` gate. CLI turns run
  one-shot on the same background worker and emit a single `console-done`
  with the full text (no incremental deltas, no tool-activity frames).
- **Frontend:** no change — the delta-accumulating handler treats "zero
  intermediate deltas, one done" uniformly; show a spinner while the
  opaque tool loop runs.

**Done when:** a `--backend claude-cli` (OAuth, no API key) review yields
a working multi-turn console — rendered markdown and mermaid included —
answered in one shot per turn, with the spinner as the only UX
difference from the SDK path.

---

## Not in these slices

- **CLI streaming + tool-activity** (`--output-format stream-json`).
  Deferred deliberately: per-provider divergence, and the non-streaming
  path already covers the no-API-key audience.
- **Persisted transcripts / round-trip to the Claude session.** The
  conversation is ephemeral by design (dropped on dismiss).
- **Multi-selection accumulation.** Slice 4 ships single-selection
  replace; "compare these two" can be done conversationally until the
  single path is proven.
- **A console-answer → reviewer-comment promotion** (turning an answer
  into an anchored comment). Weighed once the console is felt in use.
