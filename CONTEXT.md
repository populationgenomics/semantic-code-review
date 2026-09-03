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
- `explainer.json` — the [[change-explainer]] document, when one has
  been generated. Absent until the reviewer asks for it; written and
  refilled section by section by the `serve_review` explainer routes.
  Discarded on load if its `(base_sha, head_sha)` no longer match the
  run's.
- `trace/` — one JSON per LLM call (prompt, response, usage), plus
  `augment.log`. Every LLM call in the run writes one, live-session
  surfaces included: a fold summary, an explainer section and a console
  turn each land here as they happen.
- `usage.json` — token accounting for the run, derived from `trace/`
  by `augment/usage.py` after the passes finish: totals, a per-pass
  breakdown, and the per-call and (CLI backends) internal-turn
  distributions. Written once, so it is a snapshot of the run up to
  that point — the live-session traces arrive after it. Recomputable
  from the traces at any time, which is what makes them, not this file,
  the ledger.

The layout is owned by one type, `paths.RunDir`: the directory plus a
named accessor for everything in it — `head`, `base`, `repo_git`,
`raw_diff`, `files_txt`, `meta`, `spec_md`, `augmented`, `sidecar`,
`trace`, `usage`, `comments`, `explainer`. Every subsystem under
`fetch/`, `review/`, `augment/`, and `viewer/` takes a
`run_dir: paths.RunDir` and operates inside it, so "everything I need
to do my job lives under this one path" is the type rather than a
convention, and no filename is spelled in two modules.

It wraps a `Path` instead of subclassing one — a `RunDir` is the whole
directory's contract, and must not be passable where a plain path is
meant. `RunDir(p)` is the way in from a bare path, `.path` the way back
out, `.slug` the directory's own name, `.create()` the mkdir. Accessors
name files; they don't promise the files exist, since a run is filled
in over its lifetime.

It lives in `paths.py` rather than beside its producer in
`fetch/run_source.py` because every layer reads a run while only
`fetch/` writes one, and `paths.py` already resolves the runs root
these directories sit under while depending on no other layer.

The act of *producing* a run directory is named: see [[run-spec]].

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
`materialize_run_metadata(spec, runs_root) → RunDir` writes the shared
artefacts (`raw.diff`, `files.txt`, `meta.json`, optional `spec.md`)
and hands back the [[run-directory]] naming them.

Two sources today (`fetch/github.py`, `fetch/local.py`), each
producing a `RunSpec` plus per-source extras carried on a wrapper —
`GithubResolved` adds the `PRRef`; `LocalResolved` adds the cwd
`.git` location, the working-state flag, and the diagnostic mode
(`"range"`, `"ref-working"`, etc.). The wrapper is transient: once
materialise + per-source worktree setup are done, downstream
consumers see only `run_dir: paths.RunDir`.

Worktree mechanics stay per-source on purpose — fresh bare clone +
remote fetch for GitHub, `worktree add` against the cwd repo (or a
symlink for working-state mode) for local. Unifying them would have
meant a multi-axis conditional inside `materialize_run_metadata` for
no callsite benefit.

**Review config**
The half of a review's inputs that is independent of where the diff
came from. `ReviewConfig` (in `review/config.py`) holds the fifteen
settings `scr review` and `scr pr` share — runs root, augment on/off,
model, concurrency, cache switches, port, idle timeout, browser,
skip globs, extra-review prompt, client, debug, explainer on/off and
its house style. Each flow's options type composes one as a `config`
field and adds only its own source-side fields: `ReviewOptions` the
[[run-spec]] endpoints, `PrFlowOptions` the repo/number/`--yes`.

The rule for what belongs in it is settings vs collaborators. A value
the user chose travels in the config; a constructed object a flow hands
to a callee — the [[client]], the `CacheStore`, the `on_event`
publisher — stays an explicit parameter. `no_cache` and `cache_dir` are
settings, the `CacheStore` built from them is not. Composition is one
level deep: `opts.config.model`, never deeper.

Two consumers derive from it rather than reading it whole:

- `ReviewConfig.for_augment()` projects an `AugmentConfig`
  (`augment/config.py`) — model, concurrency, skip globs, extra-review
  prompt. It exists so `augment_run_dir` can name its inputs without
  the augment layer importing the review layer.
- `build_server_tasks(run_dir, cfg)` (in `review/runner.py`) returns a
  `ServerTasks` bundle (defined in `review/session.py`, next to what
  consumes it): the augment, fold-summary, console and two explainer
  closures plus the `--debug` sink binder, all `None` on a
  `--no-augment` run. One builder feeds both entry points, so a
  generator added to the bundle reaches `scr review` and `scr pr`
  together — it was two independent builders that let the per-section
  explainer pass ship on one path and not the other.

**Review session**
The state of one live review and the operations over it — `ReviewSession`
in `review/session.py`. Holds the [[run-directory]], the [[viewer-data]]
served as `/data.json`, the [[reviewer-comment]] store, the
`ServerTasks` once attached, and the guards that allow one console turn
and one explainer pass at a time. `review/server.py` is HTTP transport in
front of it and holds no review state of its own: a route decodes the
request, calls one session operation, and turns the result — or the
failure — into a response.

Three rules make that split hold:

- **The session owns the request body.** An operation validates the
  payload it is given and refuses a malformed one itself; only values
  lifted out of a URL (a path segment, a query parameter) are parsed by
  the transport. The wire format is part of what the session promises,
  so `FoldAddress.from_payload` lives with the address type rather than
  in a handler.
- **Every refusal is a `ScrError`** (`semantic_code_review/errors.py`),
  carrying the `status` and `body()` the route answers with. That is the
  whole error-to-status map: `_Handler._dispatch` reads them off the
  error, and anything that is *not* a `ScrError` is a bug and answers
  500 naming its type. The augment-side refusals (`FoldSummaryNotReady`,
  `SectionNotReady`, …) derive from it where they are defined, so the
  server maps them without importing the augment package.
- **The SSE fan-out stays the server's.** The session publishes
  *through* an injected `EventPublisher`, which is what lets its
  broadcasts be asserted without a socket.

`attach(tasks, viewer_json)` is one call because it is one event: the
augment pass has left a sidecar, so the augmented view and every task
that resolves the diff arrive together. Before it, each LLM-backed route
409s — the state the viewer polls against.

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
filter's focused hunks. Picking one from inside overview mode also leaves
the mode into the diff at that level — the document is not shown at a
level, so reaching for the zoom while reading it is a request for the
ladder.

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
file's identity everywhere client-side: [[rendered-mode]] keys both its
source cache and each pane's view state (flipped set, fold level,
reveal/section overrides) on it, and parses the index back out for the
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
existing line diff. `markdown.ts` turns source into sanitized HTML
(markdown-it GFM → DOMPurify); `render.ts` consults `Rendered.isOn` and
delegates the body. The dependency is one-way (`render.ts →
rendered.ts`); every control repaints via a callback rather than
importing back.

State splits two ways. The **view** state — which files are flipped,
and per file the fold level, run reveals and forced-open sections — is
a `Rendered.PaneState` the caller owns; `render.ts` hangs one off each
`PaneScope`, so the diff pane and the explainer's detail panel each
have their own and a control repaints only the pane it sits in. Two
consequences: a `.md` reference opens in the panel on the text diff
even when the diff pane has it flipped, and flipping it there leaves
the diff as the reviewer left it (the rule `PaneScope.overrides`
already states). The **source cache** is module-global in
`file_text.ts` (`FileTextCache`), a leaf both `render.ts` and
`rendered.ts` import: base+head text is pane-independent and costs a
`/file-text` round trip, so a second pane flipping the same file reads
it back rather than refetching, and two askers during one flight share
the request.

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
The handle the augment pipeline and the review console drive. Wraps
either a pydantic-ai model id string (for SDK backends) or a
`pydantic_ai.models.Model` instance (for CLI subprocess backends).
Constructed by `Backend.resolve(model=...)`. Defined in
`augment/agents.py`. Its `request_limit` bounds every agent loop driven
against it — an augment pass and a console turn alike, via
`augment/recording.py`, which also writes each one's trace.

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

Only `claude` has a CLI driver. The gemini-cli driver was removed
(commit b210096); Gemini access stays on the `gemini-api` SDK backend.
agy (the Gemini CLI successor) is re-checked periodically for
reintroduction. Last reviewed 2026-07 (agy 1.0.16): still blocked. It
now has `--json-schema` (validated `structured_output` in the envelope)
and `--output-format=json`, and plan-quota auth works via subprocess —
so a single-shot, no-tools pass could run on it. But it exposes no
per-invocation `--mcp-config`, so the console and per-hunk/fold-summary
passes can't ground the model in the worktree (the core mechanism, ADR
0002/0003); no `--system-prompt` (and ~31k tokens of un-suppressible
own-scaffolding per call); and only all-or-nothing
`--dangerously-skip-permissions`, not read-only tool scoping. Gate for
reintroduction: `--mcp-config` + `--system-prompt`.

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
per-file `FileSymbols` — that answers "why did this change" (semantic,
fallible); `Symbol` answers "where is the code and what does it
literally declare" (structural, exact). The two coexist as separate
layers by design (ADR 0001). The PR-level `Overview.symbols_*` used to
be the other half of that pairing and is gone: it was the model
transcribing the [[SymbolDelta]] out of its own prompt, 89% of the
overview pass's output, read by nothing.

**SymbolDelta**
The deterministic base→head structural delta — `{added, removed,
modified, moved}` lists of flat `ChangedSymbol`s, defined in
`structural/diff.py`. Computed by a `qualified_name` set-diff over the
flattened base and head `Symbol` forests (`diff_file` per file, `merge`
diff-wide), refined by comparing the two sides' **span text**:

- `added` / `removed` — the name exists on one side only.
- `modified` — the text differs. `reason` is `ChangeReason.SIGNATURE`
  when the declared header moved (an API change) or `BODY` when only the
  implementation did.
- `moved` — the text is byte-identical in a new position. Its own bucket
  rather than a reason, because it is the bulk of the delta and says
  nothing about the change: `added + removed + modified` is "what
  changed" with nothing to filter. Measured on cpg-infrastructure#373,
  244 of 262 same-name-both-sides symbols were moves.

Text is the comparison, not the span: a body edit that adds and removes
the same number of lines preserves `end - start`, and an in-place edit
preserves the span outright — both would read as no-change under a range
or length test. `merge` also collapses **cross-file** moves diff-wide: a
qualified name `removed` at one path and `added` at another with
identical text becomes one `moved` entry carrying `from_path`. Identity
is required, not similarity — `qualified_name` is unique only within a
file — so a symbol that both moved and changed stays two entries.
`ChangedSymbol.body_sha` is the comparison key `merge` needs across
files; it is excluded from every serialisation.

Each `ChangedSymbol` carries its `path` and the span on its live side
(head for added/modified/moved, base for removed). Computed by
`RepoTools.compute_symbol_delta()`, which reads base via `git show` and
head from the worktree for every changed file in a supported language;
`changed_symbols()` is its JSON wrapper for the LLM tool surface.

**Overview seed**
Before the overview pass, the pipeline computes the [[SymbolDelta]] and
passes it to `format_overview_prompt`, which appends a `# Symbols
changed (deterministic …)` section listing each changed symbol by kind
and `qualified_name`, tagging a `modified` entry with its reason. It is
context, not an order: the model uses it to ground `summary` / `themes`
/ `groups` and reports none of it back. Asking for it back was 89% of
the pass's output tokens and a pure transcription of the prompt (ADR
0001, amended). The `moved` bucket is omitted — byte-identical code that
shifted lines is prompt weight with no signal. The explainer skeleton's
`_format_symbol_section` renders the same shape for the same reasons.

The seed is our own tree-sitter parse rather than LLM tool access, and
best-effort (a failure leaves the overview unseeded). When every
rendered bucket is empty — e.g. every changed file is in an unsupported
language — no section is appended and the prompt is byte-identical to
the pre-seed form.

**Symbols axis**
The third sidebar grouping axis (after Themes and Files), built
deterministically from the `SymbolDelta`. `build_json._symbol_blocks`
parses each changed file's base/head worktree, takes the per-file
`diff_file` set-diff, and maps every changed symbol to the hunk ids its
*live*-side range overlaps (head for added/modified, base for removed).
The changed symbols are then nested by `qualified_name` into a forest of
`GroupBlock` nodes (id `SY<i>`, class ▸ method): a changed method hangs
off its enclosing class, and an unchanged ancestor is synthesized as a
context node from the live forest. A `moved` symbol is context too — its
text is byte-identical across the revisions, so it earns no pill of its
own and renders only when a changed descendant keeps it alive. A
`modified` pill's rationale names its reason ("function signature
changed in …"). A parent's `hunk_ids` is its subtree
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

**Change explainer**
The generated document *about* a change, as opposed to the per-hunk
annotations *on* it — Background, Intuition, Code and Map, with typed
references into the diff (ADR 0007). Lives in `explainer.json` in the
[[run-directory]], generated by `serve_review` routes on request rather
than by the augment pipeline, and filled section by section as the
reviewer opens them.

The four sections are not four calls: sections are what a reader
navigates, a **pass** is what gets paid for, and `PROSE_PASSES` maps
between them. The skeleton writes the Map and assigns each prose section
the files it is about. Background is then one call keyed on `base_sha`
alone, so it survives head movement; Intuition and Code are one merged
call keyed on its seed, since they are the two that most need to agree
and the seed is the dominant cost to re-pay. Both prose passes get
`RepoTools` under one turn budget shared by the whole document
(`turns_used` on the file), and both cite the files they opened —
recorded from the tool surface rather than claimed by the model, and
rendered once per call. Every section carries its `pass_id`, so the
viewer never buys one call twice (ADR 0007 addendum).

Not a partition of hunks: the themes axis stays the only one of those.
References address a [[viewer-id]] — a file (`F<i>`) or a [[hunk]]
(`H<fi>_<hi>`), never a line range — and are validated by membership,
with invalid ones dropped, counted and surfaced. Index-based ids are
safe here precisely because the document is *not* durable across a
moving diff: it is discarded wholesale when `(base_sha, head_sha)`
changes, since re-anchoring prose that describes vanished code yields a
correct pointer to a wrong sentence.

The reviewed repo can add **house style** to the three explainer passes
— `[augment].explainer_prompt`, or `--explainer-prompt PATH` per run
(ADR 0007's second addendum). It reaches the document and nothing else:
`augment_run_dir` has no parameter for it, because the hunk intents are
what the document's claims are checked against and one instruction able
to shape both would let the two agree for the wrong reason. It also
widens nothing structural — the figure allowlists and reference
validation by membership do not consult the prompt.

Rendered in **overview mode**, which is orthogonal to [[fold-level]]
rather than a fifth value of it — it hides nothing, so it has no span
set, and leaving it restores the reviewer's zoom and hand-set folds
untouched. Inside the mode a reference opens the file it addresses in a
detail panel beside the document, with its own pane state — text-mode
folds and [[rendered-mode]]'s alike — so checking a claim costs no mode
switch and moves nothing in the diff; the panel's
"Open in diff" is the way out to the full ladder. The viewer opens in
the mode when a document already exists, since showing one that is
written spends nothing; with none it opens on the diff, because entering
the mode is what buys the skeleton. A `mode=` in the URL hash outranks
both. The presentation vocabulary is `docs/explainer-presentation.md`,
and it has three consumers that must agree: the prompt
(`augment/prompts.py`), the stylesheet (`viewer/assets/viewer.css`) and
the figure sanitiser, which runs on the way to disk
(`augment/explainer_figures.py`, from `save_explainer`) and again at
render (`viewer/assets/explainer_figure.ts`).
