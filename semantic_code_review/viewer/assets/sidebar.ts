// Sidebar / grouping axes.
//
// Owns the #group-sidebar DOM tree and the multi-axis state behind
// it: a Themes axis populated by the overview LLM pass (refreshed
// in place when the `overview` SSE event arrives) and a Files axis
// derived deterministically from DATA.files.
//
// Filter semantics: one active pill at a time across all axes;
// clicking a pill toggles visibility on the per-file body via
// applyGroupFilter. The "ungrouped" visual tell is anchored to the
// themes axis (every hunk lives in a file, so the files axis has
// no useful "ungrouped" signal). Active pill is persisted in
// localStorage as `<axis>:<id>`.

import { Annotations } from "./annotations";
import { Comments } from "./comments";

type AxisId = "themes" | "files" | "symbols";

interface SidebarAxis {
  id: AxisId;
  label: string;
  groups: GroupBlock[];
  byId: Record<string, GroupBlock>;
  hunkCount: Record<string, number>;
}

interface ActivePill {
  axis: AxisId;
  id: string;
}

const THEMES_AXIS: SidebarAxis = {
  id: "themes", label: "Themes",
  groups: [], byId: Object.create(null), hunkCount: Object.create(null),
};
const FILES_AXIS: SidebarAxis = {
  id: "files", label: "Files",
  groups: [], byId: Object.create(null), hunkCount: Object.create(null),
};
// Deterministic tree-sitter symbol delta (ADR 0001). Like the Files
// axis it's structural — populated from DATA at boot, never refreshed
// by an SSE pass. Rendered as a nested class ▸ method tree (slice 5).
const SYMBOLS_AXIS: SidebarAxis = {
  id: "symbols", label: "Symbols",
  groups: [], byId: Object.create(null), hunkCount: Object.create(null),
};
const AXES: SidebarAxis[] = [THEMES_AXIS, FILES_AXIS, SYMBOLS_AXIS];

let _data: ViewerData | null = null;
let _activePill: ActivePill | null = null;
let _lsKey = "scr-active-group:local";
// Notified with the focused symbol's name (or null) whenever the active
// pill changes — boot points this at Render.setSymbolSearch. Kept as an
// injected callback rather than a direct import so the sidebar doesn't
// take a cyclic dependency on render.
let _onActivePillChange: ((symbolName: string | null) => void) | null = null;
// Collapsed symbol-tree nodes, by pill id. In-memory only (the tree is
// expanded by default; collapse is a transient view preference that
// survives re-renders within a session but not a reload).
const _collapsedSymbols = new Set<string>();

/** Populate axes from the initial viewer data + restore any active
 *  pill from localStorage. Idempotent (call again after DATA mutates
 *  in a way the in-place refreshers don't cover). */
function init(
  data: ViewerData,
  opts?: { onActivePillChange?: (symbolName: string | null) => void },
): void {
  _data = data;
  if (opts && opts.onActivePillChange) _onActivePillChange = opts.onActivePillChange;
  _lsKey =
    "scr-active-group:"
    + (data.pr && data.pr.head_sha ? data.pr.head_sha : "local");

  // Themes axis: refresh in place so any references the rest of
  // the viewer holds to THEMES_AXIS.* arrays stay live.
  refreshThemes(data.groups || []);
  rebuildFilesAxis();
  rebuildSymbolsAxis();

  // Restore the active pill across axes. Legacy entries are bare
  // ids (themes axis); new entries are `<axis>:<id>`.
  try {
    const saved = localStorage.getItem(_lsKey);
    if (saved) {
      let axisId: ActivePill["axis"] = "themes";
      let pillId = saved;
      if (saved.includes(":")) {
        const parts = saved.split(":", 2);
        axisId = parts[0] as ActivePill["axis"];
        pillId = parts[1];
      }
      const axis = AXES.find((a) => a.id === axisId);
      if (axis && axis.byId[pillId]) _activePill = { axis: axisId, id: pillId };
    }
  } catch (_) { /* localStorage may be unavailable */ }
  // Seed the symbol search now (init runs before Render.init in boot):
  // the emit's repaint finds no cells yet, but it sets `_symbolSearch` in
  // render so Render.init's first paint highlights a restored Symbols pill.
  _emitActivePill();
}

/** Rebuild the themes axis from a fresh list of groups. Mutates the
 *  THEMES_AXIS arrays in place so existing live references survive
 *  (the original sidebar bug from commit e800632 was forgetting to
 *  do exactly this). */
function refreshThemes(groups: GroupBlock[]): void {
  THEMES_AXIS.groups.length = 0;
  for (const k of Object.keys(THEMES_AXIS.byId)) delete THEMES_AXIS.byId[k];
  for (const k of Object.keys(THEMES_AXIS.hunkCount)) delete THEMES_AXIS.hunkCount[k];
  for (const g of groups) {
    THEMES_AXIS.groups.push(g);
    THEMES_AXIS.byId[g.id] = g;
    for (const hid of g.hunk_ids || []) {
      THEMES_AXIS.hunkCount[hid] = (THEMES_AXIS.hunkCount[hid] || 0) + 1;
    }
  }
}

/** Derive the by-file axis from DATA.files. One pill per file with
 *  hunks; pill ids `BF<fi>` (distinct ID space from themes' `G<i>`).
 *  Skipped files (zero hunks) get no pill. */
function rebuildFilesAxis(): void {
  if (!_data) return;
  FILES_AXIS.groups.length = 0;
  for (const k of Object.keys(FILES_AXIS.byId)) delete FILES_AXIS.byId[k];
  for (const k of Object.keys(FILES_AXIS.hunkCount)) delete FILES_AXIS.hunkCount[k];
  for (let fi = 0; fi < (_data.files || []).length; fi++) {
    const f = _data.files[fi];
    if (!f.hunks || f.hunks.length === 0) continue;
    const hunk_ids = f.hunks.map((h) => h.id);
    const g: GroupBlock = {
      id: `BF${fi}`,
      title: _shortenPath(f.path),
      rationale: f.path,
      hunk_ids,
    };
    FILES_AXIS.groups.push(g);
    FILES_AXIS.byId[g.id] = g;
    for (const hid of hunk_ids) {
      FILES_AXIS.hunkCount[hid] = (FILES_AXIS.hunkCount[hid] || 0) + 1;
    }
  }
}

/** Load the symbols axis from DATA.symbols — pre-built server-side as a
 *  forest of GroupBlock nodes (class ▸ method; a parent's hunk_ids is its
 *  subtree union). `groups` holds the roots for the tree render; `byId`
 *  flattens every node (roots and descendants alike) so active-pill
 *  lookup and restore resolve any node by id. Pill ids `SY<i>` are a
 *  distinct ID space from themes/files. The delta is deterministic, so
 *  unlike themes there's no in-place refresh: what boot ships is final. */
function rebuildSymbolsAxis(): void {
  SYMBOLS_AXIS.groups.length = 0;
  for (const k of Object.keys(SYMBOLS_AXIS.byId)) delete SYMBOLS_AXIS.byId[k];
  for (const k of Object.keys(SYMBOLS_AXIS.hunkCount)) delete SYMBOLS_AXIS.hunkCount[k];
  const register = (g: GroupBlock): void => {
    SYMBOLS_AXIS.byId[g.id] = g;
    for (const hid of g.hunk_ids || []) {
      SYMBOLS_AXIS.hunkCount[hid] = (SYMBOLS_AXIS.hunkCount[hid] || 0) + 1;
    }
    for (const c of g.children || []) register(c);
  };
  for (const g of (_data && _data.symbols) || []) {
    SYMBOLS_AXIS.groups.push(g);
    register(g);
  }
}

/** Render the sidebar's pill rows. One section per populated axis,
 *  a "Show all" button above them that clears any active pill. */
function render(): void {
  const sidebar = document.getElementById("group-sidebar");
  if (!sidebar) return;
  sidebar.innerHTML = "";
  const populated = AXES.filter((a) => a.groups.length > 0);
  if (populated.length === 0) {
    sidebar.classList.add("empty");
    return;
  }
  sidebar.classList.remove("empty");

  const showAll = _el("button", "group-btn group-btn-all", "Show all");
  showAll.title = "Clear filter — show every hunk";
  if (_activePill === null) showAll.classList.add("active");
  showAll.addEventListener("click", () => setActivePill(null));
  sidebar.appendChild(showAll);

  for (const axis of populated) {
    const section = _el("div", "group-axis");
    section.dataset.axis = axis.id;
    const header = _el("div", "group-axis-header");
    header.appendChild(_el("h3", null, axis.label));
    section.appendChild(header);
    if (axis.id === "symbols") {
      // Nested class ▸ method tree (ADR 0001 slice 5).
      for (const g of axis.groups) section.appendChild(_symbolNode(axis, g));
    } else {
      const commentCounts = axis.id === "files" ? _commentCountsByFilePath() : null;
      for (const g of axis.groups) section.appendChild(_pillButton(axis, g, commentCounts));
    }
    sidebar.appendChild(section);
  }
}

/** One pill button for a group. Shared by the flat axes and the symbols
 *  tree's per-node row. `commentCounts` (Files axis only) appends the
 *  unresolved/total badge. */
function _pillButton(
  axis: SidebarAxis,
  g: GroupBlock,
  commentCounts: Record<string, CommentCounts> | null,
): HTMLElement {
  const btn = _el("button", "group-btn");
  btn.dataset.axis = axis.id;
  btn.dataset.pillId = g.id;
  btn.appendChild(_el("span", "group-btn-label", g.title));
  btn.appendChild(_el("span", "group-btn-count", String((g.hunk_ids || []).length)));
  if (commentCounts) {
    // Files axis only — Themes axis is keyed by hunks, not paths,
    // so there's no single "comments per pill" mapping to surface.
    const cc = commentCounts[g.rationale] || { total: 0, unresolved: 0 };
    const badge = _renderCommentCountBadge(cc);
    if (badge) btn.appendChild(badge);
  }
  if (g.rationale) btn.title = g.rationale;
  if (_isActivePill(axis.id, g.id)) btn.classList.add("active");
  btn.addEventListener("click", () => {
    setActivePill(
      _isActivePill(axis.id, g.id) ? null : { axis: axis.id, id: g.id },
    );
  });
  return btn;
}

/** Render one symbol-tree node: an optional expand/collapse toggle, the
 *  node's own filtering pill, and (recursively) its children indented
 *  beneath it. Ancestors are shown for context even when unchanged;
 *  clicking the pill filters to the subtree union, clicking a leaf to
 *  just that symbol's hunks. */
function _symbolNode(axis: SidebarAxis, g: GroupBlock): HTMLElement {
  const node = _el("div", "group-tree-node");
  const row = _el("div", "group-tree-row");
  const kids = g.children || [];
  let childWrap: HTMLElement | null = null;
  if (kids.length > 0) {
    const collapsed = _collapsedSymbols.has(g.id);
    const toggle = _el("button", "group-tree-toggle", collapsed ? "▸" : "▾");
    toggle.title = collapsed ? "Expand" : "Collapse";
    toggle.addEventListener("click", (e) => {
      e.stopPropagation();
      const nowCollapsed = !_collapsedSymbols.has(g.id);
      if (nowCollapsed) _collapsedSymbols.add(g.id);
      else _collapsedSymbols.delete(g.id);
      toggle.textContent = nowCollapsed ? "▸" : "▾";
      toggle.title = nowCollapsed ? "Expand" : "Collapse";
      if (childWrap) childWrap.style.display = nowCollapsed ? "none" : "";
    });
    row.appendChild(toggle);
  } else {
    // Keep leaves aligned with parents' pills under the toggle column.
    row.appendChild(_el("span", "group-tree-toggle group-tree-toggle-leaf"));
  }
  row.appendChild(_pillButton(axis, g, null));
  node.appendChild(row);
  if (kids.length > 0) {
    childWrap = _el("div", "group-tree-children");
    if (_collapsedSymbols.has(g.id)) childWrap.style.display = "none";
    for (const c of kids) childWrap.appendChild(_symbolNode(axis, c));
    node.appendChild(childWrap);
  }
  return node;
}

function _isActivePill(axisId: string, pillId: string): boolean {
  return _activePill !== null
    && _activePill.axis === axisId
    && _activePill.id === pillId;
}

function setActivePill(pill: ActivePill | null): void {
  _activePill = pill;
  try {
    if (pill === null) localStorage.removeItem(_lsKey);
    else localStorage.setItem(_lsKey, `${pill.axis}:${pill.id}`);
  } catch (_) { /* ignore */ }
  document.querySelectorAll(".group-btn").forEach(
    (b) => b.classList.remove("active"),
  );
  if (pill === null) {
    const all = document.querySelector(".group-btn-all");
    if (all) all.classList.add("active");
  } else {
    const sel = `.group-btn[data-axis="${pill.axis}"][data-pill-id="${pill.id}"]`;
    const btn = document.querySelector(sel);
    if (btn) btn.classList.add("active");
  }
  applyFilter();
  _emitActivePill();
  Annotations.reflowAll();
}

/** Name to search-highlight across the diff for the active pill: the
 *  focused symbol's bare name, or null for any non-Symbols pill / no
 *  pill. Wired to Render.setSymbolSearch by boot. */
function _activeSymbolName(): string | null {
  if (_activePill === null || _activePill.axis !== "symbols") return null;
  const g = SYMBOLS_AXIS.byId[_activePill.id];
  return g ? g.title : null;
}

function _emitActivePill(): void {
  _onActivePillChange?.(_activeSymbolName());
}

function _activePillHunkIds(): Set<string> | null {
  if (_activePill === null) return null;
  const axis = AXES.find((a) => a.id === _activePill!.axis);
  if (!axis) return null;
  const g = axis.byId[_activePill.id];
  return g ? new Set(g.hunk_ids || []) : new Set<string>();
}

/** Walk every .hunk in the file body, tag `.ungrouped` for hunks no
 *  themes-axis group claims, and toggle visibility based on the
 *  active pill. Files with no visible hunks hide too — keeps the
 *  filtered view tidy. */
function applyFilter(): void {
  const activeIds = _activePillHunkIds();
  document.querySelectorAll(".file").forEach((fileEl) => {
    let visible = 0;
    fileEl.querySelectorAll(".hunk").forEach((hunkEl) => {
      const hid = (hunkEl as HTMLElement).dataset.id || "";
      const inAnyGroup = (THEMES_AXIS.hunkCount[hid] || 0) > 0;
      hunkEl.classList.toggle("ungrouped", !inAnyGroup);
      const show = activeIds === null ? true : activeIds.has(hid);
      (hunkEl as HTMLElement).style.display = show ? "" : "none";
      if (show) visible++;
    });
    (fileEl as HTMLElement).style.display =
      visible === 0 && activeIds !== null ? "none" : "";
  });
}

interface CommentCounts { total: number; unresolved: number }

/** Tally comment threads per file path.
 *
 * "Threads" are counted as the root comments — i.e. those whose
 * ``in_reply_to_id`` is null/absent. A local reply to an ingested
 * root carries the parent's id; the parent (the ingested root) is
 * what gets counted. This matches how the discussion is grouped in
 * the diff view: one annotation block per thread.
 *
 * "Unresolved" mirrors the resolved-thread fold: a thread is
 * resolved iff its root's ``thread_resolved`` is true. Pure-local
 * threads (no upstream) default to unresolved — the reviewer hasn't
 * told us otherwise. */
function _commentCountsByFilePath(): Record<string, CommentCounts> {
  const out: Record<string, CommentCounts> = Object.create(null);
  for (const c of Comments.getAll()) {
    if (c.in_reply_to_id) continue;            // only thread roots
    if (!c.file) continue;
    const bucket = (out[c.file] ||= { total: 0, unresolved: 0 });
    bucket.total += 1;
    if (!c.thread_resolved) bucket.unresolved += 1;
  }
  return out;
}

function _renderCommentCountBadge(cc: CommentCounts): HTMLElement | null {
  if (cc.total === 0) return null;
  const badge = _el(
    "span",
    "group-btn-comments" + (cc.unresolved > 0 ? " has-unresolved" : ""),
    `${cc.unresolved}/${cc.total}`,
  );
  badge.title = cc.unresolved > 0
    ? `${cc.unresolved} unresolved of ${cc.total} thread${cc.total === 1 ? "" : "s"}`
    : `${cc.total} thread${cc.total === 1 ? "" : "s"} — all resolved`;
  return badge;
}

/** Re-paint just the comment-count badges on existing Files-axis pills.
 *  Boot wires this to Comments' onChange so the badges stay in sync
 *  with the store without re-rendering the whole sidebar. */
function refreshFileCommentCounts(): void {
  const sidebar = document.getElementById("group-sidebar");
  if (!sidebar) return;
  const filesSection = sidebar.querySelector('[data-axis="files"]');
  if (!filesSection) return;
  const counts = _commentCountsByFilePath();
  filesSection.querySelectorAll<HTMLElement>(".group-btn").forEach((btn) => {
    const pillId = btn.dataset.pillId || "";
    const group = FILES_AXIS.byId[pillId];
    if (!group) return;
    const cc = counts[group.rationale] || { total: 0, unresolved: 0 };
    const existing = btn.querySelector(".group-btn-comments");
    if (existing) existing.remove();
    const fresh = _renderCommentCountBadge(cc);
    if (fresh) btn.appendChild(fresh);
  });
}

function _shortenPath(path: string): string {
  if (!path) return "";
  if (path.length <= 28) return path;
  const idx = path.lastIndexOf("/");
  return idx >= 0 ? path.slice(idx + 1) : path;
}

function _el(tag: string, className: string | null, text?: string): HTMLElement {
  const n = document.createElement(tag);
  if (className) n.className = className;
  if (text !== undefined) n.textContent = text;
  return n;
}

export const Sidebar = {
  init,
  render,
  refreshThemes,
  rebuildFilesAxis,
  rebuildSymbolsAxis,
  refreshFileCommentCounts,
  setActivePill,
  applyFilter,
};
