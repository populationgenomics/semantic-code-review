# ADR 0007 — Change explainer

- Status: Accepted
- Date: 2026-08-27

> ADR 0006 (one visibility model), cited throughout, is in flight on
> PR #11 and not yet on `main`. This ADR is written against it.

## Context

scr's annotations are bottom-up. The per-hunk pass answers "what does
this hunk do" forty times independently; the overview pass adds a
summary, themes and hunk groups. Nothing answers "what is this change,
what did the system look like before it, and in what order should I read
it."

The gap shows on a change like `themis-internal#365` — 17 files, +2722
/ −1161. The viewer's first screen is `apps/web/src/gen/themis/rpc/
literature_pb.ts` because file order is diff order is git's path sort,
and that file is regenerated exhaust. The file a reviewer must read
first, `schema/proto/themis/rpc/literature.proto`, is fourth of the five
non-generated paths. Every semantic decision in the change is stated in
that proto and restated nowhere.

The theme groups do not close this. `OverviewGroup` is a *filter*:
members may overlap, groups need not cover every hunk, and the target is
2–6 per change. Filtering answers "show me the hunks about X" for a
reviewer who already knows to ask about X. It does not answer "what
should I look at, and why."

The prior art is a hand-run explainer prompt (Geoffrey Litt's
`explain-diff`, in use at CPG against real PRs) that produces a
standalone HTML page: Background, Intuition, a code walkthrough, and a
reading map. The outputs are good. Two things prevent adopting it as-is.
It is a one-shot artifact that *duplicates* the diff rather than
indexing it, so the prose is not clickable and drifts from the code it
describes. And it references another skill for its drawing mechanics,
which scr cannot rely on: skills are deliberately disabled on the
`claude-cli` path (`--disable-slash-commands`, because an
advertised-but-unloadable skill makes the model hedge), and three of the
five supported backends have no skill mechanism at all.

### The bar

This ADR assumes the counterfactual is **a human skimming a large diff
and forming a confident wrong model of it**, not an oracle. That
happens routinely and leaves no artifact anyone can interrogate. A
generated explanation that is wrong is wrong in writing, anchored to
the hunks it claims to describe, and adjacent to a console that can be
asked to defend it.

Every decision below — no quality gate, no LLM-as-judge, opt-out rather
than opt-in — rests on that assumption. A future reader who disagrees
with the bar should expect to revisit all three.

## Decision

### Shape — a document, not a partition

The explainer is a **document about the change**, an extension of the
change summary, carrying typed references into the diff. It is not a
second grouping of hunks. The themes axis remains the only partition, so
there is never a question of why two sidebar selections light up
different hunk sets.

### Surface — a mode, orthogonal to the collapse level

The viewer gains an **overview mode**, reached from the same control
strip as the collapse-level buttons (`files` / `hunks` / `segments` /
`off`) but *not* a member of `CollapseLevel`.

Levels are span sets: ADR 0006's model is that a line is visible iff no
`HiddenSpan` covers it, and each level seeds `level`-owned spans. The
overview hides nothing — it replaces the main pane. Encoding it as a
span covering every line would make the span store assert a reason for
hiding that is not the real one, and every consumer of the store would
need a case for it.

Keeping it orthogonal also makes the return trip free: `collapseLevel`
is untouched while in overview mode, so leaving it restores the
reviewer's zoom and their hand-set `user`-owned folds without a latch.
The intended loop is: read the document, drop into the ladder to check
or comment, return and keep reading. Read position is restored
**section-granularly** (active section, scrolled to its heading) rather
than by scroll offset, because offsets go stale when prose streams in
above them.

In overview mode the sidebar shows the document's section tree in place
of the themes / files / symbols axes. It takes its own axis id in the
`<axis>:<id>` localStorage key; switching modes must not clobber the
diff-mode pill.

### Placement — a route on the live server, not a pipeline pass

Generation is **not** part of `augment_run_dir`. It is a route on
`serve_review`, following the shipped `/fold-summary` pattern: an async
LLM closure wired in after augment completes, results fanned out over
the existing SSE bus, `RepoTools` bound per request. `console.py`
already demonstrates both halves of this — `RepoTools` constructed per
turn, `McpHttpHost` opened per turn as a context manager.

Nothing runs until the reviewer asks. The button is **disabled until the
`overview` SSE event lands** (the skeleton's inputs are the overview and
the symbol delta; both complete before the per-hunk pass starts), then
enabled. Pressing it generates the skeleton. Prose is generated per
section on first open.

This makes spend user-initiated, which matters given the review cost
curve (~18,994·hunks^1.16 tokens, ~98% of it context re-transmission). A
document nobody opens costs nothing.

The two halves have different readiness preconditions, and the
difference is not cosmetic: prose built on half the hunk intents reads
exactly as fluently as prose built on all of them. A section whose
anchored hunks are not yet annotated says so and offers to wait.

### Gating — opt-out

A config flag disables the feature; there is no flag to enable it. An
opt-in gate would mean the reviewers who benefit most — someone opening
a large change they did not write — are the ones who have not configured
it. Since generation is press-triggered, default-on costs nothing.

The skeleton may return **"no narrative warranted"** as a first-class
value, rendered as the document. A three-hunk rename gets "read them
directly" rather than a padded Background section. This is the same
distinction the skipped-file placeholder needs between "we chose not to"
and "the pass failed".

### Sections — four fixed, model-chosen subsections under Code

Top level is fixed: **Map**, **Background**, **Intuition**, **Code**.

Map leads because it is the only section the skeleton can fill, so it
is what renders the moment the button is pressed. Behind the three
prose sections the first screen would be entirely things that are not
ready yet.

The top level is fixed for a structural reason, not an aesthetic one —
the four behave differently:

| Section | Cache key | Tools | Cost |
|---|---|---|---|
| Map | `(base_sha, head_sha)` | no | cheap; part of the skeleton |
| Background | `base_sha` | yes | expensive |
| Intuition | `(base_sha, head_sha)` | no | moderate |
| Code | `(base_sha, head_sha)` | no | moderate |

Background describes the system *before* the change, so it is invariant
across head movement and across every prompt-iteration re-run on the
same branch. It is the only section asserting facts about code outside
the diff, so it is the only one granted tools. A model-chosen section
list has nowhere to hang any of that: you cannot key a cache on a
heading the model invented, nor grant tools to "whichever section turns
out to need the worktree."

**Map** is the reading order — an ordered list of files with one
sentence of *why* each. It is the direct answer to the case in Context,
it is the cheapest section, and it is worth the most at t=0. It is
produced by the skeleton, so it renders the moment the button is
pressed, with prose filling in behind it.

Subsections under **Code** are model-chosen, because that is where the
change-specific grouping lives. For `#365` the natural order is proto
contract → hand-written consumers → design docs → generated exhaust, and
no fixed taxonomy produces that.

Background is a separate lazy unit so a reader who knows the codebase
never pays for it.

### Anchors — file or hunk, validated by membership

A reference addresses a **file** (`F<i>`) or a **hunk**
(`H<fi>_<hi>`) — the ids the viewer already uses. No line ranges: a
hunk reference reveals the hunk and the reviewer reads it, and file
references are what a claim like "these five files are regenerated from
the proto — skim them" actually needs.

Validation is membership in the two id maps built at render time. An
invented reference is caught without AST work, which is the failure
mode `PARKED_IDEAS.md` #17 files as speculative for `refs[]` — avoided
rather than solved.

Index-based ids appear to contradict CONTEXT.md's rule that durable
addresses use absolute file lines. They do not, because the document is
not durable across a moving diff. That rule earns its keep for
`comments.json`, which must survive a rebase (hence `anchor_status`
with `file_gone` / `commit_unavailable`). If the head SHA moves, the
narrative is stale as *prose*: re-anchoring a paragraph describing code
that no longer exists yields a correct pointer to a wrong sentence. So
the document is **invalidated wholesale on any change to
`(base_sha, head_sha)`**, and within one run indices are stable by
construction.

An invalid reference is **dropped, counted, and surfaced** — not raised,
not silenced. Aborting the document because one reference of forty is
bad discards thirty-nine good ones plus the prose; dropping silently is
how references thin out unnoticed. Coverage ("covers 31 of 47 hunks, 2
references dropped") is rendered, and doubles as navigation.

### Persistence — its own file

The document is `explainer.json` in the run directory, referencing hunk
and file ids. It is not a field on `AnnotatedDiff`.

Lazy per-section generation means repeated partial writes. The shipped
lazy-fold-summary path rewrites `augmented.scr.json` wholesale for each
summary; adding per-section explainer writes multiplies that churn on a
file that is already large. A separate file keeps each write small and
makes a torn write lose the document rather than the annotations.

The lifetimes also differ. `AnnotatedDiff` is the annotation tree for
the diff; the explainer is a document *about* the change that references
that tree, regenerable section by section without touching annotations.

It also leaves the emit/parse equivalence contract alone. That contract
is currently broken in a way this decision must not widen:
`Overview.groups` is written to the sidecar but dropped by
`_overview_to_jsonable`, while `lint_text` compares `model_dump()`
wholesale — so `scr lint --sidecar` reports a mismatch on any run whose
overview produced groups. Fixing that is a separate change; adding
another sidecar-only field would have added a second instance of it.

### Prompt carrier — content is backend-agnostic, transport is not

The guidance block is inline in `augment/prompts.py`. No skill
references: they are unavailable or actively harmful on the backends
scr supports.

`claude_cli.py` splits its payload — `user_text` goes to **stdin**,
while `system_text` and `schema_json` go on **argv** (`--system-prompt`,
`--json-schema`). Long command lines are not free: Cortex XDR, deployed
on CPG laptops, parses them quadratically.

So the rule is **argv carries only bounded, fixed strings; anything
that scales with the change, or is bulk guidance, rides stdin**, and the
guidance block's carrier is chosen per backend on the existing
`Client.is_subprocess_backend` discriminator — system prompt on SDK
backends (where it is the cacheable prefix), prepended to stdin on
subprocess backends (where nothing in the user prompt is cached anyway).
The model sees the same words either way.

### Presentation — slots, not markup

Prose is **markdown**, rendered by the existing `markdown-it`
(`html: false`) + DOMPurify path. Everything above plain markdown —
callouts, skip boxes, figures, the Map table — is a **structured field
with a closed kind enum**, styled by scr. The model never chooses
presentation.

Figures are inline SVG in a structured slot, sanitised through
DOMPurify's SVG profile with presentation attributes stripped and
`class` filtered to a fixed vocabulary in which every fill and stroke
resolves to a theme custom property. Consistency comes from constraining
the *paint*, not the diagram grammar — which leaves layout free, so a
figure can place two framed regions side by side over a third. Mermaid
was considered and rejected for this reason; it remains available for
console answers, which have no such requirement.

The contract both the prompt and the stylesheet must agree on lives in
[`../explainer-presentation.md`](../explainer-presentation.md). Drift
between them is how the look degrades.

Overview mode gets **reading typography** — serif, ~18px, ~1.6 line
height, ~72ch measure — rather than inheriting the diff's dense
monospace chrome. It is a reading surface and should look like one.

### Console integration

The document is available to the console agent, but not by seeding it
wholesale: ADR 0002's discipline is "seed compact, pull on demand" and
the document is unbounded prose. The console's seed grows the **section
list** (titles and references — bounded); bodies come through a
`section(id)` accessor beside the existing `hunk(id)`.

`ConsoleSelection` grows a fourth kind, `"explainer"`, carrying
`section_id`. It is the richest of the four: `_format_selection` can
inline both the section body and its anchored hunks, so "why does it
claim this?" arrives with the claim *and* the code the claim is about. A
code selection can only ever supply the latter. The explainer pane must
be matched before the `"plain"` fallthrough and must not be swept up by
the `.console-drawer, .console-input, #status-bar` ignore.

This is the "are you sure?" surface, and it is why no quality gate is
needed in v1.

### Quality — no gate

Two mechanical affordances, neither a gate:

- **Reference coverage and drop count**, rendered.
- **Background's read list** as a visible citation. A Background section
  citing no reads is one that made it up, and that is legible without
  judging the prose.

No score, no suppression. There is no calibrated score to threshold on,
and a silently withheld document is worse than a visibly thin one.
`PARKED_IDEAS.md` #19 (LLM-as-judge harness) stays parked; a judge
written before fifty real outputs have been read scores the wrong
things.

## Consequences

- `serve_review` gains explainer routes and `ServerContext` gains the
  document. `augment_run_dir` is untouched.
- The run directory gains `explainer.json`; CONTEXT.md's run-directory
  entry grows accordingly.
- `StoredViewState` grows a `mode` field and `_VERSION` goes to 2. Older
  records are discarded with a warning — distinct from the loud
  `ViewStateError` raised for corruption.
- Entering overview mode clears `focusReveal`, as touching the collapse
  slider already does.
- viewer.css gains reading typography, the affordance styles, and the
  diagram class vocabulary. It already has the token structure (dark
  default, `prefers-color-scheme: light` flip); it needs `--accent2`,
  `--box`, `--box-alt` and the soft variants.
- A document built from slots is less inventive than one where the model
  draws the page. Bespoke one-off layout is lost. This is a real trade,
  taken because model-authored markup is both a styling conflict and an
  injection surface — prose here derives from a diff that may be
  hostile, which `console_render.ts` already names as its threat model.
- Reference resolution and mode switching are new interaction paths in
  `render.ts`; the diff renderer itself is unchanged.

## Alternatives considered

**Extend the overview pass in place.** Add fields to `Overview` and let
the existing pass fill them. Rejected: the overview pass has no tools
(`run_overview_pass` takes no `RepoTools`; the instance is constructed
after it), so Background would be inferred from diff text alone. It is
also on the critical path for first render.

**A fourth pipeline pass at position 4.** Runs after the per-hunk pass,
inside the `AsyncExitStack` where `repo_tools` and a warm `mcp_host` are
live, modelled on `run_pr_level_extra_review`. Rejected in favour of a
route: press-triggered generation makes the pipeline stage unnecessary,
and a route needs no new SSE skeleton event or partially-populated
pipeline state.

**Hang narration on `OverviewGroup`.** Rejected: groups may overlap and
may omit hunks, so a narrative built on them silently omits code with no
way for the reviewer to distinguish omission from "nothing to say".

**The explainer supersedes the themes axis.** Coherent — one semantic
grouping, better informed. Rejected because the axis would then mean
different things depending on a config flag.

**A fifth `CollapseLevel`.** Rejected under ADR 0006; see Surface.

**A console conversation with a fixed opening move.** Much less new
code, and "regenerate with a nudge" falls out of `message_history`.
Rejected: the document needs a schema, and the console is deliberately
the one agent without a `ToolOutput` submit tool. Console turns are also
transient, and this document is persisted.

**Model-authored HTML**, as the prior-art artifact produces. Rejected;
see Presentation.

**Mermaid for figures.** Rejected; see Presentation.

## Backlog (deliberately not v1)

- Regenerate-a-section-with-a-nudge (`PARKED_IDEAS.md` #3), which this
  makes natural.
- Per-section follow-up conversation pinned to the section's references
  (#4, #6).
- `scr eval` as the judged-quality instrument (#19).
- Cross-run reuse of Background beyond the `base_sha` cache key.
- Fixing the `Overview.groups` emit/parse loss — separate change,
  required before `scr lint --sidecar` is trustworthy.
- Whether the **Code** section earns its prose, or collapses to the
  subsection list that drives navigation. Connective tissue between
  hunks is what forty independent per-hunk calls structurally cannot
  produce, so it is kept for v1 — but it is the first section to cut if
  the document reads bloated.
