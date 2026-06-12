# Slices — Symbol-aware fold boundaries

Implements backlog item **(i)** of [ADR 0001](../adr/0001-tree-sitter-structural-layer.md):
fold at function/class boundaries (from the tree-sitter `Symbol` tree)
rather than by indentation, and upgrade `fold_summary` accordingly.

Vertical slices, ordered. Each ends in something that ships and is
exercisable on its own; later slices add consumers but never block
earlier ones from landing.

## Background — two fold detectors, kept in lockstep

Fold regions are computed **twice**, and the two must agree:

- **Server** — `hunk_layout.compute_fold_regions(rows)` produces the
  per-hunk `fold_regions` shipped in the viewer JSON. These carry the
  `(context, line-range)` address `/fold-summary` uses.
- **Client** — `folds.ts._computeFoldRegions(rows)` runs over the
  *unified* per-file row sequence (hunks **plus** expanded context the
  server never saw) to attach chevrons, then reconciles each detected
  region back to a wire `fold_regions` record by line range
  (`_findExistingFoldRecord`) to hang the summary on it.

Both are purely indentation-based today (`_row_indent`), and the server
algorithm exists in its current shape specifically to match the JS one.
A change to one without the other desyncs the reconciliation, so the
detector change (Slice 2) lands on both sides together.

## Shared currency

The normalized `Symbol{kind, name, qualified_name, range, children[]}`
tree (ADR 0001). Folds need only its **line spans per side**: the
flattened `(start_line, end_line, kind, qualified_name, depth)` of every
definition on head and base. The mapping from a row to a symbol is by
line number — `new_line` into the head tree, `old_line` into the base
tree.

The governing rule, applied identically on both sides:

> A fold region's body is the innermost enclosing symbol's span, clamped
> to the rows actually present. Nested definitions nest. Rows inside no
> definition — module-level statements, an unsupported language, a file
> with no parse — fall back to the existing indentation detection.

Graceful degradation is preserved: an unsupported language yields no
spans, every row falls back, and output is byte-identical to today.

---

## Slice 1 — Ship per-file symbol spans (inert) ✅ done

The data path, with no behaviour change.

- `build_json` already parses the head and base `Symbol` trees per file
  to build the changed-symbol delta. Flatten each to a per-side list of
  `{start_line, end_line, kind, qualified_name, depth}` and add it to
  `FileBlock` as `fold_symbols: {head: [...], base: [...]}`.
- Empty lists for an unsupported language / unavailable worktree (the
  delta already degrades this way — reuse the guard).
- Mirror the field in `types.d.ts`.

**Done when:** the viewer JSON carries each supported-language file's
definition spans on both sides, an unsupported-language file carries
empty lists, and nothing yet reads the field.

## Slice 2 — Symbol-aware folding, both detectors ✅ done

The behavioural change; server and client move together.

- Implement the snapping rule (see Shared currency) once and port it to
  both `compute_fold_regions` (Python) and `_computeFoldRegions` (TS),
  each taking the file's `fold_symbols` for the relevant side(s).
- A region whose rows fall inside a definition spans that definition
  (clamped to the present rows); nested defs produce nested regions;
  uncovered runs keep indentation-based folds.
- Preserve the existing `context` (right/left/both), `has_changes`, and
  line-range fields unchanged — only the boundaries move.
- Guard the lockstep with a **shared fixture**: the same `(rows, spans)`
  input drives a pytest case and a vitest case that must produce
  identical regions.

**Done when:** on a Python/TS/JS diff a changed method folds as one
region under its (possibly unchanged) class, the collapsed body is the
whole definition rather than an indentation guess, and an all-unsupported
diff folds byte-identically to pre-slice output.

## Slice 3 — `fold_summary` + chevron carry the symbol identity

The "upgrades `fold_summary`" payoff.

- When a fold region aligns to a symbol, thread its `qualified_name` and
  `kind` through to `fold_summary` (seed the prompt with "this is
  `function foo`" rather than leaving the model to infer it from the
  body text) and onto the collapsed placeholder, e.g. `function foo —
  <summary>`.
- Indentation-fallback regions keep today's unlabelled placeholder.

**Done when:** collapsing a symbol-aligned region shows the symbol's name
beside its summary, the summary prompt is seeded with the symbol, and
fallback regions are unchanged.

---

## Not in these slices

- A dedicated **"Symbols" fold level** on the slider (collapse every file
  to its top-level definitions), alongside Files/Hunks/Segments/Off.
- Aligning the existing **segment fold** to symbol spans.

Each is weighed once Slices 1–3 are felt in use.
