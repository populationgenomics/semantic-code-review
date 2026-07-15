# Slices

Vertical, tracer-bullet implementation plans — each slice an
independently-grabbable, end-to-end-shippable unit of work, ordered so
earlier slices never depend on later ones. Slice plans pair with the
[ADR](../adr/) that decides the design; the ADR holds the *why*, the
slice plan holds the *how, in order*.

- [Tree-sitter structural layer](tree-sitter-structural-layer.md) — ADR 0001
- [Review console](review-console.md) — ADR 0002
- [Tool surface & MCP hosting](tool-surface-hosting.md) — ADR 0003
- [Rendered markdown diff](rendered-markdown-diff.md) — ADR 0004
