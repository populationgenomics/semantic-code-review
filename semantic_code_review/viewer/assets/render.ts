// Diff renderer + fold-state machinery.
//
// Owns the layout pass that turns DATA into the on-page DOM: PR
// panel, file blocks, hunk headers, the side-by-side row grid, gap
// chips for unchanged context, the centre gutter's span bars and text, the label tree
// a collapsed fold shows, refs, smell pills. Carries the fold state too
// (STATE.fold / overrides / renderedDiffs cache) because all of that
// exists to feed the renderer, and binds the user inputs that drive it
// (fold-slider buttons, keyboard 1-3, hash sync).
//
// Other modules attach to surfaces this module creates:
//   - sidebar.ts mutates pill state but reads from .file / .hunk
//   - folds.ts attaches chevrons to the per-half row elements stashed
//     on each .diff and .gap-expansion container; this module hands it
//     the labels a collapsed region shows (`_foldLabels`)
//   - comments.ts replays its comment rows after each renderAll
//   - annotations.ts hosts the row-annotation DOM
//   - explainer_panel.ts hosts a second render of one file, beside the
//     document; it takes the per-file renderer from here as a callback,
//     so the dependency runs one way
//   - console.ts owns the transcript drawer, which this module's Esc
//     chain collapses (render.ts → console.ts, as boot.ts wires it)

import { Annotations } from "./annotations";
import { Comments } from "./comments";
import { Console } from "./console";
import { Explainer } from "./explainer";
import { ExplainerPanel, type PanelHost } from "./explainer_panel";
import { FileRows } from "./file_rows";
import { FileTextCache, type FileText } from "./file_text";
import { Folds, type FoldLabels } from "./folds";
import { Progress } from "./progress";
import { Rendered, type PaneState } from "./rendered";
import { Sidebar } from "./sidebar";
import { blockDiff, matchRanges, wrapRanges, type CharRange } from "./text_highlight";

// --- Module state --------------------------------------------------------

// The collapse ladder (ADR 0008): the diff's own units at decreasing
// detail — file headers, hunk headers, code. `code` is the bottom rung,
// where the three affordances live: the expand chip hides, the definition
// chevron folds, the span labels.
type FoldMode = "files" | "hunks" | "code";
const _FOLD_MODES: readonly FoldMode[] = ["files", "hunks", "code"];

// Which renderer owns the main pane. Orthogonal to FoldMode, not a
// fifth value of it (ADR 0007): overview mode hides nothing, it
// replaces the pane, so `fold` and the per-item overrides are left
// exactly as the reviewer set them and the return trip needs no latch.
type ViewMode = "diff" | "overview";

interface RenderState {
  fold: FoldMode;
  mode: ViewMode;
  overrides: Record<string, boolean>;
  renderedDiffs: Record<string, HTMLElement>;
  rendered: PaneState;
  /** The focus: the hunks the last focus gesture asked to see — a sidebar
   *  pill click, or the explainer's "Open in diff" — or null. A focused
   *  hunk renders open to its code, and its file open, whatever the
   *  level (ADR 0008: the one reveal that unfolds). Ephemeral: the slider
   *  and entering overview mode clear it; it is never an override, so it
   *  neither reaches the URL hash nor outlives the gesture. */
  focus: ReadonlySet<string> | null;
}

let _data: ViewerData = { version: "1", pr: {} as PRBlock, smells_catalogue: {}, files: [], groups: [], symbols: [] };
let _smells: Record<string, SmellCatalogueEntry> = {};
// The focused symbol's name, highlighted search-style across every diff
// line, or null when no symbol pill is active. Newly rendered cells pick
// it up in _renderContent; setSymbolSearch repaints cells already in the
// DOM. See setSymbolSearch / sidebar's active-pill callback.
let _symbolSearch: string | null = null;
// Whether the centre gutter is folded to `lineno · bars` — its text column
// zero wide, the rationales hidden, the bars and dots still showing where
// every span is. A reading preference like the fold level, global across
// files and panes, kept in localStorage rather than the hash: it is the
// reader's, not the link's. Default expanded.
let _gutterCollapsed = false;
const _GUTTER_FOLD_KEY = "scr-gutter-fold";
// Whether the change explainer exists for this review (`--no-augment`
// and `[augment].explainer = false` both switch it off server-side).
// Gates the mode entirely: with it false there is no button, and a
// `mode=overview` left in the URL is ignored rather than painting a
// pane whose only control 409s.
let _explainerEnabled = false;
// Set by renderInit. A repaint asked for before it would paint the
// empty default diff and, worse, sync the hash from default state,
// overwriting the fold level and mode the URL carries. Nothing is lost
// by dropping it — renderInit paints, so an early request is coalesced
// into that one. The explainer's boot-time load is what gets here
// first: its repaint hook is wired before the renderer.
let _initialised = false;
const _state: RenderState = {
  fold: "hunks",
  mode: "diff",
  overrides: Object.create(null),
  renderedDiffs: Object.create(null),
  rendered: Rendered.newPaneState(),
  focus: null,
};

// What a render pass reads its per-item state from, and where a fold
// click in it repaints. Two exist: the diff pane's, and the
// overview-mode detail panel's.
interface PaneScope {
  /** Per-item fold overrides, over the collapse level's defaults. The
   *  panel keeps its own: a reference read beside the document must not
   *  move the folds waiting in the diff (ADR 0007's free return trip). */
  overrides: Record<string, boolean>;
  /** Cache of live per-hunk `.diff` nodes, or null for a pass that must
   *  build fresh — a node cannot be in two trees, so the panel neither
   *  reads nor writes the diff pane's. */
  cache: Record<string, HTMLElement> | null;
  /** Whether the sidebar's hunk filter applies. It does not in the
   *  panel: a reference addresses a file whether or not the filter the
   *  reviewer left in the diff covers it. */
  filtered: boolean;
  /** The hunks a focus gesture opened (`RenderState.focus`), or null.
   *  The panel has none: `openReference` seeds its overrides instead. */
  focus: ReadonlySet<string> | null;
  /** Rendered mode's view state (ADR 0004) for this pane: which `.md`
   *  files are flipped, and their fold level / reveals. Per-pane for the
   *  same reason `overrides` is. */
  rendered: PaneState;
  repaint: () => void;
}

/** The diff pane's scope. Built per pass rather than held, because a
 *  reset reassigns `_state.overrides`; every handler carrying a scope is
 *  rebuilt by the render that follows. */
function _diffScope(): PaneScope {
  return {
    overrides: _state.overrides,
    cache: _state.renderedDiffs,
    filtered: true,
    focus: _state.focus,
    rendered: _state.rendered,
    repaint: render,
  };
}

// The detail panel's scope. `openReference` seeds the overrides per
// reference; a fold click inside the panel repaints the panel alone.
const _panelScope: PaneScope = {
  overrides: Object.create(null),
  cache: null,
  filtered: false,
  focus: null,
  rendered: Rendered.newPaneState(),
  repaint: () => ExplainerPanel.repaint(),
};

function _isFocused(scope: PaneScope, hunkId: string): boolean {
  return scope.focus !== null && scope.focus.has(hunkId);
}

// --- Public API ----------------------------------------------------------

/** Wire input handlers + restore state from URL hash + run initial
 *  render. Called once at boot from viewer.js. Resets the rendered-
 *  diff cache + fold overrides so a re-boot (tests, future hot
 *  reload) starts fresh. */
function renderInit(data: ViewerData): void {
  _data = data;
  _smells = data.smells_catalogue || {};
  _explainerEnabled = data.explainer === true;
  _initialised = true;
  _state.fold = "hunks";
  _state.mode = _initialMode();
  _state.overrides = Object.create(null);
  _state.renderedDiffs = Object.create(null);
  _state.rendered = Rendered.newPaneState();
  // A filter restored from localStorage is not a gesture: the diff opens
  // at its level, filtered, with nothing focused.
  _state.focus = null;
  _applyGutterFold(_readGutterFold());
  _wireInputs();
  _restoreHash();
  render();
  // Being in the mode is what queues the sections a document left
  // pending, however the mode was reached — the alternative is a
  // default-opened document that sits unwritten behind per-section
  // buttons. Never the skeleton: with no document `generateAllPending`
  // does nothing, and buying one stays on the press.
  if (_state.mode === "overview") Explainer.generateAllPending();
}

/** The mode the viewer opens in, before the URL hash gets a say.
 *
 *  A document that already exists is the natural first screen: it is
 *  what the reviewer generated last time, and showing it spends nothing
 *  (ADR 0007's addendum — a cache hit adds no budget). With no document
 *  the diff stays the default, because entering the mode is what buys
 *  the skeleton and that spend is the reviewer's to initiate. */
function _initialMode(): ViewMode {
  return _explainerEnabled && Explainer.hasDocument() ? "overview" : "diff";
}

/** Re-render the entire app DOM. Cheap-ish — STATE.renderedDiffs
 *  caches the per-hunk .diff so this isn't quadratic on revisits. */
function render(): void {
  if (!_initialised) return;
  const app = document.getElementById("app");
  if (!app) return;
  if (_state.mode === "overview") {
    _renderOverviewMode(app);
    return;
  }
  // The panel is a cell of the overview split, so leaving the mode takes
  // it with the split.
  ExplainerPanel.unmount();
  app.innerHTML = "";
  Sidebar.setSectionTree(null);
  const scope = _diffScope();
  app.appendChild(_renderPRPanel(_data.pr));
  for (const f of _data.files) {
    const el = _renderFile(f, scope);
    if (el) app.appendChild(el);   // focused render drops files with no surviving hunk
  }
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
 *  entry first so attachLineNotes / fold placement re-run against
 *  the (possibly different) row set. Called from the SSE patchers
 *  in viewer.js when a `hunk` event arrives.
 *
 *  A no-op in overview mode, which has no diff pane: the only `.hunk` on
 *  the page is the detail panel's, rendered off its own scope and
 *  outside the node cache, and patching it here would move a cached node
 *  into it. The panel picks the new annotation up when the reference is
 *  opened again. */
function renderHunkReplace(file: FileBlock, hunkIdx: number): void {
  if (_state.mode === "overview") return;
  const h = file.hunks[hunkIdx];
  if (!h) return;
  delete _state.renderedDiffs[h.id];
  // Under an active filter the file body is the focused merged diff
  // (header-less .hunk wrappers), not the normal hunk layout — a
  // surgical swap would inject a full hunk header. Fall back to a full
  // re-render, which rebuilds the focused body correctly.
  if (Sidebar.activeHunkIds() !== null) { render(); return; }
  const fresh = _renderHunk(h, file, _diffScope());
  const existing = document.querySelector(
    '.hunk[data-id="' + _cssEscape(h.id) + '"]',
  );
  if (existing && existing.parentNode) {
    existing.parentNode.replaceChild(fresh, existing);
  }
}

/** Re-render just the header of one hunk (intent slot + meta).
 *  Used by the hunk-start SSE handler to flip the "queued"
 *  placeholder to "analysing…" without rebuilding the diff body.
 *  Skipped in overview mode, for the reason renderHunkReplace is. */
function repaintHunkHeader(hunkId: string): void {
  if (_state.mode === "overview") return;
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
  const scope = _diffScope();
  const fresh = _renderHunkHeader(h, _hunkFolded(scope, h), f, scope);
  oldHdr.replaceWith(fresh);
}

/** Drop the cached `.diff` element for a hunk. Called by SSE
 *  patchers before they replace the surrounding hunk DOM. */
function clearRenderedDiffCache(hunkId: string): void {
  delete _state.renderedDiffs[hunkId];
}

// --- Fold state ---------------------------------------------------------

function _defaultFileFolded(): boolean    { return _state.fold === "files"; }
/** An open hunk is its code; only `code` opens hunks. */
function _defaultHunkFolded(): boolean    { return _state.fold !== "code"; }

/** A hunk's visible fold state. A focus opens the hunk to its code below
 *  the level's default; an explicit override the reviewer set still wins
 *  over both. */
function _hunkFolded(scope: PaneScope, h: HunkBlock): boolean {
  return _isFolded(scope, h.id, _isFocused(scope, h.id) ? false : _defaultHunkFolded());
}
/** A file opens for a focused hunk in it: the code asked for must be on
 *  screen at `files` too. */
function _fileFolded(scope: PaneScope, f: FileBlock): boolean {
  const focused = f.hunks.some((h) => _isFocused(scope, h.id));
  return _isFolded(scope, f.id, focused ? false : _defaultFileFolded());
}

function _isFolded(scope: PaneScope, id: string, fallback: boolean): boolean {
  return Object.prototype.hasOwnProperty.call(scope.overrides, id)
    ? scope.overrides[id] : fallback;
}

function _toggleFold(scope: PaneScope, id: string, currentDefault: boolean): void {
  const current = _isFolded(scope, id, currentDefault);
  scope.overrides[id] = !current;
  scope.repaint();
}

/** Pick a collapse level, from the slider or keys 1-3.
 *
 *  In overview mode this also leaves the mode: the document is not shown
 *  at a collapse level, so a reviewer reaching for the zoom while reading
 *  it is asking for the diff at that zoom — ADR 0007's "read the
 *  document, drop into the ladder" loop. `setMode` is the same exit the
 *  Overview button runs, and it repaints. */
function _setGlobalFold(fold: FoldMode): void {
  _state.fold = fold;
  _state.overrides = Object.create(null);
  // The slider is authoritative: every hunk, focused or not, folds to
  // this level.
  _state.focus = null;
  if (_state.mode === "overview") {
    setMode("diff");
    return;
  }
  render();
}

/** Paint the explainer's document into the main pane and swap the
 *  sidebar to its section tree.
 *
 *  The pane is a split: the document, and the detail panel a reference
 *  opens beside it. Only the document half is written here — an open
 *  panel survives every repaint of the prose, and replays its own
 *  comments and annotations when its content changes.
 *
 *  `_state.fold` and `_state.overrides` are not touched, so leaving the
 *  mode restores the reviewer's zoom and their hand-set folds exactly. */
function _renderOverviewMode(app: HTMLElement): void {
  ExplainerPanel.mount(app, Explainer.renderPane(), _PANEL_HOST);
  Sidebar.setSectionTree({
    sections: Explainer.sections(),
    activeId: Explainer.activeSectionId(),
    onPick: (id) => Explainer.setActiveSection(id),
    statusOf: (id) => Explainer.sectionStatus(id),
  });
  Sidebar.render();
  _syncHash();
  _updateSliderButtons();
}

function setMode(mode: ViewMode): void {
  if (mode === "overview" && !_explainerEnabled) return;
  if (_state.mode === mode) return;
  _state.mode = mode;
  // Entering the mode clears the focus, as the slider does: it belongs
  // to a gesture the reviewer is stepping away from, and must not
  // survive the round trip.
  if (mode === "overview") _state.focus = null;
  render();
}

function mode(): ViewMode {
  return _state.mode;
}

/** Focus `hunkIds` in the diff: leave overview mode if in it, render them
 *  open to their code with their files open, and scroll to the first.
 *  The one reveal that unfolds (ADR 0008). Ephemeral — the slider folds
 *  them back to level, and nothing is written to the overrides or the
 *  hash. */
function _focus(hunkIds: Iterable<string>, scrollTo: string): void {
  setMode("diff");
  _state.focus = new Set(hunkIds);
  render();
  const el = document.querySelector('[data-id="' + _cssEscape(scrollTo) + '"]');
  if (el) el.scrollIntoView({ block: "start" });
}

/** The file a reference addresses. References are validated by
 *  membership server-side, so an id that resolves to nothing here is a
 *  bug rather than something a reviewer can cause. */
function _fileOfRef(ref: ExplainerRef): FileBlock {
  const idx = Number(ref.id.replace(/^[FH]/, "").split("_")[0]);
  const file = _data.files && _data.files[idx];
  if (!file) throw new Error(`reference ${ref.id} addresses no file in this diff`);
  return file;
}

/** "Open in diff" on a reference is a focus: the reader has seen it at
 *  the collapse level in the panel and is asking for the lines. A hunk
 *  reference focuses that hunk; a file reference focuses the file's
 *  hunks, as the Files-axis pill does. */
function focusRef(ref: ExplainerRef): void {
  const file = _fileOfRef(ref);
  if (ref.kind === "file") _focus(file.hunks.map((h) => h.id), file.id);
  else _focus([ref.id], ref.id);
}

// What the detail panel asks of the renderer. Injected rather than
// imported the other way, so explainer_panel.ts stays free of the
// renderer that hosts it.
const _PANEL_HOST: PanelHost = {
  renderFile: (ref) => {
    const el = _renderFile(_fileOfRef(ref), _panelScope);
    // `_renderFile` drops a file only under a sidebar filter, and the
    // panel's scope has none.
    if (el === null) throw new Error(`reference ${ref.id} rendered nothing`);
    return el;
  },
  openInDiff: focusRef,
};

/** A reference in the document was clicked.
 *
 *  In overview mode it opens beside the document rather than in place of
 *  it: the reader checks the code with the sentence that sent them there
 *  still on screen, and the document column keeps its DOM and its scroll
 *  position. Elsewhere — a reference reachable with no document painted
 *  — it is the jump the panel's "Open in diff" also runs. */
function openReference(ref: ExplainerRef): void {
  if (_state.mode !== "overview") {
    focusRef(ref);
    return;
  }
  const file = _fileOfRef(ref);
  const overrides: Record<string, boolean> = Object.create(null);
  // The file itself always opens: a panel showing a folded header shows
  // nothing. Under it the collapse level still holds, except for the
  // hunk a hunk reference names — that reference is the claim "read
  // these lines", so it opens to its code, as a focus does in the diff.
  overrides[file.id] = false;
  if (ref.kind === "hunk") overrides[ref.id] = false;
  _panelScope.overrides = overrides;
  ExplainerPanel.open(ref);
}

/** A sidebar pill was clicked. The pill's hunks become the focus — the
 *  filter narrows the diff to them, and the click is the request to see
 *  their code; "show all" (no pill) clears it. Boot wires this to the
 *  sidebar's onFilterChange. */
function applyFilterChange(): void {
  _state.focus = Sidebar.activeHunkIds();
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

/** Render one file. Returns null only in the focused view for a file no
 *  surviving hunk touches (the caller drops it). A file body is an
 *  alternating sequence of live hunks and collapsible regions; which
 *  hunks are live depends on the active filter (see _renderFileBody). */
function _renderFile(f: FileBlock, scope: PaneScope): HTMLElement | null {
  const liveIds = scope.filtered ? Sidebar.activeHunkIds() : null;   // null → every hunk is live
  if (liveIds !== null && !f.hunks.some((h) => liveIds.has(h.id))) return null;
  const div = _el("div", "file");
  if (liveIds !== null) div.classList.add("filtered");
  div.dataset.id = f.id;
  const folded = _fileFolded(scope, f);
  div.classList.toggle("folded", folded);
  div.appendChild(_renderFileHeader(f, folded, scope));
  if (!folded) {
    const body = _el("div", "file-body");
    if (Rendered.isOn(scope.rendered, f.id)) {
      // Rendered markdown mode is a separate body renderer: no diff
      // overview, no fold chevrons — just the rendered prose.
      Rendered.renderBody(scope.rendered, body, f, scope.repaint);
      div.appendChild(body);
    } else {
      const overview = _renderFileOverview(f);
      if (overview) body.appendChild(overview);
      _setGutterWidths(body, f);
      _renderFileBody(body, f, liveIds, scope);
      div.appendChild(body);
      // Run a file-level fold pass once the body is assembled.
      attachFileFolds(div, f);
    }
  }
  return div;
}

/** Lay out a file body as live hunks separated by collapsible regions.
 *  `liveIds` is the set of hunks rendered as full hunks (header + diff);
 *  null means every hunk is live (the normal, unfiltered view). Each
 *  region — before, between, and after the live hunks — folds its
 *  unchanged context and, under a filter, the demoted (non-live) hunks
 *  into one "expand" chip that opens to a continuous diff. A hunk and a
 *  demoted region are the same diff-row stream; the only difference is
 *  the chrome around it (an explanatory header vs. a bare collapse). */
function _renderFileBody(
  body: HTMLElement, f: FileBlock, liveIds: Set<string> | null, scope: PaneScope,
): void {
  const isLive = (h: HunkBlock): boolean => liveIds === null || liveIds.has(h.id);
  let curNew = 1;
  let curOld = 1;
  let emittedLive = false;

  const flush = (newEnd: number | null, position: "top" | "between" | "bottom"): void => {
    const demoted = f.hunks.filter(
      (h) => !isLive(h) && h.new_start >= curNew && (newEnd === null || h.new_start <= newEnd),
    );
    const region: DiffRegion = { position, newStart: curNew, oldStart: curOld, newEnd, demoted };
    if (_regionCount(region) === 0) return;
    body.appendChild(_renderRegionChip(f, region));
  };

  for (const h of f.hunks.filter(isLive)) {
    flush(h.new_start - 1, emittedLive ? "between" : "top");
    body.appendChild(_renderHunk(h, f, scope));
    emittedLive = true;
    curNew = h.new_start + h.new_count;
    curOld = h.old_start + h.old_count;
  }
  flush(f.head_line_count, "bottom");
}

function _renderFileHeader(f: FileBlock, folded: boolean, scope: PaneScope): HTMLElement {
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
  if (Rendered.isMarkdown(f)) hdr.appendChild(_renderMdToggle(f, scope));
  hdr.addEventListener("click", () => _toggleFold(scope, f.id, folded));
  return hdr;
}

/** Per-file toggle flipping a markdown file between the text diff and
 *  rendered mode. stopPropagation keeps the click off the header's
 *  fold handler; the toggle fetches (if needed) then re-renders. */
function _renderMdToggle(f: FileBlock, scope: PaneScope): HTMLElement {
  const on = Rendered.isOn(scope.rendered, f.id);
  const btn = _el("button", "md-toggle");
  btn.textContent = on ? "Diff" : "Rendered";
  btn.title = on ? "Show the text diff" : "Show the rendered markdown";
  btn.setAttribute("aria-pressed", on ? "true" : "false");
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    void Rendered.toggle(scope.rendered, f, scope.repaint);
  });
  return btn;
}

function _uniqueFileSmells(f: FileBlock): string[] {
  const s = new Set<string>();
  for (const h of f.hunks) {
    for (const sm of h.smells || []) s.add(sm.tag);
    for (const span of h.spans || []) for (const sm of span.smells || []) s.add(sm.tag);
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

// --- Collapsible diff regions -------------------------------------------
//
// A region is the folded counterpart of a hunk: the same diff-row stream,
// shown behind an "expand" chip instead of a hunk header. In the normal
// view a region holds only unchanged context between hunks; under a
// filter it also swallows the demoted (non-live) hunks, so their changes
// render inline when expanded.
//
// A region is laid out from the hunks' coordinates alone — its span and
// its row count need no text — and reads its unchanged lines from the
// file's full source (FileTextCache) when its chip is clicked. The diff
// carries hunks and their context; the rest of the file is fetched the
// first time a reviewer asks for any of it, whatever the file's size.

interface DiffRegion {
  position: "top" | "between" | "bottom";
  /** The region's first line, per side. The two advance together over
   *  unchanged context; a demoted hunk resets them to its own end. */
  newStart: number;
  oldStart: number;
  /** Last post-image line of the trailing context, inclusive. Null when
   *  the post-image's length is unknown (no head): nothing trails the
   *  last demoted hunk. */
  newEnd: number | null;
  /** Non-live hunks folded into the region, in file order. */
  demoted: HunkBlock[];
}

/** Visit a region's row stream in file order — unchanged context one
 *  (old, new) line pair at a time, demoted hunks as themselves. The one
 *  walk behind both the chip's count and the expansion's rows, so the
 *  two agree. */
function _walkRegion(
  region: DiffRegion, ctx: (oldLine: number, newLine: number) => void, hunk: (h: HunkBlock) => void,
): void {
  let cn = region.newStart;
  let co = region.oldStart;
  const ctxTo = (upTo: number): void => {
    while (cn < upTo) { ctx(co, cn); co++; cn++; }
  };
  for (const h of region.demoted) {
    ctxTo(h.new_start);
    hunk(h);
    cn = h.new_start + h.new_count;
    co = h.old_start + h.old_count;
  }
  if (region.newEnd !== null) ctxTo(region.newEnd + 1);
}

function _regionCount(region: DiffRegion): number {
  let n = 0;
  _walkRegion(region, () => { n++; }, (h) => { n += (h.rows || []).length; });
  return n;
}

/** The row stream for a region, its unchanged context read from the
 *  post-image. Unchanged lines are identical on both sides, so the head
 *  text serves every file that has one; a file with none (deleted) has
 *  nothing unchanged to disclose — its diff already carries every base
 *  line as a `del` row — so a region needing text the file lacks is an
 *  error, not a reason to read the other side.
 *
 *  Throws when the text is missing or shorter than the region: a
 *  dirty-tree review's head is the live checkout, and a file edited
 *  under the review may no longer reach the lines the diff recorded. */
function _regionRows(
  f: FileBlock, region: DiffRegion, text: FileText,
): { rows: RowBlock[]; marks: (_RowMarks | undefined)[] } {
  if (text.head === null) throw new Error(`${f.path}: no post-image text to expand`);
  const lines = FileTextCache.splitLines(text.head);
  const rows: RowBlock[] = [];
  const marks: (_RowMarks | undefined)[] = [];
  _walkRegion(region, (co, cn) => {
    if (cn > lines.length) {
      throw new Error(`${f.path}: line ${cn} is past the end of the file (${lines.length} lines)`);
    }
    const t = lines[cn - 1];
    rows.push({ kind: "ctx", old_line: co, new_line: cn, old_text: t, new_text: t });
    marks.push(undefined);
  }, (h) => {
    const hr = h.rows || [];
    const hm = _blockMarks(hr);
    for (let i = 0; i < hr.length; i++) { rows.push(hr[i]); marks.push(hm[i]); }
  });
  return { rows, marks };
}

/** The chip standing in for a region. A click expands it in place; when
 *  the file's text is not yet cached the chip fetches it first, showing
 *  the wait, and on failure says so and takes the click again. The
 *  fetch is the chip's, never the render pass's. */
function _renderRegionChip(f: FileBlock, region: DiffRegion): HTMLElement {
  const chip = _el("div", "gap-chip");
  const count = _regionCount(region);
  const icon = region.position === "top" ? "⬆" : region.position === "bottom" ? "⬇" : "⋯";
  const word = count === 1 ? "line" : "lines";
  const label = region.position === "top" ? `expand ${count} ${word} above`
              : region.position === "bottom" ? `expand ${count} ${word} below`
              : `expand ${count} hidden ${word}`;
  const labelEl = _el("span", "gap-label", label);
  chip.appendChild(_el("span", "gap-icon", icon));
  chip.appendChild(labelEl);
  const fail = (e: unknown): void => {
    chip.classList.remove("loading");
    chip.classList.add("failed");
    const why = e instanceof Error ? e.message : String(e);
    labelEl.textContent = `${label} — could not load: ${why} (click to retry)`;
  };
  const expand = (text: FileText): void => {
    let expansion: HTMLElement;
    try {
      expansion = _renderRegionExpansion(f, region, text);
    } catch (e) {
      fail(e);
      return;
    }
    chip.replaceWith(expansion);
    _refreshFileFolds(expansion, f);
  };
  chip.addEventListener("click", () => {
    if (chip.classList.contains("loading")) return;
    const cached = FileTextCache.cached(f.id);
    if (cached) { expand(cached); return; }
    chip.classList.remove("failed");
    chip.classList.add("loading");
    labelEl.textContent = `${label} — loading…`;
    FileTextCache.load(f).then(expand, fail);
  });
  return chip;
}

function _renderRegionExpansion(f: FileBlock, region: DiffRegion, text: FileText): HTMLElement {
  const { rows, marks } = _regionRows(f, region, text);
  const container = _el("div", "gap-expansion");
  const collapse = _el("button", "gap-collapse", "× collapse");
  collapse.title = "Hide these lines again";
  collapse.addEventListener("click", () => {
    const chip = _renderRegionChip(f, region);
    container.replaceWith(chip);
    _refreshFileFolds(chip, f);
  });
  container.appendChild(collapse);
  const { diff, oldEls, newEls } = _renderDiffRows(f, rows, marks);
  // The file-level fold walker (folds.ts) recovers the row stream + DOM
  // elements from the container.
  FileRows.record(container, { rows, oldEls, newEls });
  container.appendChild(diff);
  return container;
}

/** Render a diff-row stream into a `.diff` grid, pairing old/new rows.
 *  The single primitive behind both hunk bodies and region expansions —
 *  what differs between them is only the surrounding chrome. */
function _renderDiffRows(
  f: FileBlock, rows: RowBlock[], marks: (_RowMarks | undefined)[],
): { diff: HTMLElement; oldEls: HTMLElement[]; newEls: HTMLElement[] } {
  const diff = _el("div", "diff");
  const live = _liveSide(rows);
  if (live) diff.classList.add(`diff-only-${live}`);
  const halfOld = _el("div", "half half-old");
  const halfNew = _el("div", "half half-new");
  diff.appendChild(halfOld);
  diff.appendChild(halfNew);
  const oldEls: HTMLElement[] = [];
  const newEls: HTMLElement[] = [];
  for (let i = 0; i < rows.length; i++) {
    const pair = _renderRow(rows[i], f, marks[i]?.old, marks[i]?.new);
    (pair.old as { _scrPair?: HTMLElement })._scrPair = pair.new;
    (pair.new as { _scrPair?: HTMLElement })._scrPair = pair.old;
    halfOld.appendChild(pair.old);
    halfNew.appendChild(pair.new);
    oldEls.push(pair.old);
    newEls.push(pair.new);
  }
  return { diff, oldEls, newEls };
}

/** The side carrying every row of a stream, when the other side is empty
 *  for the stream's whole length — an added file's rows have no
 *  pre-image, a deleted file's no post-image. Null when the stream has
 *  content on both sides (or on neither), which is when both halves are
 *  worth their width.
 *
 *  Read off the rows rather than the file's role: the role speaks for the
 *  file, this speaks for the grid being built, and a region expansion is
 *  a stream of its own. */
function _liveSide(rows: RowBlock[]): "old" | "new" | null {
  let anyOld = false;
  let anyNew = false;
  for (const r of rows) {
    if (r.old_line !== null && r.old_line !== undefined) anyOld = true;
    if (r.new_line !== null && r.new_line !== undefined) anyNew = true;
    if (anyOld && anyNew) return null;
  }
  if (anyNew) return "new";
  if (anyOld) return "old";
  return null;
}

/** Re-run the file-level fold pass over the `.file` enclosing `el` —
 *  that pane's copy of the file, so a region expanded in the explainer's
 *  panel never re-folds the diff pane's. A no-op for a node a repaint
 *  has since detached. */
function _refreshFileFolds(el: HTMLElement, f: FileBlock): void {
  const fileEl = el.closest(".file") as HTMLElement | null;
  if (fileEl) attachFileFolds(fileEl, f);
}

// --- Hunk + diff body ---------------------------------------------------

function _renderHunk(h: HunkBlock, f: FileBlock, scope: PaneScope): HTMLElement {
  const div = _el("div", "hunk");
  div.dataset.id = h.id;
  const folded = _hunkFolded(scope, h);
  div.classList.toggle("folded", folded);
  div.appendChild(_renderHunkHeader(h, folded, f, scope));
  if (!folded) {
    div.appendChild(_renderHunkDiff(h, f, scope));
    if (h.context) {
      const c = _el("div", "context-note");
      c.innerHTML = `<strong>context:</strong> ${_esc(h.context)}`;
      div.appendChild(c);
    }
    if (h.refs && h.refs.length) {
      div.appendChild(_renderRefs(h.refs));
    }
    // Spans attach to their rows when the code is shown: _attachSpans()
    // in _renderHunkDiff.
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

function _renderHunkHeader(
  h: HunkBlock, folded: boolean, f: FileBlock, scope: PaneScope,
): HTMLElement {
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
    // Flip the visible state — `folded` is the actual current state
    // (respecting focus + overrides), not just the level default.
    _toggleFold(scope, h.id, folded);
  });
  return hdr;
}

// --- The label tree: what a collapsed fold shows ----------------------------
//
// The labels on the rows a fold hid (ADR 0008: folding a region shows its
// label): every definition touched within them (a `FileBlock.fold_regions`
// entry with a name whose range holds a changed row) and every annotation
// span with a row in them, nested by containment over row indices and
// rendered as labelled rows. Row indices are the one coordinate both
// kinds share: a deleted definition has only pre-image lines, a span only
// post-image ones. Nothing is synthesised for rows with neither.

type LabelNode =
  | { kind: "definition"; region: FoldRegion; first: number; last: number; children: LabelNode[] }
  | { kind: "span"; span: AnnotationSpan; first: number; last: number; children: LabelNode[] };

/** What the tree is being built for: the fold's chevron row, which stays
 *  visible above the rows it labels. A definition covering that row
 *  encloses the fold rather than sitting inside it; a span covering it
 *  has its bar and text on screen already. Neither is a hidden
 *  label. */
interface LabelScope {
  visibleRow: RowBlock;
}

/** Row-index extent, inclusive, of the rows `hit` selects; null when none. */
function _rowExtent(rows: RowBlock[], hit: (r: RowBlock) => boolean): { first: number; last: number } | null {
  let first = -1;
  let last = -1;
  for (let i = 0; i < rows.length; i++) {
    if (!hit(rows[i])) continue;
    if (first < 0) first = i;
    last = i;
  }
  return first < 0 ? null : { first, last };
}

/** The definitions touched within `rows`: named regions holding one of
 *  the changed rows. A region reached only by context rows is the
 *  neighbour the diff brushed past, not something the diff changed. */
function _touchedDefinitions(f: FileBlock, rows: RowBlock[], scope: LabelScope): LabelNode[] {
  const out: LabelNode[] = [];
  for (const region of f.fold_regions || []) {
    if (region.qualified_name === null) continue;
    if (Folds.rowInRegion(scope.visibleRow, region)) continue;
    if (!rows.some((r) => r.kind !== "ctx" && Folds.rowInRegion(r, region))) continue;
    const extent = _rowExtent(rows, (r) => Folds.rowInRegion(r, region));
    if (extent === null) continue;
    out.push({ kind: "definition", region, ...extent, children: [] });
  }
  return out;
}

/** Nest definitions and spans by containment. Sorted outermost first —
 *  `(first, -last)`, a definition before a span of the same extent — so a
 *  node is a child of the nearest open node that still covers it. Two
 *  spans of one extent are siblings (two notes on one line are two
 *  observations). A span is drawn from whichever hunk carries it; one
 *  with no row here is simply elsewhere, and one still covering the
 *  visible line has its bar and text on screen already. */
function _labelTree(f: FileBlock, rows: RowBlock[], scope: LabelScope): LabelNode[] {
  const nodes: LabelNode[] = _touchedDefinitions(f, rows, scope);
  for (const h of f.hunks) {
    for (const span of h.spans || []) {
      const shown = scope.visibleRow.new_line;
      if (shown != null && span.start <= shown && shown <= span.end) continue;
      const extent = _rowExtent(rows, (r) => r.new_line != null && span.start <= r.new_line && r.new_line <= span.end);
      if (extent === null) continue;
      nodes.push({ kind: "span", span, ...extent, children: [] });
    }
  }
  nodes.sort((a, b) => a.first - b.first || b.last - a.last
    || (a.kind === b.kind ? 0 : a.kind === "definition" ? -1 : 1));
  const roots: LabelNode[] = [];
  const open: LabelNode[] = [];
  for (const node of nodes) {
    while (open.length) {
      const top = open[open.length - 1];
      const same = top.first === node.first && top.last === node.last;
      const covers = top.first <= node.first && node.last <= top.last
        && !(same && top.kind === "span" && node.kind === "span");
      if (covers) break;
      open.pop();
    }
    const parent = open.length ? open[open.length - 1] : null;
    (parent ? parent.children : roots).push(node);
    open.push(node);
  }
  return roots;
}

/** The text a folded definition shows beside its name: the fold-summary
 *  pass's summary, else its opener line when the rows carry it. A
 *  definition whose opener the diff never reached shows its name alone;
 *  no row is fetched for a label. */
function _definitionText(region: FoldRegion, rows: RowBlock[]): string {
  if (region.summary) return region.summary;
  const opener = rows.find((r) => r.new_line != null && r.new_line === region.right_start)
    || rows.find((r) => r.old_line != null && r.old_line === region.left_start);
  if (!opener) return "";
  return (opener.new_line != null ? opener.new_text : opener.old_text).trim();
}

/** A label row was clicked, asking for the lines it paraphrases. */
type LabelPick = (node: LabelNode) => void;

function _renderLabelTree(f: FileBlock, rows: RowBlock[], scope: LabelScope, pick: LabelPick): HTMLElement | null {
  const nodes = _labelTree(f, rows, scope);
  if (!nodes.length) return null;
  const tree = _el("div", "label-tree");
  for (const node of nodes) tree.appendChild(_renderLabelNode(node, f, rows, pick));
  return tree;
}

/** One labelled row, its children indented beneath. */
function _renderLabelNode(node: LabelNode, f: FileBlock, rows: RowBlock[], pick: LabelPick): HTMLElement {
  const row = _el("div", `label-row label-${node.kind}`);
  row.appendChild(_chev(true));
  if (node.kind === "definition") {
    const { region } = node;
    row.dataset.def = region.qualified_name || "";
    const kind = region.kind ? `${region.kind} ` : "";
    row.appendChild(_el("span", "label-def", `${kind}${region.qualified_name}`));
    const text = _definitionText(region, rows);
    row.appendChild(_el("span", text ? "label-text" : "label-text empty", text));
  } else {
    const s = node.span;
    row.dataset.id = s.id;
    row.appendChild(_el("span", "label-range", s.start === s.end ? `+${s.start}` : `+${s.start}..+${s.end}`));
    row.appendChild(_el("span", s.intent ? "label-text" : "label-text empty", s.intent || "(no intent)"));
    for (const sm of s.smells || []) row.appendChild(_smellPill(sm, {
      smellId: `${s.id}:smell:${sm.tag}`, file: f.path, side: "new", line: s.start,
    }));
  }
  row.addEventListener("click", (e) => {
    e.stopPropagation();
    pick(node);
  });
  if (!node.children.length) return row;
  const wrap = _el("div", "label-node");
  wrap.appendChild(row);
  const children = _el("div", "label-children");
  for (const child of node.children) children.appendChild(_renderLabelNode(child, f, rows, pick));
  wrap.appendChild(children);
  return wrap;
}

// --- A collapsed fold's labels ---------------------------------------------

/** The labels a collapsed region shows (the `FoldLabels` folds.ts asks
 *  for): the tree over the rows the fold hid. Clicking one opens the fold
 *  and lands on what it names — a span's text, a definition's first row
 *  — found within this pane's copy of the file, so the explainer panel
 *  never scrolls the diff pane. */
function _foldLabels(f: FileBlock): FoldLabels {
  return (headerRow, bodyRows, open) => {
    const scope: LabelScope = { visibleRow: headerRow };
    let tree: HTMLElement | null = null;
    const pick: LabelPick = (node) => {
      open();
      const pane = tree?.closest(".file") ?? document;
      const target = node.kind === "span"
        ? pane.querySelector<HTMLElement>(`.span-text[data-span-id="${_cssEscape(node.span.id)}"]`)
        : (bodyRows[node.first].newEl.children[1].classList.contains("empty")
          ? bodyRows[node.first].oldEl : bodyRows[node.first].newEl);
      if (target) target.scrollIntoView({ block: "nearest" });
    };
    tree = _renderLabelTree(f, bodyRows, scope, pick);
    return tree;
  };
}

/** Attach the fold chrome to one pane's copy of `f`, with the labels a
 *  collapsed region shows. boot.ts calls this when a summary lands from
 *  another tab. */
function attachFileFolds(fileEl: HTMLElement, f: FileBlock): void {
  Folds.attachFileFolds(fileEl, f, _foldLabels(f));
}

function _renderHunkDiff(h: HunkBlock, file: FileBlock, scope: PaneScope): HTMLElement {
  const cached = scope.cache?.[h.id];
  if (cached) return cached;
  const rows = h.rows || [];
  const marks = _blockMarks(rows);
  const { diff, oldEls, newEls } = _renderDiffRows(file, rows, marks);
  _attachSpans(newEls, rows, h.spans || [], h.id, file.path);
  // Record this hunk's rows so folds.ts can build a unified row stream
  // across the hunk and adjacent expanded context.
  FileRows.record(diff, { rows, oldEls, newEls });
  if (scope.cache) scope.cache[h.id] = diff;
  return diff;
}

// --- Spans on visible code: the centre gutter ------------------------------
//
// The right half's grid has four columns — line number, text, bars, code —
// the first three sticky so they read as a fixed centre gutter between the
// halves while the code scrolls beneath. Every row carries an empty text
// and bars cell for the gutter's background; a span puts its marks in
// them. Every span takes one form: a mark in the bars column — a bar over
// its rows, one column further right per level of nesting, or a dot on a
// span of one line — and its text block in the text column, starting on
// its first row. Nothing of a span lives in the code column.
//
// A text block is a zero-height grid item on its span's first row, so it
// takes no part in the grid's row sizing: its body hangs down the strip
// from the top of that row for as long as it needs, past the end of the
// bar if it must. The one invariant is that blocks do not overlap. A
// downward pass over the rows (`_layoutSpanTexts`) keeps it: where a
// block would start inside the one above, the nearest code row above it
// is stretched by the overlap (a `min-height`, mirrored onto the old
// half's paired row), and text running past the last row stretches the
// last code row. Two blocks starting on one row chain, outermost first.
// The pass is redone whenever the half's rows change (an annotation
// inserted, a fold hiding rows) or its size does; it measures once,
// writes once, and discards the mutation records its own writes queue.

/** A span placed on a hunk's rows: its row extent and nesting depth. */
interface PlacedSpan {
  span: AnnotationSpan;
  first: number;
  last: number;
  depth: number;
}

/** A span's text block: the grid item, the body whose height is the
 *  block's, and the span's first row. */
interface SpanTextBlock {
  el: HTMLElement;
  body: HTMLElement;
  row: HTMLElement;
}

/** A multi-line span's bar: the code rows it runs from and to, and its
 *  column. The rows between them that are not code — a comment thread's
 *  row, a fold's label row — arrive after the bar is drawn, so the
 *  placement pass gives them their segment. */
interface SpanBar {
  span: AnnotationSpan;
  first: HTMLElement;
  last: HTMLElement;
  depth: number;
}

/** A right half's gutter state: its text blocks and bars, the code rows
 *  the last pass stretched, and the observer whose records the pass
 *  discards. */
interface SpanGutter {
  blocks: SpanTextBlock[];
  bars: SpanBar[];
  stretched: HTMLElement[];
  observer: MutationObserver | null;
}

/** Gutter state per right half. */
const _SPAN_GUTTERS = new WeakMap<HTMLElement, SpanGutter>();

/** The gutter's fixed text width. Held constant so the code halves give
 *  up a predictable amount. Measured in the half's `ch` (12.5px mono):
 *  ~30 characters per line at the text's own 11px, so a typical intent
 *  is two or three lines and fits a span of that many rows. */
const _SPAN_TEXT_WIDTH = "26ch";
/** One bar column per level of nesting, plus a margin. */
const _SPAN_BAR_COLUMN_PX = 6;

/** Spans on visible code — the one owner of the form a span takes when
 *  its rows are on screen: its marks in the bars column (a bar over its
 *  rows, or a dot for a span of one line) and its text block in the text
 *  column, whatever its length. A span whose rows are not in the hunk is
 *  warned about and left out; one the reviewer has turned into a comment
 *  is left out too — the comment stands in its place. Everything hangs
 *  off the rows themselves, so it survives the hunk's `.diff` being
 *  reused across repaints and rows arriving above or below it from a
 *  chip. */
function _attachSpans(
  rowElsNew: HTMLElement[], rows: RowBlock[], spans: AnnotationSpan[],
  hunkId: string, filePath: string,
): void {
  if (!spans.length || !rows.length) return;
  const placed = _placeSpans(rows, spans, hunkId);
  if (!placed.length) return;
  const half = rowElsNew[0].parentElement;
  if (!half) throw new Error(`${hunkId}: rows are not in a half`);
  const blocks: SpanTextBlock[] = [];
  const bars: SpanBar[] = [];
  for (const ps of placed) {
    if (_spanPromoted(ps.span, hunkId)) continue;
    if (ps.first === ps.last) {
      _gutterBars(rowElsNew[ps.first]).appendChild(_spanMark(ps.span, ps.depth, "dot"));
    } else {
      for (let i = ps.first; i <= ps.last; i++) {
        const pos = i === ps.first ? "bar-top" : i === ps.last ? "bar-bottom" : "bar";
        _gutterBars(rowElsNew[i]).appendChild(_spanMark(ps.span, ps.depth, pos));
      }
      bars.push({ span: ps.span, first: rowElsNew[ps.first], last: rowElsNew[ps.last], depth: ps.depth });
    }
    const el = _el("div", "span-text");
    el.dataset.spanId = ps.span.id;
    const body = _spanText(ps.span, filePath);
    // A block starting where the one before it starts chains under it;
    // the class is the divider between the two.
    if (blocks.length && blocks[blocks.length - 1].row === rowElsNew[ps.first]) body.classList.add("chained");
    el.appendChild(body);
    half.appendChild(el);
    blocks.push({ el, body, row: rowElsNew[ps.first] });
  }
  if (!blocks.length) return;
  const gutter: SpanGutter = { blocks, bars, stretched: [], observer: null };
  _SPAN_GUTTERS.set(half, gutter);
  _observeHalf(half, gutter);
  _layoutSpanTexts(half);
}

/** Row extents and nesting for a hunk's spans, outermost first (first
 *  row ascending, then the longer span first). Depth counts enclosing
 *  multi-line spans (a single-line span takes no bar column). Two spans
 *  over one range nest, so both bars show. */
function _placeSpans(rows: RowBlock[], spans: AnnotationSpan[], hunkId: string): PlacedSpan[] {
  const out: PlacedSpan[] = [];
  for (const span of spans) {
    const extent = _rowExtent(rows, (r) => r.new_line != null && span.start <= r.new_line && r.new_line <= span.end);
    if (extent === null) {
      console.warn(`${hunkId}: span ${span.id} covers no row of the hunk; not shown`);
      continue;
    }
    out.push({ span, ...extent, depth: 0 });
  }
  out.sort((a, b) => a.first - b.first || b.last - a.last);
  const open: PlacedSpan[] = [];
  for (const ps of out) {
    while (open.length && !(open[open.length - 1].first <= ps.first && ps.last <= open[open.length - 1].last)) open.pop();
    ps.depth = open.length;
    if (ps.span.start !== ps.span.end) open.push(ps);
  }
  return out;
}

/** The gutter columns a file's spans need (its deepest bar plus one) and
 *  the text width, as CSS variables on the file body — one gutter for the
 *  whole file, so every row of every hunk and disclosed region in it pays
 *  the same and the halves split it evenly. A file with no span has none. */
function _setGutterWidths(body: HTMLElement, f: FileBlock): void {
  let cols = 0;
  for (const h of f.hunks) {
    for (const ps of _placeSpans(h.rows || [], h.spans || [], h.id)) cols = Math.max(cols, ps.depth + 1);
  }
  body.style.setProperty("--span-bars-w", cols ? `${cols * _SPAN_BAR_COLUMN_PX + 4}px` : "0px");
  body.style.setProperty("--span-text-w", cols ? _SPAN_TEXT_WIDTH : "0px");
}

// --- The gutter fold ---------------------------------------------------------

/** The stored preference; expanded when nothing (or nothing this code
 *  wrote) is stored. */
function _readGutterFold(): boolean {
  try {
    return localStorage.getItem(_GUTTER_FOLD_KEY) === "collapsed";
  } catch (_) {
    return false;
  }
}

/** Fold or unfold the gutter everywhere it is on screen. The class on
 *  the document is what the stylesheet zeroes the text column by, in the
 *  diff pane and the explainer's panel alike; the placement pass is then
 *  re-run on every half that has one, so a collapse releases its
 *  stretches and an expand puts them back. */
function _applyGutterFold(collapsed: boolean): void {
  _gutterCollapsed = collapsed;
  document.documentElement.classList.toggle("gutter-collapsed", collapsed);
  for (const half of document.querySelectorAll<HTMLElement>(".half-new[data-span-observed]")) _layoutSpanTexts(half);
  // The code columns moved; the comment arrows point at characters in them.
  Annotations.reflowAll();
}

function _setGutterFold(collapsed: boolean): void {
  if (collapsed === _gutterCollapsed) return;
  _applyGutterFold(collapsed);
  try {
    localStorage.setItem(_GUTTER_FOLD_KEY, collapsed ? "collapsed" : "expanded");
  } catch (_) { /* localStorage may be unavailable */ }
}

/** The strip's own affordances, delegated from the document so one
 *  listener covers every pane: a click on a span's mark expands the
 *  gutter (if folded) and brings that span's text into view; a click on
 *  the strip's empty area — a gutter cell itself, not a block or mark on
 *  it — toggles the fold. */
function _onGutterClick(e: MouseEvent): void {
  const target = e.target as HTMLElement | null;
  if (!target) return;
  const mark = target.closest<HTMLElement>(".span-mark[data-span-id]");
  if (mark) {
    e.stopPropagation();
    _setGutterFold(false);
    const half = mark.closest<HTMLElement>(".half-new");
    const body = half?.querySelector<HTMLElement>(`.span-text-body[data-span-id="${_cssEscape(mark.dataset.spanId!)}"]`);
    if (body) body.scrollIntoView({ block: "nearest" });
    return;
  }
  if (target.classList.contains("cell-gutter-text") || target.classList.contains("cell-gutter-bars")) {
    e.stopPropagation();
    _setGutterFold(!_gutterCollapsed);
  }
}

/** What a gutter cell says on hover; the strip has no chrome of its own. */
const _GUTTER_CELL_TITLE = "Click to fold or unfold the span gutter (g)";

/** A row's bars cell (`children[3]`; `[2]` is its text cell). */
function _gutterBars(rowEl: HTMLElement): HTMLElement {
  const cell = rowEl.children[3] as HTMLElement | undefined;
  if (!cell || !cell.classList.contains("cell-gutter-bars")) throw new Error("row has no gutter bars cell");
  return cell;
}

/** A bar segment or dot in a row's bars cell, at its depth's column. Its
 *  tooltip is the span's rationale — what a folded gutter shows of it on
 *  hover; the native tooltip holds a 700-character intent whole. */
function _spanMark(span: AnnotationSpan, depth: number, kind: "bar" | "bar-top" | "bar-bottom" | "dot"): HTMLElement {
  const mark = _el("span", `span-mark span-${kind}`);
  mark.dataset.spanId = span.id;
  mark.style.setProperty("--depth", String(depth));
  mark.setAttribute("aria-hidden", "true");
  mark.title = _spanTooltip(span);
  return mark;
}

function _spanTooltip(span: AnnotationSpan): string {
  const smells = (span.smells || []).map((sm) => sm.tag);
  const intent = span.intent || "(no intent)";
  return smells.length ? `${smells.join(" · ")}\n${intent}` : intent;
}

/** Whether the reviewer has already turned this span into a comment: a
 *  local comment derived from its id, or — for a span of one line — from
 *  the `line_note` id a comment store written before spans recorded the
 *  same observation under. */
function _spanPromoted(span: AnnotationSpan, hunkId: string): boolean {
  if (Comments.isPromoted(span.id)) return true;
  return span.start === span.end && Comments.isPromoted(`${hunkId}:line_note:${span.start}`);
}

/** One span's text: its smells as promotable pills on the block's first
 *  line — beside the bar's start, since they describe the span — then its
 *  intent, wrapping at the gutter's width. */
function _spanText(span: AnnotationSpan, filePath: string): HTMLElement {
  const el = _el("p", "span-text-body");
  el.dataset.spanId = span.id;
  if (span.smells && span.smells.length) {
    const pills = _el("span", "span-text-smells");
    for (const sm of span.smells) pills.appendChild(_smellPill(sm, {
      smellId: `${span.id}:smell:${sm.tag}`, file: filePath, side: "new", line: span.start,
    }));
    el.appendChild(pills);
  }
  el.appendChild(_el("span", span.intent ? "span-text-intent" : "span-text-intent empty", span.intent || "(no intent)"));
  return el;
}

/** Re-run the placement pass whenever the half's rows change or its
 *  size (or a text body's) does. One pair of observers per half; the
 *  WeakMap entry outlives them. A row mutation runs the pass after the
 *  burst that caused it settles; a resize runs it on the next frame, so
 *  the pass's own writes never land inside the resize loop. */
function _observeHalf(half: HTMLElement, gutter: SpanGutter): void {
  if (half.dataset.spanObserved !== undefined) return;
  half.dataset.spanObserved = "";
  let queued = false;
  const schedule = (): void => {
    if (queued) return;
    queued = true;
    queueMicrotask(() => { queued = false; _layoutSpanTexts(half); });
  };
  const mo = new MutationObserver((records) => {
    for (const r of records) {
      if (r.type === "childList" || (r.target as HTMLElement).classList?.contains("row")) { schedule(); return; }
    }
  });
  mo.observe(half, { childList: true, attributes: true, attributeFilter: ["style"], subtree: true });
  gutter.observer = mo;
  if (typeof ResizeObserver !== "undefined") {
    let frame = false;
    const ro = new ResizeObserver(() => {
      if (frame) return;
      frame = true;
      requestAnimationFrame(() => { frame = false; _layoutSpanTexts(half); });
    });
    ro.observe(half);
    for (const block of gutter.blocks) ro.observe(block.body);
  }
}

/** Place a half's text blocks: give every visible row its grid row, put
 *  each block on its span's first row (hidden while that row is hidden —
 *  the fold's label tree carries it then), then run the waterfall over
 *  the blocks' measured heights so none overlaps the one above it. The
 *  writes happen before and after a single measurement; the mutation
 *  records they queue are discarded, so the pass does not re-run itself. */
function _layoutSpanTexts(half: HTMLElement): void {
  const gutter = _SPAN_GUTTERS.get(half);
  if (!gutter) return;
  // A span promoted to a comment after the attach has had its block
  // removed from the half (comments.ts's sweep); it places nothing from
  // then on. Not `isConnected`: the first pass runs before the hunk's
  // `.diff` is in the document.
  gutter.blocks = gutter.blocks.filter((block) => block.el.parentElement === half);
  // Release the last pass's stretches: the rows measure at their natural
  // heights and the pass recomputes every one.
  for (const row of gutter.stretched) {
    row.style.minHeight = "";
    _pairOf(row).style.minHeight = "";
  }
  gutter.stretched = [];
  const track = new Map<Element, number>();
  const visible: HTMLElement[] = [];
  const rows: HTMLElement[] = [];
  let n = 0;
  for (const child of Array.from(half.children) as HTMLElement[]) {
    if (!child.classList.contains("row")) continue;
    rows.push(child);
    if (child.style.display === "none") { child.style.gridRow = ""; continue; }
    n++;
    child.style.gridRow = String(n);
    track.set(child, n);
    visible.push(child);
  }
  _barNonCodeRows(gutter.bars, rows);
  const shown: SpanTextBlock[] = [];
  for (const block of gutter.blocks) {
    const start = track.get(block.row);
    // Folded, the gutter has no text to place: every block hides and no
    // row is stretched — the compact view is the undistorted diff.
    if (start === undefined || _gutterCollapsed) {
      block.el.style.display = "none";
      block.el.style.transform = "";
      continue;
    }
    block.el.style.display = "";
    block.el.style.gridRow = String(start);
    shown.push(block);
  }
  if (!shown.length) {
    gutter.observer?.takeRecords();
    return;
  }
  // The one measurement.
  const rects = new Map<HTMLElement, DOMRect>();
  for (const row of visible) rects.set(row, row.getBoundingClientRect());
  const heights = new Map<SpanTextBlock, number>();
  for (const block of shown) heights.set(block, block.body.getBoundingClientRect().height);
  // The waterfall. `shift` is what the stretches so far have pushed every
  // row below them down by; `occupiedUntil` is where the lowest block so
  // far ends.
  shown.sort((a, b) => track.get(a.row)! - track.get(b.row)!);
  const stretch = new Map<HTMLElement, number>();
  const stretchBy = (row: HTMLElement, px: number): void => { stretch.set(row, (stretch.get(row) ?? 0) + px); };
  let shift = 0;
  let occupiedUntil = -Infinity;
  for (let i = 0; i < shown.length;) {
    const row = shown[i].row;
    let top = rects.get(row)!.top + shift;
    if (top < occupiedUntil) {
      const overlap = occupiedUntil - top;
      stretchBy(_codeRowAbove(visible, visible.indexOf(row) - 1), overlap);
      shift += overlap;
      top = occupiedUntil;
    }
    let y = top;
    for (; i < shown.length && shown[i].row === row; i++) {
      const offset = y - top;
      shown[i].el.style.transform = offset > 0 ? `translateY(${offset}px)` : "";
      y += heights.get(shown[i])!;
    }
    occupiedUntil = Math.max(occupiedUntil, y);
  }
  if (visible.length) {
    const last = visible[visible.length - 1];
    const bottom = rects.get(last)!.bottom + shift;
    if (occupiedUntil > bottom) stretchBy(_codeRowAbove(visible, visible.length - 1), occupiedUntil - bottom);
  }
  for (const [row, px] of stretch) {
    const h = `${rects.get(row)!.height + px}px`;
    row.style.minHeight = h;
    _pairOf(row).style.minHeight = h;
    gutter.stretched.push(row);
  }
  gutter.observer?.takeRecords();
}

/** The nearest code row at or above `visible[from]` — the row a stretch
 *  goes on, since a code row has a paired row in the old half to carry
 *  the stretch across; an annotation row or a placeholder does not. */
function _codeRowAbove(visible: HTMLElement[], from: number): HTMLElement {
  for (let i = from; i >= 0; i--) {
    const row = visible[i];
    if (!row.classList.contains("row-annotation") && !row.classList.contains("row-placeholder")) return row;
  }
  throw new Error("no code row above a colliding span text");
}

/** Give every non-code row lying inside a bar — a comment thread's row,
 *  a fold's label row, the placeholder for an old-side thread — a bars
 *  cell with a segment per enclosing bar at its depth, so a bar runs
 *  unbroken through them. `rows` is the half's rows in DOM order, hidden
 *  ones included (a bar may open on a hidden row); a hidden row is left
 *  alone. Idempotent: a cell already carrying the right segments stays. */
function _barNonCodeRows(bars: SpanBar[], rows: HTMLElement[]): void {
  if (!bars.length) return;
  const open = new Set<SpanBar>();
  for (const row of rows) {
    for (const bar of bars) if (bar.first === row) open.add(bar);
    const code = !row.classList.contains("row-annotation") && !row.classList.contains("row-placeholder");
    if (!code && row.style.display !== "none") {
      const key = Array.from(open, (b) => b.span.id).join("\n");
      let cell = row.querySelector<HTMLElement>(":scope > .cell-gutter-bars");
      if (cell && cell.dataset.spans !== key) { cell.remove(); cell = null; }
      if (!cell && open.size) {
        cell = _el("span", "cell-gutter-bars");
        cell.dataset.spans = key;
        for (const bar of open) cell.appendChild(_spanMark(bar.span, bar.depth, "bar"));
        row.appendChild(cell);
      }
    }
    for (const bar of bars) if (bar.last === row) open.delete(bar);
  }
}

/** The old-half row paired with a new-half code row. */
function _pairOf(row: HTMLElement): HTMLElement {
  const pair = (row as { _scrPair?: HTMLElement })._scrPair;
  if (!pair) throw new Error("code row has no paired row");
  return pair;
}

function _renderRow(
  row: RowBlock,
  file: FileBlock,
  oldMarks?: CharRange[],
  newMarks?: CharRange[],
): { old: HTMLElement; new: HTMLElement } {
  const hasOld = row.old_line !== null && row.old_line !== undefined;
  const hasNew = row.new_line !== null && row.new_line !== undefined;
  const oldRow = _el("div", `row row-${row.kind}`);
  oldRow.appendChild(_renderLineno(row.old_line, "old", hasOld));
  oldRow.appendChild(_renderContent(row.old_text, "old", hasOld, file, oldMarks));
  const newRow = _el("div", `row row-${row.kind}`);
  newRow.appendChild(_renderLineno(row.new_line, "new", hasNew));
  newRow.appendChild(_renderContent(row.new_text, "new", hasNew, file, newMarks));
  // The centre gutter's cells, after the content so `children[1]` stays
  // the content cell; the grid places them between number and code.
  const text = _el("span", "cell-gutter-text");
  const bars = _el("span", "cell-gutter-bars");
  text.title = bars.title = _GUTTER_CELL_TITLE;
  newRow.appendChild(text);
  newRow.appendChild(bars);
  return { old: oldRow, new: newRow };
}

interface _RowMarks { old?: CharRange[]; new?: CharRange[]; }

/** Per-row intra-line change marks for a hunk's rows. Consecutive changed
 *  rows form a block; a block that has *both* deleted and inserted lines (a
 *  replacement) is token-diffed across line boundaries so a change spanning
 *  several old lines is marked as one deletion + insertion. Pure
 *  deletions / insertions and context get no marks (the row tint suffices).
 */
function _blockMarks(rows: RowBlock[]): (_RowMarks | undefined)[] {
  const out: (_RowMarks | undefined)[] = new Array(rows.length).fill(undefined);
  let i = 0;
  while (i < rows.length) {
    if (rows[i].kind === "ctx") { i++; continue; }
    const oldRows: number[] = [];
    const newRows: number[] = [];
    const oldLines: string[] = [];
    const newLines: string[] = [];
    let j = i;
    for (; j < rows.length && rows[j].kind !== "ctx"; j++) {
      const r = rows[j];
      if (r.old_line !== null && r.old_line !== undefined) { oldRows.push(j); oldLines.push(r.old_text); }
      if (r.new_line !== null && r.new_line !== undefined) { newRows.push(j); newLines.push(r.new_text); }
    }
    if (oldLines.length > 0 && newLines.length > 0) {
      const d = blockDiff(oldLines, newLines);
      oldRows.forEach((ri, k) => { (out[ri] ??= {}).old = d.old[k]; });
      newRows.forEach((ri, k) => { (out[ri] ??= {}).new = d.new[k]; });
    }
    i = j;
  }
  return out;
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
  // Search-highlight the focused symbol on this fresh cell.
  if (_symbolSearch) _applySymbolHits(code);
  c.appendChild(code);
  return c;
}

// --- Symbol-focus search highlight ---------------------------------------

/** Set (or clear, with null) the symbol name highlighted across the diff,
 *  then repaint every cell already in the DOM. Driven by the sidebar when
 *  a Symbols-axis pill is focused. */
function setSymbolSearch(term: string | null): void {
  const next = term && term.trim() ? term : null;
  if (next === _symbolSearch) return;
  _symbolSearch = next;
  for (const code of document.querySelectorAll<HTMLElement>("#app .cell-content code")) {
    _clearSymbolHits(code);
    if (_symbolSearch) _applySymbolHits(code);
  }
}

function _applySymbolHits(code: HTMLElement): void {
  if (!_symbolSearch) return;
  const ranges = matchRanges(code.textContent || "", _symbolSearch);
  if (ranges.length) wrapRanges(code, ranges, "symbol-hit");
}

/** Unwrap this cell's `symbol-hit` spans back to plain text, leaving any
 *  highlight.js / char-chg markup untouched. */
function _clearSymbolHits(code: HTMLElement): void {
  const hits = code.querySelectorAll("span.symbol-hit");
  if (hits.length === 0) return;
  for (const hit of Array.from(hits)) {
    hit.replaceWith(document.createTextNode(hit.textContent || ""));
  }
  code.normalize(); // merge the text nodes the unwrap left adjacent
}

// --- Slider / status / hash / keyboard ---------------------------------

//: What the slider promises while the pane is the document. The level
//: highlight stays — it is the level a press lands on — so the tooltip
//: is what says the press also leaves the document.
const _OVERVIEW_SLIDER_TITLE = "Leave the document and read the diff at this level";

function _updateSliderButtons(): void {
  const overview = _state.mode === "overview";
  document.querySelectorAll(".fold-slider button").forEach((b) => {
    const btn = b as HTMLElement;
    btn.classList.toggle("active", btn.dataset.fold === _state.fold);
    // The markup's own title, kept the first time it is swapped out.
    if (btn.dataset.levelTitle === undefined) btn.dataset.levelTitle = btn.title;
    btn.title = overview ? _OVERVIEW_SLIDER_TITLE : btn.dataset.levelTitle;
  });
  _updateModeButton();
}

/** Repaint the overview-mode button: pressed state, and disabled until
 *  the skeleton's inputs exist server-side (pressing earlier can only
 *  409). Absent entirely when the feature is off for this review. */
function _updateModeButton(): void {
  const strip = document.querySelector(".mode-strip");
  if (!_explainerEnabled) {
    strip?.remove();
    return;
  }
  const btn = document.getElementById("overview-btn") as HTMLButtonElement | null;
  if (!btn) return;
  btn.classList.toggle("active", _state.mode === "overview");
  btn.disabled = !Explainer.isReady();
  btn.title = Explainer.isReady()
    ? "Reading guide for this change"
    : "Available once the change overview lands";
}

/** Called by boot when the overview SSE event arrives. */
function markExplainerReady(): void {
  if (!_explainerEnabled) return;
  Explainer.markReady();
  _updateModeButton();
}

function _updateStatus(): void {
  // Prefer the dedicated counts span (the console bar shares the footer
  // with it); fall back to the footer itself for the static-render path
  // where no console bar is mounted.
  const s = document.getElementById("status-counts")
    || document.getElementById("status-bar");
  if (!s) return;
  let smells = 0, critical = 0;
  for (const f of _data.files) {
    for (const h of f.hunks) {
      for (const sm of h.smells || []) {
        smells++;
        if ((_smells[sm.tag] || {} as SmellCatalogueEntry).severity === "critical") critical++;
      }
      for (const span of h.spans || []) {
        for (const sm of span.smells || []) {
          smells++;
          if ((_smells[sm.tag] || {} as SmellCatalogueEntry).severity === "critical") critical++;
        }
      }
    }
  }
  s.textContent = `${_data.files.length} files · ${smells} smells · ${critical} critical · keys 1-3 fold · g gutter · ? help`;
}

function _syncHash(): void {
  // The mode rides alongside the collapse level rather than replacing
  // it, so a reload into overview mode still restores the zoom the
  // reviewer will return to. `mode=diff` is written too, so a URL says
  // which mode the reviewer was in rather than only saying when they
  // were in the document: the default in `_initialMode` applies to a
  // hash that carries no mode at all, and an omitted key would make a
  // reader of the diff indistinguishable from a fresh open.
  const parts = [`fold=${_state.fold}`, `mode=${_state.mode}`];
  for (const [id, folded] of Object.entries(_state.overrides)) {
    parts.push(`${id}=${folded ? "f" : "o"}`);
  }
  const newHash = "#" + parts.join("&");
  if (window.location.hash !== newHash) {
    history.replaceState(null, "", newHash);
  }
}

/** Apply what the hash says, leaving anything it does not mention as
 *  the caller set it — so an absent `mode` keeps the boot default at
 *  init, and the mode in hand on a `hashchange`. */
function _restoreHash(): void {
  const h = window.location.hash.slice(1);
  if (!h) return;
  for (const kv of h.split("&")) {
    const [k, v] = kv.split("=");
    if (k === "fold") {
      // `segments` and `definitions` were middle rungs before ADR 0008
      // settled the ladder, and `off` the bottom rung's name; a link that
      // carries any of them lands on the code.
      const level = v === "segments" || v === "definitions" || v === "off" ? "code" : v;
      if ((_FOLD_MODES as readonly string[]).includes(level)) _state.fold = level as FoldMode;
    } else if (k === "mode") {
      _state.mode = v === "overview" && _explainerEnabled ? "overview" : "diff";
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
    case "3": _setGlobalFold("code"); e.preventDefault(); break;
    case "g": _setGutterFold(!_gutterCollapsed); e.preventDefault(); break;
    case "?": _toggleHelp(); e.preventDefault(); break;
    case "Escape": _onEscape(); break;
  }
}

/** Esc dismisses the topmost thing this handler owns, one per press: the
 *  help overlay, else the console drawer, else the detail panel. The
 *  comment editor and the console prompt handle their own Esc on the
 *  input, which never reaches here — `_onKeydown` returns early for text
 *  fields — so the prompt's Esc keeps its own meaning (cancel the turn,
 *  else collapse *and* drop the conversation). From anywhere else the
 *  drawer collapses with its transcript intact. */
function _onEscape(): void {
  const overlay = document.getElementById("help-overlay");
  if (overlay && !overlay.classList.contains("hidden")) {
    _closeHelp();
    return;
  }
  if (Console.drawerOpen()) {
    Console.collapse();
    return;
  }
  ExplainerPanel.close();
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
  const overview = document.getElementById("overview-btn");
  if (overview) {
    overview.addEventListener("click", () => {
      const entering = _state.mode !== "overview";
      setMode(entering ? "overview" : "diff");
      // Generating is the press's whole point when nothing exists yet;
      // the pane shows progress while the call is in flight. With a
      // document already in hand, the sections it left pending are
      // queued instead — entering the mode is the decision to spend,
      // and a second press per section asks twice for one choice.
      if (!entering) return;
      if (Explainer.hasDocument()) Explainer.generateAllPending();
      else void Explainer.generate();
    });
  }
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
  document.addEventListener("click", _onGutterClick);
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
  mode,
  setMode,
  openReference,
  focusRef,
  markExplainerReady,
  applyFilterChange,
  renderHunkReplace,
  repaintHunkHeader,
  clearRenderedDiffCache,
  attachFileFolds,
  setSymbolSearch,
};
