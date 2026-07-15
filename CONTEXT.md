# CONTEXT — semantic-code-review

A glossary of domain terms used across the codebase. Each entry pins a
concept that recurs in source, tests, and docs so we can talk about it
without re-inventing vocabulary.

This file grows incrementally — add an entry when a refactor needs a
term, not all at once. Terms not yet listed but recurring in code
include: **pass** (overview / hunk / fold-summary), **annotation**,
**row**, **smell**, **theme**. Pin these the next time a refactor
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
is "everything I need to do my job lives under this one path". The
act of *producing* a run directory is named: see [[run-spec]].

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

**RunSpec**
The shared shape both [[run-directory]] sources hand to the
materialise step. A `RunSpec` (in `fetch/run_source.py`) carries
`slug`, `raw_diff`, `base_sha`, `head_sha`, `files`, `meta` (PR-shaped,
written verbatim to `meta.json`), and an optional `spec_md_text`.
`materialize_run_metadata(spec, runs_root) → Path` writes the shared
artefacts (`raw.diff`, `files.txt`, `meta.json`, optional `spec.md`).

Two sources today (`fetch/github.py`, `fetch/local.py`), each
producing a `RunSpec` plus per-source extras carried on a wrapper —
`GithubResolved` adds the `PRRef`; `LocalResolved` adds the cwd
`.git` location, the working-state flag, and the diagnostic mode
(`"range"`, `"ref-working"`, etc.). The wrapper is transient: once
materialise + per-source worktree setup are done, downstream
consumers see only `run_dir: Path`.

Worktree mechanics stay per-source on purpose — fresh bare clone +
remote fetch for GitHub, `worktree add` against the cwd repo (or a
symlink for working-state mode) for local. Unifying them would have
meant a multi-axis conditional inside `materialize_run_metadata` for
no callsite benefit.

**Hunk**
A contiguous range of changed lines in a diff, with its `@@` header
plus old/new start+count. Both on-disk forms — the
[[augmented-diff]] text and its sidecar — model files as ordered
lists of hunks. The augment pipeline runs the per-hunk LLM pass once
per hunk (`HunkAnnotations`); the [[viewer-data]] addresses each
hunk by a stable id of the form `"H<file_idx>_<hunk_idx>"`.

**Fold region**
A collapsible region within a [[hunk]] in the viewer. Addressed by
`(file_idx, context, right_range, left_range)`:

- `context = "right"` — unchanged-context fold (collapses lines that
  exist in the post-image only). Pure-context folds are the common
  case.
- `context = "left"` — deletion-only fold (lines present pre-image,
  removed in post).
- `context = "both"` — straddles changed content; the LLM sees a
  unified-diff view of the region.

Summaries are produced on demand by the fold-summary pass the first
time a region is collapsed, then persisted in the
`augmented.scr.json` sidecar as a `FoldDescription` on the file's
first hunk — a stable home pending a schema migration that lifts
fold descriptions up to `AnnotatedFile`.

**Segment**
An LLM-produced semantic sub-slice of a [[hunk]]: a contiguous run of
the hunk's changed lines the per-hunk pass groups by intent.
`SegmentBlock` carries `new_start`/`new_count` (its head-side line
range) plus its own `intent`, `smells`, `context`, `refs`, and a
stable `id`. When a hunk has segments and segment-fold is on (viewer
fold mode ≠ `"off"`), the viewer renders the hunk body as a `seg-list`
— one collapsed summary row per segment, each independently foldable —
instead of the raw diff; toggling any segment (or fold=off) drops back
to the raw hunk diff.

Segments are semantic and fallible, *not* the deterministic structural
[[symbol]] ranges: a segment need not line up with one symbol, and the
two layers are computed independently.

**Collapsible region**
The viewer renders a file body from one model (`render._renderFileBody`):
an ordered run of *live hunks* and *collapsible regions*. Both are the
same diff-row stream (`_renderDiffRows`); the difference is only the
chrome — a live hunk shows the full [[hunk]] (header, intent, segments),
a region shows a bare "expand N lines" chip that opens to a continuous
diff.

Which hunks are live is set by the active sidebar filter (the pill's
`activeHunkIds`): with no filter every hunk is live and regions hold only
unchanged context (the between-hunk expand gaps). With a filter, only the
pill's hunks are live (their code revealed — see [[fold-level]]) — every
other hunk *demotes*, folded together with its surrounding context into
one region whose expansion shows those changes inline with no header. A
file no live hunk touches is dropped from the render.

Distinct from [[fold-region]]: a fold region is an indent-based collapse
*within* a rendered hunk (chevrons + the fold-summary pass); a
collapsible region is the between-/around-hunk expand chip that stands in
for context and, under a filter, demoted hunks.

**Fold level**
The viewer's global collapse depth (`RenderState.fold`, driven by the
fold slider / keys 1–4): `files` → `hunks` → `segments` → `off`, each a
shallower fold. Code (raw diff rows) shows only at `off`; `segments`
shows each [[hunk]]'s [[segment]] summaries (a segment-less hunk folds as
one synthetic whole-hunk segment, so every hunk behaves uniformly);
`hunks` shows hunk headers; `files` shows file headers.

Per-item exceptions live in `RenderState.overrides` — a reviewer
expanding/collapsing one file/hunk/segment; an override wins over the
level default. Picking a level (`_setGlobalFold`) is authoritative: it
clears every override, folding the whole tree to that depth, including a
filter's focused hunks.

Focus reveal (`RenderState.focusReveal`) is a separate *ephemeral* bit,
not an override: set when a sidebar pill is clicked
(`Render.applyFilterChange`), cleared the moment the slider is touched.
While set, the filter's live hunks render open (code shown) regardless of
level — so clicking a symbol shows its code — but because it isn't a
stored override it never leaks an expanded hunk back into the unfiltered
view. Fold toggles flip the actually-visible state, so one click collapses
a focus-revealed hunk rather than no-op'ing against the level default.

**Viewer data**
The in-memory runtime data structure served as `/data.json` by the
review server and consumed by the TS viewer. Defined by the
`ViewerData` interface in `viewer/assets/types.d.ts`, with subtypes
`FileBlock`, `HunkBlock`, `RowBlock`, `FoldRegion`, etc. Built from
the [[augmented-diff]] sidecar by `viewer/build_json.py` +
`viewer/hunk_layout.py`, augmented with metadata from `meta.json`.

Distinct from the [[augmented-diff]] sidecar in two ways: (1) it
includes pre-rendered row layout (the diff's two-column structure
expanded into row objects) which the sidecar leaves implicit; (2)
it carries transient runtime flags (e.g. `pending` while the
augment pass is still streaming) that have no place on the
persisted sidecar.

The TS side has no single owner for the in-memory tree today —
`boot.ts` fetches it and mutates it in response to SSE events,
while every other module reads from the same global reference. A
deepening to give it a typed owner is in flight.

**Viewer id**
The stable per-node identity the viewer keys DOM and state on, minted in
`build_json.py`: `F<idx>` per file (index into the diff's file list),
`H<fileidx>_<hunkidx>` per [[hunk]], `G<i>` per [[overview-seed]] group,
`SY<i>` per [[symbol]] node ([[symbols-axis]]). The `F<idx>` id is a
file's identity everywhere client-side: [[rendered-mode]] keys its
per-file state (source cache, flipped set, fold level, reveal/section
overrides) on it and parses the index back out for the
`/file-text?file_idx=` fetch. Ids are position-derived, so they're
stable only within one build of a given diff — not across diffs.

**Rendered mode**
A second body renderer for `.md` files (ADR 0004), switched in by a
per-file toggle in the file header. The text-diff renderer
(`_renderDiffRows`, `hunk_layout.py`, the [[collapsible-region]] model)
stays untouched and authoritative — it owns "what changed", hunks,
segments, and comment anchoring; rendered mode answers only "does the
finished prose read well". It is a separate renderer, not a feature on
the existing one: nothing keyed on row objects carries over.

Client-side given two inputs: the file's full base+head source (fetched
lazily from the `/file-text` server route on first flip, cached per
file — kept out of [[viewer-data]] so untoggled docs stay lean) and the
existing line diff. `rendered.ts` owns the mode state (which files are
flipped, the source cache); `markdown.ts` turns source into sanitized
HTML (markdown-it GFM → DOMPurify); `render.ts` consults
`Rendered.isOn` and delegates the body. The dependency is one-way
(`render.ts → rendered.ts`); the toggle repaints via a callback rather
than importing back.

Fully built (ADR 0004 slices 1–4 plus follow-ups). Two-pane base→head
render with block-level delta and run folding — `_plan` in `rendered.ts`
collapses contiguous runs of unchanged block-pairs into a full-width
chip, breaking runs at unchanged headings which stay visible as
landmarks, with context bleed and a min-run threshold. Controls are
**per-file, in the file body** (not the global slider/sidebar — rendered
mode is a per-file toggle, so a mixed text/rendered file set can't share
one global ladder): a `sections → runs → open` fold ladder and a heading
**outline** badged changed/unchanged. The outline is a third structural
notion alongside the LLM-semantic ([[segment]]) and tree-sitter-
structural ([[symbol]]) models; like them it answers a different
question and is not reconciled with them.

Delta specifics worth pinning:

- **List splitting.** `markdown.ts` splits a top-level list into one
  block per item (each re-wrapped in its own single-item `<ul>`/`<ol>`),
  so a single changed item classifies/aligns/folds alone instead of
  reddening the whole list.
- **Alignment projects the diff's own pairing**, not a positional zip: a
  block keeping any diff-aligned line (`ctx`/`pair` row) is *matched* and
  pairs 1:1 in order with the next matched block opposite; a fully
  one-sided block (`del`/`ins` only) drains against a blank cell. Still
  no cross-side content matching. See `_diffLines`/`_classify`/`_align`.
- **Intra-block sub-diff** marks the changed characters inside a replaced
  pair (deleted red left / added green right) by reusing the text diff's
  `blockDiff` + `wrapRanges` over each block's *rendered* `textContent`.
- **Math + mermaid** render from their source delimiters via the shared
  `katex.ts` / `mermaid.ts` modules (lazy-loaded, off the DOMPurify
  path), hydrated by `Markdown.hydrate` once a block is in the DOM.

**Reviewer comment**
A reviewer-authored inline comment anchored to a specific
`(file, side, line)`. Round-trips between the viewer and the
review server's `/comments` route during a session, and is
persisted to `comments.json` in the [[run-directory]].

Named `ReviewerComment` in TypeScript and `Comment` in Python —
the TS name is qualified because `lib.dom.Comment` (a `Node`
subtype) is in the global namespace and an unqualified `Comment`
would shadow it.

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
third-party LLM CLI. One today: `ClaudeCLIModel` (wraps `claude -p`).
It spawns the CLI on every
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

**Symbol**
The normalized unit of the *structural layer* — `Symbol{kind, name,
qualified_name, range, signature?, children[]}`, defined in
`structural/symbols.py`. Produced deterministically by tree-sitter
(no LLM, no hallucination): one definition (class / function /
constant) with its declared signature and exact 1-indexed line range,
nested by source containment (class ▸ method). `structural.parse`
runs a grammar's `tags.scm` tag query and folds the `@definition.*`
captures into this tree; `outline_symbols(source, lang)` is the entry
point, returning `[]` for an unsupported language or a parse failure
rather than raising.

This is the single internal currency the structural consumers read:
the `RepoTools.outline` / `symbol_at` tools, the diff-wide delta, the
overview-prompt seed, and the sidebar Symbols axis.
It is deliberately *not* reconciled with the LLM-derived
`Overview.symbols_*` / `FileSymbols` — those answer "why did this
change" (semantic, fallible); `Symbol` answers "where is the code and
what does it literally declare" (structural, exact). The two coexist as
separate layers by design (ADR 0001).

**SymbolDelta**
The deterministic base→head structural delta — `{added, removed,
modified}` lists of flat `ChangedSymbol`s, defined in
`structural/diff.py`. Computed by a `qualified_name` set-diff over the
flattened base and head `Symbol` forests (`diff_file` per file, `merge`
diff-wide): added = head-only name, removed = base-only, **modified =
same name on both sides with a differing range** (a same-span body edit
is not flagged — the range is the signal; finer "what changed" meaning
stays the LLM's). Each `ChangedSymbol` carries its `path` and the span
on its live side (head for added/modified, base for removed). Computed
by `RepoTools.compute_symbol_delta()`, which reads base via `git show`
and head from the worktree for every changed file in a supported
language; `changed_symbols()` is its JSON wrapper for the LLM tool
surface.

**Overview seed**
Before the overview pass, the pipeline computes the `SymbolDelta` and
passes it to `format_overview_prompt`, which appends a `# Symbols
changed (deterministic …)` section listing each changed symbol by kind
and `qualified_name`. The overview system prompt instructs the model to
populate `Overview.symbols_*` from that section verbatim — turning the
symbol fields from inference into a deterministic seed (ADR 0001 Slice
3). The seed is independent of `--no-context` (it's our own tree-sitter
parse, not LLM tool access) and best-effort (a failure leaves the
overview unseeded). When the delta is empty — every changed file is in
an unsupported language — no section is appended and the prompt is
byte-identical to the pre-seed form.

**Symbols axis**
The third sidebar grouping axis (after Themes and Files), built
deterministically from the `SymbolDelta`. `build_json._symbol_blocks`
parses each changed file's base/head worktree, takes the per-file
`diff_file` set-diff, and maps every changed symbol to the hunk ids its
*live*-side range overlaps (head for added/modified, base for removed).
The changed symbols are then nested by `qualified_name` into a forest of
`GroupBlock` nodes (id `SY<i>`, class ▸ method): a changed method hangs
off its enclosing class, and an unchanged ancestor is synthesized as a
context node from the live forest. A parent's `hunk_ids` is its subtree
union (clicking it filters to every changed descendant) and the count is
the distinct hunks beneath it; a leaf carries only its own. Any node
whose whole subtree touches no hunk yields no block. The viewer's
`Sidebar.rebuildSymbolsAxis` loads the forest from `DATA.symbols` at boot
(flattening every node into `byId` for active-pill lookup) and
`Sidebar` renders it as an expand/collapse tree (`_symbolNode`) reusing
the existing pill machinery (`applyFilter`, localStorage `<axis>:<id>`,
count badges). Like the Files axis it's structural — present from boot,
never refreshed by an SSE pass (ADR 0001 Slice 5).

Filtering is hunk-granular, not symbol-precise: a pill resolves to the
*hunks* its symbols overlap, and focus renders those whole hunks live
(see [[collapsible-region]]). Two symbols in one hunk — adjacent edits
with no unchanged gap between them — share that hunk id, so focusing
either surfaces both. Sub-hunk narrowing would key on [[segment]] ranges
(which carry line coordinates) but isn't done today.
