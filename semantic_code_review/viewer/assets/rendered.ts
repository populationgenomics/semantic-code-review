// Rendered-mode markdown diff (ADR 0004) — a second body renderer the
// per-file toggle switches to for `.md` files, leaving the text-diff
// renderer (render.ts) untouched.
//
// Slice 2: two-pane, block-level delta. Flipping a `.md` file fetches
// its full base+head source from /file-text (lazily, cached per file),
// parses both sides to top-level blocks, classifies each block from the
// existing line diff, and lays the aligned block-pairs out as max-height
// grid rows: base rendered left, head rendered right, changed base
// blocks tinted red / changed head blocks green. No cross-side
// similarity matching — alignment is anchor-and-pad off the line diff
// (unchanged blocks pair 1:1 in order; runs of changed blocks between
// anchors zip positionally, padding the shorter side).
//
// Slice 3: run folding + landmarks. `_plan` collapses contiguous runs of
// unchanged block-pairs into a full-width expand chip (equal-height on
// both panes, so alignment holds), breaking runs at unchanged headings
// which stay visible as landmarks. Context bleed keeps a block either
// side of a change; a min-run threshold leaves short gaps alone. Chip
// chevrons reveal one block from an end (single click) or the whole
// remaining run (double / shift-click); that reveal state is ephemeral,
// cleared when the fold level moves. The fold ladder — sections / runs /
// open — parallels the text-mode slider.
//
// Commenting reuses the existing (file, side, line) anchor: a block
// carries its source line via the markdown-it token map, so a comment on
// a block round-trips unchanged (comments.ts owns the editor/store).
//
// This module owns rendered-mode state (which files are flipped, the
// source cache) and the async fetch; render.ts consults isMarkdown /
// isOn and delegates the body to renderBody. The toggle handler fetches
// then repaints via a callback rather than importing render.ts, keeping
// the dependency one-way (render.ts → rendered.ts).

import { Comments } from "./comments";
import { Markdown, type HeadingInfo, type RenderedBlock } from "./markdown";
import { blockDiff, wrapRanges } from "./text_highlight";

interface FileText {
  file_idx: number;
  path: string;
  base: string | null;
  head: string | null;
}

// Rendered-mode fold ladder (ADR 0004 slice 3). `runs` (the default)
// collapses contiguous runs of unchanged blocks; `sections` also
// collapses whole unchanged heading-sections; `open` reveals everything.
// The level is per-file, driven by an in-body ladder rather than the
// global text-mode slider — rendered mode is a per-file toggle, so its
// controls stay inside the file body (they never touch a text-mode
// file's fold state).
type MdFoldLevel = "sections" | "runs" | "open";

const _FOLD_LADDER: ReadonlyArray<{ level: MdFoldLevel; label: string; title: string }> = [
  { level: "sections", label: "Sections", title: "Collapse whole unchanged sections" },
  { level: "runs", label: "Runs", title: "Collapse unchanged block-runs" },
  { level: "open", label: "Open", title: "Reveal everything" },
];

// Keep K unchanged blocks visible on the side of a run adjacent to a
// change (context bleed), and never collapse a gap this short — a 1–2
// block chip costs more attention than it saves.
const _BLEED = 1;
const _MIN_RUN = 3;

// Prefixed onto the /file-text fetch; the same session-endpoint boot.ts
// resolves for the other back-channel routes. Empty string = same origin.
let _endpoint = "";
// Full re-render, injected by boot (Render.render). Fold-level changes
// and chevron reveals repaint through this rather than importing
// render.ts, keeping the dependency one-way (render.ts → rendered.ts).
let _rerender: () => void = () => {};
// File ids (F<idx>) currently flipped to rendered mode.
const _on = new Set<string>();
// Lazy per-file source cache, keyed by file id. Populated on first flip;
// never invalidated (the base/head worktrees are pinned for the run).
const _cache: Record<string, FileText> = Object.create(null);

// Per-file fold level, keyed by file id; absent → the `runs` default.
const _foldLevel: Record<string, MdFoldLevel> = Object.create(null);
// Ephemeral incremental-reveal state, per file then per run key: how many
// blocks the reviewer has revealed from each end of a collapsed run.
// Cleared for a file when its fold level moves (the ladder is
// authoritative), so partial reveals never leak into a fresh level.
const _reveal: Record<string, Record<string, { top: number; bottom: number }>> =
  Object.create(null);
// Section keys the reviewer has forced fully open (section chip / outline
// expand). Same ephemeral lifetime as _reveal.
const _sectionOpen: Record<string, Set<string>> = Object.create(null);

function init(endpoint: string, rerender?: () => void): void {
  _endpoint = endpoint;
  if (rerender) _rerender = rerender;
}

function foldLevel(fileId: string): MdFoldLevel {
  return _foldLevel[fileId] || "runs";
}

/** Set one file's rendered-mode fold level and repaint. Clears that
 *  file's ephemeral reveal state — the level is authoritative, same as
 *  the text-mode slider's `_setGlobalFold`. */
function setFoldLevel(fileId: string, level: MdFoldLevel): void {
  _foldLevel[fileId] = level;
  delete _reveal[fileId];
  delete _sectionOpen[fileId];
  _rerender();
}

/** True for files rendered mode can handle — markdown by extension. */
function isMarkdown(f: FileBlock): boolean {
  const p = f.path.toLowerCase();
  return p.endsWith(".md") || p.endsWith(".markdown");
}

function isOn(fileId: string): boolean {
  return _on.has(fileId);
}

/** Flip a file between text-diff and rendered mode, then repaint.
 *
 *  Enabling fetches the file's source on first use; a failed fetch
 *  leaves the file in text mode (no flip, no repaint) rather than
 *  showing an empty rendered pane. Disabling is synchronous.
 */
async function toggle(f: FileBlock, rerender: () => void): Promise<void> {
  if (_on.has(f.id)) {
    _on.delete(f.id);
    rerender();
    return;
  }
  if (!_cache[f.id]) {
    try {
      _cache[f.id] = await _fetchText(f);
    } catch (e) {
      console.warn("rendered-mode: /file-text fetch failed, staying in text mode", e);
      return;
    }
  }
  _on.add(f.id);
  rerender();
}

async function _fetchText(f: FileBlock): Promise<FileText> {
  const idx = _fileIdx(f);
  const r = await fetch(`${_endpoint}/file-text?file_idx=${idx}`, { cache: "no-store" });
  if (!r.ok) throw new Error(`GET /file-text -> ${r.status}`);
  return (await r.json()) as FileText;
}

/** Recover the file index from the "F<idx>" id build_json assigns. */
function _fileIdx(f: FileBlock): number {
  const n = Number.parseInt(f.id.replace(/^F/, ""), 10);
  if (Number.isNaN(n)) throw new Error(`unexpected file id ${f.id}`);
  return n;
}

/** Render the file's rendered-mode body into `body`. Requires the
 *  source to be cached (toggle guarantees it before flipping on). */
function renderBody(body: HTMLElement, f: FileBlock): void {
  const text = _cache[f.id];
  const base = text ? text.base : null;
  const head = text ? text.head : null;
  const container = _el("div", "rendered-md");
  if (base == null && head == null) {
    // The toggle only appears for present .md files, so both sides null
    // here means the source went missing between flip and paint —
    // surface it rather than render a blank pane.
    container.appendChild(_el("div", "rendered-md-notice", "No rendered content available."));
    body.appendChild(container);
    return;
  }
  // An added file has no base (all-green head beside an empty left); a
  // deleted file has no head. Treat a missing side as empty so alignment
  // still runs.
  const diff = _diffLines(f);
  const baseBlocks = _classify(Markdown.renderBlocks(base ?? ""), diff.baseChanged, diff.baseDeleted);
  const headBlocks = _classify(Markdown.renderBlocks(head ?? ""), diff.headChanged, diff.headInserted);
  const pairs = _align(baseBlocks, headBlocks);
  container.appendChild(_renderControls(f.id, pairs));
  const grid = _el("div", "rmd-grid");
  for (const item of _plan(pairs, foldLevel(f.id), f.id)) {
    if (item.kind === "pair") {
      const oldCol = _renderCol(item.pair.base, "old", f);
      const newCol = _renderCol(item.pair.head, "new", f);
      _markInlineChanges(item.pair, oldCol, newCol);
      grid.appendChild(oldCol);
      grid.appendChild(newCol);
    } else {
      grid.appendChild(_renderFoldChip(item, f.id));
    }
  }
  container.appendChild(grid);
  body.appendChild(container);
}

// --- Block classification + alignment -----------------------------------

interface DiffBlock extends RenderedBlock {
  /** Touched by an ins/del/pair row on its side → tinted red (base) or
   *  green (head). */
  changed: boolean;
  /** Has a counterpart on the other side (at least one line the diff
   *  aligns via a ctx/pair row, i.e. not every line is a pure add/delete)
   *  → pairs 1:1; false → a one-sided block laid out against a blank. */
  matched: boolean;
}

interface BlockPair {
  base: DiffBlock | null;
  head: DiffBlock | null;
}

interface DiffLines {
  /** old-side lines touched by del|pair (drive the red tint). */
  baseChanged: Set<number>;
  /** new-side lines touched by ins|pair (drive the green tint). */
  headChanged: Set<number>;
  /** old-side lines with *no* head counterpart (`del` rows only, not
   *  `pair`). A base block all of whose lines are deleted has no head
   *  block to pair with. */
  baseDeleted: Set<number>;
  /** new-side lines with no base counterpart (`ins` rows only). */
  headInserted: Set<number>;
}

/** Project the hunks' row stream into the per-side line sets alignment
 *  and tinting need. `pair` rows (replacements) are aligned *and* changed
 *  on both sides; `del`/`ins` are one-sided and changed; `ctx` is aligned
 *  and unchanged. Hunks cover only changed regions, so a line absent from
 *  every set is context — aligned and unchanged. */
function _diffLines(f: FileBlock): DiffLines {
  const baseChanged = new Set<number>();
  const headChanged = new Set<number>();
  const baseDeleted = new Set<number>();
  const headInserted = new Set<number>();
  for (const h of f.hunks || []) {
    for (const r of h.rows || []) {
      if (r.kind === "ins" && r.new_line != null) {
        headChanged.add(r.new_line);
        headInserted.add(r.new_line);
      } else if (r.kind === "del" && r.old_line != null) {
        baseChanged.add(r.old_line);
        baseDeleted.add(r.old_line);
      } else if (r.kind === "pair") {
        if (r.new_line != null) headChanged.add(r.new_line);
        if (r.old_line != null) baseChanged.add(r.old_line);
      }
    }
  }
  return { baseChanged, headChanged, baseDeleted, headInserted };
}

/** Classify each block for tinting (`changed`) and alignment (`matched`).
 *  A block is `matched` unless *every* one of its lines is one-sided (all
 *  `del` for a base block, all `ins` for a head block) — i.e. it keeps at
 *  least one line the diff aligns to the other side. Exact because
 *  markdown blocks break on line boundaries. */
function _classify(blocks: RenderedBlock[], changed: Set<number>, oneSided: Set<number>): DiffBlock[] {
  return blocks.map((b) => {
    let hitChanged = false;
    let allOneSided = true;
    for (let ln = b.startLine; ln <= b.endLine; ln++) {
      if (changed.has(ln)) hitChanged = true;
      if (!oneSided.has(ln)) allOneSided = false;
    }
    return { ...b, changed: hitChanged, matched: !allOneSided };
  });
}

/** Pair base blocks with head blocks by projecting the line diff's own
 *  alignment — no cross-side content matching. A `matched` block pairs
 *  1:1 in order with the next matched block on the other side; a purely
 *  deleted base block or inserted head block drains against a blank cell.
 *  Because the diff already aligns replacements (`pair` rows) and marks
 *  add/delete (`ins`/`del`), a replaced item lands beside its replacement
 *  and a deleted item sits on its own row — rather than zipping unrelated
 *  changed blocks positionally. Matched blocks correspond 1:1 by the
 *  diff's monotonicity; a count mismatch (one block split into two on the
 *  other side) drains as one-sided pairs — the accepted failure mode. */
function _align(base: DiffBlock[], head: DiffBlock[]): BlockPair[] {
  const pairs: BlockPair[] = [];
  let i = 0;
  let j = 0;
  while (i < base.length || j < head.length) {
    if (i < base.length && !base[i].matched) {
      pairs.push({ base: base[i++], head: null });
    } else if (j < head.length && !head[j].matched) {
      pairs.push({ base: null, head: head[j++] });
    } else if (i < base.length && j < head.length) {
      pairs.push({ base: base[i++], head: head[j++] });
    } else if (i < base.length) {
      pairs.push({ base: base[i++], head: null });
    } else {
      pairs.push({ base: null, head: head[j++] });
    }
  }
  return pairs;
}

// --- Fold planning ------------------------------------------------------
//
// Turn the aligned block-pairs into a render plan: an ordered list of
// visible pairs interleaved with fold chips standing in for collapsed
// runs. Pure — no DOM, no reveal mutation — so it's unit-testable and
// re-runs cheaply on every repaint.

/** A block-pair the plan keeps visible (one grid row). */
interface PairItem {
  kind: "pair";
  pair: BlockPair;
}

/** A collapsed run of unchanged block-pairs, standing in as one
 *  full-width chip. `count` is how many pairs it hides; `key` is the
 *  stable reveal key; `run`/`section` distinguishes a run-fold from a
 *  whole-section fold (which chevron affordances differ). */
interface FoldItem {
  kind: "fold";
  key: string;
  count: number;
  scope: "run" | "section";
}

type PlanItem = PairItem | FoldItem;

interface Section {
  heading: BlockPair | null;
  body: BlockPair[];
}

function _pairChanged(p: BlockPair): boolean {
  return !!(p.base?.changed || p.head?.changed);
}

function _pairHeading(p: BlockPair): HeadingInfo | null {
  return p.head?.heading ?? p.base?.heading ?? null;
}

/** Stable identity for a pair across reveal-only repaints: its head
 *  source line, else its base line. Reveal state keys off the topmost
 *  pair of a run, which never shifts as the run reveals. */
function _pairKey(p: BlockPair): string {
  if (p.head) return `h${p.head.startLine}`;
  if (p.base) return `b${p.base.startLine}`;
  return "?";
}

/** Split the pair stream into sections: each heading pair opens one,
 *  its body runs to the next heading. Blocks ahead of the first heading
 *  form a headingless preamble section. */
function _sections(pairs: BlockPair[]): Section[] {
  const out: Section[] = [{ heading: null, body: [] }];
  for (const p of pairs) {
    if (_pairHeading(p)) out.push({ heading: p, body: [] });
    else out[out.length - 1].body.push(p);
  }
  // Drop a leading empty preamble (document opens with a heading).
  return out.filter((s) => s.heading || s.body.length);
}

function _sectionChanged(s: Section): boolean {
  return (s.heading != null && _pairChanged(s.heading)) || s.body.some(_pairChanged);
}

/** One entry per heading section for the in-body outline: its level and
 *  text (for the indented label), a changed/unchanged badge, and the
 *  section key an outline click expands. The headingless preamble
 *  contributes nothing. Pure — unit-tested alongside `_plan`. */
interface OutlineEntry {
  key: string;
  level: number;
  text: string;
  changed: boolean;
}

function _outline(pairs: BlockPair[]): OutlineEntry[] {
  const out: OutlineEntry[] = [];
  for (const s of _sections(pairs)) {
    if (!s.heading) continue;
    const h = _pairHeading(s.heading)!;
    out.push({ key: _pairKey(s.heading), level: h.level, text: h.text, changed: _sectionChanged(s) });
  }
  return out;
}

function _revealFor(fileId: string, key: string): { top: number; bottom: number } {
  const perFile = _reveal[fileId];
  return (perFile && perFile[key]) || { top: 0, bottom: 0 };
}

function _plan(pairs: BlockPair[], level: MdFoldLevel, fileId: string): PlanItem[] {
  const open = _sectionOpen[fileId];
  const out: PlanItem[] = [];
  for (const s of _sections(pairs)) {
    const forcedOpen = s.heading != null && !!open && open.has(_pairKey(s.heading));
    if (level === "sections" && s.heading != null && !_sectionChanged(s) && !forcedOpen) {
      out.push({
        kind: "fold", scope: "section",
        key: _pairKey(s.heading), count: 1 + s.body.length,
      });
      continue;
    }
    // Heading stays visible as a landmark; a changed heading counts as a
    // change bounding the first body run (context bleeds against it). A
    // section the reviewer expanded (chip / outline) renders fully open.
    if (s.heading) out.push({ kind: "pair", pair: s.heading });
    const bodyLevel = forcedOpen ? "open" : level;
    out.push(..._foldBody(s.body, s.heading != null && _pairChanged(s.heading), bodyLevel, fileId));
  }
  return out;
}

/** Fold the unchanged runs inside one section body. `leftIsChange` is
 *  whether the block preceding the body (the heading) was itself a
 *  change — the first run bleeds context against it. */
function _foldBody(
  body: BlockPair[], leftIsChange: boolean, level: MdFoldLevel, fileId: string,
): PlanItem[] {
  if (level === "open") return body.map((pair) => ({ kind: "pair", pair }));
  const out: PlanItem[] = [];
  let i = 0;
  while (i < body.length) {
    if (_pairChanged(body[i])) {
      out.push({ kind: "pair", pair: body[i++] });
      continue;
    }
    const start = i;
    while (i < body.length && !_pairChanged(body[i])) i++;
    const run = body.slice(start, i);
    // A run is maximal, so any body pair bounding it is a change; at the
    // body edges the bound is the heading (start) or section end (end).
    const leftChange = start === 0 ? leftIsChange : true;
    const rightChange = i < body.length;
    out.push(..._foldRun(run, leftChange, rightChange, fileId));
  }
  return out;
}

function _foldRun(
  run: BlockPair[], leftChange: boolean, rightChange: boolean, fileId: string,
): PlanItem[] {
  const key = _pairKey(run[0]);
  const reveal = _revealFor(fileId, key);
  const topKeep = (leftChange ? _BLEED : 0) + reveal.top;
  const bottomKeep = (rightChange ? _BLEED : 0) + reveal.bottom;
  const collapsible = run.length - topKeep - bottomKeep;
  if (collapsible < _MIN_RUN) {
    return run.map((pair) => ({ kind: "pair", pair }));
  }
  const out: PlanItem[] = [];
  for (let k = 0; k < topKeep; k++) out.push({ kind: "pair", pair: run[k] });
  out.push({ kind: "fold", scope: "run", key, count: collapsible });
  for (let k = run.length - bottomKeep; k < run.length; k++) {
    out.push({ kind: "pair", pair: run[k] });
  }
  return out;
}

/** Bump a run's reveal count and repaint. `end` selects which side;
 *  `toBoundary` reveals the whole remaining run (double / shift-click)
 *  rather than one block. A run never crosses a heading, so "to the
 *  section boundary" is just "the rest of this run". */
function _revealRun(
  fileId: string, key: string, end: "top" | "bottom", toBoundary: boolean,
): void {
  const perFile = (_reveal[fileId] ||= Object.create(null));
  const cur = perFile[key] || { top: 0, bottom: 0 };
  // A generous bump for "to boundary": foldRun clamps against run length,
  // so any value ≥ run length fully reveals that end.
  const step = toBoundary ? Number.MAX_SAFE_INTEGER : 1;
  perFile[key] = {
    top: cur.top + (end === "top" ? step : 0),
    bottom: cur.bottom + (end === "bottom" ? step : 0),
  };
  _rerender();
}

function _openSection(fileId: string, key: string): void {
  (_sectionOpen[fileId] ||= new Set<string>()).add(key);
  _rerender();
}

// --- Column DOM ---------------------------------------------------------

/** One grid cell: the rendered block tinted by status, a hover
 *  comment-add affordance, and any existing comment threads for the
 *  block's span. A null block is an alignment pad (blank cell). */
function _renderCol(block: DiffBlock | null, side: "old" | "new", f: FileBlock): HTMLElement {
  const col = _el("div", `rmd-col rmd-col-${side}`);
  if (!block) {
    col.classList.add("rmd-col-empty");
    return col;
  }
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "rmd-comment-btn";
  btn.title = "Comment on this block";
  btn.textContent = "+";
  col.appendChild(btn);

  const blk = _el("div", "rmd-block");
  if (block.changed) blk.classList.add(side === "old" ? "rmd-removed" : "rmd-added");
  // A split list item: tighten the inter-item margin (rmd-li), and give
  // the final item the normal after-block gap (rmd-li-last).
  if (block.listItem) blk.classList.add("rmd-li");
  if (block.listItem === "last") blk.classList.add("rmd-li-last");
  // Already sanitized by Markdown.renderBlocks — untrusted .md crossed
  // DOMPurify there, so this innerHTML is safe.
  blk.innerHTML = block.html;
  // Render mermaid/math from their source delimiters (controlled
  // renderers, off the sanitized-HTML path); a cached diagram swaps in
  // synchronously, an uncached one when its async render settles — the
  // max-height grid row absorbs the height change without realignment.
  Markdown.hydrate(blk);
  col.appendChild(blk);

  const line = block.startLine;
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    Comments.openBlockEditor({ anchorEl: blk, file: f.path, side, line });
  });
  Comments.attachBlockThreads({
    anchorEl: blk, file: f.path, side,
    startLine: block.startLine, endLine: block.endLine,
  });
  return col;
}

/** Intra-block sub-diff for a replaced pair: mark the changed characters
 *  within the two blocks — deleted on the base (left), added on the head
 *  (right). Reuses the text diff's token sub-diff (`blockDiff`) and range
 *  painter (`wrapRanges`), but over each block's *rendered* `textContent`
 *  (markdown syntax already stripped) rather than the source line — so the
 *  offsets blockDiff computes line up with the DOM wrapRanges paints, and
 *  the marks land on the reader-facing prose. Only fires on a matched pair
 *  changed on both sides (a replacement); a pure add/delete block is
 *  wholly tinted already. `wrapRanges` crosses inline elements (`<strong>`,
 *  `<a>`, `<code>`) by splitting text nodes, and `blockDiff` falls back to
 *  no ranges past its size guard — so a huge block just keeps the block
 *  tint. */
function _markInlineChanges(pair: BlockPair, oldCol: HTMLElement, newCol: HTMLElement): void {
  const base = pair.base;
  const head = pair.head;
  if (!base || !head || !base.changed || !head.changed) return;
  const oldBlk = oldCol.querySelector<HTMLElement>(".rmd-block");
  const newBlk = newCol.querySelector<HTMLElement>(".rmd-block");
  if (!oldBlk || !newBlk) return;
  const d = blockDiff([oldBlk.textContent ?? ""], [newBlk.textContent ?? ""]);
  wrapRanges(oldBlk, d.old[0], "char-chg");
  wrapRanges(newBlk, d.new[0], "char-chg");
}

/** In-body control bar for one rendered file: the fold ladder plus the
 *  heading outline. Per-file (not the global text-mode slider), so a
 *  rendered `.md` carries its own structural controls without touching
 *  any other file's fold state. */
function _renderControls(fileId: string, pairs: BlockPair[]): HTMLElement {
  const bar = _el("div", "rmd-controls");
  bar.appendChild(_renderLadder(fileId));
  const outline = _renderOutline(fileId, pairs);
  if (outline) bar.appendChild(outline);
  return bar;
}

function _renderLadder(fileId: string): HTMLElement {
  const cur = foldLevel(fileId);
  const ladder = _el("div", "rmd-ladder");
  ladder.setAttribute("role", "tablist");
  ladder.setAttribute("aria-label", "Fold level");
  for (const step of _FOLD_LADDER) {
    const btn = _el("button", "rmd-ladder-btn", step.label);
    btn.title = step.title;
    btn.dataset.level = step.level;
    if (step.level === cur) btn.classList.add("active");
    btn.setAttribute("aria-selected", step.level === cur ? "true" : "false");
    btn.addEventListener("click", () => setFoldLevel(fileId, step.level));
    ladder.appendChild(btn);
  }
  return ladder;
}

/** The heading outline: one entry per section, indented by heading level
 *  and badged changed/unchanged. Clicking expands that section fully
 *  (all its runs revealed). Returns null when the doc has no headings. */
function _renderOutline(fileId: string, pairs: BlockPair[]): HTMLElement | null {
  const entries = _outline(pairs);
  if (entries.length === 0) return null;
  const nav = _el("div", "rmd-outline");
  nav.setAttribute("aria-label", "Document outline");
  for (const e of entries) {
    const level = Math.min(Math.max(e.level, 1), 6);
    const cls = e.changed ? "rmd-outline-changed" : "rmd-outline-unchanged";
    const btn = _el("button", `rmd-outline-entry rmd-outline-l${level} ${cls}`);
    btn.title = (e.changed ? "changed" : "unchanged") + " — expand this section";
    btn.appendChild(_el("span", "rmd-outline-badge"));
    btn.appendChild(_el("span", "rmd-outline-text", e.text));
    btn.addEventListener("click", () => _openSection(fileId, e.key));
    nav.appendChild(btn);
  }
  return nav;
}

/** A collapsed run/section as one full-width chip. A run chip carries a
 *  chevron at each end (reveal one block, or the whole run on double /
 *  shift-click); a section chip is a single expand into runs-level view.
 *  Height is symmetric across both panes by construction, so the grid
 *  stays aligned. */
function _renderFoldChip(item: FoldItem, fileId: string): HTMLElement {
  const chip = _el("div", `rmd-fold rmd-fold-${item.scope}`);
  const noun = item.scope === "section" ? "section" : "block";
  const label = `${item.count} unchanged ${noun}${item.count === 1 ? "" : "s"}`;
  if (item.scope === "section") {
    const btn = _el("button", "rmd-fold-expand", `⋯ ${label} ⋯`);
    btn.title = "Expand this section";
    btn.addEventListener("click", () => _openSection(fileId, item.key));
    chip.appendChild(btn);
    return chip;
  }
  chip.appendChild(_revealChevron(fileId, item.key, "top"));
  chip.appendChild(_el("span", "rmd-fold-label", `⋯ ${label} ⋯`));
  chip.appendChild(_revealChevron(fileId, item.key, "bottom"));
  return chip;
}

// A single click rebuilds the DOM (one reveal), which discards this
// button — so `dblclick` never fires across the replacement. shift-click
// is the reliable "reveal to the section boundary" gesture; the chip
// re-renders after every reveal regardless.
function _revealChevron(fileId: string, key: string, end: "top" | "bottom"): HTMLElement {
  const btn = _el("button", `rmd-fold-chev rmd-fold-chev-${end}`);
  btn.textContent = end === "top" ? "▲" : "▼";
  const dir = end === "top" ? "above" : "below";
  btn.title = `Reveal the block ${dir} (shift-click for the whole run)`;
  btn.addEventListener("click", (e) => _revealRun(fileId, key, end, e.shiftKey));
  return btn;
}

function _el(tag: string, cls: string, text?: string): HTMLElement {
  const e = document.createElement(tag);
  e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

export const Rendered = {
  init, isMarkdown, isOn, toggle, renderBody, foldLevel, setFoldLevel,
};

// Exposed for unit tests (tests/js/rendered.test.ts): the pure fold
// planner + outline and the reveal state, plus the diff-driven block
// classification + alignment — all driven directly without DOM/rerender.
export { _plan, _outline, _reveal, _sectionOpen, _diffLines, _classify, _align };
export type { BlockPair, PlanItem, OutlineEntry, DiffBlock };
