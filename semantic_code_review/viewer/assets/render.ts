// Diff renderer + fold-state machinery.
//
// Owns the layout pass that turns DATA into the on-page DOM: PR
// panel, file blocks, hunk headers, the side-by-side row grid, gap
// chips for unchanged context, segment-folded summaries, refs, smell
// pills. Carries the fold state too (STATE.fold / overrides /
// renderedDiffs cache) because all of that exists to feed the
// renderer, and binds the user inputs that drive it (fold-slider
// buttons, keyboard 1-4, hash sync).
//
// Other modules attach to surfaces this module creates:
//   - sidebar.ts mutates pill state but reads from .file / .hunk
//   - folds.ts attaches chevrons to the per-half row elements stashed
//     on each .diff and .gap-expansion container
//   - comments.ts replays its comment rows after each renderAll
//   - annotations.ts hosts the row-annotation DOM

import { Annotations } from "./annotations";
import { Comments } from "./comments";
import { FileRows } from "./file_rows";
import { Folds } from "./folds";
import { Progress } from "./progress";
import { Sidebar } from "./sidebar";
import { charDiff, wrapRanges, type CharRange } from "./text_highlight";

// --- Module state --------------------------------------------------------

type FoldMode = "files" | "hunks" | "segments" | "off";

interface RenderState {
  fold: FoldMode;
  overrides: Record<string, boolean>;
  renderedDiffs: Record<string, HTMLElement>;
}

let _data: ViewerData = { version: "1", pr: {} as PRBlock, smells_catalogue: {}, files: [], groups: [], symbols: [] };
let _smells: Record<string, SmellCatalogueEntry> = {};
const _state: RenderState = {
  fold: "hunks",
  overrides: Object.create(null),
  renderedDiffs: Object.create(null),
};

// --- Public API ----------------------------------------------------------

/** Wire input handlers + restore state from URL hash + run initial
 *  render. Called once at boot from viewer.js. Resets the rendered-
 *  diff cache + fold overrides so a re-boot (tests, future hot
 *  reload) starts fresh. */
function renderInit(data: ViewerData): void {
  _data = data;
  _smells = data.smells_catalogue || {};
  _state.fold = "hunks";
  _state.overrides = Object.create(null);
  _state.renderedDiffs = Object.create(null);
  _wireInputs();
  _restoreHash();
  render();
}

/** Re-render the entire app DOM. Cheap-ish — STATE.renderedDiffs
 *  caches the per-hunk .diff so this isn't quadratic on revisits. */
function render(): void {
  const app = document.getElementById("app");
  if (!app) return;
  app.innerHTML = "";
  app.appendChild(_renderPRPanel(_data.pr));
  for (const f of _data.files) app.appendChild(_renderFile(f));
  Sidebar.render();
  Sidebar.applyFilter();
  _updateStatus();
  _syncHash();
  _updateSliderButtons();
  Comments.renderAll();
  // Annotation arrows attached during render were sized while the
  // tree was still detached. The viewport watcher hooks
  // window-resize + fonts.ready for post-mount reflow; double-RAF a
  // fresh pass for the first paint.
  Annotations.watchViewport();
  requestAnimationFrame(() => {
    Annotations.reflowAll();
    requestAnimationFrame(() => Annotations.reflowAll());
  });
}

/** Replace one hunk's DOM in place. Drops the renderedDiffs cache
 *  entry first so attachLineNotes / fold detection re-run against
 *  the (possibly different) row set. Called from the SSE patchers
 *  in viewer.js when a `hunk` event arrives. */
function renderHunkReplace(file: FileBlock, hunkIdx: number): void {
  const h = file.hunks[hunkIdx];
  if (!h) return;
  delete _state.renderedDiffs[h.id];
  const fresh = _renderHunk(h, file);
  const existing = document.querySelector(
    '.hunk[data-id="' + _cssEscape(h.id) + '"]',
  );
  if (existing && existing.parentNode) {
    existing.parentNode.replaceChild(fresh, existing);
  }
}

/** Re-render just the header of one hunk (intent slot + meta).
 *  Used by the hunk-start SSE handler to flip the "queued"
 *  placeholder to "analysing…" without rebuilding the diff body. */
function repaintHunkHeader(hunkId: string): void {
  const node = document.querySelector(
    '.hunk[data-id="' + _cssEscape(hunkId) + '"]',
  );
  if (!node) return;
  const oldHdr = node.querySelector(".hunk-header");
  if (!oldHdr) return;
  const parts = hunkId.replace("H", "").split("_").map(Number);
  const [fi, hi] = parts;
  const f = _data.files && _data.files[fi];
  const h = f && f.hunks && f.hunks[hi];
  if (!h) return;
  const folded = _isFolded(h.id, _defaultHunkFolded());
  const fresh = _renderHunkHeader(h, folded, f);
  oldHdr.replaceWith(fresh);
}

/** Drop the cached `.diff` element for a hunk. Called by SSE
 *  patchers before they replace the surrounding hunk DOM. */
function clearRenderedDiffCache(hunkId: string): void {
  delete _state.renderedDiffs[hunkId];
}

// --- Fold state ---------------------------------------------------------

function _defaultFileFolded(): boolean    { return _state.fold === "files"; }
function _defaultHunkFolded(): boolean    { return _state.fold === "files" || _state.fold === "hunks"; }
function _defaultSegmentFolded(): boolean { return _state.fold !== "off"; }

function _isFolded(id: string, fallback: boolean): boolean {
  return Object.prototype.hasOwnProperty.call(_state.overrides, id)
    ? _state.overrides[id] : fallback;
}

function _toggleFold(id: string, currentDefault: boolean): void {
  const current = _isFolded(id, currentDefault);
  _state.overrides[id] = !current;
  render();
}

function _setGlobalFold(fold: FoldMode): void {
  _state.fold = fold;
  _state.overrides = Object.create(null);
  render();
}

// --- DOM helpers (private) ----------------------------------------------

const _SVG_NS = "http://www.w3.org/2000/svg";

function _el(tag: string, className: string | null, text?: string): HTMLElement {
  const n = document.createElement(tag);
  if (className) n.className = className;
  if (text !== undefined) n.textContent = text;
  return n;
}

function _chev(folded: boolean, extraClass?: string): SVGElement {
  const svg = document.createElementNS(_SVG_NS, "svg") as unknown as SVGElement;
  svg.setAttribute("viewBox", "0 0 12 12");
  svg.setAttribute("aria-hidden", "true");
  svg.classList.add("chevron");
  if (extraClass) svg.classList.add(extraClass);
  if (!folded) svg.classList.add("open");
  const path = document.createElementNS(_SVG_NS, "path");
  path.setAttribute("d", "M4.25 2.75 L8 6 L4.25 9.25");
  path.setAttribute("fill", "none");
  path.setAttribute("stroke", "currentColor");
  path.setAttribute("stroke-width", "1.75");
  path.setAttribute("stroke-linecap", "round");
  path.setAttribute("stroke-linejoin", "round");
  svg.appendChild(path);
  return svg;
}

interface SmellPromotion {
  /** Stable id of the source smell — "<container_id>:smell:<tag>". */
  smellId: string;
  file: string;
  side: "old" | "new";
  line: number;
}

/** Bucket the LLM's 0-100 confidence into a subtle three-star
 *  indicator that sits at the top-right of a hunk header. Returns
 *  null when no confidence was emitted (so the slot is invisible
 *  rather than rendering an empty rating). */
function _confidenceStars(confidence: number | null | undefined): HTMLElement | null {
  if (confidence == null) return null;
  // Buckets chosen so a model that hedges (<50) gets one star and a
  // confident answer (≥80) gets three. The middle band (50-79) is the
  // most common "I think so, not 100%" outcome.
  const filled = confidence >= 80 ? 3 : confidence >= 50 ? 2 : 1;
  const wrap = _el("span", "hunk-confidence");
  wrap.dataset.level = String(filled);
  wrap.title = `Model confidence ${confidence}/100`
    + (filled === 1 ? " — low, review carefully" : "");
  for (let i = 0; i < 3; i++) {
    const star = _el("span", "conf-star" + (i < filled ? " on" : ""));
    star.textContent = i < filled ? "★" : "☆";
    wrap.appendChild(star);
  }
  return wrap;
}

function _smellPill(smell: Smell, promotion?: SmellPromotion): HTMLElement {
  const def = _smells[smell.tag];
  const sev = def ? def.severity : "minor";
  const p = _el("span", `smell sev-${sev}`, smell.tag);
  p.title = smell.note || (def ? def.label : smell.tag);
  if (promotion) {
    // Skip rendering at all if the user has already promoted this smell
    // — the renderer treats a non-attached element as a no-op.
    if (Comments.isPromoted(promotion.smellId)) {
      p.style.display = "none";
    }
    p.dataset.smellId = promotion.smellId;
    p.classList.add("smell-promotable");
    p.title = `${smell.tag}${smell.note ? ` — ${smell.note}` : ""} (click to add as comment)`;
    p.addEventListener("click", (e) => {
      e.stopPropagation();
      const body = smell.note
        ? `${smell.tag}: ${smell.note}`
        : smell.tag;
      Comments.promoteSmell({
        ...promotion, body, smellId: promotion.smellId,
      });
    });
  }
  return p;
}

function _esc(s: string): string {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;",
  }[c] || c));
}

function _cssEscape(s: string): string {
  const w = window as unknown as { CSS?: { escape?: (s: string) => string } };
  if (w.CSS && typeof w.CSS.escape === "function") return w.CSS.escape(s);
  return String(s).replace(/[^a-zA-Z0-9_-]/g, (c) => "\\" + c);
}

// --- Renderers ----------------------------------------------------------

function _renderPRPanel(pr: PRBlock): HTMLElement {
  const panel = _el("section", "pr-panel");
  panel.appendChild(_el("h2", null, "PR summary"));
  panel.appendChild(_el("p", null, pr.summary || "(no summary)"));
  if (pr.themes && pr.themes.length) {
    const themes = _el("div", "themes");
    for (const t of pr.themes) themes.appendChild(_el("span", null, t));
    panel.appendChild(themes);
  }
  return panel;
}

function _renderFile(f: FileBlock): HTMLElement {
  const div = _el("div", "file");
  div.dataset.id = f.id;
  const folded = _isFolded(f.id, _defaultFileFolded());
  div.classList.toggle("folded", folded);
  div.appendChild(_renderFileHeader(f, folded));
  if (!folded) {
    const body = _el("div", "file-body");
    const overview = _renderFileOverview(f);
    if (overview) body.appendChild(overview);
    const top = _gapBeforeFirstHunk(f);
    if (top) body.appendChild(_renderGapChip(f, top));
    for (let i = 0; i < f.hunks.length; i++) {
      body.appendChild(_renderHunk(f.hunks[i], f));
      const mid = _gapAfterHunk(f, i);
      if (mid) body.appendChild(_renderGapChip(f, mid));
    }
    div.appendChild(body);
    // Run a file-level fold pass once the body is assembled.
    Folds.attachFileFolds(div, f);
  }
  return div;
}

function _renderFileHeader(f: FileBlock, folded: boolean): HTMLElement {
  const hdr = _el("div", "file-header");
  hdr.appendChild(_chev(folded));
  hdr.appendChild(_el("span", "file-path", f.path));
  hdr.appendChild(_el("span", "file-summary", f.summary || ""));
  const meta = _el("div", "file-meta");
  meta.appendChild(_el("span", "adds", `+${f.adds}`));
  meta.appendChild(_el("span", "dels", `-${f.dels}`));
  hdr.appendChild(meta);
  const smells = _uniqueFileSmells(f);
  if (smells.length) {
    const badge = _el("div", "file-meta");
    for (const sm of smells) badge.appendChild(_smellPill({ tag: sm, note: "" }));
    hdr.appendChild(badge);
  }
  hdr.addEventListener("click", () => _toggleFold(f.id, _defaultFileFolded()));
  return hdr;
}

function _uniqueFileSmells(f: FileBlock): string[] {
  const s = new Set<string>();
  for (const h of f.hunks) {
    for (const sm of h.smells || []) s.add(sm.tag);
    for (const seg of h.segments || []) for (const sm of seg.smells || []) s.add(sm.tag);
  }
  return Array.from(s);
}

function _renderFileOverview(f: FileBlock): HTMLElement | null {
  const sym = f.symbols || { added: [], modified: [], removed: [] };
  const parts: string[] = [];
  if (sym.added && sym.added.length) parts.push(`<span class="label">added:</span>${_esc(sym.added.join(", "))}`);
  if (sym.modified && sym.modified.length) parts.push(`<span class="label">modified:</span>${_esc(sym.modified.join(", "))}`);
  if (sym.removed && sym.removed.length) parts.push(`<span class="label">removed:</span>${_esc(sym.removed.join(", "))}`);
  if (parts.length === 0) return null;
  const div = _el("div", "file-overview");
  div.innerHTML = parts.join("&nbsp;&nbsp;");
  return div;
}

// --- Inter-hunk context expansion ---------------------------------------

interface GapDescriptor {
  position: "top" | "between" | "bottom";
  new_start: number;
  new_end: number;
  old_start: number;
  old_end: number;
}

function _gapBeforeFirstHunk(f: FileBlock): GapDescriptor | null {
  if (!f.head_lines || f.hunks.length === 0) return null;
  const h = f.hunks[0];
  const newStart = 1, newEnd = h.new_start - 1;
  if (newEnd < newStart) return null;
  return {
    position: "top",
    new_start: newStart, new_end: newEnd,
    old_start: 1, old_end: h.old_start - 1,
  };
}

function _gapAfterHunk(f: FileBlock, i: number): GapDescriptor | null {
  if (!f.head_lines) return null;
  const h = f.hunks[i];
  const newStart = h.new_start + h.new_count;
  const oldStart = h.old_start + h.old_count;
  if (i + 1 < f.hunks.length) {
    const n = f.hunks[i + 1];
    const newEnd = n.new_start - 1;
    if (newEnd < newStart) return null;
    return {
      position: "between",
      new_start: newStart, new_end: newEnd,
      old_start: oldStart, old_end: n.old_start - 1,
    };
  }
  const total = f.head_lines.length;
  if (newStart > total) return null;
  return {
    position: "bottom",
    new_start: newStart, new_end: total,
    old_start: oldStart, old_end: oldStart + (total - newStart),
  };
}

function _renderGapChip(f: FileBlock, gap: GapDescriptor): HTMLElement {
  const chip = _el("div", "gap-chip");
  const count = gap.new_end - gap.new_start + 1;
  const icon = gap.position === "top" ? "⬆" : gap.position === "bottom" ? "⬇" : "⋯";
  const word = count === 1 ? "line" : "lines";
  const label = gap.position === "top" ? `expand ${count} ${word} above`
              : gap.position === "bottom" ? `expand ${count} ${word} below`
              : `expand ${count} hidden ${word}`;
  chip.innerHTML = `<span class="gap-icon">${icon}</span> <span class="gap-label">${label}</span>`;
  chip.title = `lines ${gap.new_start}–${gap.new_end}`;
  chip.addEventListener("click", () => {
    chip.replaceWith(_renderGapExpansion(f, gap));
    _refreshFileFolds(f);
  });
  return chip;
}

function _renderGapExpansion(f: FileBlock, gap: GapDescriptor): HTMLElement {
  const container = _el("div", "gap-expansion");
  const collapse = _el("button", "gap-collapse", "× collapse");
  collapse.title = "Hide these lines again";
  collapse.addEventListener("click", () => {
    container.replaceWith(_renderGapChip(f, gap));
    _refreshFileFolds(f);
  });
  container.appendChild(collapse);

  const diff = _el("div", "diff");
  const halfOld = _el("div", "half half-old");
  const halfNew = _el("div", "half half-new");
  diff.appendChild(halfOld);
  diff.appendChild(halfNew);

  const rows: RowBlock[] = [];
  const rowElsOld: HTMLElement[] = [];
  const rowElsNew: HTMLElement[] = [];
  const count = gap.new_end - gap.new_start + 1;
  const headLines = f.head_lines || [];
  for (let i = 0; i < count; i++) {
    const ol = gap.old_start + i;
    const nl = gap.new_start + i;
    const text = headLines[nl - 1] ?? "";
    const rowRecord: RowBlock = {
      kind: "ctx", old_line: ol, new_line: nl,
      old_text: text, new_text: text,
    };
    rows.push(rowRecord);
    const pair = _renderRow(rowRecord, f);
    (pair.old as { _scrPair?: HTMLElement })._scrPair = pair.new;
    (pair.new as { _scrPair?: HTMLElement })._scrPair = pair.old;
    halfOld.appendChild(pair.old);
    halfNew.appendChild(pair.new);
    rowElsOld.push(pair.old);
    rowElsNew.push(pair.new);
  }

  // The file-level fold walker (folds.ts) needs to recover the row
  // stream + per-side DOM elements; hand them off through FileRows.
  FileRows.record(container, { rows, oldEls: rowElsOld, newEls: rowElsNew });

  container.appendChild(diff);
  return container;
}

function _refreshFileFolds(f: FileBlock): void {
  const fileEl = document.querySelector(
    '.file[data-id="' + _cssEscape(f.id) + '"]',
  ) as HTMLElement | null;
  if (fileEl) Folds.attachFileFolds(fileEl, f);
}

// --- Hunk + diff body ---------------------------------------------------

function _renderHunk(h: HunkBlock, f: FileBlock): HTMLElement {
  const div = _el("div", "hunk");
  div.dataset.id = h.id;
  const folded = _isFolded(h.id, _defaultHunkFolded());
  div.classList.toggle("folded", folded);
  div.style.borderLeftColor = _maxSeverityColor(h);
  div.appendChild(_renderHunkHeader(h, folded, f));
  if (!folded) {
    if (
      h.segments && h.segments.length > 0
      && _defaultSegmentFolded()
      && !_anySegmentOverridden(h, false)
    ) {
      const list = _el("div", "seg-list");
      for (const s of h.segments) list.appendChild(_renderSegmentFolded(s, f));
      div.appendChild(list);
    } else {
      div.appendChild(_renderHunkDiff(h, f));
    }
    if (h.context) {
      const c = _el("div", "context-note");
      c.innerHTML = `<strong>context:</strong> ${_esc(h.context)}`;
      div.appendChild(c);
    }
    if (h.refs && h.refs.length) {
      div.appendChild(_renderRefs(h.refs));
    }
    // line_notes used to render as a bottom-of-hunk block; they're
    // attached inline by _attachLineNotes() in _renderHunkDiff.
  }
  return div;
}

function _renderRefs(refs: Ref[]): HTMLElement {
  const div = _el("div", "refs");
  div.appendChild(_el("strong", null, "refs: "));
  for (const ref of refs) {
    div.appendChild(_buildRefLink(ref));
    if (ref.reason) div.appendChild(_el("span", "ref-reason", " " + ref.reason + " "));
  }
  return div;
}

function _buildRefLink(ref: Ref): HTMLElement {
  const pr = _data.pr || ({} as PRBlock);
  const sha = pr.head_sha || pr.base_sha || "HEAD";
  const a = document.createElement("a");
  a.className = "ref-link";
  a.href = pr.repo
    ? `https://github.com/${pr.repo}/blob/${sha}/${ref.path}#L${ref.line}`
    : "#";
  a.target = "_blank";
  a.rel = "noopener";
  a.textContent = `${ref.path}:${ref.line}`;
  a.title = ref.reason || "";
  return a;
}

function _anySegmentOverridden(h: HunkBlock, toValue: boolean): boolean {
  return (h.segments || []).some((s) => {
    const val = _isFolded(s.id, _defaultSegmentFolded());
    return val === toValue;
  });
}

function _renderHunkHeader(h: HunkBlock, folded: boolean, f: FileBlock): HTMLElement {
  const hdr = _el("div", "hunk-header");
  hdr.appendChild(_chev(folded));
  hdr.appendChild(_el("span", "hunk-pos", h.header));
  let intent: HTMLElement;
  if (h.intent) {
    intent = _el("span", "hunk-intent", h.intent);
  } else if (_data.pending && !h._failed) {
    // Still streaming. Distinguish "queued, model hasn't looked yet"
    // (static, dim) from "running, model is working on it right now"
    // (pulse). State comes from the Progress module.
    const st = Progress.getHunkState(h.id);
    if (st === "running") {
      intent = _el("span", "hunk-intent pending", "analysing…");
    } else {
      intent = _el("span", "hunk-intent queued", "queued");
    }
  } else {
    intent = _el("span", "hunk-intent empty", "(no intent — may need re-run)");
  }
  hdr.appendChild(intent);
  const meta = _el("span", "hunk-meta");
  for (const sm of h.smells || []) meta.appendChild(_smellPill(sm, {
    smellId: `${h.id}:smell:${sm.tag}`,
    file: f.path, side: "new", line: h.new_start,
  }));
  if (h.context) {
    const icon = _el("span", "context-icon", "ⓘ");
    icon.title = h.context;
    meta.appendChild(icon);
  }
  const stars = _confidenceStars(h.confidence);
  if (stars) meta.appendChild(stars);
  hdr.appendChild(meta);
  hdr.addEventListener("click", (e) => {
    e.stopPropagation();
    _toggleFold(h.id, _defaultHunkFolded());
  });
  return hdr;
}

function _renderSegmentFolded(s: SegmentBlock, f: FileBlock): HTMLElement {
  const div = _el("div", "segment");
  div.dataset.id = s.id;
  div.appendChild(_chev(true));
  div.appendChild(_el("span", "segment-range", `+${s.new_start}..+${s.new_start + s.new_count - 1}`));
  div.appendChild(_el("span", s.intent ? "segment-intent" : "segment-intent empty", s.intent || "(no intent)"));
  for (const sm of s.smells || []) div.appendChild(_smellPill(sm, {
    smellId: `${s.id}:smell:${sm.tag}`,
    file: f.path, side: "new", line: s.new_start,
  }));
  div.addEventListener("click", (e) => {
    e.stopPropagation();
    _toggleFold(s.id, _defaultSegmentFolded());
  });
  return div;
}

function _renderHunkDiff(h: HunkBlock, file: FileBlock): HTMLElement {
  const cached = _state.renderedDiffs[h.id];
  if (cached) return cached;
  const container = _el("div", "diff");
  const halfOld = _el("div", "half half-old");
  const halfNew = _el("div", "half half-new");
  container.appendChild(halfOld);
  container.appendChild(halfNew);

  const rowElsOld: HTMLElement[] = [];
  const rowElsNew: HTMLElement[] = [];
  for (const row of h.rows || []) {
    const pair = _renderRow(row, file);
    (pair.old as { _scrPair?: HTMLElement })._scrPair = pair.new;
    (pair.new as { _scrPair?: HTMLElement })._scrPair = pair.old;
    halfOld.appendChild(pair.old);
    halfNew.appendChild(pair.new);
    rowElsOld.push(pair.old);
    rowElsNew.push(pair.new);
  }
  _attachLineNotes(rowElsOld, rowElsNew, h.rows || [], h.line_notes || [], h.id, file.path);
  // Record this hunk's rows so folds.ts can build a unified row stream
  // across the hunk and adjacent expanded context.
  FileRows.record(container, {
    rows: h.rows || [], oldEls: rowElsOld, newEls: rowElsNew,
  });
  _state.renderedDiffs[h.id] = container;
  return container;
}

function _attachLineNotes(
  rowElsOld: HTMLElement[], rowElsNew: HTMLElement[],
  rows: RowBlock[], notes: LineNote[],
  hunkId: string, filePath: string,
): void {
  if (!notes.length || !rows.length) return;
  const byNewLine = new Map<number, number>();
  for (let i = 0; i < rows.length; i++) {
    const ln = rows[i].new_line;
    if (ln !== null && ln !== undefined) byNewLine.set(ln, i);
  }
  for (const note of notes) {
    const idx = byNewLine.get(note.line);
    if (idx === undefined) continue;
    const noteId = `${hunkId}:line_note:${note.line}`;
    // If this line_note has already been promoted to a local comment,
    // skip rendering it — the comment now stands in its place. Keeps
    // a re-augment from resurrecting an observation the reviewer has
    // already turned into a comment.
    if (Comments.isPromoted(noteId)) continue;
    Annotations.attach({
      anchor: rowElsNew[idx],
      shadowAnchor: rowElsOld[idx],
      variant: "note",
      content: _buildLineNoteContent(note, noteId, filePath, rowElsNew[idx]),
      onInsert: (el) => { el.dataset.lineNoteId = noteId; },
    });
  }
}

/** Compose a line_note's annotation body: the LLM's text plus a small
 *  "Add as comment" affordance that hands the body to the comment
 *  editor pre-filled and anchored at the same row. */
function _buildLineNoteContent(
  note: LineNote, noteId: string, filePath: string, rowEl: HTMLElement,
): HTMLElement {
  const wrap = document.createElement("div");
  wrap.className = "line-note-body";
  const text = document.createElement("div");
  text.className = "line-note-text";
  text.textContent = note.body || "";
  wrap.appendChild(text);

  const actions = document.createElement("div");
  actions.className = "line-note-actions";
  const promote = document.createElement("button");
  promote.className = "comment-btn comment-btn-promote";
  promote.type = "button";
  promote.textContent = "Add as comment";
  promote.title = "Open the comment editor pre-filled with this observation";
  promote.addEventListener("click", (e) => {
    e.stopPropagation();
    Comments.openPromotionEditor({
      rowEl, side: "new", line: note.line,
      file: filePath, body: note.body || "",
      derivedFrom: noteId,
    });
  });
  actions.appendChild(promote);
  wrap.appendChild(actions);
  return wrap;
}

function _renderRow(row: RowBlock, file: FileBlock): { old: HTMLElement; new: HTMLElement } {
  const hasOld = row.old_line !== null && row.old_line !== undefined;
  const hasNew = row.new_line !== null && row.new_line !== undefined;
  // On a paired delete+insert, mark the changed characters on each side
  // so a small edit in an otherwise unchanged line stands out.
  let oldMarks: CharRange[] | undefined;
  let newMarks: CharRange[] | undefined;
  if (row.kind === "pair") {
    const d = charDiff(row.old_text, row.new_text);
    oldMarks = d.oldRanges;
    newMarks = d.newRanges;
  }
  const oldRow = _el("div", `row row-${row.kind}`);
  oldRow.appendChild(_renderLineno(row.old_line, "old", hasOld));
  oldRow.appendChild(_renderContent(row.old_text, "old", hasOld, file, oldMarks));
  const newRow = _el("div", `row row-${row.kind}`);
  newRow.appendChild(_renderLineno(row.new_line, "new", hasNew));
  newRow.appendChild(_renderContent(row.new_text, "new", hasNew, file, newMarks));
  return { old: oldRow, new: newRow };
}

function _renderLineno(line: number | null, side: "old" | "new", present: boolean): HTMLElement {
  const c = _el("span", `cell cell-lineno cell-lineno-${side}`);
  if (!present || line === null) {
    c.classList.add("empty");
    return c;
  }
  c.textContent = String(line);
  return c;
}

function _renderContent(
  text: string,
  side: "old" | "new",
  present: boolean,
  file: FileBlock,
  markRanges?: CharRange[],
): HTMLElement {
  const c = _el("span", `cell cell-content cell-content-${side}`);
  if (!present) {
    c.classList.add("empty");
    return c;
  }
  const code = _el("code", "hljs");
  const lang = file && file.language;
  const hljs = (window as unknown as {
    hljs?: { highlight(text: string, opts: { language: string; ignoreIllegals: boolean }): { value: string } };
  }).hljs;
  if (hljs && lang) {
    try {
      code.innerHTML = hljs.highlight(text || " ", { language: lang, ignoreIllegals: true }).value;
    } catch (_) {
      code.textContent = text;
    }
  } else {
    code.textContent = text;
  }
  // Paint the intra-line change marks over the (possibly highlighted)
  // text. Offsets are over the raw line, which highlight.js preserves.
  if (markRanges && markRanges.length) wrapRanges(code, markRanges, "char-chg");
  c.appendChild(code);
  return c;
}

// --- Severity color ----------------------------------------------------

const _SEV_ORDER: Record<string, number> = {
  info: 1, minor: 2, major: 3, critical: 4,
};

function _maxSeverityColor(h: HunkBlock): string {
  let worst = 0;
  let color = "var(--border)";
  const check = (sm: Smell): void => {
    const def = _smells[sm.tag];
    if (!def) return;
    const s = _SEV_ORDER[def.severity] || 0;
    if (s > worst) { worst = s; color = def.color; }
  };
  for (const sm of h.smells || []) check(sm);
  for (const seg of h.segments || []) for (const sm of seg.smells || []) check(sm);
  return color;
}

// --- Slider / status / hash / keyboard ---------------------------------

function _updateSliderButtons(): void {
  document.querySelectorAll(".fold-slider button").forEach((b) => {
    const btn = b as HTMLElement;
    btn.classList.toggle("active", btn.dataset.fold === _state.fold);
  });
}

function _updateStatus(): void {
  const s = document.getElementById("status-bar");
  if (!s) return;
  let smells = 0, critical = 0;
  for (const f of _data.files) {
    for (const h of f.hunks) {
      for (const sm of h.smells || []) {
        smells++;
        if ((_smells[sm.tag] || {} as SmellCatalogueEntry).severity === "critical") critical++;
      }
      for (const seg of h.segments || []) {
        for (const sm of seg.smells || []) {
          smells++;
          if ((_smells[sm.tag] || {} as SmellCatalogueEntry).severity === "critical") critical++;
        }
      }
    }
  }
  s.textContent = `${_data.files.length} files · ${smells} smells · ${critical} critical · keys 1-4 fold · space toggle · ? help`;
}

function _syncHash(): void {
  const parts = [`fold=${_state.fold}`];
  for (const [id, folded] of Object.entries(_state.overrides)) {
    parts.push(`${id}=${folded ? "f" : "o"}`);
  }
  const newHash = "#" + parts.join("&");
  if (window.location.hash !== newHash) {
    history.replaceState(null, "", newHash);
  }
}

function _restoreHash(): void {
  const h = window.location.hash.slice(1);
  if (!h) return;
  for (const kv of h.split("&")) {
    const [k, v] = kv.split("=");
    if (k === "fold" && ["files", "hunks", "segments", "off"].includes(v)) {
      _state.fold = v as FoldMode;
    } else if (k && v != null) {
      _state.overrides[k] = (v === "f");
    }
  }
}

function _onKeydown(e: KeyboardEvent): void {
  const target = e.target as HTMLElement | null;
  const tag = ((target && target.tagName) || "").toLowerCase();
  if (tag === "input" || tag === "textarea") return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  switch (e.key) {
    case "1": _setGlobalFold("files"); e.preventDefault(); break;
    case "2": _setGlobalFold("hunks"); e.preventDefault(); break;
    case "3": _setGlobalFold("segments"); e.preventDefault(); break;
    case "4": _setGlobalFold("off"); e.preventDefault(); break;
    case "?": _toggleHelp(); e.preventDefault(); break;
    case "Escape": _closeHelp(); break;
  }
}

function _toggleHelp(): void {
  const o = document.getElementById("help-overlay");
  if (o) o.classList.toggle("hidden");
}
function _closeHelp(): void {
  const o = document.getElementById("help-overlay");
  if (o) o.classList.add("hidden");
}

function _wireInputs(): void {
  document.querySelectorAll(".fold-slider button").forEach((b) => {
    const btn = b as HTMLElement;
    btn.addEventListener("click", () => {
      const f = btn.dataset.fold as FoldMode | undefined;
      if (f) _setGlobalFold(f);
    });
  });
  const reset = document.getElementById("reset-btn");
  if (reset) {
    reset.addEventListener("click", () => {
      _state.overrides = Object.create(null);
      render();
    });
  }
  const help = document.getElementById("help-btn");
  if (help) help.addEventListener("click", _toggleHelp);
  const overlay = document.getElementById("help-overlay");
  if (overlay) overlay.addEventListener("click", (e) => {
    if (e.target === overlay) _closeHelp();
  });
  document.addEventListener("keydown", _onKeydown);
  window.addEventListener("hashchange", () => {
    _state.overrides = Object.create(null);
    _restoreHash();
    render();
  });
}

// --- Public surface -----------------------------------------------------

export const Render = {
  init: renderInit,
  render,
  renderHunkReplace,
  repaintHunkHeader,
  clearRenderedDiffCache,
};
