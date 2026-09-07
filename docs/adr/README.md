# Architecture Decision Records

One file per decision, numbered `NNNN-kebab-title.md`. Each records the
context, the decision, and its consequences at the time it was made —
ADRs are append-only history, not living docs. Supersede rather than
edit: a later ADR can mark an earlier one `Superseded by NNNN`.

An ADR is merged to `main` as *proposed* before the work it decides
starts, and never lives only on a branch: 0006 did, its branch never
merged, and 0008 re-decided the same ground two weeks later without
knowing. This index points at files on `main`, not at PRs.

- [0001 — Tree-sitter structural layer](0001-tree-sitter-structural-layer.md)
- [0002 — Review console](0002-review-console.md)
- [0003 — Tool surface: shared cache, long-lived MCP host](0003-tool-surface-hosting.md)
- [0004 — Rendered markdown diff](0004-rendered-markdown-diff.md)
- [0005 — Thinking on the augment passes, and the output mode it forces](0005-thinking-on-the-augment-passes.md)
- [0006 — One visibility model](0006-one-visibility-model.md) — superseded by 0008
- [0007 — Change explainer](0007-change-explainer.md)
- [0008 — Hide by the diff, fold by the structure, label by meaning](0008-hide-by-the-diff-fold-by-the-structure-label-by-meaning.md) — accepted
- [0009 — Comments are sent, not dumped: a two-way review loop](0009-comments-are-sent-not-dumped.md) — proposed
