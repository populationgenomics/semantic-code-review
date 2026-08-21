# CONTEXT — semantic-code-review

A glossary of domain terms used across the codebase. Each entry pins a
concept that recurs in source, tests, and docs so we can talk about it
without re-inventing vocabulary.

This file grows incrementally — add an entry when a refactor needs a
term, not all at once. Terms not yet listed but recurring in code
include: **pass** (overview / hunk / fold-summary), **annotation**,
**smell**, **theme**. Pin these the next time a refactor brushes
against them.

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
  so [[repo-tools]] can resolve paths during the LLM passes.
- `comments.json` — the [[reviewer-comment]] store. Seeded on first
  GitHub materialise from the PR's existing review comments
  (`fetch/github_comments.materialize_pr_comments`), then appended to
  by the back-channel HTTP server during a `scr review` session.
- `trace/` — one JSON per LLM call (prompt, response, usage), plus
  `augment.log`.
- `usage.json` — token accounting for the run, derived from `trace/`
  by `augment/usage.py` after the passes finish: totals, a per-pass
  breakdown, and the per-call and (CLI backends) internal-turn
  distributions. Recomputable from the traces at any time.

Each subsystem under `fetch/`, `review/`, `augment/`, and `viewer/`
takes a `run_dir: Path` and operates inside it. The implicit contract
is "everything I need to do my job lives under this one path". The
act of *producing* a run directory is named: see [[runspec]].

**Augmented diff**
The output of the augment pipeline, kept on disk in two paired forms:

- `augmented.diff` — the unified diff with LLM annotations encoded as
  `#scr:`-prefixed directive lines (`#scr: scr-hunk-intent: …`,
  `#scr: scr-fold: …`, etc; `#scr>` continues a wrapped value).
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

**Row**
One line of the viewer's side-by-side grid: `RowBlock{kind, old_line,
new_line, old_text, new_text}`, `kind` ∈ `ctx` / `ins` / `del` / `pair`.
Built per [[hunk]] server-side by `hunk_layout.build_rows` (consecutive
`-`/`+` runs paired positionally, not by LCS) and shipped on
`HunkBlock.rows`.

Three row sequences coexist and must not be confused: a hunk's own rows;
the *rendered* rows, meaning whatever a given render actually put on
screen (this grows as the reviewer reveals context and shrinks as they
collapse a [[fold-region]]); and the *whole-file* row stream `folds.ts`
synthesises from the [[file-content]] the diff never mentions, to detect
[[fold-region]]s. An index into any of them is presentation, never
identity — durable addresses use absolute file lines.

Unchanged context rows are synthesised, not shipped: the diff carries
only what a hunk covers. A run of them is the same text on both sides by
definition, so either side's content serves both halves — which is why a
file the route can serve only the base of still renders its gaps. A
zero-count hunk side is written by git as the line *before* the hunk
(`@@ -10,3 +9,0 @@`), so the cursor either side of one comes from
`FileText.hunkBounds`, not from `start + count`.

**Hidden span**
The viewer's one hiding primitive (`viewer/assets/visibility.ts`, ADR
0006). A `HiddenSpan` has an id, an owner, a kind, and side-tagged
absolute 1-indexed line ranges (`right` in head/`<path>`, `left` in
base/`<path>`, either nullable). A line is visible iff no span covers it
— visibility is the complement of the union, answerable from state with
no DOM read. Every hide is one: a [[fold-region]] collapse (`codefold`),
a file / hunk / segment header collapse, and the unchanged context a
[[collapsible-region]] stands in for (`context`). Reveal is the removal
of a span, so a re-render reproduces it, and dropping an outer span
leaves the spans nested inside it standing.

`kind` picks the presentation the renderer puts in the span's place — a
chip, a `@@` header, a segment summary row; for a `codefold` the run's
first rendered line, kept as the header the chevron and summary box hang
off (`Visibility.planRows`).

`owner` decides what may retract it, which is what makes a bulk action
not a reset:

- `level` — asserted by the [[fold-level]]. Picking a level drops every
  level-owned span *and* its mark, then re-seeds at the new depth.
- `user` — asserted by a click. Only another click, or Reset, retracts
  it. A lockfile folded away by hand therefore survives "off".
- `gap` — a [[collapsible-region]]'s context, seeded by the renderer as
  it lays each region out. Also survives a level change.

Each file keeps two ledgers: `spans` (what is hidden now) and `marks`
(every id ever asserted, and by whom). Seeding a marked id is a no-op —
that is what makes a reveal outlive both the renderer's per-render
re-seed and a bulk action. `Visibility.reset()` is the only thing that
clears the marks.

Both ledgers persist, as [[view-state]]: a reload restores the reveals
along with the hides.

A hide is not an erasure: whatever stands in a span's place is headed by
the span's [[manifest]].

**Manifest**
The list of notes a hide covers, rendered at the head of whatever stands
in its place (`viewer/assets/manifest.ts`, ADR 0006). Two columns, old
and new, of line number plus the note's first line; the colour sidebar on
each entry carries its kind, matching the expanded view's palette — amber
for a [[reviewer-comment]], blue for an LLM line note.

What is listed:

- **Unresolved reviewer comments**, one entry per thread at its root.
  Local and ingested alike: what matters is that a human owns it, not
  where it came from, so a comment promoted from an LLM annotation counts
  and the annotation it replaced does not.
- **LLM line notes** that no comment has replaced.
- **Not** resolved threads (`thread_resolved` on the thread root), and
  **not** a `CodeFold`'s generated summary — a summary describes hidden
  content and is redundant once that content is hidden again.

Entries are **not navigable** — clicking one neither scrolls to nor
reveals its line — and the list is **not bounded**.

Which notes a hide covers comes from the span store
(`Visibility.covering`), so a manifest can only be built for a hide that
is in place. Two stand-ins are not one span and take a line range
instead: a hunk body rendered as a `seg-list` (a `segment` span is a
binary switch on the body, not a hide of its own lines, so the hunk's
base side and context rows belong to no span), and the inert band that
names the unchanged lines the viewer has no [[file-content]] for.

**Fold region**
A collapsible structural region in the viewer — the `> def foo(): …`
chevron. Addressed by `(file_idx, context, right_start..right_end,
left_start..left_end)`, where the ranges are absolute 1-indexed file
lines:

- `context = "right"` — unchanged-context fold (collapses lines that
  exist in the post-image only). Pure-context folds are the common
  case.
- `context = "left"` — deletion-only fold (lines present pre-image,
  removed in post).
- `context = "both"` — straddles changed content; the LLM sees a
  unified-diff view of the region.

The address is the region's identity: what a detected region is matched
against, what `/fold-summary` asks for, and what the sidecar stores. A
symbol-snapped region takes the definition's own `fold_symbols` span; an
indentation-fallback region takes the extent of the [[row]]s it was
detected over — and the viewer detects over the whole-file row stream,
not the rendered rows, so revealing context around a hunk does not move
the address.

**Detection is the viewer's alone** (ADR 0006 slice 6), because it is
the side that knows which [[file-content]] it has. A file with none —
the route served neither side, or has not answered yet — is offered no
fold at all: a region read off the rendered rows would be addressed
differently after the next reveal, which is the bug the absolute address
exists to fix. A `CodeFold` the reviewer already collapsed needs no
detection; its [[hidden-span]] describes itself and the renderer honours
it at first paint, so what arrives with the content is the affordance,
not the state.

`FileBlock.fold_regions` is therefore not detection output: it is the
run's persisted summaries as addressed records, which the viewer matches
its own detected regions against. `hunk_layout.compute_fold_regions` is
the reference implementation the viewer's detector is pinned against on
`tests/fixtures/fold_regions_cases.json`, not part of the wire build.

Collapsing one is a [[hidden-span]] over that same address; the renderer
drops the rows it covers and keeps the region's first rendered line as
the header the chevron hangs off. The one row index left is that header,
recomputed by `_placeRegion` on every render. Under it hang the summary
and the region's [[manifest]] — minus any note on the header line
itself, which is still on screen with its own comment row.

Summaries are produced on demand by the fold-summary pass the first
time a region is collapsed, then persisted in the
`augmented.scr.json` sidecar as a `FoldDescription` on the file's
first hunk — a stable home pending a schema migration that lifts
fold descriptions up to `AnnotatedFile`. `/fold-summary` seeds its
prompt with the definition the address names, read off the file's
`fold_symbols` — the spans that address came from.

**Segment**
An LLM-produced semantic sub-slice of a [[hunk]]: a contiguous run of
the hunk's changed lines the per-hunk pass groups by intent.
`SegmentBlock` carries `new_start`/`new_count` (its head-side line
range) plus its own `intent`, `smells`, `context`, `refs`, and a
stable `id`. When a hunk has segments and the [[fold-level]] is not
`"off"`, the viewer renders the hunk body as a `seg-list` — one
collapsed summary row per segment, each independently foldable —
instead of the raw diff; toggling any segment (or dropping the level to
`off`) drops back to the raw hunk diff.

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

Chip or diff is a [[hidden-span]] of kind `context`, seeded per region as
the renderer lays it out and identified by the region's own line extent.
Clicking the chip removes the span, clicking "× collapse" puts it back;
both go through a re-render, so a reveal survives every later one. A chip
carries the region's [[manifest]] under its label.

A run of unchanged lines the viewer has no [[file-content]] for cannot be
a region — there are no rows to expand and a span would be a record no
click could retract. The renderer puts an inert `.gap-absent` band there
instead, naming how many lines are missing and carrying the notes that
sit on them, and the run *breaks* at it: the region either side stands on
its own rather than the two being spliced into one whose expansion skips
the lines in between. That happens for a file the route cannot serve, and
for the paint before it has answered.

The filter is *not* in the span model — it decides which hunks are live
and therefore what a region covers, and its demoted hunks' rows render
inside an expanded region regardless of the [[fold-level]].

Distinct from [[fold-region]]: a fold region is a structural collapse of
a definition or indented block (chevrons + the fold-summary pass), which
may straddle hunks and revealed context; a collapsible region is the
between-/around-hunk expand chip that stands in for context and, under a
filter, demoted hunks.

**Fold level**
The viewer's global collapse depth (`RenderState.collapseLevel`, driven
by the fold slider / keys 1–4): `files` → `hunks` → `segments` → `off`,
each a shallower fold. Code (raw diff rows) shows only at `off`; `segments`
shows each [[hunk]]'s [[segment]] summaries (a segment-less hunk folds as
one synthetic whole-hunk segment, so every hunk behaves uniformly);
`hunks` shows hunk headers; `files` shows file headers.

The level is not consulted per item: it *seeds* one `level`-owned
[[hidden-span]] per file / hunk / segment at that depth, and the renderer
reads the spans. Picking a level (`_setCollapseLevel`) is authoritative
over those and nothing else — it drops every level-owned span and
re-seeds, so a hunk the reviewer expanded folds back, while a hide the
reviewer asserted by hand and a gap they revealed both stand. A node that
arrives later (segments off an SSE hunk patch) is seeded at the current
level by `syncLevel`, which every `render()` calls.

Focus reveal (`RenderState.focusReveal`) is a separate *ephemeral* bit,
not a span: set when a sidebar pill is clicked
(`Render.applyFilterChange`), cleared the moment the slider is touched.
While set, the filter's live hunks render open (code shown) regardless of
level — so clicking a symbol shows its code — but because no span moved
it never leaks an expanded hunk back into the unfiltered view. It
suppresses only *level*-owned hides; one the reviewer set by hand wins.
Fold toggles flip the actually-visible state, so one click collapses a
focus-revealed hunk rather than no-op'ing.

The level persists with the spans, as [[view-state]]. Nothing about it
rides in the URL.

**View state**
What the reviewer has folded and at what depth: the [[hidden-span]]
ledgers of every file plus the [[fold-level]]. Persisted by
`viewer/assets/view_state.ts` as one `sessionStorage` record under
`scr-view-state:<run_id>`, written at the end of each `render()` — the
funnel every span mutation already passes through — and skipped when
neither the level nor the store's revision has moved. Restored by
`Render.init`, which seeds the level from scratch only when there was no
record; a restored store already carries the level's marks, and seeding
over them would re-hide what the reviewer had expanded.

`sessionStorage` is the ceiling, not a preference (ADR 0006). Every
browser tier is origin-scoped and the review server binds `--port 0`, so
the origin changes each run; a tab's lifetime, reload included, is all
that is reachable. The run id in the key is not what separates two tabs
— `sessionStorage` already does — it stops a later run inheriting an
earlier one's record when the kernel reuses the ephemeral port.

Failures split: an unavailable or full store degrades to in-memory with a
console warning; a record that parses but does not describe view state
raises `ViewStateError`, uncaught, rather than being coerced into a
plausible span set. A record at another schema `version` is discarded as
stale, which is neither.

Not everything the viewer remembers is view state under this entry: the
active sidebar pill is a [[collapsible-region]] filter, and `sidebar.ts`
still keeps it in `localStorage` keyed on `pr.head_sha`.

**Viewer data**
The in-memory runtime data structure served as `/data.json` by the
review server and consumed by the TS viewer. Defined by the
`ViewerData` interface in `viewer/assets/types.d.ts`, with subtypes
`FileBlock`, `HunkBlock`, `RowBlock`, `FoldRegion`, etc. Built from
the [[augmented-diff]] sidecar by `viewer/build_json.py` +
`viewer/hunk_layout.py`, augmented with metadata from `meta.json`.

Distinct from the [[augmented-diff]] sidecar in two ways: (1) it
includes pre-rendered [[row]] layout (the diff's two-column structure
expanded into row objects) which the sidecar leaves implicit; (2) it
carries transient runtime flags (`pending` while the augment pass is
still streaming; `_failed` on hunks, `_inflight` / `_summaryFailed` on
fold regions) that have no place on the persisted sidecar.

What it does *not* carry is file **content**. It used to — the whole
post-image of every file under 5,000 lines — and that is now the
[[file-content]] route's job, fetched per file as the renderer needs it.
The payload is the diff, its annotations, and the addresses everything
else hangs off.

Two fields are not built by `build_json.py` at all: `debug` and
`run_id` are merged into the payload by the `/data.json` handler,
because both are properties of the server and the run rather than of
the diff, and `update_viewer_json` swaps the built payload wholesale.
`run_id` is the [[run-directory]]'s name; it keys the [[view-state]].

`data_store.ts` owns every mutation of the in-memory tree. `boot.ts`
still holds the reference (it owns the `/data.json` fetch result) and
its SSE handlers translate a payload into a `DataStore` call, then ask
the right module to repaint. `DataStore` is stateless: each mutator
takes the `ViewerData` reference as its first argument.

**File content**
The text of a changed file, which the diff does not carry and
[[viewer-data]] does not ship: the viewer fetches it per file from the
review server's `/file-text?file_idx=N` route and caches it for the run
(`viewer/assets/file_text.ts`). Both sides come back — `{base, head}`,
read from the [[run-directory]]'s `base/` and `head/` worktrees, a
renamed file's pre-image from its `old_path`.

Two consumers, one cache: [[rendered-mode]] parses both sides to render
prose, and the text diff needs it for everything the diff never mentions
— the unchanged lines a [[collapsible-region]] stands in front of, and
the whole-file [[row]] stream [[fold-region]] detection reads.

**A side over `_FILE_TEXT_CAP_BYTES` (2 MB) comes back null**, as does
one that does not exist (an added file's base) or cannot be read. Null
is not an empty file and must never be read as one — an empty file
detects every fold out of existence and makes every gap zero lines long.
A file with neither side renders bands where its unchanged runs would be
and is offered no fold. This is the only bound left on either: the
payload's own 5,000-line cap went with `head_lines` (ADR 0006 slice 6).

Content arrives **after the first paint**, which is safe because a
[[hidden-span]] is self-describing: restoring state needs no content,
only offering a new fold does. An arrival repaints, coalesced to one
render per batch.

**Viewer id**
The stable per-node identity the viewer keys DOM and state on, minted in
`build_json.py`: `F<idx>` per file (index into the diff's file list),
`H<fileidx>_<hunkidx>` per [[hunk]], `G<i>` per overview theme group,
`SY<i>` per [[symbol]] node ([[symbols-axis]]). The Files sidebar axis is
the exception: it mints `BF<file_idx>` client-side in `sidebar.ts`.
The `F<idx>` id is a file's identity everywhere client-side: the
[[file-content]] cache is keyed on it, [[rendered-mode]] keys its
per-file state (flipped set, fold level, reveal/section overrides) on it,
and the index parses back out of it for the `/file-text?file_idx=`
fetch. Ids are position-derived, so they're
stable only within one build of a given diff — not across diffs.

**Rendered mode**
A second body renderer for `.md` files (ADR 0004), switched in by a
per-file toggle in the file header. The text-diff renderer
(`_renderDiffRows`, `hunk_layout.py`, the [[collapsible-region]] model)
stays untouched and authoritative — it owns "what changed", hunks,
segments, and comment anchoring; rendered mode answers only "does the
finished prose read well". It is a separate renderer, not a feature on
the existing one: nothing keyed on row objects carries over.

Client-side given two inputs: the file's full base+head source
([[file-content]] — the route rendered mode was the first consumer of)
and the existing line diff. `rendered.ts` owns the mode state (which
files are flipped, their fold level and reveals) but not the source;
`markdown.ts` turns source into sanitized
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
An inline comment anchored to a specific `(file, side, line)`.
Round-trips between the viewer and the review server's `/comments`
route during a session, and is persisted to `comments.json` in the
[[run-directory]].

`source` splits two populations. `"local"` — authored in this session,
editable, postable upstream. `"github"` — ingested from the PR on first
materialise, read-only, carrying `author`, `in_reply_to_id`,
`commit_id`, `html_url`, and provider-rendered `body_html`. Posting a
local comment resolves it through an [[anchor]] first, then converts it
to an ingested one.

A comment whose line is hidden is not gone: it appears as an entry in the
[[manifest]] of the hide covering it, unless its thread is resolved.

Named `ReviewerComment` in TypeScript and `Comment` in Python —
the TS name is qualified because `lib.dom.Comment` (a `Node`
subtype) is in the global namespace and an unqualified `Comment`
would shadow it.

**Anchor**
Two mechanisms share the word; both concern where a
[[reviewer-comment]] sits.

- *Inbound* (`fetch/anchor.py`): an ingested comment was written against
  an older commit, so its line is propagated forward through the
  intervening diffs onto the run's head, yielding `head_line` and
  `anchor_status`.
- *Outbound* (`review/anchors.py`): GitHub threads a review comment only
  to a line inside a diff hunk, but the viewer lets a reviewer comment on
  revealed context. `postable_ranges(diff_text)` collects the legal
  `(path, side)` ranges; `resolve()` returns an `Anchor` — unchanged when
  the line is already legal, else moved to the nearest hunk line within
  `NEAREST_LINE_LIMIT`, else demoted to a file-level thread (`line=None`).
  The reason is appended to the body by `with_note`, so a reader is never
  silently misdirected. Out-of-hunk lines otherwise fail silently: the
  GraphQL mutation returns 200 with no errors and a null thread.

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

Two flags on it bound and shape a pass. `request_limit` caps the SDK
agentic loop the way `--max-turns` caps a CLI driver. `native_output`
constrains output with the provider's native structured output instead
of a submit tool; SDK backends set it from
`backends/base.supports_native_output_with_tools(model)` — a per-*model*
capability read off the pydantic-ai profile, so a model outside the
supported list falls back to the tool path rather than failing every
hunk. It matters because Anthropic rejects thinking and output tools in
the same request. CLI drivers always leave it false: their
`--json-schema` is derived from the output tool.

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

**Repo tools**
The read-only tool surface the LLM calls during a pass: `RepoTools` in
`augment/tools.py`, holding the run's head worktree and bare repo, the
base/head SHAs, and an optional shared `SourceCache`. Every method
marked `@_tool` is exported by introspection to both consumers —
pydantic-ai SDK Agents via `TOOL_FUNCTIONS`, and the hosted MCP server
the [[cli-driver]]s reach via `mcp_tool_schemas` / `mcp_dispatch`
(`mcp_http_host.py`, ADR 0003) — so renaming a method moves both wire
surfaces at once.

Today: `read_file`, `read_file_at`, `outline`, `symbol_at`,
`changed_symbols`, `grep`, `grep_at`, `references`, `list_dir`,
`git_log`. The review console appends `hunk(id)`, deliberately *not*
`@_tool`-marked because it needs a bound sidecar that does not exist at
augment time. Results are capped at `TOOL_RESULT_CAP_BYTES` (20 KB) and
flagged when truncated.

Every exported tool takes a leading `purpose` string, required by the
generated schema and stripped before dispatch — the method never sees
it. It is injected once in `_make_tool_fn` rather than declared per
method, and exists so a trace shows *why* each call was made.

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

This is the single internal currency the *definition-side* structural
consumers read: the [[repo-tools]] `outline` / `symbol_at` tools, the
diff-wide delta, the overview-prompt seed, and the sidebar Symbols axis.
It is deliberately *not* reconciled with the LLM-derived
`Overview.symbols_*` / `FileSymbols` — those answer "why did this
change" (semantic, fallible); `Symbol` answers "where is the code and
what does it literally declare" (structural, exact). The two coexist as
separate layers by design (ADR 0001). Use sites are a separate unit
again — see [[reference]].

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
3). The seed is independent of `--skip-context` (it's our own tree-sitter
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
`Sidebar` renders it as an expand/collapse tree (`_treeNode`, shared
with the Files axis) reusing the existing pill machinery (`applyFilter`,
localStorage `<axis>:<id>`, count badges). Like the Files axis it's
structural — present from boot, never refreshed by an SSE pass (ADR
0001 Slice 5).

Filtering is hunk-granular, not symbol-precise: a pill resolves to the
*hunks* its symbols overlap, and focus renders those whole hunks live
(see [[collapsible-region]]). Two symbols in one hunk — adjacent edits
with no unchanged gap between them — share that hunk id, so focusing
either surfaces both. Sub-hunk narrowing would key on [[segment]] ranges
(which carry line coordinates) but isn't done today.

**Reference**
The use-site unit of the structural layer — `Reference{line, kind, text}`
in `structural/references.py`, behind the [[repo-tools]] `references`
tool. Answers "where is this name used", the counterpart to
[[symbol]]'s "where is it defined", and the question the model asks far
more often.

Two backends. Python uses `ast`: the tree-sitter tags query captures
only `reference.call`, and captures `np.array(x)` as `array` — losing
the module root, so "is this import still used" is unanswerable from it.
Every other language uses the tags query's `@reference.*` captures.
Both are name-based, not scope-resolved: two distinct `helper`s in one
file are indistinguishable. Better than a text search — comments,
strings, substrings and the definition itself are excluded — but not a
resolver, and callers must not present it as one. A file that will not
parse falls back to a text search of that file and is flagged inline
rather than dropped.
