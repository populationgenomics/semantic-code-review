# Slices — Change explainer

A generated document about the change under review — Background,
Intuition, Code, and a reading Map — rendered in its own viewer mode,
with typed references into the diff. Design rationale lives in **ADR
0007 (change explainer)**; the presentation vocabulary lives in
[`../explainer-presentation.md`](../explainer-presentation.md). This
plan holds the *how, in order*.

Vertical slices. Each ends in something that ships and is exercisable on
its own; later slices add sections, figures and console reach, but never
block earlier ones from landing.

## Background — the fold-summary pattern as template

Generation is a route on the live server, not a pipeline pass, and it
reuses machinery `/fold-summary` and the console already proved:

- An async LLM closure wired into `serve_review` **after augment
  completes** and **only when an LLM backend is present** — a
  `--no-augment` review leaves it unset and the route 409s.
- Results fanned out over the existing SSE `/events` bus
  (`_ctx_publish`), with its subscriber fan-out, reconnect replay buffer
  and `state_lock` discipline.
- `RepoTools` bound per request to the run's `head/` + `base/`
  worktrees; on subprocess backends an `McpHttpHost` opened per request
  as a context manager, exactly as `console.py` does.

Two things are genuinely new and carry the risk:

1. **A persisted, incrementally-filled artefact.** `/fold-summary`
   writes one string back into `augmented.scr.json`. This writes a
   growing document to its own file, section by section, while the
   reviewer reads it.
2. **A second main-pane renderer.** Every existing view is the diff grid
   or a per-file variant of it (ADR 0004's rendered mode). Overview mode
   replaces the pane and re-purposes the sidebar.

## Shared currency

**`explainer.json`** in the run directory:

```
{ version, base_sha, head_sha, verdict, figure_family, cast,
  toy_data: bool, turns_used: int, sections: [Section],
  dropped_refs: int }
```

`verdict` is `"narrate"` or `"not_warranted"`. A section carries an id,
a kind (`background` | `intuition` | `code` | `map`), a `pass_id`, a
title, an ordered list of references, its subsections, its structured
affordances, and a `state` of `pending` | `ready` | `failed`. The
skeleton writes every section with `state: "pending"` and no prose; each
prose call flips the sections *it* writes to `ready`. `pass_id` is which
call that is: sections do not map one-to-one onto calls (slice 6).
`turns_used` is the document's shared tool-turn spend.

**Invalidation** is wholesale on `(base_sha, head_sha)`. On load, a
document whose SHAs do not match the run's is discarded, not migrated.

**References** are `{kind: "file" | "hunk", id}` against `F<i>` /
`H<fi>_<hi>`. Validation is membership in the id maps `build_json`
already constructs. Invalid references are dropped, counted into
`dropped_refs`, and surfaced.

**Prompt carrier** follows `Client.is_subprocess_backend`: guidance in
the system prompt on SDK backends, prepended to stdin on subprocess
backends. Nothing that scales with the change goes on argv.

---

## Slice 1 — The Map, end to end ✅ done

The smallest thing that fixes the case in the ADR's Context: press a
button, get a reading order.

- **Server.** `POST /explainer/skeleton` → runs one structured pass
  seeded with the overview JSON, the changed-file list and the
  `SymbolDelta`. No tools. Returns `verdict`, `figure_family`, `cast`,
  and the four sections with the Map's rows populated and the other
  three `pending`. Writes `explainer.json`. `GET /explainer` serves it.
- **Schema.** `explainer.json` shape above; Map rows as
  `{ref, why}`. Reference validation and `dropped_refs`.
- **Prompt.** Guidance block constant in `augment/prompts.py`, carrier
  split by backend. Map-only instructions: derivation order —
  source-of-truth before generated output before consumers — with one
  sentence of *why* per file.
- **Viewer.** Overview mode as a peer button on the collapse-level
  strip, disabled until the `overview` SSE event lands, then enabled;
  pressing it while no document exists POSTs and shows progress.
  `StoredViewState` grows `mode`, `_VERSION` → 2. Sidebar swaps to the
  section tree under its own axis id. Main pane renders the Map table.
  Clicking a row leaves overview mode and reveals the file at the live
  `collapseLevel`.
- **Config.** Opt-out flag under `[augment]`.

Ships as: a reading order for any PR, on any backend.

**Pitfalls.** The mode must not touch `collapseLevel` or `user`-owned
spans. Switching modes must not clobber the diff-mode sidebar pill in
localStorage. `verdict: "not_warranted"` must render as a real answer,
not an empty state.

**As built**, where it differs from the plan above:

- **View-state persistence.** This slice landed on `main`, where ADR
  0006 is not merged: there is no `StoredViewState` and no
  `visibility.ts`, and `RenderState.fold` persists in the URL hash. So
  the mode is `RenderState.mode` and rides the hash as `mode=`
  alongside `fold=`. Written in both modes, not only in overview: the
  viewer opens into an existing document, and a hash that omitted
  `mode=diff` could not tell a reviewer who chose the diff from a fresh
  open. When ADR 0006 lands, `mode` moves onto `StoredViewState` and
  `_VERSION` goes to 2 with the discard-with-a-warning path the ADR
  describes.
- **Two localStorage keys, not one.** The plan says the section tree
  "takes its own axis id in the `<axis>:<id>` key". With one key that
  clobbers the diff-mode pill, which the same paragraph forbids, so the
  section lives under `scr-explainer-section:<sha>` (value still
  `explainer:<id>`) and the pill keeps `scr-active-group:<sha>`. The
  section tree also bypasses the pill machinery entirely: a section is
  not a hunk filter, and routing it through `setActivePill` would break
  `activeHunkIds`.
- **`verdict_note`.** The document shape needed a field to carry the
  `not_warranted` answer; without one that verdict renders as an empty
  document, which the pitfall forbids.
- **`not_warranted` omits the prose sections** rather than writing them
  `pending`. `pending` invites generation, and the verdict is precisely
  the claim that generating them is not worth it.
- **Prose-section references were not assigned by the skeleton.** The
  shared-currency shape gives every section an ordered reference list,
  but the slice's prompt bullet says "Map-only instructions", so the
  three prose sections landed with empty `refs`. *Closed in slice 2*:
  the submission grew `section_refs` (file ids per prose section, since
  the skeleton is shown files and not hunks) and `_assemble` populates
  them. A section's prose pass expands each file into its hunks and may
  narrow to hunk references of its own.
- **Corrupt vs stale `explainer.json`.** A SHA or version mismatch is
  discarded with a log line; an unparseable file raises
  `ExplainerCorrupt` (500 on `GET /explainer`). Regenerating overwrites
  it, so corruption cannot wedge the button.
- **Styling** is the minimum that makes the Map legible; the reading
  type scale and the affordance vocabulary stay in slice 4.
- **Map leads the document**, ahead of the three prose sections, so the
  first screen after pressing the button is the section that is ready
  rather than three that are not.

## Slice 2 — Code prose, lazily ✅ done

- **Server.** `POST /explainer/section/{id}` → one structured pass per
  section, seeded with the overview, the section's own references and
  the hunk intents under them. No tools. Writes that section back and
  publishes an SSE frame.
- **Viewer.** Opening a `pending` section triggers the call and shows a
  progress state. Markdown rendered through the existing `markdown-it`
  (`html: false`) + DOMPurify path; inline references render as
  clickable chips.
- **Readiness.** A section whose anchored hunks are not yet annotated
  says so and offers to wait rather than narrating over gaps.
- **Sub-sections.** Model-chosen under Code, carrying their own
  references; they become child nodes in the sidebar tree.

Ships as: a code walkthrough with connective tissue between hunks.

**Pitfalls.** Per-section writes must not race — serialise them under
the existing `state_lock`. A failed section is `failed`, retryable, and
must not poison the document.

**As built**, where it differs from the plan above:

- **Serialised by `explainer_busy`, not by a write lock.** The plan says
  "serialise per-section writes under the existing `state_lock`". What
  landed is stronger: `state_lock` guards `explainer_busy`, and every
  explainer pass — skeleton and section alike — takes it, so a second
  POST while one is in flight is a 409. With one pass at a time there is
  no interleaved read-modify-write of `explainer.json` to guard. The
  viewer queues its own opens so the common single-tab case never sees
  the 409; a second tab does, as an error with a retry.
- **The route returns the whole document, not the section.** A section
  write mutates `dropped_refs` and `toy_data` too, so a fragment would
  be a partial answer the client has to merge. The SSE frame is the same
  `explainer` event the skeleton publishes.
- **A failed pass is a 500 *and* a broadcast.** `SectionFailed` carries
  the persisted document; the route fans it out before answering 500, so
  every other tab converges on `failed` rather than sitting on `pending`
  forever.
- **Nothing generates on render.** The pane stacks all four sections, so
  "opening a section" cannot mean "it became visible" — that would buy
  three calls the reviewer never asked for. Opening means selecting it
  in the sidebar tree, or pressing the section's own button.
- **Readiness is reachable but rare.** The plan implies a section can be
  opened mid-augment. It cannot, today: slice 1 wires the generators only
  after the augment pass completes, because both are seeded from the
  sidecar, which does not exist before then. What does reach the check is
  a hunk whose per-hunk call failed or was skipped — its intent is empty
  and the section refuses rather than narrating over it. Making the
  document reachable mid-pass needs the skeleton's seed to come from
  somewhere other than `augmented.scr.json`; it is not in these slices.
- **Inline references are `[F3]` / `[H3_1]` tokens in the prose**,
  swapped for chips over the sanitised DOM. The prompt and the renderer
  agree on the token form; see `docs/explainer-presentation.md`.
- **Subsection ids are minted server-side** as `code-1`, `code-2`. The
  ADR says subsections "mint their own ids"; a model-chosen id has to be
  unique in the document and safe as a DOM id, and a title is neither.
- **A hunk chip unfolds what it points at**; a file chip only scrolls,
  as the Map's rows already did. A hunk reference is the claim "read
  these lines", and landing on a folded header would need a second press.

## Slice 3 — Background, with tools and provenance ✅ done

- **Server.** Background's pass gets `RepoTools` (and an `McpHttpHost`
  on subprocess backends), with a **bounded turn budget**; the cap and
  the actual count go to `trace/`.
- **Cache.** Keyed on `base_sha` alone through the existing
  `CacheStore`, so it survives head movement and prompt-iteration
  re-runs on the same branch.
- **Provenance.** The files the pass read are recorded and rendered as a
  citation line under the section.
- **Affordances.** The skip box (Background's two-layer structure) and
  term lists land here.

Ships as: an explanation of the system the change lands in, with its
sources visible.

**Pitfalls.** Tool grants are Background-only; the other passes stay
seeded and tool-less. An unbounded agentic pass whose cost is invisible
is the failure mode the cap exists to prevent.

> The first pitfall was wrong and slice 6 reverses it — every prose pass
> reads the repository now. The second stands, and slice 6 tightens it:
> the cap is the document's, not the section's.

**As built**, where it differs from the plan above:

- **The turn budget is a `run_pass` parameter.** `run_pass` grew
  `request_limit`, which narrows the backend's own ceiling for one pass.
  The effective cap and the requests made go to the trace envelope as
  `turn_budget: {cap, used}` — recorded for *every* pass that declares a
  ceiling, not only this one, since the number was already in hand.
  `used` is the model-request count, including a final request that
  errored, because that request was spent. *Superseded in slice 6*:
  Background's `BACKGROUND_TURN_CAP = 12` became a document-wide
  `DOCUMENT_TURN_BUDGET`, and `used` became the driver's own count
  rather than the trace's iteration count.
- **Provenance is recorded, never submitted.** The plan says "the files
  the pass read are recorded"; the shape that satisfies the ADR's "a
  Background citing no reads is one that made it up" is one where the
  model cannot write the citation line. So the read list comes off the
  tool surface: `TOOL_FUNCTIONS` wrapped for the SDK path, the
  `McpHttpHost` dispatch hook for the subprocess path, both feeding one
  recorder. The wrappers live in `explainer_section.py`, not `tools.py`
  — this is the one pass that cites itself. *Generalised in slice 6*:
  every prose pass cites itself, and the read list belongs to the call
  rather than to one section.
- **Only `path` arguments count as a read.** `grep`'s `path_glob` is a
  filter over a search, not a file opened, so it is not recorded.
- **The read list rides the cache with the prose.** `run_pass` grew
  `payload_extra`, a supplier of run facts the model did not submit,
  merged into the payload before it is cached. Without it a cache hit
  would restore Background's prose and lose its citation line — the
  section would then claim it read nothing, which is precisely the claim
  the line exists to make believable. It is not a submission field, so a
  model that emits one is ignored.
- **The citation line renders even when empty**, as "Written without
  reading any file." A missing line and an empty one look the same, and
  the empty case is the one the affordance exists for.
- **Background's cache key is `(pass, model, system, base_sha)`.** The
  ADR says `base_sha` alone; `system` and the model are already part of
  every `run_pass` key, and dropping them would serve a document written
  by another model or under an older prompt.
- **Callouts did not land.** `docs/explainer-presentation.md` lists them
  alongside the skip box and term lists, but no slice claims them; they
  belong with slice 4's affordance styles.

## Slice 4 — Intuition and figures ✅ done

- **Presentation.** viewer.css gains the reading type scale, the
  affordance styles, the diagram class vocabulary and the added tokens.
- **Figures.** SVG slot, sanitiser (attribute strip, class allowlist,
  element allowlist, per-figure marker-id namespacing), `figcaption`,
  required `alt`.
- **Prompt.** Figure guidance — geometry plus classes, never colours —
  and the family/cast fixed by the skeleton in Slice 1 threaded into
  each prose call.
- **Toy-data notice** rendered in the footer when set.

Ships as: the document as designed, worked examples and diagrams
included.

**Pitfalls.** Sanitisation runs server-side *and* at render. A figure
that loses content is kept with its strip count recorded, not dropped
silently.

**As built**, where it differs from the plan above:

- **Attributes are an allowlist, not the contract's denylist.** The
  presentation doc names ten presentation attributes to strip; a
  per-element geometry allowlist removes those *and* everything nobody
  enumerated (`on*`, namespaced attributes, `xlink:href`). The cost is
  that a legitimate-but-unlisted attribute counts as a strip.
- **`href` never survives**, so the contract's same-document `#id`
  carve-out is moot: no element in the allowlist can carry one. The
  only surviving reference form is `marker-*="url(#id)"`.
- **A DTD ends the figure.** Nothing in the vocabulary needs one, and
  entity expansion is the only way an SVG this size is expensive to
  parse.
- **Ids are namespaced twice** — by `<section id>-<index>` on the way
  to disk, and by a render-scoped counter in the browser. Each pass is
  self-contained, so a document that reaches a browser from somewhere
  this server never wrote still cannot collide.
- **`save_explainer` sanitises and returns the document it wrote**,
  rather than a path. That is where `Figure.stripped` gets set, and the
  caller fans out the same bytes the next `GET /explainer` will serve.
  Putting it there means slices 2 and 3 get the guarantee without
  remembering to ask for it.
- **`--ui` was added to the token set.** The contract says chrome uses
  "the UI sans stack" but names no token for it, and once prose is
  serif the stack needs a name. `body` now uses it too.
- **The affordance styles landed without their slots.** Callout, skip
  box and term list are styled here because the stylesheet is one half
  of the presentation contract; their schema fields and rendering are
  slice 3's, which is where the prompt learns to emit them.
- **The toy-data notice is its own footer line**, not another item in
  the coverage stats: it qualifies what was read rather than measuring
  it.
- **The submission end was joined later.** Slices 2–4 were built in
  parallel worktrees, and what merged had a figure sanitiser, a renderer
  and a guidance block with no way for a model to emit a figure:
  `prose_figure_guidance` had no caller and `SubmittedSection` had no
  `figures`. A run over `themis-internal#448` fixed a family and an
  eight-name cast and wrote four sections with no figures. What closed
  it:
  - `SubmittedFigure` (`svg`, `alt` required, `caption`), on both a
    section and a subsection. `stripped` is not a submission field.
  - `prose_figure_guidance(doc)` in the prose call's guidance, ahead of
    the Background-only block so both passes of one document share as
    long a cacheable prefix as they can.
  - A figure submitted by a call that was given no figure rules is
    dropped, as a skip box outside Background is: with no family it was
    drawn in no vocabulary. `explainer_schema.figures_fixed` is the one
    predicate both halves ask.
  - The prose pass returns the document `save_explainer` wrote. It was
    returning the one in hand, so the SSE frame carried the model's own
    SVG with `stripped` still zero — the wire and the disk disagreeing
    about what the reader is looking at.
  - A figure's strip count is the highest any write recorded. Every
    prose call rewrites the whole document, so the second one
    re-sanitised the first one's figures, found nothing left to remove
    and recorded that zero over the count. The id prefix had the same
    shape of problem, and is now applied once rather than per write.
  - A caption renders as inline markdown, and a subsection's figures
    render at all.

## Slice 5 — Console reach ✅ done

- **Seed.** The console's first-turn seed grows the bounded section list
  (titles and references). Bodies come through a new `section(id)`
  accessor beside `hunk(id)`.
- **Selection.** `ConsoleSelection` grows `selection_kind: "explainer"`
  with `section_id`; `console_selection.ts` matches the explainer pane
  before the `"plain"` fallthrough and outside the console's own ignore
  set. `_format_selection` inlines the section body and its anchored
  hunks.

Ships as: highlight a claim, ask the model to defend it, with the code
already in context.

**As built**, where it differs from the plan above:

- **The seed's changed-file list gained `F<i>` ids.** The section list
  carries references, and a reference the model cannot turn into a path
  addresses nothing — the seed named files by path only. One id per
  file; still bounded.
- **`section(id)` renders the whole subtree**, not just the body: the
  heading, `state`, the resolved references (`F0 (src/users.py)`), the
  prose, the Map's rows, and any subsections, capped by the shared
  20 KB `_cap`. The Code section's prose lives in its subsections, so a
  body-only accessor would return the connective sentence and none of
  the walkthrough.
- **The explainer selection inlines `section(id)`'s rendering**, not the
  raw body, so the claim arrives with the section's references
  alongside it. Anchored *hunks* are capped at eight; past a handful the
  block stops being context and becomes the diff, and `hunk(id)` covers
  the rest.
- **A corrupt `explainer.json` does not fail a console turn.** `GET
  /explainer` raises `ExplainerCorrupt`; the console logs and proceeds
  without the document, because a question about something else should
  not die with the document.
- **`section(id)` is unreachable on subprocess backends.** So is
  `hunk(id)`, and for the same pre-existing reason: `mcp_tool_schemas`
  and `mcp_dispatch` derive from the `@_tool`-marked surface, and both
  console accessors are deliberately outside it. On the CLI path the
  seeded section list and the selection block are all the document
  reach there is. Fixing it means a console-flavoured MCP tool surface —
  its own change.
- **`build_console_seed`'s `explainer` argument has no default.** The
  document being absent is an ordinary state, but it is the caller's to
  state, not the function's to assume.

## Slice 6 — Passes, not sections ✅ done

Reverses slice 3's Background-only tool grant and merges two of the
three prose calls. Rationale and the amended decision table live in the
**ADR 0007 addendum**; this records what landed.

- **Passes.** `explainer_schema.PROSE_PASSES` maps the three prose
  sections onto two calls: `background` (Background alone, keyed on
  `base_sha`) and `walkthrough` (Intuition and Code together). Every
  section carries its `pass_id`.
- **Tools everywhere.** Both prose passes get `RepoTools`, with the
  per-request `McpHttpHost` on subprocess backends. The tool vocabulary
  moved out of `EXPLAINER_BACKGROUND_GUIDANCE` into a shared
  `EXPLAINER_TOOL_GUIDANCE`; what stays Background-only is its
  two-layer structure, its skip box and its term list.
- **One budget.** `DOCUMENT_TURN_BUDGET` for the whole document,
  tracked as `turns_used` on `explainer.json`; each call's ceiling is
  what is left.
- **Provenance for every pass.** The read list is the *call's*, so both
  sections of a merged pass carry it and the viewer renders one line
  under the last of them.

**As built**, where it differs from the ADR addendum:

- **The walkthrough's cache key is the assembled user text**, not
  `(base_sha, head_sha)` — as slice 2's per-section key already was. The
  seed carries the hunk intents, and prose written over a different set
  of them is different prose; the pair alone would serve one for the
  other. Background keeps `base_sha`.
- **`DOCUMENT_TURN_BUDGET = 36`, `MIN_TOOL_TURNS = 2`.** It landed at
  18, and 18 bought a Background that could name the pieces but not
  build the ground under them — the section that reads the most is the
  one whose subject is not in the diff at all. Below two turns
  remaining a pass runs seeded and tool-less rather than under a ceiling
  it cannot finish under — and is not told about the tools, since an
  advertised-but-unreachable surface makes the model hedge. That
  tool-less shape is what every prose pass was before this slice, so
  the exhausted path is known-good rather than a degraded guess.
- **`turn_budget.used` was over-reporting by one.** It counted trace
  *iterations*; a run that ends on a tool return the model never saw has
  one more iteration than it made requests, which is every successful
  `ToolOutput` pass. `run_pass` now reports the driver's own request
  count through a new `on_requests` callback, and passes the same figure
  to the trace writer — so a shared budget and the `UsageLimits` ceiling
  that enforces it cannot disagree. Pre-existing; found by metering
  against it.
- **`DOCUMENT_VERSION` → 2, and the version is checked before the
  document is parsed.** `pass_id` is required, so a v1 document no
  longer validates — and the old order (parse, then compare versions)
  would have reported an ordinary stale document as `ExplainerCorrupt`
  and 500'd `GET /explainer`. The version field exists precisely to
  detect a shape change before the models see it.
- **The route did not change shape.** `POST /explainer/section/{id}`
  still addresses a section; it runs the pass that owns it and returns
  the whole document, which slice 2 already established. Posting either
  half of the merged pair runs the same call.
- **A partial answer lands, and the missing section goes `failed`.**
  Not `pending`: `pending` is what `generateAllPending` picks up, so it
  would buy the same call again unasked. `failed` renders as "Try
  again", which re-runs the pass — the right retry for a section its own
  call did not return.
- **The viewer's queue is keyed on the pass**, and so are the in-flight,
  waiting and error states: a merged call that is writing, waiting or
  failed is all three for both of its sections, and marking only the one
  the reviewer pressed leaves its sibling claiming it was never asked
  for.
- **The submission grew an envelope.** `submit_explainer_prose` takes
  `sections: [{section, body, refs, …}]` rather than one section's
  fields at the top level — one call, one *or more* answers, and the
  partial case falls out of iterating what came back.

## House style — the reviewed repo's own note ✅ done

Not in the plan above; added after slice 6. `[augment].explainer_prompt`
(inline) and `--explainer-prompt PATH` (per run, both `scr review` and
`scr pr`) carry a note from the repo under review about how a document
like this reads there. Rationale and the boundary live in the **ADR 0007
addendum of 2026-08-28**; this records what landed.

**As built**:

- **Three passes, not four.** The note joins the guidance of the
  skeleton and the two prose passes. It is not a pass of its own —
  that is `extra_prompt`, whose output is `line_notes` on hunks.
- **The per-hunk pass has no channel for it.** `augment_run_dir` takes
  no parameter for the note, so the isolation is the absence of a
  channel rather than a check inside one. What holds it there is a test
  comparing the per-hunk pass's system text and user prompts byte for
  byte with the note set and unset, driven through `pr_flow._build_tasks`
  — the layer that does hold it. Conventional, not structural: a later
  change could thread a config field into `hunks.py`, and the test is
  what would notice.
- **`house_style` has no default at any seam it crosses**, including
  both shared task builders. Omitting it is a `TypeError`, which is the
  cheapest available fix for the failure mode that lost the per-section
  generator on the PR path; a source-inspection test additionally checks
  that neither flow hardcodes `None`.
- **No cache key of its own.** It rides `guidance`, which is in the
  system text on SDK backends and in the user text on subprocess ones,
  and both are in `run_pass`'s key. The tests run each pass twice and
  count cache entries rather than restating the key's contents — the
  shape of test that would have caught the bug the previous commit
  fixed.
- **It sits with the document-wide guidance blocks**, before
  Background's section-specific one, so the two prose passes share as
  long a cacheable prefix as they can. Its standing does not depend on
  its position: `format_house_style` states that the built-in rules win.
- **Parsing is shared with `extra_prompt`.** One `_inline_prompt`
  helper: same type check, same "whitespace means unset", same
  scope-override.
- **`--explainer-prompt` with `[augment].explainer = false` exits 2.**
  The flag is an explicit request for a document that will not be
  generated. The inline config value in the same situation stays silent
  — a setting is not a request.

## Opening into the document ✅ done

Not in the plan above; added after slice 6. The viewer opens in overview
mode when the run already has a document, and on the diff when it does
not. Precedence is explicit hash state, then the document, then the
diff.

**As built**:

- **The seam is `GET /explainer`, awaited.** Boot already fires it to
  pick up a document another tab paid for; awaiting it means the mode
  is decided from the document the viewer will paint rather than from a
  second advertisement of its existence that could disagree. `data.json`
  gains no field. The cost is one localhost round trip, and only on a
  review the feature is on for.
- **`mode=` rides the hash in both modes.** See the view-state note
  under slice 1.
- **A repaint before `renderInit` is dropped.** The explainer's
  `onChange` hook is wired before the renderer has `DATA`, so the
  awaited load's `_adopt` now reaches `render()` first — which would
  paint the empty default diff and sync the hash from default state,
  overwriting the fold level and mode the URL carries. `renderInit`
  paints immediately after, so an early request is coalesced rather
  than lost.
- **Being in the mode is what queues a document's pending sections**,
  however the mode was reached: the default, a press, or `mode=overview`
  in the URL. The skeleton is never bought that way — with no document
  there is nothing to queue, and buying one stays on the press.
- **A document in hand marks the explainer ready.** Readiness gated the
  button on the `overview` SSE frame alone. A run dir is reused for the
  same head SHA, so a tab that boots mid-pass can hold an earlier run's
  document; it would have opened into the mode with the button that
  leaves it disabled.

## A reference opens beside the document ✅ done

Not in the plan above; added after slice 6. A reference in the document
opens the file it addresses in a detail panel beside the prose instead
of leaving the mode. Rationale is the **ADR 0007 addendum of
2026-08-31**; this records what landed.

**As built**:

- **The mode paints a split**, `#app` › `.explainer-split` › the
  document cell + `aside.explainer-detail`. Only the document cell is
  written on a repaint, which is what keeps a panel — and its scroll —
  mounted across a section write the reader did not ask for. The panel
  is sticky with its own scroll, the pattern the sidebar opposite it
  already uses.
- **`explainer_panel.ts` owns the panel; `render.ts` owns the render.**
  The per-file renderer and "Open in diff" reach the panel as an
  injected `PanelHost`, so the dependency runs one way (render.ts →
  panel), as `rendered.ts` does.
- **The panel renders fresh, into its own `PaneScope`.** A scope is what
  a render pass reads its overrides from, whether the sidebar filter
  applies to it, and where a fold click in it repaints. The panel's
  `cache` is null on purpose: `renderedDiffs` holds live nodes, and a
  node cannot be in two trees — reusing one would have moved the diff's
  DOM into the panel.
- **The seed is `revealHunk`'s, scoped.** The file always opens (a panel
  showing a folded header shows nothing); a hunk reference also opens
  that hunk and its segments. Everything else keeps the collapse level,
  and folds clicked inside the panel repaint the panel alone.
- **The hunk SSE patchers no-op in overview mode.** The only `.hunk` on
  the page is then the panel's, and patching it there would write a
  panel-owned node into the diff's cache. A panel opened mid-stream
  shows the annotations that existed when it was opened; re-opening the
  reference renders the current ones.
- **Comments work unchanged.** The panel is inside `#app`, so the
  delegated gutter listener reaches it, and it renders the `.file` ›
  `.row` › `.cell-lineno` chain `renderAll` walks. Mounting content
  replays threads and reflows arrows the way a diff render does.
- **Esc closes the panel**, after the help overlay: the two things
  render.ts's key handler owns, topmost first. The comment editor and
  the console prompt keep their own Esc on their input, which the
  handler never sees.
- **The panel is not in the hash.** Reloading a URL restores the mode
  and the collapse level, not what was open beside the document. A
  follow-up if reviewers miss it; the reference is one click away in the
  prose either way.
- **An open panel takes the slack.** The document cell centres its column
  only while it is the whole pane; with the panel open (`panel-open` on
  the split, set by `explainer_panel.ts`) the cell shrink-wraps to the
  document's own max-width box and the panel takes the remainder down to
  a 380px floor, below which the document gives way first. Centred, a
  wide window left a dead zone on each side of the prose and pinned the
  panel to the far edge. The shrink-wrap is `flex: 0 1 auto`, so nothing
  couples the layout to the serif's `ch` metrics.
- **No draggable separator.** The document is capped at its measure, so a
  splitter could only take the prose below a readable width — and it
  would need `Annotations.reflowAll` on every drag frame. A considered
  follow-up if the fixed division turns out to bind.

## The fold slider drops into the ladder ✅ done

Not in the plan above; added after slice 6. In overview mode the collapse
slider and keys 1–4 leave the mode into the diff at the level they name.

**As built**:

- **Picking a level is the request.** The mode has no collapse level of
  its own, so a slider that only recorded one for later looked live and
  did nothing — while still clearing the reviewer's overrides and the
  focus reveal, invisibly. `_setGlobalFold` now routes through `setMode`,
  the same exit the Overview button runs, so the hash carries both
  `fold=` and `mode=diff`, the panel unmounts with the split, and the
  button's pressed state comes off with it.
- **The level highlight stays; the tooltip carries the rest.** The
  highlighted level is the one a press lands on, so greying the strip out
  would make chrome inert to say something the tooltip says better:
  "Leave the document and read the diff at this level". The markup's own
  per-level title is stashed on first swap and restored on the way back.
- **Overview mode repaints the slider too.** `_renderOverviewMode` calls
  `_updateSliderButtons`, which it did not before: a viewer that opened
  into the document with `fold=off` in the URL showed no level at all.

## A section says what is happening to it ✅ done

Not in the plan above; added after slice 6. The per-section status lines
report liveness while a pass runs. Client-only.

**As built**:

- **The elapsed figure is minutes, on a 30-second repaint.** Measured
  wall clock is 5 minutes for Background and 8 for the walkthrough;
  "Writing this section…" held still for that long cannot be told from a
  wedged tab. The interval runs only while a call is in flight — started
  in `_drainQueue`, stopped when the queue empties and on `init` — and
  fires the module's repaint hook, repainting the whole pane as every
  other state change here already does. An open detail panel survives it:
  `mount` replaces the document cell only.
- **A wait names what it is waiting for.** "Queued behind Background…"
  when this tab's own call holds the server's slot; "Waiting for the
  server — retrying." when a busy 409 deferred the section and nothing
  here is ahead of it — the slot is another tab's, and this tab is on the
  fallback timer. The two are different waits, and the second one is the
  reviewer's cue that the tab holding it is elsewhere. Both halves of the
  merged walkthrough report the same wait: one call writes them.
- **The sidebar tree carries the glance.** A section row's existing count
  badge shows "writing…" / "queued" / "waiting" while there is one,
  through a `statusOf` on the tree that the explainer fills in — the
  vocabulary stays with the state that decides it, and the sidebar keeps
  its no-dependency-on-the-explainer shape.

## Not in these slices

- Regenerate-a-section-with-a-nudge (`PARKED_IDEAS.md` #3).
- A per-section conversation thread distinct from the console (#4, #6).
- `scr eval` / judged quality (#19).
- Fixing the `Overview.groups` emit/parse loss — required before
  `scr lint --sidecar` is trustworthy, but independent of this work.
- Reordering the diff-mode file list, and `.gitattributes`-driven
  generated-file detection (`PARKED_IDEAS.md` #25). The Map answers the
  reading-order question inside the document; the file list itself is
  untouched here.
