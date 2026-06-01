# Parked ideas

A running list of things discussed but deferred. Sourced from sessions on
23 Apr, 6 May, and 7 May 2026. Tree-sitter items also live in
`TREE_SITTER.md`; they are summarised here for completeness.

## Unlocked by the viewer being a live process

The shift from statically-generated HTML to a server-backed viewer opens
a whole back-channel of capabilities. None of the items below are in
tree; the 6 May discussion ranked the first three as the cheapest and
highest-value next moves, and they share infrastructure (an SSE/websocket
stream from the server to the page).

1. **Lazy fold summaries.** Today every fold gets a pre-generated summary
   at augment time, even though most folds are never closed. Defer until
   first close: a `POST /fold-summary {hunk_id, region}` runs a one-shot
   LLM call against the cached overview plus file prefix and writes the
   result back into `augmented.scr.json` so subsequent loads are free.
   Saves tokens on every review and improves time-to-first-render.

2. **Streaming annotation arrival.** The current pipeline `asyncio.gather`s
   all hunks and only renders once they all return. With a live process,
   each hunk's annotation can appear as soon as its call completes, via
   SSE or websocket. Big perceived-latency win on PRs with more than ten
   hunks; reviewers can start reading the early hunks while later ones
   are still being annotated.

3. **"Regenerate with nudge" button.** When an intent comes back wrong,
   click regenerate and type a hint ("describe the error-handling path",
   "this is a refactor not a behaviour change"). The server re-runs that
   hunk with the hint appended to the prompt. Pays down the manual
   prompt-iteration loop that currently requires editing source and
   re-running the CLI.

4. **Per-hunk action menu.** Each hunk gets a small menu: "explain this
   hunk", "where else is this called", "suggest a test". Each option
   fires a fresh agentic call with full tool access against the head
   worktree. Was impossible in the static-HTML world; high value when a
   reviewer hits a hunk they don't immediately understand.

5. **Live ref navigation.** A `refs[]` entry currently renders as a label.
   Clicking it should open a side-panel showing the referenced file
   around that line, without leaving the viewer. The server already has
   the head worktree on disk, so this is mostly viewer-side wiring.

6. **Reviewer-comment dialog with the model.** When a reviewer leaves a
   comment ("this looks racy?"), offer "ask the model to check"; server
   re-runs with the reviewer's comment in context and posts a reply.
   Turns the inline-comment surface into a conversation rather than a
   one-shot annotation.

7. **Live token / $ usage in the viewer footer.** Server streams usage
   as each hunk completes. Cheap to add on top of (2) and reframes how
   cost is perceived during a review — currently it's an opaque post-hoc
   number.

8. **Cross-session prompt-cache id / Files API.** Hold an Anthropic
   prompt-cache id across re-reviews of the same commit range so the
   prompt-engineering loop hits cache instead of re-billing the full
   context every time. Needs the relevant Anthropic beta features and a
   SHA-keyed invalidation rule.

9. **Cross-hunk in-process cache.** Even when the upstream prompt cache
   misses, a long-lived server process can avoid re-sending the per-file
   overview / summary bytes for each hunk's call within a single review.
   Plain in-memory caching, no API beta needed.

10. **Selective re-augment on reopen.** When a reviewer reopens a run
    directory, only re-augment hunks whose body hash or prompt version
    changed, instead of either a full re-run or stale cache. Makes
    "iterate on prompts overnight, review in the morning" cheap.

11. **Interactive prompt-version bump.** Edit the per-hunk prompt in the
    viewer and re-run just that hunk. Useful for prompt-engineering
    sessions, which is how a lot of the work has been getting done
    anyway — currently that loop happens outside the viewer.

12. **Lazy on-expand annotation for generated / deprioritised files.**
    Generalises the binary `--skip-context` / generated-globs filter:
    rather than skip annotation outright, materialise it only when the
    reviewer actually expands the file's block. Weaker than the others —
    annotations *are* the product, so skipping by default fights the
    UX — but worth keeping on the list.

## Tree-sitter (also enabled by a live process holding ASTs in memory)

These are recorded in detail in `TREE_SITTER.md` with a recommended
ordering. Summarised here so they appear in the same backlog.

13. **Symbol-based grouping as a second sidebar axis.** Parse post-images,
    enumerate top-level symbols, and group hunks by symbol where two or
    more hunks share a name. Renders alongside the existing theme axis.
    Smallest scope of the tree-sitter ideas, no LLM call needed,
    recommended first if the dependency is taken on.

14. **AST-driven fold regions.** Replace the indent-based
    `compute_fold_regions` with real function / class / block boundaries.
    Lazy-parse on first hunk-expand to keep cold-start cheap, with the
    indent heuristic as a fallback for languages without grammars.

15. **Semantic hunk splitting plus cross-file move / rename detection.**
    Re-segment the diff so each segment maps to one AST node's worth of
    change. Two stretch goals fall out: cross-file move detection
    (identical body deleted in A, added in B → one "moved" op) and
    sub-symbol rename detection. Biggest scope of the tree-sitter group.

16. **Symbol-table diff feeding the overview pass.** Compute
    `symbols_added/modified/removed` from the AST rather than asking the
    LLM — cheaper and no hallucination. Lower priority in
    `TREE_SITTER.md`: "rounding error" today.

17. **`refs[]` ground-truth verification.** Resolve the symbol named in
    `reason` against the parsed AST and drop or correct refs that don't
    actually live at the cited line. Explicitly tagged as more
    speculative — depends on how often refs land on the wrong line.

## Other parked ideas (not specifically tied to the live process)

18. **Per-file / change-wide comment anchors.** Reviewers can currently
    only attach comments to lines. Lack of a file-level or PR-wide
    comment surface was flagged as a real friction point — valid future
    feature, just not changing now.

19. **`scr eval` — LLM-as-judge harness.** A top-level command that takes
    a fixture diff, runs each backend, and asks a model to score
    annotation quality side-by-side. Was the original motivation for
    writing `GeminiSDKClient`; explicitly listed as standalone follow-up
    work that "can come after" the current backend refactors.

20. **Playwright layout tests for the annotations module.** Vitest
    already covers algorithmic correctness via an injected rect provider;
    Playwright would catch real-layout regressions (e.g. the per-half
    subgrid-style episodes) but costs ~100 MB of browser binary plus CI
    work. Explicitly deferred.

21. **Wheel build with a tsc-via-PEP-517 hook.** Today the bootstrap
    runs `tsc`. Once wheels are published, a `build_py` hook can bake
    `annotations.js` into `package_data` so pip-only users don't need
    Node installed.

22. **GC for run artefacts under `~/.cache/scr/runs/`.** When the runs
    root was moved out of the repo, garbage collection was noted as
    "a separate increment, skip for now." Likely needed before the
    cache becomes a noticeable disk citizen.

23. **From the 23 Apr v1 wrap-up, still unbuilt:** hierarchical
    summarization for PRs with more than ~500 files; a call-graph
    diagram view; intra-line word diff; Web Workers for heavier
    viewer-side work. (Hunk re-splitting, also on that list, is now
    superseded by item 15. Local-note capture is now done as inline
    comments.)

24. **Sync-upstream as a GitHub Actions workflow.** On 7 May the
    local-script path was chosen ("we control the upstream"), but the
    cron / manual-dispatch GHA design is fully specced and could be
    revived if the cadence picks up.

## Pointers

- `semantic_code_review/review/runner.py` — `serve_review` is the natural
  home for back-channel routes (`/fold-summary`, `/regenerate`, the SSE
  stream, etc.).
- `semantic_code_review/viewer/assets/annotations.ts` — the typed module
  that would mediate any new viewer-side interactions.
- `TREE_SITTER.md` — full design for items 13–17.
