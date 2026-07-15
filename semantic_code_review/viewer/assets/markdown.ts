// Markdown → sanitized HTML for rendered-mode diff (ADR 0004).
//
// Wraps markdown-it (GFM core: tables, strikethrough, autolinks, task
// lists) and DOMPurify. `render()` returns sanitized HTML ready to
// assign to innerHTML; callers never hand raw markdown-it output to the
// DOM. Untrusted `.md` under review can carry <script>, javascript:
// URLs, or tracking-pixel <img>, so every string crosses DOMPurify.
//
// Math and mermaid (ADR 0004 slice 4) render from their source
// delimiters via their own renderers and never traverse this HTML path:
// `renderBlocks` leaves a mermaid fence as its `<pre><code>` source and
// each `$…$` / `$$…$$` span as a `.rmd-math` placeholder holding the raw
// TeX; `hydrate()` — called by rendered.ts once a block is in the DOM —
// swaps each for its rendered form (via the shared mermaid / katex
// modules). katex/mermaid output is generated from the source with
// no author HTML, so it is injected directly, off the DOMPurify path.

import MarkdownIt from "markdown-it";
import DOMPurify from "dompurify";
import { Mermaid } from "./mermaid";
import { Katex } from "./katex";

// markdown-it's Token / State types aren't reachable off the default
// import; pull them from their submodules (type-only, erased at build).
import type MdToken from "markdown-it/lib/token.mjs";
import type MdStateCore from "markdown-it/lib/rules_core/state_core.mjs";
import type MdStateInline from "markdown-it/lib/rules_inline/state_inline.mjs";
import type MdStateBlock from "markdown-it/lib/rules_block/state_block.mjs";

type MdTokenCtor = MdStateCore["Token"];

// html: false — raw HTML blocks in the source are escaped, not passed
// through; DOMPurify is the backstop, not the front door. linkify:
// autolink bare URLs (GFM autolinks). typographer stays off: rewriting
// quotes/dashes misrepresents the prose under review.
const _md = new MarkdownIt({
  html: false,
  linkify: true,
  typographer: false,
  breaks: false,
});

_installTaskLists(_md);
_installExternalLinks(_md);
_installMath(_md);

/** Render markdown source to sanitized HTML. */
function render(src: string): string {
  return _sanitize(_md.render(src));
}

/** A heading block's level (1–6) and its plain-text content — the run
 *  fold's landmark signal and the outline sidebar's label (ADR 0004
 *  slice 3). Present only on heading blocks; null otherwise. */
interface HeadingInfo {
  level: number;
  text: string;
}

/** One top-level block of a rendered document: its sanitized HTML plus
 *  the 1-indexed, inclusive source line span it came from. The span is
 *  the classification/alignment key for rendered-mode diff (ADR 0004
 *  slice 2) — markdown blocks break on line boundaries, so projecting
 *  the line diff onto these spans is exact. `heading` marks the block as
 *  a section landmark (slice 3): fold runs break at headings and the
 *  sidebar outline is built from them. `listItem` marks a block that is
 *  one item of a split list ("last" = the final item): a list breaks
 *  into one block per top-level item so a single changed item highlights
 *  and aligns alone rather than reddening the whole list; the marker only
 *  drives the stacked-item spacing (rendered.ts / CSS). */
interface RenderedBlock {
  html: string;
  startLine: number;
  endLine: number;
  heading: HeadingInfo | null;
  listItem: "item" | "last" | null;
}

/** Render markdown source to a per-top-level-block list, each block
 *  sanitized independently and tagged with its source line span. Two
 *  independently-parsed sides (base, head) are aligned by these spans;
 *  see rendered.ts. */
function renderBlocks(src: string): RenderedBlock[] {
  const env: Record<string, unknown> = {};
  // parse() runs the core ruler (including the task-list rule), so the
  // token stream is already GFM-transformed; renderer.render() then
  // applies the external-links rule per block.
  const tokens = _md.parse(src, env);
  return _splitTopLevel(tokens).map((g) => ({
    html: _sanitize(_md.renderer.render(g.tokens, _md.options, env)),
    startLine: g.startLine,
    endLine: g.endLine,
    heading: _headingInfo(g.tokens),
    listItem: g.listItem ?? null,
  }));
}

/** Render the controlled-renderer content (mermaid diagrams) inside a
 *  block already placed in the DOM. Called by rendered.ts after a
 *  block's sanitized HTML is assigned; safe to call again on a repaint —
 *  a cached diagram re-injects synchronously (no flicker), an uncached
 *  one renders async and swaps in when ready. Mermaid source that fails
 *  to render (or fails to load) stays as its raw `<pre>` fence. */
function hydrate(el: HTMLElement): void {
  _hydrateMermaid(el);
  _hydrateMath(el);
}

/** Swap each `mermaid` fence in `el` for its rendered SVG. markdown-it
 *  renders the fence as `<pre><code class="language-mermaid">source</code>`
 *  (html:false, so the source is escaped); `code.textContent` recovers
 *  the raw source the shared renderer needs. */
function _hydrateMermaid(el: HTMLElement): void {
  const codes = el.querySelectorAll<HTMLElement>("pre > code.language-mermaid");
  codes.forEach((code) => {
    const pre = code.parentElement;
    if (!pre) return;
    const src = (code.textContent || "").replace(/\n$/, "");
    const cached = Mermaid.cachedSvg(src);
    if (cached) {
      _swapMermaid(pre, cached);
      return;
    }
    if (Mermaid.hasFailed(src)) return; // invalid → leave the source fence
    void Mermaid.renderToSvg(src).then((svg) => {
      // The block may have been repainted (fold change) while we awaited;
      // only swap if this `pre` is still in the live DOM.
      if (svg && pre.isConnected) _swapMermaid(pre, svg);
    });
  });
}

function _swapMermaid(pre: HTMLElement, svg: string): void {
  const fig = document.createElement("div");
  fig.className = "rmd-mermaid";
  // svg is already sanitised by the shared mermaid module.
  fig.innerHTML = svg;
  pre.replaceWith(fig);
}

/** Render each `.rmd-math` placeholder in `el` via katex. The placeholder
 *  holds the raw TeX as text (see `_installMath`); `textContent` recovers
 *  it. A cached render fills synchronously (no flicker on repaint); an
 *  uncached one fills when katex loads. Invalid TeX (or a katex that
 *  fails to load) leaves the placeholder showing the raw source. */
function _hydrateMath(el: HTMLElement): void {
  const nodes = el.querySelectorAll<HTMLElement>(".rmd-math");
  nodes.forEach((node) => {
    const tex = node.textContent || "";
    const display = node.classList.contains("rmd-math-display");
    const cached = Katex.cached(tex, display);
    if (cached != null) {
      _fillMath(node, cached);
      return;
    }
    if (Katex.hasFailed(tex, display)) return; // invalid → leave raw TeX
    void Katex.render(tex, display).then((html) => {
      // A repaint may have replaced this node while katex loaded; only
      // fill if it is still in the live DOM.
      if (html != null && node.isConnected) _fillMath(node, html);
    });
  });
}

function _fillMath(node: HTMLElement, html: string): void {
  // html is katex output (trust:false, generated from the TeX source) —
  // inject directly, off the sanitized-HTML path.
  node.innerHTML = html;
}

/** A block is a heading iff its opening token is `heading_open`; its
 *  level comes from the tag (`h2` → 2) and its label from the following
 *  inline token's raw content (markdown syntax intact — good enough for
 *  an outline entry). Returns null for any non-heading block. */
function _headingInfo(tokens: MdToken[]): HeadingInfo | null {
  const open = tokens[0];
  if (!open || open.type !== "heading_open") return null;
  const level = Number.parseInt(open.tag.slice(1), 10);
  if (Number.isNaN(level)) return null;
  const inline = tokens[1];
  const text = inline && inline.type === "inline" ? inline.content : "";
  return { level, text };
}

// ADD_ATTR target: DOMPurify drops `target` by default, but the
// external-links rule sets it (with rel=noopener) so links open in a
// new tab rather than navigating away from the viewer.
function _sanitize(html: string): string {
  return DOMPurify.sanitize(html, { ADD_ATTR: ["target"] });
}

interface _TokenGroup {
  tokens: MdToken[];
  /** 1-indexed inclusive source line span (from token.map). */
  startLine: number;
  endLine: number;
  /** Set when the group is one item of a split list; drives item spacing. */
  listItem?: "item" | "last";
}

/** Split a flat markdown-it token stream into top-level blocks. Nesting
 *  is tracked via `token.nesting` (+1 open, -1 close, 0 self-contained);
 *  a block is the run of tokens from a depth-0 token back to depth 0.
 *  The span comes from the opening token's `map` ([start, end) 0-indexed,
 *  end exclusive → start+1 .. end inclusive 1-indexed). A top-level list
 *  is the exception: it splits into one group per item (`_splitList`) so
 *  each item classifies and aligns on its own line span. */
function _splitTopLevel(tokens: MdToken[]): _TokenGroup[] {
  const groups: _TokenGroup[] = [];
  let i = 0;
  while (i < tokens.length) {
    if (tokens[i].type === "bullet_list_open" || tokens[i].type === "ordered_list_open") {
      const { items, next } = _splitList(tokens, i);
      groups.push(...items);
      i = next;
      continue;
    }
    // Consume one top-level block: from this depth-0 token back to depth 0.
    const start = i;
    let depth = 0;
    do {
      depth += tokens[i].nesting;
      i += 1;
    } while (i < tokens.length && depth !== 0);
    const map = tokens[start].map;
    groups.push({
      tokens: tokens.slice(start, i),
      startLine: map ? map[0] + 1 : 1,
      endLine: map ? map[1] : 1,
    });
  }
  return groups;
}

/** Split a top-level list into one group per item, each re-wrapped in its
 *  own single-item list so it renders as a proper bullet/number. The
 *  list-open token is cloned around each item (with `start` set per item
 *  for ordered lists) so markers/numbering stay correct; the list-close
 *  is reused (read-only at render). Nested lists ride whole inside their
 *  parent item — only the outermost list splits. Returns the item groups
 *  and the index just past the list-close. */
function _splitList(tokens: MdToken[], openIdx: number): { items: _TokenGroup[]; next: number } {
  const open = tokens[openIdx];
  const ordered = open.type === "ordered_list_open";
  // Matching list-close (depth back to 0 across the whole list).
  let depth = 0;
  let closeIdx = openIdx;
  for (; closeIdx < tokens.length; closeIdx++) {
    depth += tokens[closeIdx].nesting;
    if (depth === 0) break;
  }
  const close = tokens[closeIdx];
  const items: _TokenGroup[] = [];
  let ordinal = ordered ? _listStart(open) : 0;
  let k = openIdx + 1;
  while (k < closeIdx) {
    if (tokens[k].type !== "list_item_open") {
      k += 1;
      continue;
    }
    // Span this item: list_item_open … its matching list_item_close.
    let d = 0;
    let m = k;
    for (; m < closeIdx; m++) {
      d += tokens[m].nesting;
      if (d === 0) break;
    }
    const map = tokens[k].map;
    items.push({
      tokens: [_cloneListOpen(open, ordered ? ordinal : null), ...tokens.slice(k, m + 1), close],
      startLine: map ? map[0] + 1 : 1,
      endLine: map ? map[1] : 1,
      listItem: "item",
    });
    ordinal += 1;
    k = m + 1;
  }
  if (items.length) items[items.length - 1].listItem = "last";
  return { items, next: closeIdx + 1 };
}

/** The starting number of an ordered list (the `start` attr markdown-it
 *  sets when the list doesn't begin at 1), or 1. */
function _listStart(open: MdToken): number {
  const attr = (open.attrs || []).find((a) => a[0] === "start");
  const n = attr ? Number.parseInt(attr[1], 10) : 1;
  return Number.isNaN(n) ? 1 : n;
}

/** Clone a list-open token so a single item can be re-wrapped without
 *  mutating (or sharing mutable attrs with) the original. For an ordered
 *  list, `ordinal` sets/overrides the `start` attr so the item renders
 *  its own number; other attrs (e.g. the task-list class) are preserved. */
function _cloneListOpen(open: MdToken, ordinal: number | null): MdToken {
  const clone = Object.assign(Object.create(Object.getPrototypeOf(open)), open) as MdToken;
  const attrs = (open.attrs || []).map((a) => [a[0], a[1]] as [string, string]);
  if (ordinal != null) {
    const existing = attrs.find((a) => a[0] === "start");
    if (existing) existing[1] = String(ordinal);
    else attrs.push(["start", String(ordinal)]);
  }
  clone.attrs = attrs.length ? attrs : null;
  return clone;
}

// --- GFM task lists ------------------------------------------------------
// markdown-it core has tables, strikethrough, and (via linkify)
// autolinks, but not GFM task-list checkboxes. This core rule rewrites
// list items whose text starts with `[ ]` / `[x]` into a disabled
// checkbox, matching GitHub's rendered output. Condensed from the
// markdown-it-task-lists plugin (MIT) to avoid a stale dependency.

function _installTaskLists(md: MarkdownIt): void {
  md.core.ruler.after("inline", "gfm-task-lists", (state: MdStateCore) => {
    const tokens = state.tokens;
    for (let i = 2; i < tokens.length; i++) {
      if (!_isTaskItem(tokens, i)) continue;
      _todoify(tokens[i], state.Token);
      tokens[i - 2].attrJoin("class", "task-list-item");
      // attrSet (not attrJoin): the list is hit once per task item, so
      // joining would repeat the class for every checkbox in the list.
      const list = tokens[_parentList(tokens, i - 2)];
      if (list) list.attrSet("class", "contains-task-list");
    }
    return true;
  });
}

/** An inline token at `index` is a task item iff it follows a
 *  `paragraph_open` inside a `list_item_open` and its text opens with a
 *  `[ ]` / `[x]` marker. */
function _isTaskItem(tokens: MdToken[], index: number): boolean {
  return (
    tokens[index].type === "inline" &&
    tokens[index - 1].type === "paragraph_open" &&
    tokens[index - 2].type === "list_item_open" &&
    _startsWithMarker(tokens[index].content)
  );
}

function _startsWithMarker(content: string): boolean {
  return (
    content.startsWith("[ ] ") ||
    content.startsWith("[x] ") ||
    content.startsWith("[X] ")
  );
}

/** Prepend a disabled checkbox to the inline token and strip the `[ ]`
 *  marker from both the token content and its first text child. */
function _todoify(token: MdToken, TokenCtor: MdTokenCtor): void {
  const checked = !token.content.startsWith("[ ] ");
  const box = new TokenCtor("html_inline", "", 0);
  box.content =
    `<input class="task-list-item-checkbox" ${checked ? 'checked="" ' : ""}` +
    'disabled="" type="checkbox">';
  const children = token.children || [];
  children.unshift(box);
  // children[1] is the original leading text token ("[ ] foo"); drop the
  // 3-char marker, leaving a leading space markdown-it renders as-is.
  if (children[1] && children[1].type === "text") {
    children[1].content = children[1].content.slice(3);
  }
  token.children = children;
  token.content = token.content.slice(3);
}

/** Index of the `bullet_list_open` / `ordered_list_open` enclosing the
 *  `list_item_open` at `itemIndex`. It's the token immediately before,
 *  by markdown-it's flat token order. */
function _parentList(tokens: MdToken[], itemIndex: number): number {
  for (let i = itemIndex - 1; i >= 0; i--) {
    const t = tokens[i].type;
    if (t === "bullet_list_open" || t === "ordered_list_open") return i;
  }
  return -1;
}

// --- External links ------------------------------------------------------
// Open links in a new tab so a click doesn't navigate away from the
// viewer, and set rel=noopener to sever the opener reference. Applied at
// render time; DOMPurify keeps target/rel on anchors (both are in its
// default allowlist).

function _installExternalLinks(md: MarkdownIt): void {
  const base = md.renderer.rules.link_open;
  md.renderer.rules.link_open = (tokens, idx, options, env, self) => {
    const token = tokens[idx];
    token.attrSet("target", "_blank");
    token.attrSet("rel", "noopener noreferrer");
    return base
      ? base(tokens, idx, options, env, self)
      : self.renderToken(tokens, idx, options);
  };
}

// --- Math ($…$ inline, $$…$$ block) --------------------------------------
// markdown-it has no math support. These rules recognise TeX delimiters
// and emit a placeholder carrying the raw source as escaped text; katex
// renders it once the block is in the DOM (hydrate()), keeping the katex
// bundle lazy and its output off the sanitized-HTML path. The delimiter
// scan is condensed from markdown-it-katex (MIT, Waylon Flinn): the
// currency-`$` heuristic (an opener can't be followed by whitespace, a
// closer can't be preceded by whitespace or followed by a digit) and the
// backslash-escape count are load-bearing, not cosmetic.

function _installMath(md: MarkdownIt): void {
  md.inline.ruler.after("escape", "math-inline", _mathInline);
  md.block.ruler.after("blockquote", "math-block", _mathBlock, {
    alt: ["paragraph", "reference", "blockquote", "list"],
  });
  md.renderer.rules.math_inline = (tokens, idx) =>
    `<span class="rmd-math rmd-math-inline">${md.utils.escapeHtml(tokens[idx].content)}</span>`;
  md.renderer.rules.math_block = (tokens, idx) =>
    `<div class="rmd-math rmd-math-display">${md.utils.escapeHtml(tokens[idx].content)}</div>\n`;
}

/** Whether a `$` at `pos` can open / close inline math. A `$` adjacent to
 *  whitespace on the inner side, or a closer followed by a digit (`$5`),
 *  is treated as literal currency, not a delimiter. */
function _isValidDelim(state: MdStateInline, pos: number): { canOpen: boolean; canClose: boolean } {
  const prev = pos > 0 ? state.src.charCodeAt(pos - 1) : -1;
  const next = pos + 1 <= state.posMax ? state.src.charCodeAt(pos + 1) : -1;
  let canOpen = true;
  let canClose = true;
  // 0x20 space, 0x09 tab; 0x30–0x39 digits.
  if (prev === 0x20 || prev === 0x09 || (next >= 0x30 && next <= 0x39)) canClose = false;
  if (next === 0x20 || next === 0x09) canOpen = false;
  return { canOpen, canClose };
}

function _mathInline(state: MdStateInline, silent: boolean): boolean {
  if (state.src[state.pos] !== "$") return false;
  let res = _isValidDelim(state, state.pos);
  if (!res.canOpen) {
    if (!silent) state.pending += "$";
    state.pos += 1;
    return true;
  }
  // Scan for the closing `$`, skipping an escaped `\$` (odd backslash run).
  const start = state.pos + 1;
  let match = start;
  while ((match = state.src.indexOf("$", match)) !== -1) {
    let pos = match - 1;
    while (state.src[pos] === "\\") pos -= 1;
    if ((match - pos) % 2 === 1) break; // even backslash count → real closer
    match += 1;
  }
  if (match === -1) {
    if (!silent) state.pending += "$";
    state.pos = start;
    return true;
  }
  if (match - start === 0) {
    if (!silent) state.pending += "$$";
    state.pos = start + 1;
    return true;
  }
  res = _isValidDelim(state, match);
  if (!res.canClose) {
    if (!silent) state.pending += "$";
    state.pos = start;
    return true;
  }
  if (!silent) {
    const token = state.push("math_inline", "math", 0);
    token.markup = "$";
    token.content = state.src.slice(start, match);
  }
  state.pos = match + 1;
  return true;
}

function _mathBlock(state: MdStateBlock, start: number, end: number, silent: boolean): boolean {
  let pos = state.bMarks[start] + state.tShift[start];
  let max = state.eMarks[start];
  if (pos + 2 > max) return false;
  if (state.src.slice(pos, pos + 2) !== "$$") return false;
  pos += 2;
  let firstLine = state.src.slice(pos, max);
  if (silent) return true;
  let found = false;
  let lastLine = "";
  if (firstLine.trim().slice(-2) === "$$") {
    // Single-line `$$…$$`.
    firstLine = firstLine.trim().slice(0, -2);
    found = true;
  }
  let next = start;
  while (!found) {
    next += 1;
    if (next >= end) break;
    pos = state.bMarks[next] + state.tShift[next];
    max = state.eMarks[next];
    if (pos < max && state.tShift[next] < state.blkIndent) break; // unindented → block ended
    if (state.src.slice(pos, max).trim().slice(-2) === "$$") {
      const lastPos = state.src.slice(0, max).lastIndexOf("$$");
      lastLine = state.src.slice(pos, lastPos);
      found = true;
    }
  }
  state.line = next + 1;
  const token = state.push("math_block", "math", 0);
  token.block = true;
  token.content =
    (firstLine.trim() ? firstLine + "\n" : "") +
    state.getLines(start + 1, next, state.tShift[start], true) +
    (lastLine.trim() ? lastLine : "");
  token.map = [start, state.line];
  token.markup = "$$";
  return true;
}

export const Markdown = { render, renderBlocks, hydrate };
export type { RenderedBlock, HeadingInfo };
