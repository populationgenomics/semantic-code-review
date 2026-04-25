# Tree-sitter: design notes

> **Status:** speculative — captured for future reference, not committed work.

We're not using tree-sitter today. The viewer's structural reasoning is
delegated to the LLM via tool use (`read_file`, `grep`, `read_file_at`,
`git_log`); fold regions inside hunks come from a simple indent-based
heuristic in `compute_fold_regions`. The decision to skip tree-sitter
was reasonable when the viewer was a static, self-contained HTML
artifact — every structural pre-computation had to be paid up front
for files the reviewer might never look at, and added a Python dep
with native bindings against the supply-chain pinning regime we'd
just tightened.

This document records why that calculus changed once `scr review`
became a long-running localhost HTTP server with a back-channel, and
the three concrete opportunities we identified that would make the
tree-sitter dependency worth carrying.

## What changed

The shift from *static HTML* to *live local process* opens three new
levers:

- **Lazy parsing.** A file's AST only needs to exist when the reviewer
  actually expands that file or interacts with one of its hunks.
  Up-front parsing of every touched file is no longer the only option.
- **Cached state across calls.** The server holds parsed ASTs in
  memory across every per-hunk call, every fold-toggle, every
  ref-click. We pay parsing once, query many times.
- **Interactive features that need ground truth.** Click-to-jump,
  symbol cross-referencing, and on-demand "where else is this used"
  queries become affordable when the parse is already sitting there.

## Three opportunities, ordered by leverage

### 1. Symbol-based grouping (recommended first)

The semantic-groups sidebar today carries one axis: LLM-curated
**themes** ("node toolchain setup", "annotation arrow geometry").
Tree-sitter would let us add a second, **structural** axis with no
LLM call needed:

- Parse each touched file's post-image. Enumerate top-level symbols
  (functions, methods, classes, constants).
- For each changed symbol, find every hunk in the diff that touches
  that symbol's definition or calls it.
- Filter to symbols where ≥ 2 hunks touch the same name (single-hunk
  symbols don't deserve a sidebar entry).
- Render as a separate sidebar section — "By symbol" alongside the
  existing "By theme".

The two axes solve complementary navigation problems. Themes answer
"what is this PR for?"; symbols answer "show me everything about X".
Reviewers reach for both depending on the question.

Hunks with no symbol identity (config files, JSON, plain text) don't
appear in the symbol axis — they fall through to the theme axis or
the existing "ungrouped" visual tell.

**Why this first:** smallest scope of the three, no schema change worth
mentioning, plugs into the sidebar surface we just shipped, directly
addresses Leo's "build the map" complaint.

### 2. Better fold regions

Today's `compute_fold_regions` is indent-based. That's adequate for
Python and broken-or-mediocre for everything else (JS/TS, C, Go,
anything without significant whitespace). Tree-sitter would give us
*real* function / class / block boundaries.

- Compute fold ranges from the AST, not from indentation.
- Lazily — only when a hunk is first expanded, since most hunks in a
  big PR are never expanded.
- Cache the parsed ranges per file for the rest of the review session.
- Indent-based fallback stays in place for languages we don't ship
  grammars for, so the floor doesn't get worse.

This pairs naturally with the lazy-fold-summary deferral we already
intend to do: the summary call only runs when the reviewer closes a
fold, and now the fold regions themselves are also computed on first
expand. Both move work off the eager-augment hot path.

**Sub-line folding is not on the table.** Per-line is the floor for
diff review (cursor / selection / search / git diff all expect the
line as the unit). The lever to pull is smarter line-level fold
boundaries (function bodies instead of indent blocks), which
tree-sitter delivers. The few sub-line patterns that exist in IDEs
(collapse-to-glyph for long string literals, parameter-list elision)
don't generalise and break too many tooling expectations.

### 3. Semantic hunk splitting

Today's hunks are whatever `git diff` happened to coalesce. The
per-hunk LLM pass already does *some* semantic splitting after the
fact, via the optional `segments[]` field — when a hunk fuses
unrelated edits, the model is asked to split them. Tree-sitter could
move that boundary detection upstream and make it deterministic:

- Parse pre and post of every touched file.
- Identify which AST nodes changed: function definitions, class
  definitions, top-level statements.
- Re-segment the diff so each segment maps to one AST node's worth
  of change.
- The LLM annotates per-AST-node rather than per-line-range-the-diff-
  algorithm-happened-to-emit, and the prompt's "split into segments"
  guidance becomes redundant (handled offline).

Two stretch goals that fall out once AST diffs exist:

- **Cross-file move detection.** Same body-hash AST node deleted
  from file A, added to file B → present as one "moved" operation
  rather than as paired delete+add. This is the single biggest
  mental-model improvement for refactor-heavy PRs and is impossible
  without structural parsing.
- **Sub-symbol rename detection.** Distinguish "function `foo`
  renamed to `bar`, body unchanged" from "function `foo` had one
  line changed". We currently hand the LLM a confusing diff for both.

**Why this third:** most ambitious of the three. Bigger pipeline
change (re-segments the per-hunk pass). Worth doing only after we've
exercised tree-sitter on (1) and (2) and confirmed the dep earns its
keep.

## Arguments against

Real costs to weigh, even if we proceed:

- **Per-language grammar maintenance.** `tree-sitter-languages` bundles
  ~25 grammars. Each new language we want to first-class needs a
  grammar package. Languages without grammars degrade to the indent
  fallback — same floor we have today, but the ceiling rises only for
  what we ship.
- **The LLM is already doing most of this** via tool use. Tree-sitter
  optimises a path that works, just slower and less reliably. The
  win is correctness/latency on hot-path features (fold regions,
  symbol grouping), not raw token savings.
- **Native bindings.** Adds a Python dep with C extensions. Has to
  fit the supply-chain regime (`requirements.lock`,
  `--require-hashes`). Not blocking — just one more thing to hash and
  pin.
- **The free-symbol-enumeration idea** (replace LLM `symbols_added /
  modified / removed` in the overview pass) is appealing but lower
  priority. The overview is small enough today that the token saving
  is a rounding error; we'd do it for reliability, not cost.

## Recommended sequence if we adopt

1. **Symbol grouping.** Adds a structural sidebar axis. No schema
   change, no pipeline change, easy to back out if the dep proves
   painful.
2. **Better fold regions.** Replaces the indent heuristic for
   languages with grammars; indent fallback for everything else.
   Pairs with lazy-fold-summary deferral.
3. **Semantic hunk splitting.** Reshapes the per-hunk pipeline.
   Worth doing only after (1) and (2) prove the dep carries its
   weight; the cross-file move-detection payoff is the most
   compelling argument for going this far.

## Out of scope

- Replacing the LLM as the source of `intent` / `context` / `smells`.
  The structural parse augments the LLM; it doesn't supplant it.
- Sub-line folding (see fold-regions section).
- Live ref-link verification ("does the symbol named in `reason`
  actually live at the cited line?"). Plausible follow-on once
  symbol indexing exists, but speculative.
- IDE-style real-time editing in the viewer. The viewer is a review
  surface, not an editor.
