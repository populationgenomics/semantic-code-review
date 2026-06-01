// Semantic Code Review — viewer.
// Single foldable diff with IDE-style code folding at PR/file/hunk/segment levels.
// Custom side-by-side diff renderer driven by pre-paired rows from the Python
// build step (no diff2html dependency).

(function () {
  "use strict";

  const DATA = JSON.parse(document.getElementById("scr-data").textContent);
  const SMELLS = DATA.smells_catalogue || {};

  // DATA.pending is true while the server is streaming overview / per-hunk
  // events from a running augmentation pass. Hunks without an annotation
  // render an "analysing…" spinner during that window and the failure
  // copy once the `done` event clears the flag. See installSessionEvents.

  const SESSION_ENDPOINT = (() => {
    const m = document.querySelector('meta[name="scr-session-endpoint"]');
    return m ? m.getAttribute("content") : "";
  })();

  const STATE = {
    fold: "hunks",      // 'files' | 'hunks' | 'segments' | 'off'
    overrides: {},      // regionId -> bool (true = folded)
    renderedDiffs: {},  // hunkId -> pre-rendered <div>
    comments: {},       // id -> Comment
    // Active sidebar pill, scoped to one axis. `null` = show every
    // hunk. One pill across all axes is active at a time; switching
    // axes clears the previous selection. Persisted in localStorage
    // as `<axis>:<id>` (legacy plain ids load as themes:<id>).
    activePill: null,   // { axis: "themes"|"files", id: string } | null
  };

  // Per-hunk progress state, populated while DATA.pending is true.
  // Hunks start in 'queued', flip to 'running' on the `hunk-start` SSE
  // event, then to 'ok' / 'failed' on the completion event. The header
  // strip squares + the per-hunk intent slot copy both read from here.
  const PROGRESS = {
    byId: Object.create(null),    // hunkId -> 'queued'|'running'|'ok'|'failed'
    order: [],                    // hunkIds in pipeline-queue order
    overview: "pending",          // 'pending'|'running'|'ok'|'failed'
  };

  // --- Semantic groups -----------------------------------------------------
  // The overview LLM pass may emit `DATA.groups`, a flat list of
  // {id, title, rationale, hunk_ids[]} clusters. The sidebar renders
  // them as pill buttons; clicking one filters the visible hunks to
  // that group's members. Hunks in NO group get a subtle visual tell
  // in the default view so reviewers can see which changes didn't
  // cluster with anything. A hunk can appear in multiple groups.

  // Sidebar axes. Each axis owns its pill collection + lookup tables.
  // `groups` is the array of {id, title, rationale, hunk_ids} the
  // sidebar renders; `byId`/`hunkCount` are kept consistent with it.
  // The themes axis is populated from DATA.groups (the LLM-curated
  // semantic clusters) and refreshed in place when the streaming
  // `overview` event arrives. The files axis is derived deterministically
  // from DATA.files and built once at boot — no LLM call needed.
  const THEMES_AXIS = {
    id: "themes", label: "Themes",
    groups: [], byId: Object.create(null), hunkCount: Object.create(null),
  };
  const FILES_AXIS = {
    id: "files", label: "Files",
    groups: [], byId: Object.create(null), hunkCount: Object.create(null),
  };
  const AXES = [THEMES_AXIS, FILES_AXIS];

  // Legacy aliases: applyOverviewPatch and the boot logic both target
  // the themes axis explicitly; these names keep the older code paths
  // readable while only one axis was LLM-driven.
  const GROUPS = THEMES_AXIS.groups;
  const GROUP_BY_ID = THEMES_AXIS.byId;
  const HUNK_GROUP_COUNT = THEMES_AXIS.hunkCount;

  for (const g of DATA.groups || []) {
    GROUPS.push(g);
    GROUP_BY_ID[g.id] = g;
    for (const hid of g.hunk_ids || []) {
      HUNK_GROUP_COUNT[hid] = (HUNK_GROUP_COUNT[hid] || 0) + 1;
    }
  }
  rebuildFilesAxis();

  const GROUP_LS_KEY = "scr-active-group:" + (DATA.pr && DATA.pr.head_sha ? DATA.pr.head_sha : "local");
  try {
    const saved = localStorage.getItem(GROUP_LS_KEY);
    if (saved) {
      // Legacy entries are bare ids (themes axis). New entries are
      // "<axis>:<id>". Resolve to {axis, id} if the target still exists.
      let axisId = "themes", pillId = saved;
      if (saved.includes(":")) [axisId, pillId] = saved.split(":", 2);
      const axis = AXES.find((a) => a.id === axisId);
      if (axis && axis.byId[pillId]) STATE.activePill = { axis: axisId, id: pillId };
    }
  } catch (_) { /* localStorage may be unavailable */ }

  // --- Fold defaults per region type ---------------------------------------
  function defaultFileFolded()    { return STATE.fold === "files"; }
  function defaultHunkFolded()    { return STATE.fold === "files" || STATE.fold === "hunks"; }
  function defaultSegmentFolded() { return STATE.fold !== "off"; }

  function isFolded(id, fallback) {
    return Object.prototype.hasOwnProperty.call(STATE.overrides, id)
      ? STATE.overrides[id] : fallback;
  }

  // --- DOM helpers ---------------------------------------------------------
  function el(tag, className, text) {
    const n = document.createElement(tag);
    if (className) n.className = className;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  const SVG_NS = "http://www.w3.org/2000/svg";

  // A disclosure chevron, drawn as a stroked caret (`>`). When `.open` is
  // applied, CSS rotates it 90° so it points down. Reused by file, hunk,
  // segment, and in-diff indent fold controls.
  function chev(folded, extraClass) {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 12 12");
    svg.setAttribute("aria-hidden", "true");
    svg.classList.add("chevron");
    if (extraClass) svg.classList.add(extraClass);
    if (!folded) svg.classList.add("open");
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", "M4.25 2.75 L8 6 L4.25 9.25");
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", "currentColor");
    path.setAttribute("stroke-width", "1.75");
    path.setAttribute("stroke-linecap", "round");
    path.setAttribute("stroke-linejoin", "round");
    svg.appendChild(path);
    return svg;
  }

  function smellPill(smell) {
    const def = SMELLS[smell.tag];
    const sev = def ? def.severity : "minor";
    const p = el("span", `smell sev-${sev}`, smell.tag);
    p.title = smell.note || (def ? def.label : smell.tag);
    return p;
  }

  // --- Rendering -----------------------------------------------------------
  function render() {
    const app = document.getElementById("app");
    app.innerHTML = "";
    app.appendChild(renderPRPanel(DATA.pr));
    for (const f of DATA.files) app.appendChild(renderFile(f));
    renderGroupSidebar();
    applyGroupFilter();
    updateStatus();
    syncHash();
    updateSliderButtons();
    // Re-attach any loaded comments to freshly-rendered rows.
    if (Object.keys(STATE.comments).length) renderAllExistingComments();
    // Annotation arrows attached during render were sized while the
    // tree was still detached. Install the viewport watcher (idempotent)
    // which hooks window-resize + fonts.ready for post-mount reflow, and
    // double-RAF a fresh pass for the first paint.
    window.ScrAnnotations.watchViewport();
    requestAnimationFrame(() => {
      window.ScrAnnotations.reflowAll();
      requestAnimationFrame(() => window.ScrAnnotations.reflowAll());
    });
  }

  // Build the sidebar's axis sections. Each axis with non-empty
  // groups gets its own labelled section + pill row. A single
  // "Show all" sits at the top and clears the active pill across
  // every axis. The themes axis goes first because its pills are
  // the most semantically dense; files follows for structural nav.
  function renderGroupSidebar() {
    const sidebar = document.getElementById("group-sidebar");
    if (!sidebar) return;
    sidebar.innerHTML = "";
    const populated = AXES.filter((a) => a.groups.length > 0);
    if (populated.length === 0) {
      sidebar.classList.add("empty");
      return;
    }
    sidebar.classList.remove("empty");

    const showAll = el("button", "group-btn group-btn-all", "Show all");
    showAll.title = "Clear filter — show every hunk";
    if (STATE.activePill === null) showAll.classList.add("active");
    showAll.addEventListener("click", () => setActivePill(null));
    sidebar.appendChild(showAll);

    for (const axis of populated) {
      const section = el("div", "group-axis");
      section.dataset.axis = axis.id;
      const header = el("div", "group-axis-header");
      header.appendChild(el("h3", null, axis.label));
      section.appendChild(header);
      for (const g of axis.groups) {
        const btn = el("button", "group-btn");
        btn.dataset.axis = axis.id;
        btn.dataset.pillId = g.id;
        btn.appendChild(el("span", "group-btn-label", g.title));
        btn.appendChild(el("span", "group-btn-count", String((g.hunk_ids || []).length)));
        if (g.rationale) btn.title = g.rationale;
        if (isActivePill(axis.id, g.id)) btn.classList.add("active");
        btn.addEventListener("click", () => {
          setActivePill(
            isActivePill(axis.id, g.id) ? null : { axis: axis.id, id: g.id },
          );
        });
        section.appendChild(btn);
      }
      sidebar.appendChild(section);
    }
  }

  function isActivePill(axisId, pillId) {
    return STATE.activePill !== null
      && STATE.activePill.axis === axisId
      && STATE.activePill.id === pillId;
  }

  function setActivePill(pill) {
    STATE.activePill = pill;
    try {
      if (pill === null) localStorage.removeItem(GROUP_LS_KEY);
      else localStorage.setItem(GROUP_LS_KEY, `${pill.axis}:${pill.id}`);
    } catch (_) { /* ignore */ }
    document.querySelectorAll(".group-btn").forEach((b) => b.classList.remove("active"));
    if (pill === null) {
      const all = document.querySelector(".group-btn-all");
      if (all) all.classList.add("active");
    } else {
      const sel = `.group-btn[data-axis="${pill.axis}"][data-pill-id="${pill.id}"]`;
      const btn = document.querySelector(sel);
      if (btn) btn.classList.add("active");
    }
    applyGroupFilter();
    window.ScrAnnotations.reflowAll();
  }

  function activePillHunkIds() {
    if (STATE.activePill === null) return null;
    const axis = AXES.find((a) => a.id === STATE.activePill.axis);
    if (!axis) return null;
    const g = axis.byId[STATE.activePill.id];
    return g ? new Set(g.hunk_ids || []) : new Set();
  }

  // Walk every .hunk element, tag .ungrouped for hunks no themes-axis
  // group claims (the file axis always covers every hunk so it's not
  // a useful "ungrouped" signal), and apply visibility based on the
  // currently-active pill. Files with no visible hunks are hidden too
  // so the sidebar view reads cleanly.
  function applyGroupFilter() {
    const activeIds = activePillHunkIds();
    document.querySelectorAll(".file").forEach((fileEl) => {
      let visible = 0;
      fileEl.querySelectorAll(".hunk").forEach((hunkEl) => {
        const hid = hunkEl.dataset.id;
        const inAnyGroup = (HUNK_GROUP_COUNT[hid] || 0) > 0;
        hunkEl.classList.toggle("ungrouped", !inAnyGroup);
        const show = activeIds === null ? true : activeIds.has(hid);
        hunkEl.style.display = show ? "" : "none";
        if (show) visible++;
      });
      fileEl.style.display = visible === 0 && activeIds !== null ? "none" : "";
    });
  }

  // Build the by-file axis from DATA.files. One pill per file with
  // hunks, label = path (basename if path is deep), count = hunks
  // count. Skipped files (status = generated / binary / deleted with
  // no diff body) still get a pill — the reviewer might want to jump
  // to them. Re-buildable in place via `rebuildFilesAxis()`.
  function rebuildFilesAxis() {
    FILES_AXIS.groups.length = 0;
    for (const k of Object.keys(FILES_AXIS.byId)) delete FILES_AXIS.byId[k];
    for (const k of Object.keys(FILES_AXIS.hunkCount)) delete FILES_AXIS.hunkCount[k];
    for (let fi = 0; fi < (DATA.files || []).length; fi++) {
      const f = DATA.files[fi];
      if (!f.hunks || f.hunks.length === 0) continue;
      const hunk_ids = f.hunks.map((h) => h.id);
      const g = {
        id: `BF${fi}`,
        title: shortenPath(f.path),
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

  function shortenPath(path) {
    // Long paths overflow the sidebar; show the basename, fall back
    // to the full path if it's already short enough.
    if (!path) return "";
    if (path.length <= 28) return path;
    const idx = path.lastIndexOf("/");
    return idx >= 0 ? path.slice(idx + 1) : path;
  }

  function renderPRPanel(pr) {
    const panel = el("section", "pr-panel");
    panel.appendChild(el("h2", null, "PR summary"));
    panel.appendChild(el("p", null, pr.summary || "(no summary)"));
    if (pr.themes && pr.themes.length) {
      const themes = el("div", "themes");
      for (const t of pr.themes) themes.appendChild(el("span", null, t));
      panel.appendChild(themes);
    }
    return panel;
  }

  function renderFile(f) {
    const div = el("div", "file");
    div.dataset.id = f.id;
    const folded = isFolded(f.id, defaultFileFolded());
    div.classList.toggle("folded", folded);
    div.appendChild(renderFileHeader(f, folded));
    if (!folded) {
      const body = el("div", "file-body");
      const overview = renderFileOverview(f);
      if (overview) body.appendChild(overview);
      const top = gapBeforeFirstHunk(f);
      if (top) body.appendChild(renderGapChip(f, top));
      for (let i = 0; i < f.hunks.length; i++) {
        body.appendChild(renderHunk(f.hunks[i], f));
        const mid = gapAfterHunk(f, i);
        if (mid) body.appendChild(renderGapChip(f, mid));
      }
      div.appendChild(body);
    }
    return div;
  }

  // --- Inter-hunk context expansion ----------------------------------------

  function gapBeforeFirstHunk(f) {
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

  function gapAfterHunk(f, i) {
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

  function renderGapChip(f, gap) {
    const chip = el("div", "gap-chip");
    const count = gap.new_end - gap.new_start + 1;
    const icon = gap.position === "top" ? "⬆" : gap.position === "bottom" ? "⬇" : "⋯";
    const word = count === 1 ? "line" : "lines";
    const label = gap.position === "top" ? `expand ${count} ${word} above`
                : gap.position === "bottom" ? `expand ${count} ${word} below`
                : `expand ${count} hidden ${word}`;
    chip.innerHTML = `<span class="gap-icon">${icon}</span> <span class="gap-label">${label}</span>`;
    chip.title = `lines ${gap.new_start}–${gap.new_end}`;
    chip.addEventListener("click", () => {
      chip.replaceWith(renderGapExpansion(f, gap));
    });
    return chip;
  }

  function renderGapExpansion(f, gap) {
    const container = el("div", "gap-expansion");
    const collapse = el("button", "gap-collapse", "× collapse");
    collapse.title = "Hide these lines again";
    collapse.addEventListener("click", () => {
      container.replaceWith(renderGapChip(f, gap));
    });
    container.appendChild(collapse);

    // Same structural shape as a regular hunk: a .diff with two .half
    // children, each holding its side's rows. renderRow now returns
    // {old, new}, so we must dispatch each side to its own half.
    const diff = el("div", "diff");
    const halfOld = el("div", "half half-old");
    const halfNew = el("div", "half half-new");
    diff.appendChild(halfOld);
    diff.appendChild(halfNew);

    const count = gap.new_end - gap.new_start + 1;
    for (let i = 0; i < count; i++) {
      const ol = gap.old_start + i;
      const nl = gap.new_start + i;
      const text = f.head_lines[nl - 1] ?? "";
      const pair = renderRow({
        kind: "ctx", old_line: ol, new_line: nl,
        old_text: text, new_text: text,
      }, f);
      pair.old._scrPair = pair.new;
      pair.new._scrPair = pair.old;
      halfOld.appendChild(pair.old);
      halfNew.appendChild(pair.new);
    }
    container.appendChild(diff);
    return container;
  }

  function renderFileHeader(f, folded) {
    const hdr = el("div", "file-header");
    hdr.appendChild(chev(folded));
    hdr.appendChild(el("span", "file-path", f.path));
    hdr.appendChild(el("span", "file-summary", f.summary || ""));
    const meta = el("div", "file-meta");
    meta.appendChild(el("span", "adds", `+${f.adds}`));
    meta.appendChild(el("span", "dels", `-${f.dels}`));
    hdr.appendChild(meta);
    const smells = uniqueFileSmells(f);
    if (smells.length) {
      const badge = el("div", "file-meta");
      for (const sm of smells) badge.appendChild(smellPill({ tag: sm, note: "" }));
      hdr.appendChild(badge);
    }
    hdr.addEventListener("click", () => toggleFold(f.id, defaultFileFolded()));
    return hdr;
  }

  function uniqueFileSmells(f) {
    const s = new Set();
    for (const h of f.hunks) {
      for (const sm of h.smells || []) s.add(sm.tag);
      for (const seg of h.segments || []) for (const sm of seg.smells || []) s.add(sm.tag);
    }
    return Array.from(s);
  }

  function renderFileOverview(f) {
    // Only render when there's something worth showing. An empty "no symbols"
    // row just pushes the hunks further from the file header.
    const sym = f.symbols || {};
    const parts = [];
    if (sym.added && sym.added.length)    parts.push(`<span class="label">added:</span>${esc(sym.added.join(", "))}`);
    if (sym.modified && sym.modified.length) parts.push(`<span class="label">modified:</span>${esc(sym.modified.join(", "))}`);
    if (sym.removed && sym.removed.length) parts.push(`<span class="label">removed:</span>${esc(sym.removed.join(", "))}`);
    if (parts.length === 0) return null;
    const div = el("div", "file-overview");
    div.innerHTML = parts.join("&nbsp;&nbsp;");
    return div;
  }

  function renderHunk(h, f) {
    const div = el("div", "hunk");
    div.dataset.id = h.id;
    const folded = isFolded(h.id, defaultHunkFolded());
    div.classList.toggle("folded", folded);
    div.style.borderLeftColor = maxSeverityColor(h);
    div.appendChild(renderHunkHeader(h, folded));
    if (!folded) {
      if (h.segments && h.segments.length > 0 && defaultSegmentFolded() && !anySegmentOverridden(h, false)) {
        const list = el("div", "seg-list");
        for (const s of h.segments) list.appendChild(renderSegmentFolded(s));
        div.appendChild(list);
      } else {
        div.appendChild(renderHunkDiff(h, f));
      }
      if (h.context) {
        const c = el("div", "context-note");
        c.innerHTML = `<strong>context:</strong> ${esc(h.context)}`;
        div.appendChild(c);
      }
      if (h.refs && h.refs.length) {
        div.appendChild(renderRefs(h.refs));
      }
      // line_notes used to render here as a bottom-of-hunk block; they
      // are now attached inline by attachLineNotes() in renderHunkDiff.
    }
    return div;
  }

  function renderRefs(refs) {
    const div = el("div", "refs");
    div.appendChild(el("strong", null, "refs: "));
    for (const ref of refs) {
      const link = buildRefLink(ref);
      div.appendChild(link);
      if (ref.reason) div.appendChild(el("span", "ref-reason", " " + ref.reason + " "));
    }
    return div;
  }

  function buildRefLink(ref) {
    const pr = DATA.pr || {};
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

  function anySegmentOverridden(h, toValue) {
    return (h.segments || []).some(s => {
      const val = isFolded(s.id, defaultSegmentFolded());
      return val === toValue;
    });
  }

  function renderHunkHeader(h, folded) {
    const hdr = el("div", "hunk-header");
    hdr.appendChild(chev(folded));
    hdr.appendChild(el("span", "hunk-pos", h.header));
    let intent;
    if (h.intent) {
      intent = el("span", "hunk-intent", h.intent);
    } else if (DATA.pending && !h._failed) {
      // Still streaming. Distinguish "queued, model hasn't looked at
      // this yet" (static, dim) from "running, model is working on it
      // right now" (pulse). State is sourced from PROGRESS, which the
      // overview-start / hunk-start / hunk SSE events drive.
      const st = PROGRESS.byId[h.id];
      if (st === "running") {
        intent = el("span", "hunk-intent pending", "analysing…");
      } else {
        intent = el("span", "hunk-intent queued", "queued");
      }
    } else {
      intent = el("span", "hunk-intent empty", "(no intent — may need re-run)");
    }
    hdr.appendChild(intent);
    const meta = el("span", "hunk-meta");
    for (const sm of h.smells || []) meta.appendChild(smellPill(sm));
    if (h.confidence != null) {
      const conf = el("span", "confidence" + (h.confidence < 30 ? " low" : ""), `c=${h.confidence}`);
      conf.title = h.confidence < 30 ? "Low confidence — review carefully" : "Model confidence";
      meta.appendChild(conf);
    }
    if (h.context) {
      const icon = el("span", "context-icon", "ⓘ");
      icon.title = h.context;
      meta.appendChild(icon);
    }
    hdr.appendChild(meta);
    hdr.addEventListener("click", e => {
      e.stopPropagation();
      toggleFold(h.id, defaultHunkFolded());
    });
    return hdr;
  }

  function renderSegmentFolded(s) {
    const div = el("div", "segment");
    div.dataset.id = s.id;
    div.appendChild(chev(true));
    div.appendChild(el("span", "segment-range", `+${s.new_start}..+${s.new_start + s.new_count - 1}`));
    div.appendChild(el("span", s.intent ? "segment-intent" : "segment-intent empty", s.intent || "(no intent)"));
    for (const sm of s.smells || []) div.appendChild(smellPill(sm));
    div.addEventListener("click", e => {
      e.stopPropagation();
      toggleFold(s.id, defaultSegmentFolded());
    });
    return div;
  }

  // --- Custom side-by-side diff renderer -----------------------------------

  function renderHunkDiff(h, file) {
    if (STATE.renderedDiffs[h.id]) return STATE.renderedDiffs[h.id];
    const container = el("div", "diff");
    const halfOld = el("div", "half half-old");
    const halfNew = el("div", "half half-new");
    container.appendChild(halfOld);
    container.appendChild(halfNew);

    // Per-half row arrays, indexed by source-row position. These parallel
    // arrays (rowElsOld[i] + rowElsNew[i]) represent the two sides of the
    // same logical diff line, so fold toggles, annotation anchoring, and
    // subgrid row alignment can all address either side by index.
    const rowElsOld = [];
    const rowElsNew = [];
    for (const row of h.rows || []) {
      const pair = renderRow(row, file);
      // Cross-link the two wrappers so dynamic inserts (comments,
      // editors) can add a matching placeholder to the opposite half
      // without re-deriving the index.
      pair.old._scrPair = pair.new;
      pair.new._scrPair = pair.old;
      halfOld.appendChild(pair.old);
      halfNew.appendChild(pair.new);
      rowElsOld.push(pair.old);
      rowElsNew.push(pair.new);
    }
    attachIndentFolds(halfOld, halfNew, rowElsOld, rowElsNew, h.fold_regions || [], h.id);
    attachLineNotes(halfOld, halfNew, rowElsOld, rowElsNew, h.rows || [], h.line_notes || []);
    STATE.renderedDiffs[h.id] = container;
    return container;
  }

  // All annotation plumbing (row construction, arrow geometry, shadow
  // placeholders, reflow coalescing) lives in `annotations.ts`, compiled
  // to annotations.js and inlined into this HTML before viewer.js runs.
  // The classic-script surface is `window.ScrAnnotations` with methods
  // `attach(opts) -> handle`, `reflow(anchor)`, `reflowAll()`,
  // `watchViewport()`, `charRectInRow(row, col)`.

  // Line-note annotations are anchored to post-image lines → the
  // new-side half. The shadow anchor on the old-side keeps the two
  // halves aligned line-for-line.
  function attachLineNotes(halfOld, halfNew, rowElsOld, rowElsNew, rows, notes) {
    if (!notes.length || !rows.length) return;
    const byNewLine = new Map();
    for (let i = 0; i < rows.length; i++) {
      const ln = rows[i].new_line;
      if (ln !== null && ln !== undefined) byNewLine.set(ln, i);
    }
    for (const note of notes) {
      const idx = byNewLine.get(note.line);
      if (idx === undefined) continue;
      window.ScrAnnotations.attach({
        anchor: rowElsNew[idx],
        shadowAnchor: rowElsOld[idx],
        variant: "note",
        content: note.body || "",
      });
    }
  }

  // --- Indent-based code folding ------------------------------------------
  // Region ranges + summaries come pre-computed from the Python pipeline
  // (so the LLM can describe each one). Wire the chevron and the
  // summary row for the folded state.

  function attachIndentFolds(halfOld, halfNew, rowElsOld, rowElsNew, regions, hunkId) {
    for (const r of regions) attachOneFold(halfOld, halfNew, rowElsOld, rowElsNew, r, hunkId);
  }

  function attachOneFold(halfOld, halfNew, rowElsOld, rowElsNew, region, hunkId) {
    const bodyStart = region.body_start_idx;
    const bodyEnd = region.body_end_idx;
    if (bodyStart > bodyEnd) return;

    const headerOld = rowElsOld[region.header_idx];
    const headerNew = rowElsNew[region.header_idx];
    if (!headerOld && !headerNew) return;

    // Choose which half the fold chevron + summary live on. Prefer the
    // side whose content cell is non-empty; fall back to new-side.
    const side = isRowContentEmpty(headerNew) && !isRowContentEmpty(headerOld)
      ? "old" : "new";
    const anchor = side === "new" ? headerNew : headerOld;
    const shadow = side === "new" ? headerOld : headerNew;

    const marker = chev(/* folded */ false, "fold-chev");
    marker.setAttribute("role", "button");
    marker.setAttribute("tabindex", "0");

    let foldHandle = null;
    if (region.summary || region.has_changes) {
      const initialContent = region.summary
        || (canRequestFoldSummary(hunkId, region) ? "summarising…"
            : "(changes here; run augment to generate a description)");
      foldHandle = window.ScrAnnotations.attach({
        anchor,
        shadowAnchor: shadow,
        variant: "fold",
        content: initialContent,
      });
      if (!region.summary) {
        const box = foldHandle.element.querySelector(".annot-box");
        if (box) box.classList.add("missing");
        if (initialContent === "summarising…" && box) box.classList.add("pending");
      }
      // Fold defaults to open, so the summary + its placeholder start hidden.
      foldHandle.element.style.display = "none";
      if (foldHandle.placeholder) foldHandle.placeholder.style.display = "none";
    }

    marker.addEventListener("click", e => {
      e.stopPropagation();
      const nowOpen = marker.classList.toggle("open");
      for (let i = bodyStart; i <= bodyEnd; i++) {
        if (rowElsOld[i]) rowElsOld[i].style.display = nowOpen ? "" : "none";
        if (rowElsNew[i]) rowElsNew[i].style.display = nowOpen ? "" : "none";
      }
      if (foldHandle) {
        foldHandle.element.style.display = nowOpen ? "none" : "";
        if (foldHandle.placeholder) foldHandle.placeholder.style.display = nowOpen ? "none" : "";
        if (!nowOpen) foldHandle.resize();
      }
      // On close, if the region still has no summary, ask the server to
      // generate one. Single-flight per region: requestFoldSummary
      // guards against the user opening + closing repeatedly.
      if (!nowOpen && !region.summary && foldHandle
          && canRequestFoldSummary(hunkId, region)) {
        requestFoldSummary(hunkId, region, foldHandle);
      }
      // Showing/hiding the fold summary shifts every sibling annotation
      // below it; re-size any that share this fold's header as anchor.
      window.ScrAnnotations.reflow(anchor);
    });

    const contentCell = anchor && anchor.children[1];
    if (contentCell) contentCell.prepend(marker);
  }

  function isRowContentEmpty(rowEl) {
    if (!rowEl) return true;
    const content = rowEl.children[1];
    return !content || content.classList.contains("empty");
  }

  function renderRow(row, file) {
    // One logical diff row becomes two wrappers — one per half — each
    // holding exactly its side's two cells (lineno + content). Callers
    // append each wrapper into the matching half container; the outer
    // grid's row tracks align both halves via `grid-template-rows: subgrid`.
    const hasOld = row.old_line !== null && row.old_line !== undefined;
    const hasNew = row.new_line !== null && row.new_line !== undefined;
    const oldRow = el("div", `row row-${row.kind}`);
    oldRow.appendChild(renderLineno(row.old_line, "old", hasOld));
    oldRow.appendChild(renderContent(row.old_text, "old", hasOld, file));
    const newRow = el("div", `row row-${row.kind}`);
    newRow.appendChild(renderLineno(row.new_line, "new", hasNew));
    newRow.appendChild(renderContent(row.new_text, "new", hasNew, file));
    return { old: oldRow, new: newRow };
  }

  function renderLineno(line, side, present) {
    const c = el("span", `cell cell-lineno cell-lineno-${side}`);
    if (!present) {
      c.classList.add("empty");
      return c;
    }
    c.textContent = String(line);
    return c;
  }

  function renderContent(text, side, present, file) {
    const c = el("span", `cell cell-content cell-content-${side}`);
    if (!present) {
      c.classList.add("empty");
      return c;
    }
    const code = el("code", "hljs");
    const lang = file && file.language;
    if (window.hljs && lang) {
      try {
        code.innerHTML = hljs.highlight(text || " ", { language: lang, ignoreIllegals: true }).value;
      } catch (_) {
        code.textContent = text;
      }
    } else {
      code.textContent = text;
    }
    c.appendChild(code);
    return c;
  }

  // --- Severity color ------------------------------------------------------
  const SEV_ORDER = { info: 1, minor: 2, major: 3, critical: 4 };
  function maxSeverityColor(h) {
    let worst = 0, color = "var(--border)";
    const check = (sm) => {
      const def = SMELLS[sm.tag];
      if (!def) return;
      const s = SEV_ORDER[def.severity] || 0;
      if (s > worst) { worst = s; color = def.color; }
    };
    for (const sm of h.smells || []) check(sm);
    for (const seg of h.segments || []) for (const sm of seg.smells || []) check(sm);
    return color;
  }

  // --- Toggle + slider -----------------------------------------------------
  function toggleFold(id, currentDefault) {
    const current = isFolded(id, currentDefault);
    STATE.overrides[id] = !current;
    render();
  }

  function setGlobalFold(fold) {
    STATE.fold = fold;
    STATE.overrides = {};
    render();
  }

  function updateSliderButtons() {
    document.querySelectorAll(".fold-slider button").forEach(b => {
      b.classList.toggle("active", b.dataset.fold === STATE.fold);
    });
  }

  function updateStatus() {
    const s = document.getElementById("status-bar");
    if (!s) return;
    let smells = 0, critical = 0;
    for (const f of DATA.files) {
      for (const h of f.hunks) {
        for (const sm of h.smells || []) {
          smells++;
          if ((SMELLS[sm.tag] || {}).severity === "critical") critical++;
        }
        for (const seg of h.segments || []) {
          for (const sm of seg.smells || []) {
            smells++;
            if ((SMELLS[sm.tag] || {}).severity === "critical") critical++;
          }
        }
      }
    }
    s.textContent = `${DATA.files.length} files · ${smells} smells · ${critical} critical · keys 1-4 fold · space toggle · ? help`;
  }

  // --- Hash sync -----------------------------------------------------------
  function syncHash() {
    const parts = [`fold=${STATE.fold}`];
    for (const [id, folded] of Object.entries(STATE.overrides)) {
      parts.push(`${id}=${folded ? "f" : "o"}`);
    }
    const newHash = "#" + parts.join("&");
    if (window.location.hash !== newHash) {
      history.replaceState(null, "", newHash);
    }
  }

  function restoreHash() {
    const h = window.location.hash.slice(1);
    if (!h) return;
    for (const kv of h.split("&")) {
      const [k, v] = kv.split("=");
      if (k === "fold" && ["files", "hunks", "segments", "off"].includes(v)) STATE.fold = v;
      else if (k && v != null) STATE.overrides[k] = (v === "f");
    }
  }

  // --- Keyboard ------------------------------------------------------------
  function onKeydown(e) {
    const tag = (e.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea") return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    switch (e.key) {
      case "1": setGlobalFold("files"); e.preventDefault(); break;
      case "2": setGlobalFold("hunks"); e.preventDefault(); break;
      case "3": setGlobalFold("segments"); e.preventDefault(); break;
      case "4": setGlobalFold("off"); e.preventDefault(); break;
      case "?": toggleHelp(); e.preventDefault(); break;
      case "Escape": closeHelp(); break;
    }
  }

  function toggleHelp() {
    const o = document.getElementById("help-overlay");
    if (!o) return;
    o.classList.toggle("hidden");
  }
  function closeHelp() {
    const o = document.getElementById("help-overlay");
    if (o) o.classList.add("hidden");
  }

  // --- Utils ---------------------------------------------------------------
  function esc(s) {
    return String(s).replace(/[&<>"']/g, c => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;",
    }[c]));
  }

  // ==========================================================================
  // Reviewer comments (line-level).
  //
  // Each comment is anchored to {file_id, side, line}. When a session
  // endpoint is present, every mutation (new / edit / delete) PUT/DELETEs
  // to the server. Without one, comments persist in localStorage keyed
  // by file+side+line+head_sha, so a reload keeps them but they won't
  // round-trip back to a Claude Code session.
  // ==========================================================================

  const LS_KEY = `scr-comments:${(DATA.pr && DATA.pr.head_sha) || "local"}`;

  function commentStorageLoad() {
    if (SESSION_ENDPOINT) {
      fetch(`${SESSION_ENDPOINT}/comments`)
        .then(r => r.ok ? r.json() : { comments: [] })
        .then(d => {
          for (const c of d.comments || []) STATE.comments[c.id] = c;
          renderAllExistingComments();
        })
        .catch(() => { /* server may have exited; ignore */ });
      return;
    }
    try {
      const raw = localStorage.getItem(LS_KEY);
      if (!raw) return;
      const data = JSON.parse(raw);
      for (const c of data.comments || []) STATE.comments[c.id] = c;
      renderAllExistingComments();
    } catch (_) { /* ignore */ }
  }

  function commentStorageFlush() {
    if (SESSION_ENDPOINT) return;  // server round-trips per-mutation
    const payload = { comments: Object.values(STATE.comments) };
    try { localStorage.setItem(LS_KEY, JSON.stringify(payload)); } catch (_) {}
  }

  function saveComment(c) {
    STATE.comments[c.id] = c;
    if (SESSION_ENDPOINT) {
      return fetch(`${SESSION_ENDPOINT}/comments`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(c),
      }).then(r => r.ok ? r.json() : null)
        .catch(() => null);
    }
    commentStorageFlush();
    return Promise.resolve(c);
  }

  function deleteComment(id) {
    delete STATE.comments[id];
    if (SESSION_ENDPOINT) {
      return fetch(`${SESSION_ENDPOINT}/comments/${encodeURIComponent(id)}`, {
        method: "DELETE",
      }).catch(() => null);
    }
    commentStorageFlush();
    return Promise.resolve();
  }

  function postExit() {
    if (!SESSION_ENDPOINT) return Promise.resolve();
    return fetch(`${SESSION_ENDPOINT}/exit`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    }).then(() => { /* server exits soon */ }).catch(() => {});
  }

  // --- Anchor key + lookup -------------------------------------------------

  function commentKey(file, side, line) { return `${file}|${side}|${line}`; }

  function commentsFor(file, side, line) {
    const k = commentKey(file, side, line);
    return Object.values(STATE.comments).filter(
      c => commentKey(c.file, c.side, c.line) === k
    );
  }

  // --- Gutter affordance + click-to-comment --------------------------------
  // Installed via event delegation on the root so it survives re-renders.

  function installCommentGutter(appEl) {
    appEl.addEventListener("click", (e) => {
      const cell = e.target.closest(".cell-lineno");
      if (!cell || cell.classList.contains("empty")) return;
      const row = cell.parentElement;
      if (!row || !row.classList.contains("row")) return;
      const side = cell.classList.contains("cell-lineno-old") ? "old" : "new";
      const line = parseInt(cell.textContent.trim(), 10);
      if (isNaN(line)) return;
      const fileEl = row.closest(".file");
      const filePath = fileEl && fileEl.querySelector(".file-path")
        ? fileEl.querySelector(".file-path").textContent
        : "";
      openCommentEditor({ rowEl: row, side, line, file: filePath });
      e.stopPropagation();
    });
  }

  // Dynamic comment rows (display + editor) are each built via
  // ScrAnnotations.attach, with the comment-specific UI (edit/delete
  // buttons, or a textarea + save/cancel) nested inside the .annot-box.
  // Comment state lives in the caller (STATE.comments + persistence);
  // the annotation module just hosts the DOM.

  function openCommentEditor({ rowEl, side, line, file, existing }) {
    // Build the editor's body (textarea + Save/Cancel bar).
    const bodyWrap = el("div", "comment-editor-body");
    const ta = el("textarea", "comment-editor-input");
    ta.rows = 1;
    ta.placeholder = "Write a comment… (Enter to save, Shift-Enter for newline, Esc to cancel)";
    ta.value = existing ? existing.body : "";
    bodyWrap.appendChild(ta);
    const bar = el("div", "comment-editor-bar");
    const save = el("button", "comment-btn comment-btn-save", existing ? "Update" : "Save");
    const cancel = el("button", "comment-btn comment-btn-cancel", "Cancel");
    bar.appendChild(save);
    bar.appendChild(cancel);
    bodyWrap.appendChild(bar);

    const handle = window.ScrAnnotations.attach({
      anchor: rowEl,
      shadowAnchor: rowEl._scrPair || null,
      variant: "comment",
      content: bodyWrap,
      onInsert: (el) => {
        el.classList.add("annot-editor");
        // The annotation module applies "max-width: 64ch" to the box
        // by default; the editor wants the full half width instead
        // (see viewer.css .annot-editor .comment-editor-box rule).
        const box = el.querySelector(".annot-box");
        if (box) box.classList.add("comment-editor-box");
      },
    });

    function autosizeTextarea() {
      ta.style.height = "auto";
      ta.style.height = ta.scrollHeight + "px";
    }
    function close() { handle.remove(); }
    function submit() {
      const body = ta.value.trim();
      if (!body) { close(); return; }
      const id = (existing && existing.id) || `c-${Math.random().toString(36).slice(2, 10)}`;
      const now = Date.now() / 1000;
      const c = {
        id, file, side, line, body,
        created_at: existing ? existing.created_at : now,
        updated_at: now,
      };
      saveComment(c).then(() => {
        close();
        refreshCommentsForAnchor(rowEl, { file, side, line });
      });
    }

    save.addEventListener("click", e => { e.stopPropagation(); submit(); });
    cancel.addEventListener("click", e => { e.stopPropagation(); close(); });
    ta.addEventListener("keydown", e => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); }
      else if (e.key === "Escape") { e.preventDefault(); close(); }
      e.stopPropagation();
    });
    ta.addEventListener("input", autosizeTextarea);
    requestAnimationFrame(() => {
      autosizeTextarea();
      ta.focus();
      ta.setSelectionRange(ta.value.length, ta.value.length);
    });
  }

  function buildCommentRow(comment, anchorRowEl) {
    // Body of the comment: prose + action bar. The annotation module
    // wraps this in a .row-annotation .cell-annotation .annot-box.
    const bodyWrap = el("div", "comment-display-body");
    const body = el("div", "comment-body");
    body.textContent = comment.body;
    bodyWrap.appendChild(body);
    const bar = el("div", "comment-actions");
    const edit = el("button", "comment-btn comment-btn-edit", "edit");
    const del = el("button", "comment-btn comment-btn-del", "delete");
    bar.appendChild(edit);
    bar.appendChild(del);
    bodyWrap.appendChild(bar);

    const handle = window.ScrAnnotations.attach({
      anchor: anchorRowEl,
      shadowAnchor: anchorRowEl._scrPair || null,
      variant: "comment",
      content: bodyWrap,
      onInsert: (elRoot) => {
        elRoot.dataset.commentId = comment.id;
        const box = elRoot.querySelector(".annot-box");
        if (box) box.classList.add("comment-display");
      },
    });

    edit.addEventListener("click", e => {
      e.stopPropagation();
      handle.remove();
      openCommentEditor({
        rowEl: anchorRowEl, side: comment.side, line: comment.line,
        file: comment.file, existing: comment,
      });
    });
    del.addEventListener("click", e => {
      e.stopPropagation();
      deleteComment(comment.id).then(() => handle.remove());
    });
    return handle;
  }

  function refreshCommentsForAnchor(anchorRowEl, anchor) {
    removeCommentRowsAfter(anchorRowEl);
    const relevant = commentsFor(anchor.file, anchor.side, anchor.line)
      .sort((a, b) => (a.created_at || 0) - (b.created_at || 0));
    for (const c of relevant) {
      buildCommentRow(c, anchorRowEl);
    }
    // Any LLM annotations (line_notes, fold summaries) that also anchor
    // at this row now sit further from it — ScrAnnotations.reflow()
    // re-measures their arrows to stretch past the newly-inserted
    // comments.
    window.ScrAnnotations.reflow(anchorRowEl);
  }

  function removeCommentRowsAfter(anchorRowEl) {
    // Comment display rows carry a `data-comment-id`. Walk forward
    // from the anchor detaching each matching row (which also removes
    // its shadow placeholder). Stops at the first non-matching sibling.
    let n = anchorRowEl.nextSibling;
    while (n) {
      const next = n.nextSibling;
      const isCommentRow = n.nodeType === 1
        && n.classList.contains("row-annotation")
        && n.classList.contains("annot-comment")
        && !n.classList.contains("annot-editor")
        && n.dataset && n.dataset.commentId;
      if (!isCommentRow) break;
      window.ScrAnnotations.detach(n);
      n = next;
    }
  }

  function renderAllExistingComments() {
    // On load, walk the DOM for every row and reattach comments.
    // After the per-half restructure each .row lives inside a single half
    // and holds only [lineno, content]; the side is readable from the
    // lineno cell's class.
    const byAnchor = {};  // anchorKey -> list
    for (const c of Object.values(STATE.comments)) {
      const k = `${c.file}|${c.side}|${c.line}`;
      (byAnchor[k] ||= []).push(c);
    }
    document.querySelectorAll(".file").forEach(fileEl => {
      const filePath = fileEl.querySelector(".file-path")
        ? fileEl.querySelector(".file-path").textContent : "";
      fileEl.querySelectorAll(".row").forEach(row => {
        const linenoCell = row.children[0];
        if (!linenoCell || !linenoCell.classList.contains("cell-lineno")) return;
        if (linenoCell.classList.contains("empty")) return;
        const side = linenoCell.classList.contains("cell-lineno-old") ? "old" : "new";
        const n = parseInt(linenoCell.textContent.trim(), 10);
        if (isNaN(n)) return;
        const relevant = byAnchor[`${filePath}|${side}|${n}`];
        if (!relevant) return;
        refreshCommentsForAnchor(row, { file: filePath, side, line: n });
      });
    });
  }

  // --- Done button ---------------------------------------------------------

  function installDoneButton() {
    if (!SESSION_ENDPOINT) return;
    const bar = document.querySelector(".pr-bar");
    if (!bar) return;
    const btn = el("button", "done-btn", "Done");
    btn.title = "Finish review and return comments to the caller";
    btn.addEventListener("click", () => {
      btn.disabled = true;
      btn.textContent = "Sending…";
      postExit().then(() => { btn.textContent = "Done ✓"; });
    });
    bar.appendChild(btn);
  }

  // --- Boot ----------------------------------------------------------------
  function boot() {
    document.querySelectorAll(".fold-slider button").forEach(b => {
      b.addEventListener("click", () => setGlobalFold(b.dataset.fold));
    });
    const reset = document.getElementById("reset-btn");
    if (reset) reset.addEventListener("click", () => { STATE.overrides = {}; render(); });
    const help = document.getElementById("help-btn");
    if (help) help.addEventListener("click", toggleHelp);
    const overlay = document.getElementById("help-overlay");
    if (overlay) overlay.addEventListener("click", e => {
      if (e.target === overlay) closeHelp();
    });
    document.addEventListener("keydown", onKeydown);
    window.addEventListener("hashchange", () => { STATE.overrides = {}; restoreHash(); render(); });
    installCommentGutter(document.getElementById("app"));
    installDoneButton();
    restoreHash();
    render();
    commentStorageLoad();
    initProgress();
    installSessionEvents();
  }

  // Subscribe to the server's SSE channel and patch the page as
  // overview / per-hunk events arrive from the augment pipeline.
  // Events:
  //   - overview: PR-level summary, themes, semantic groups, and
  //               per-file summaries land in DATA; the sidebar and
  //               file headers re-render.
  //   - hunk:     one hunk's block (the same shape as DATA.files[].hunks[i])
  //               replaces its slot in DATA and the corresponding
  //               .hunk DOM node, dropping the "analysing…" placeholder.
  //   - done:     augmentation finished; any hunks still pending get
  //               the "(no intent — may need re-run)" copy. The SSE
  //               connection is closed.
  function installSessionEvents() {
    if (!SESSION_ENDPOINT || typeof EventSource === "undefined") return;
    let es;
    try {
      es = new EventSource(SESSION_ENDPOINT + "/events");
    } catch (_) {
      return;
    }
    es.addEventListener("overview-start", () => setOverviewState("running"));
    es.addEventListener("overview-failed", () => setOverviewState("failed"));
    es.addEventListener("overview", (e) => {
      setOverviewState("ok");
      try { applyOverviewPatch(JSON.parse(e.data)); } catch (_) { /* ignore */ }
    });
    es.addEventListener("hunk-start", (e) => {
      try {
        const p = JSON.parse(e.data);
        setHunkProgress(`H${p.file_idx}_${p.hunk_idx}`, "running");
      } catch (_) { /* ignore */ }
    });
    es.addEventListener("hunk", (e) => {
      try {
        const p = JSON.parse(e.data);
        setHunkProgress(`H${p.file_idx}_${p.hunk_idx}`, p.ok ? "ok" : "failed");
        applyHunkPatch(p);
      } catch (_) { /* ignore */ }
    });
    es.addEventListener("done", () => {
      finaliseStreaming();
      // Don't close `es` yet — fold-summary events fire later on the
      // same channel when other tabs (or this one) request summaries.
    });
    es.addEventListener("fold-summary", (e) => {
      try { applyFoldSummary(JSON.parse(e.data)); } catch (_) { /* ignore */ }
    });
  }

  // --- Fold-summary RPC ----------------------------------------------------

  function canRequestFoldSummary(hunkId, region) {
    if (!SESSION_ENDPOINT) return false;
    if (!hunkId) return false;
    if (region.new_start == null || region.new_end == null) return false;
    return true;
  }

  function requestFoldSummary(hunkId, region, foldHandle) {
    if (region._inflight || region.summary) return;
    region._inflight = true;
    setFoldBoxContent(foldHandle, "summarising…", {pending: true});
    const new_count = region.new_end - region.new_start + 1;
    fetch(SESSION_ENDPOINT + "/fold-summary", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        hunk_id: hunkId, new_start: region.new_start, new_count,
      }),
    })
      .then((r) => r.json().then((j) => ({status: r.status, body: j})))
      .then(({status, body}) => {
        region._inflight = false;
        if (status === 200 && body.summary) {
          region.summary = body.summary;
          setFoldBoxContent(foldHandle, body.summary, {});
        } else {
          setFoldBoxContent(
            foldHandle,
            "(summary failed — click to retry)",
            {failed: true},
            () => requestFoldSummary(hunkId, region, foldHandle),
          );
        }
      })
      .catch(() => {
        region._inflight = false;
        setFoldBoxContent(
          foldHandle,
          "(summary failed — click to retry)",
          {failed: true},
          () => requestFoldSummary(hunkId, region, foldHandle),
        );
      });
  }

  function setFoldBoxContent(foldHandle, text, classes, onClick) {
    if (!foldHandle || !foldHandle.element) return;
    const box = foldHandle.element.querySelector(".annot-box");
    if (!box) return;
    box.textContent = text;
    box.classList.remove("pending", "failed");
    if (classes.pending) box.classList.add("pending");
    if (classes.failed) box.classList.add("failed");
    // Replace any prior click handler. cloneNode keeps the DOM but
    // sheds listeners, which is the simplest cross-browser path.
    if (onClick) {
      const clone = box.cloneNode(true);
      clone.style.cursor = "pointer";
      clone.addEventListener("click", onClick);
      box.replaceWith(clone);
    }
    foldHandle.resize();
  }

  function applyFoldSummary(payload) {
    if (!payload || !payload.hunk_id || payload.summary == null) return;
    const [fi, hi] = payload.hunk_id.replace("H", "").split("_").map(Number);
    const f = DATA.files && DATA.files[fi];
    const h = f && f.hunks && f.hunks[hi];
    if (!h) return;
    const new_end = payload.new_start + payload.new_count - 1;
    const region = (h.fold_regions || []).find(
      (r) => r.new_start === payload.new_start && r.new_end === new_end,
    );
    if (!region) return;
    // Idempotency: same payload, no work. Avoids a redundant re-render
    // (and the fold popping back open) when our own POST also arrives
    // via the SSE broadcast loop.
    if (region.summary === payload.summary) return;
    region.summary = payload.summary;
    // If a local POST is in flight, the fetch handler will update the
    // fold box in place; re-rendering here would rebuild the hunk and
    // pop the user's just-closed fold back open. Let the local path
    // own the DOM update.
    if (region._inflight) return;
    // Cross-tab path: this region wasn't requested locally. Drop the
    // cached diff and let the next render rebuild with the summary.
    delete STATE.renderedDiffs[h.id];
    const existing = document.querySelector('.hunk[data-id="' + cssEscape(h.id) + '"]');
    if (existing && existing.parentNode) {
      const fresh = renderHunk(h, f);
      existing.parentNode.replaceChild(fresh, existing);
    }
  }

  // --- Progress strip ------------------------------------------------------
  // Builds the header strip the moment the page loads in pending mode.
  // Each square corresponds to one hunk in the pipeline's queue order
  // (file-then-hunk, skipping generated / binary files — same order the
  // terminal meter uses). Mutated in place as events arrive.
  function initProgress() {
    if (!DATA.pending) return;
    const root = document.getElementById("scr-progress");
    if (!root) return;
    const grid = root.querySelector(".scr-progress-grid");
    grid.innerHTML = "";
    PROGRESS.order = [];
    for (const f of DATA.files || []) {
      if (f.status === "generated" || f.status === "binary") continue;
      for (const h of f.hunks || []) {
        PROGRESS.byId[h.id] = "queued";
        PROGRESS.order.push(h.id);
        const sq = el("div", "sq");
        sq.dataset.id = h.id;
        sq.dataset.state = "queued";
        sq.title = `${f.path} ${h.header || ""}`.trim();
        sq.setAttribute("role", "listitem");
        sq.addEventListener("click", () => scrollToHunk(h.id));
        grid.appendChild(sq);
      }
    }
    root.querySelector(".scr-progress-total").textContent = String(PROGRESS.order.length);
    refreshProgressCounters();
    root.classList.remove("hidden");
  }

  function setHunkProgress(hunkId, state) {
    if (!(hunkId in PROGRESS.byId)) return;
    PROGRESS.byId[hunkId] = state;
    const sq = document.querySelector(`.scr-progress-grid .sq[data-id="${cssEscape(hunkId)}"]`);
    if (sq) sq.dataset.state = state;
    refreshProgressCounters();
    // Re-render the intent slot of the matching hunk so "queued" flips
    // to "analysing…" without waiting for the completion event.
    if (state === "running") repaintHunkHeader(hunkId);
  }

  function setOverviewState(state) {
    PROGRESS.overview = state;
    const el_ = document.querySelector(".scr-progress-overview");
    if (el_) el_.dataset.state = state;
  }

  function refreshProgressCounters() {
    let running = 0, queued = 0, ok = 0, failed = 0;
    for (const id of PROGRESS.order) {
      const s = PROGRESS.byId[id];
      if (s === "running") running++;
      else if (s === "ok") ok++;
      else if (s === "failed") failed++;
      else queued++;
    }
    const root = document.getElementById("scr-progress");
    if (!root) return;
    root.querySelector(".scr-progress-done").textContent = String(ok + failed);
    root.querySelector(".scr-progress-running").textContent = String(running);
    root.querySelector(".scr-progress-queued").textContent = String(queued);
    root.querySelector(".scr-progress-failed").textContent = String(failed);
  }

  function repaintHunkHeader(hunkId) {
    // Replace just the .hunk-header within the existing .hunk node so
    // we don't disturb expanded segments / line notes the reviewer is
    // already interacting with.
    const node = document.querySelector('.hunk[data-id="' + cssEscape(hunkId) + '"]');
    if (!node) return;
    const oldHdr = node.querySelector(".hunk-header");
    if (!oldHdr) return;
    const [fi, hi] = hunkId.replace("H", "").split("_").map(Number);
    const f = DATA.files && DATA.files[fi];
    const h = f && f.hunks && f.hunks[hi];
    if (!h) return;
    const folded = isFolded(h.id, defaultHunkFolded());
    const fresh = renderHunkHeader(h, folded);
    oldHdr.replaceWith(fresh);
  }

  function scrollToHunk(hunkId) {
    const node = document.querySelector('.hunk[data-id="' + cssEscape(hunkId) + '"]');
    if (node) node.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  function applyHunkPatch(payload) {
    const fi = payload.file_idx;
    const hi = payload.hunk_idx;
    if (!DATA.files || !DATA.files[fi]) return;
    const file = DATA.files[fi];
    if (!file.hunks || !file.hunks[hi]) return;
    if (payload.ok && payload.block) {
      file.hunks[hi] = payload.block;
    } else {
      // Failure: mark the slot so finaliseStreaming() / renderHunk
      // show the re-run copy instead of the pending spinner.
      file.hunks[hi].intent = "";
      file.hunks[hi]._failed = true;
    }
    // Re-render the single hunk in place. STATE.renderedDiffs caches
    // by hunk id; drop the cached entry so the new annotations and
    // (possibly different) fold regions get fresh DOM.
    delete STATE.renderedDiffs[file.hunks[hi].id];
    const fresh = renderHunk(file.hunks[hi], file);
    const existing = document.querySelector('.hunk[data-id="' + cssEscape(file.hunks[hi].id) + '"]');
    if (existing && existing.parentNode) {
      existing.parentNode.replaceChild(fresh, existing);
    }
  }

  function applyOverviewPatch(payload) {
    if (payload.pr) Object.assign(DATA.pr || (DATA.pr = {}), payload.pr);
    if (Array.isArray(payload.files)) {
      for (const fp of payload.files) {
        const f = DATA.files && DATA.files[fp.file_idx];
        if (!f) continue;
        if (fp.summary !== undefined) f.summary = fp.summary;
        if (fp.language) f.language = fp.language;
        if (fp.symbols) f.symbols = fp.symbols;
        if (fp.status) f.status = fp.status;
      }
    }
    if (Array.isArray(payload.groups)) {
      // The sidebar renderer reads from `GROUPS` — a const captured
      // at module load — so we must mutate the array in place rather
      // than reassigning DATA.groups (which would leave GROUPS still
      // pointing at the original empty array).
      GROUPS.length = 0;
      for (const g of payload.groups) GROUPS.push(g);
      DATA.groups = GROUPS;
      for (const k of Object.keys(GROUP_BY_ID)) delete GROUP_BY_ID[k];
      for (const k of Object.keys(HUNK_GROUP_COUNT)) delete HUNK_GROUP_COUNT[k];
      for (const g of GROUPS) {
        GROUP_BY_ID[g.id] = g;
        for (const hid of g.hunk_ids || []) {
          HUNK_GROUP_COUNT[hid] = (HUNK_GROUP_COUNT[hid] || 0) + 1;
        }
      }
    }
    // The PR header and groups sidebar live outside the hunk list and
    // are cheap to redraw; a full re-render keeps the logic in one
    // place and avoids drift between streamed and non-streamed paths.
    render();
  }

  function finaliseStreaming() {
    // Drop the pending flag so any hunks the server never sent an
    // event for (filtered, skipped, crashed mid-pass) render the
    // failure copy on the next re-render instead of the spinner.
    DATA.pending = false;
    // Hide the progress strip — it's only useful while the run is
    // streaming. The terminal meter has the same lifecycle.
    const root = document.getElementById("scr-progress");
    if (root) root.classList.add("hidden");
    render();
  }

  // Minimal CSS.escape polyfill — only needed because some older
  // browsers ship without `CSS.escape`. Hunk ids are simple ASCII
  // identifiers, so escaping is a defensive measure.
  function cssEscape(s) {
    if (window.CSS && typeof CSS.escape === "function") return CSS.escape(s);
    return String(s).replace(/[^a-zA-Z0-9_-]/g, c => "\\" + c);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
