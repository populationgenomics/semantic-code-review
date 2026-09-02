# ADR 0001 — Tree-sitter structural layer

- Status: Accepted
- Date: 2026-06-11

## Context

The augment pipeline already emits a structural picture of a diff —
`Overview.symbols_added/modified/removed`, `callgraph_edges`, per-file
`FileSymbols`, and the hunk-cluster `groups` the sidebar filters by. All
of it is **LLM-derived**: it costs tokens, it can hallucinate symbols
that do not exist, and it cannot reliably attach exact line ranges.

Tree-sitter offers a deterministic alternative for the *syntactic*
slice of that picture: where every definition is, its declared
signature, and its exact range — parsed from source, for free, without
hallucination. It does **not** do name resolution, type inference, or
cross-file analysis; that remains the LLM's job (or an LSP's, which we
reject as too heavy for this project's hash-pinned, reproducible-install
posture).

The governing principle: tree-sitter is the *deterministic skeleton*
(where the code is, what it literally declares); the LLM is *meaning*
(what a reference resolves to, why a change was made). Every decision
below falls on the side of that seam where tree-sitter stops being able
to help.

## Decision

### Positioning

- **UI: stance C (independent layer).** The structural symbol view is
  its own axis. It does *not* reconcile with the LLM-derived
  `symbols_*` / `groups`; the two coexist as separate layers ("where
  the code is" vs "why it changed"). Forcing them to agree would be
  needless coupling.
- **Model: stance B (pull + seed).** The LLM gets structural data two
  ways: pull (tools it calls to verify itself) and seed (the
  deterministic symbol set-diff injected into the overview prompt, so
  it starts from truth rather than re-deriving `symbols_*`). Seeding is
  per-language; unsupported languages fall back to today's behaviour.

  Amended: the seed stays; the model's echo of it goes. Seeding was
  specified as "populate `symbols_*` from this list verbatim", which
  made the model transcribe its own prompt. Measured on
  cpg-infrastructure#373, the overview pass was 58% of the run and
  generation-bound at 16,066 output tokens, 89% of its structured output
  was `symbols_modified`, and the 262 entries were set-identical to a
  freshly recomputed `SymbolDelta` — nothing invented, nothing dropped.
  Nothing read the result: stance C means the viewer builds its Symbols
  axis from the delta, and `Overview.symbols_*` reached no consumer.
  The three fields are retired from the schema, the prompt, the SSE
  payload, the viewer JSON and `augmented.diff`. The prompt section
  stays as grounding for `summary` / `themes` / `groups` — input is
  cheap where output is not — and now says so instead of ordering a
  transcription.

### Parsing

- **Engine:** `tree-sitter` core + curated, individually
  **hash-pinned** grammar wheels. Rejected: `tree-sitter-language-pack`
  (one large opaque native blob, undercuts the auditable-minimal
  posture) and build-from-source (needs a toolchain, violates
  `--require-hashes` / `--ignore-scripts`).
- **Starter languages:** Python, TypeScript/TSX, JavaScript. Adding a
  language is a deliberate act (new pinned wheel + its tag query).
- **Source:** parse **both `base/` and `head/` worktrees** for changed
  files, eagerly; parse any file at any revision **on demand** (reuses
  `RepoTools.read_file_at` → `git show <sha>:<path>`). Added / removed /
  modified are derived by `qualified_name` set-diff (modified = same
  qualified name present on both sides with a differing range).

  Amended: the range was the wrong signal and `modified` gains a reason.
  Range-inequality made `modified` 93% noise — 244 of 262 entries on the
  measured diff had shifted only because lines above them moved, and it
  missed an in-place edit that left the span unmoved. The comparison is
  now the span *text*, and the result splits four ways: `added`,
  `removed`, `modified` (text differs; `reason` is `signature` when the
  declared header moved, else `body`), and `moved` (text identical, new
  position). `moved` is a bucket rather than a reason so that
  `added + removed + modified` is "what changed" with no filtering.
  Line count cannot substitute for text: a body edit that adds and
  removes equally preserves the span length, and on the measured diff
  that hid one of the six real API changes.

  `merge` additionally resolves cross-file moves diff-wide: a qualified
  name `removed` at one path and `added` at another *with identical
  text* collapses to one `moved` entry carrying `from_path`. Identity,
  not similarity — `qualified_name` is unique only within a file, and a
  symbol that both moved and changed stays two entries rather than
  becoming an inference this layer does not make.
- **Extraction:** tree-sitter **`tags.scm` tag queries** (the
  established convention; vendor a curated query where a grammar ships
  none) → a normalized `Symbol{kind, name, qualified_name, range,
  signature?, children[]}` tree at *definition* granularity. `signature`
  carries the literally-declared type text where the source has it
  (return/param/variable annotations, TS interface/alias bodies);
  `None` otherwise (e.g. untyped JS). This `Symbol` tree is the single
  internal currency every consumer reads.
- **Architecture:** parsing is a **runtime service**, not a build-time
  blob — required by the on-demand / arbitrary-sha capability.
  Changed-file trees may precompute-and-cache (the `fold_summary`
  precedent); arbitrary queries compute live.

### Failure mode

Unsupported language ⇒ structural features **silently absent**; the
LLM-derived layer is unaffected. No hard failure, no empty UI noise.

### v1 scope — two consumers

1. **MCP / agent tools.** New `@_tool` methods on `RepoTools`
   (auto-exported to both the pydantic-ai agent and the MCP stdio
   server), `sha` optional defaulting to head:
   - `outline(path, sha=None)` — the `Symbol` tree for one file.
   - `symbol_at(path, line, sha=None)` — the symbol enclosing a line.
   - `changed_symbols()` — the diff-wide deterministic set-diff.

   Plus the **seed** of `changed_symbols()` into the overview prompt for
   supported languages. **No `find_definition`** — name-resolution-shaped
   false confidence; the model already has `grep`.

2. **Sidebar Symbols axis.** A third axis in the existing multi-axis
   pill filter (`viewer/assets/sidebar.ts`). Changed symbols only;
   `hunk_ids` filter currency (hunk-granular); rendered as a **nested,
   expanded-by-default collapsible tree** (class ▸ method). Ancestors
   shown for context even when unchanged; a parent's `hunk_ids` = its
   subtree union; count badge = distinct subtree hunks. `GroupBlock`
   gains an optional `children[]`; `render()` grows one tree-walk path.
   Everything else rides existing rails (localStorage, `applyFilter`,
   counts).

### Out of scope

- **Symbol hover (cut).** Definition-site hover is trivial; use-site
  hover requires name resolution tree-sitter cannot do, so it can only
  be heuristic and confidently wrong. No worthwhile middle. Use-site
  "what does this resolve to" is delegated to the LLM.
- **LSP integration.** Heavy, stateful, per-language; wrong fit.

## Consequences

- The model can no longer hallucinate the symbol delta on supported
  languages — it is handed the truth and verifies against it, and (per
  the amendment above) is no longer asked to repeat it back.
- The pinned-dependency surface grows by one wheel per language; each is
  auditable in `requirements.lock`.
- Two "symbol" notions coexist by design (LLM-semantic, tree-sitter-
  structural). Accepted: they answer different questions.
- A normalized `Symbol` schema becomes a shared contract across Python
  (tools, seed) and TypeScript (sidebar) — a new term to pin in
  CONTEXT.md once the first slice lands.

## Backlog (deliberately not v1)

- (i) **Symbol-aware fold boundaries** — fold at function/class
  boundaries rather than by indentation (upgrades `fold_summary`).
  Scoped into vertical slices in
  [`docs/slices/symbol-aware-folds.md`](../slices/symbol-aware-folds.md).
- (ii) **Symbol-boundary hunk segmentation** — split a hunk spanning
  two definitions deterministically; seed/sanity-check
  `HunkAnnotations.segments`.
- (iii) **Symbol-relative comment anchoring** — anchor to
  `qualified_name` + offset to survive rebases. **Sequence after (ii)** —
  it builds on the segmentation work.
- (iv) **Callgraph / signature context injection** — pull
  caller/callee signatures into review context (half resolution-shaped;
  weigh later).
