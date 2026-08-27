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
  toy_data: bool, sections: [Section], dropped_refs: int }
```

`verdict` is `"narrate"` or `"not_warranted"`. A section carries an id,
a kind (`background` | `intuition` | `code` | `map`), a title, an
ordered list of references, its subsections, its structured
affordances, and a `state` of `pending` | `ready` | `failed`. The
skeleton writes every section with `state: "pending"` and no prose; each
prose call flips one to `ready`.

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
  the mode is `RenderState.mode` and rides the hash as `mode=overview`
  alongside `fold=`. When ADR 0006 lands, `mode` moves onto
  `StoredViewState` and `_VERSION` goes to 2 with the discard-with-a-
  warning path the ADR describes.
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
- **Prose-section references are not assigned by the skeleton.** The
  shared-currency shape gives every section an ordered reference list,
  but the slice's prompt bullet says "Map-only instructions", so the
  three prose sections land with empty `refs`. Slice 2 seeds each
  section pass with "the section's own references" — it will need the
  skeleton submission to grow a per-section reference field first.
- **Corrupt vs stale `explainer.json`.** A SHA or version mismatch is
  discarded with a log line; an unparseable file raises
  `ExplainerCorrupt` (500 on `GET /explainer`). Regenerating overwrites
  it, so corruption cannot wedge the button.
- **Styling** is the minimum that makes the Map legible; the reading
  type scale and the affordance vocabulary stay in slice 4.

## Slice 2 — Code prose, lazily

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

## Slice 3 — Background, with tools and provenance

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

## Slice 4 — Intuition and figures

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

## Slice 5 — Console reach

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
