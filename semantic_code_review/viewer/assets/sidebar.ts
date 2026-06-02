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

interface SidebarAxis {
  id: "themes" | "files";
  label: string;
  groups: GroupBlock[];
  byId: Record<string, GroupBlock>;
  hunkCount: Record<string, number>;
}

interface ActivePill {
  axis: "themes" | "files";
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
const AXES: SidebarAxis[] = [THEMES_AXIS, FILES_AXIS];

let _data: ViewerData | null = null;
let _activePill: ActivePill | null = null;
let _lsKey = "scr-active-group:local";

/** Populate axes from the initial viewer data + restore any active
 *  pill from localStorage. Idempotent (call again after DATA mutates
 *  in a way the in-place refreshers don't cover). */
function init(data: ViewerData): void {
  _data = data;
  _lsKey =
    "scr-active-group:"
    + (data.pr && data.pr.head_sha ? data.pr.head_sha : "local");

  // Themes axis: refresh in place so any references the rest of
  // the viewer holds to THEMES_AXIS.* arrays stay live.
  refreshThemes(data.groups || []);
  rebuildFilesAxis();

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
    const commentCounts = axis.id === "files" ? _commentCountsByFilePath() : null;
    for (const g of axis.groups) {
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
      section.appendChild(btn);
    }
    sidebar.appendChild(section);
  }
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
  Annotations.reflowAll();
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
  refreshFileCommentCounts,
  setActivePill,
  applyFilter,
};
