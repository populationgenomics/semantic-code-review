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

  // The augmentation progress strip (header counter + grid of hunk
  // squares) lives in progress.ts as `window.ScrProgress`. viewer.js
  // calls into it on every overview / hunk lifecycle event; the
  // per-hunk intent-slot repaint stays here because it's about the
  // hunk DOM, not the strip.

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
      // Run a file-level fold pass once the body is assembled. This
      // detects indent regions spanning multiple stretches (e.g. a
      // function defined in expanded context above a hunk whose body
      // contains the hunk's rows) — slice 3 of fold-anywhere.
      attachFileFolds(div, f);
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
      // Structural change to the file — rebuild file-level folds so
      // newly-visible rows participate in indent fold detection.
      const fileEl = document.querySelector('.file[data-id="' + cssEscape(f.id) + '"]');
      if (fileEl) attachFileFolds(fileEl, f);
    });
    return chip;
  }

  function renderGapExpansion(f, gap) {
    const container = el("div", "gap-expansion");
    const collapse = el("button", "gap-collapse", "× collapse");
    collapse.title = "Hide these lines again";
    collapse.addEventListener("click", () => {
      container.replaceWith(renderGapChip(f, gap));
      // Structural change to the file — rebuild file-level folds.
      const fileEl = document.querySelector('.file[data-id="' + cssEscape(f.id) + '"]');
      if (fileEl) attachFileFolds(fileEl, f);
    });
    container.appendChild(collapse);

    const diff = el("div", "diff");
    const halfOld = el("div", "half half-old");
    const halfNew = el("div", "half half-new");
    diff.appendChild(halfOld);
    diff.appendChild(halfNew);

    const rows = [];
    const rowElsOld = [];
    const rowElsNew = [];
    const count = gap.new_end - gap.new_start + 1;
    for (let i = 0; i < count; i++) {
      const ol = gap.old_start + i;
      const nl = gap.new_start + i;
      const text = f.head_lines[nl - 1] ?? "";
      const rowRecord = {
        kind: "ctx", old_line: ol, new_line: nl,
        old_text: text, new_text: text,
      };
      rows.push(rowRecord);
      const pair = renderRow(rowRecord, f);
      pair.old._scrPair = pair.new;
      pair.new._scrPair = pair.old;
      halfOld.appendChild(pair.old);
      halfNew.appendChild(pair.new);
      rowElsOld.push(pair.old);
      rowElsNew.push(pair.new);
    }

    // Stash records + DOM refs on the container so the file-level
    // fold walker (attachFileFolds) can recover them later.
    container._scrRows = rows;
    container._scrRowElsOld = rowElsOld;
    container._scrRowElsNew = rowElsNew;

    container.appendChild(diff);
    return container;
  }

  // --- JS port of compute_fold_regions -------------------------------------
  // Mirrors viewer/hunk_layout.py:compute_fold_regions so the viewer can
  // detect folds inside expanded unchanged-context blocks without a
  // server round-trip. The two implementations must produce identical
  // region boundaries for the same row sequence.

  function rowIndent(row) {
    const text = row.kind === "del" ? row.old_text : row.new_text;
    if (!text || !text.trim()) return -1;
    let ind = 0;
    for (const ch of text) {
      if (ch === " ") ind += 1;
      else if (ch === "\t") ind += 4;
      else break;
    }
    return ind;
  }

  function computeFoldRegionsJs(rows) {
    const indents = rows.map(rowIndent);
    const nextNonBlank = (i) => {
      for (let j = i + 1; j < indents.length; j++) {
        if (indents[j] !== -1) return indents[j];
      }
      return null;
    };
    const raw = [];                     // (header_idx, body_end_idx)
    const stack = [];                   // (indent, header_idx)
    for (let i = 0; i < indents.length; i++) {
      const ind = indents[i];
      if (ind === -1) continue;
      while (stack.length && stack[stack.length - 1][0] >= ind) {
        const [, top_idx] = stack.pop();
        raw.push([top_idx, i - 1]);
      }
      const ni = nextNonBlank(i);
      if (ni !== null && ni > ind) stack.push([ind, i]);
    }
    while (stack.length) {
      const [, top_idx] = stack.pop();
      raw.push([top_idx, indents.length - 1]);
    }
    raw.sort((a, b) => a[0] - b[0]);
    const regions = [];
    for (const [header_idx, body_end] of raw) {
      const body_start = header_idx + 1;
      if (body_start > body_end) continue;
      const right_start = firstLine(rows, header_idx, body_end, "new_line");
      const right_end = lastLine(rows, header_idx, body_end, "new_line");
      const left_start = firstLine(rows, header_idx, body_end, "old_line");
      const left_end = lastLine(rows, header_idx, body_end, "old_line");
      const hasChanges = anyChangesInRange(rows, header_idx, body_end);
      let context;
      if (right_start != null && left_start != null && hasChanges) context = "both";
      else if (right_start != null) context = "right";
      else context = "left";
      regions.push({
        header_idx, body_start_idx: body_start, body_end_idx: body_end,
        context,
        right_start, right_end,
        left_start, left_end,
      });
    }
    return regions;
  }

  function firstLine(rows, start, end, attr) {
    for (let j = start; j <= end; j++) {
      if (rows[j][attr] != null) return rows[j][attr];
    }
    return null;
  }

  function lastLine(rows, start, end, attr) {
    for (let j = end; j >= start; j--) {
      if (rows[j][attr] != null) return rows[j][attr];
    }
    return null;
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
      // right now" (pulse). State comes from window.ScrProgress
      // (see progress.ts), which the overview-start / hunk-start /
      // hunk SSE events drive.
      const st = window.ScrProgress.getHunkState(h.id);
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
    attachLineNotes(halfOld, halfNew, rowElsOld, rowElsNew, h.rows || [], h.line_notes || []);
    // Stash row records + DOM refs on the .diff so the file-level
    // fold walker (attachFileFolds) can build a unified row stream
    // across this hunk and its surrounding expanded context.
    container._scrRows = h.rows || [];
    container._scrRowElsOld = rowElsOld;
    container._scrRowElsNew = rowElsNew;
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

  // --- File-level fold detection ------------------------------------------
  // The viewer's fold story across stretches (hunk body vs surrounding
  // expanded context) is unified here: walk every visible row in the
  // file, run the JS port of compute_fold_regions over the unified
  // sequence, and attach chevrons accordingly. A fold whose body spans
  // a hunk boundary still collapses the right rows because each row
  // carries its own DOM refs.
  //
  // Triggered on initial render and after any gap expand/collapse.
  // FILE_FOLD_STATE keeps the previously-attached chevrons + handles
  // so we can tear them down before each re-pass.

  const FILE_FOLD_STATE = Object.create(null);  // file.id -> { handles: [], chevrons: [] }

  function teardownFileFolds(fileId) {
    const s = FILE_FOLD_STATE[fileId];
    if (!s) return;
    for (const h of s.handles) {
      try { h.remove(); } catch (_) { /* ignore */ }
    }
    for (const c of s.chevrons) {
      try { c.remove(); } catch (_) { /* ignore */ }
    }
    delete FILE_FOLD_STATE[fileId];
  }

  function collectFileRows(fileEl) {
    // Walk the file body's children in DOM order; for each .hunk and
    // .gap-expansion container, pull the stashed row records + the
    // matching DOM elements. Folded hunks contribute nothing (their
    // .diff isn't in the DOM).
    const body = fileEl.querySelector(".file-body");
    if (!body) return [];
    const out = [];
    for (const child of body.children) {
      const cls = child.classList;
      let source = null;
      if (cls.contains("hunk")) {
        source = child.querySelector(".diff");
      } else if (cls.contains("gap-expansion")) {
        source = child;
      }
      if (!source || !source._scrRows) continue;
      const rows = source._scrRows;
      const oldEls = source._scrRowElsOld;
      const newEls = source._scrRowElsNew;
      for (let i = 0; i < rows.length; i++) {
        out.push({
          ...rows[i],
          oldEl: oldEls[i],
          newEl: newEls[i],
        });
      }
    }
    return out;
  }

  function findExistingFoldRecord(file, region) {
    // Look up a pre-existing fold_region in DATA whose address
    // matches the detected region — so any cached summary lands as
    // the box's initial content.
    const rs = region.right_start || 0, re_ = region.right_end || 0;
    const ls = region.left_start || 0, le = region.left_end || 0;
    for (const h of file.hunks || []) {
      for (const r of h.fold_regions || []) {
        if (
          (r.context || "right") === region.context
          && (r.right_start || 0) === rs && (r.right_end || 0) === re_
          && (r.left_start || 0) === ls && (r.left_end || 0) === le
        ) {
          return r;
        }
      }
    }
    return null;
  }

  function attachFileFolds(fileEl, file) {
    teardownFileFolds(file.id);
    const fileIdx = Number(file.id.replace("F", ""));
    const rows = collectFileRows(fileEl);
    if (rows.length === 0) return;
    const detected = computeFoldRegionsJs(rows);
    const handles = [];
    const chevrons = [];
    for (const det of detected) {
      const region = upsertFoldRegion(file, det, rows);
      const attached = attachOneFold(rows, region, fileIdx);
      if (!attached) continue;
      if (attached.foldHandle) handles.push(attached.foldHandle);
      if (attached.marker) chevrons.push(attached.marker);
    }
    FILE_FOLD_STATE[file.id] = { handles, chevrons };
  }

  function upsertFoldRegion(file, det, rows) {
    // Look up an existing fold_region matching this address. If found,
    // refresh its detected fields and return it — that's the canonical
    // persistent object the local POST handler and SSE updater both
    // mutate, so they need to be the same reference. Otherwise create
    // a new one and stash it on the file's first hunk's fold_regions
    // list (matches the server's persistence path).
    const candidate = {
      header_idx: det.header_idx,
      body_start_idx: det.body_start_idx,
      body_end_idx: det.body_end_idx,
      context: det.context,
      right_start: det.right_start, right_end: det.right_end,
      left_start: det.left_start, left_end: det.left_end,
      has_changes: anyChangesInRange(rows, det.header_idx, det.body_end_idx),
      summary: "",
    };
    const existing = findExistingFoldRecord(file, candidate);
    if (existing) {
      existing.header_idx = candidate.header_idx;
      existing.body_start_idx = candidate.body_start_idx;
      existing.body_end_idx = candidate.body_end_idx;
      existing.has_changes = candidate.has_changes;
      return existing;
    }
    if (file.hunks && file.hunks.length > 0) {
      if (!file.hunks[0].fold_regions) file.hunks[0].fold_regions = [];
      file.hunks[0].fold_regions.push(candidate);
    }
    return candidate;
  }

  function anyChangesInRange(rows, start, end) {
    for (let i = start; i <= end; i++) {
      const k = rows[i].kind;
      if (k === "ins" || k === "del" || k === "pair") return true;
    }
    return false;
  }

  function attachOneFold(rows, region, fileIdx) {
    const bodyStart = region.body_start_idx;
    const bodyEnd = region.body_end_idx;
    if (bodyStart > bodyEnd) return null;

    const headerRow = rows[region.header_idx];
    if (!headerRow) return null;
    const headerOld = headerRow.oldEl;
    const headerNew = headerRow.newEl;
    if (!headerOld && !headerNew) return null;

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
    const canSummarise = canRequestFoldSummary(fileIdx, region);
    if (region.summary || region.has_changes || canSummarise) {
      const initialContent = region.summary
        || (canSummarise ? "summarising…"
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
      // Walk the file-level row stream so a fold whose body spans
      // multiple containers (hunk body + adjacent expanded context)
      // still hides every row that belongs to it.
      for (let i = bodyStart; i <= bodyEnd; i++) {
        const r = rows[i];
        if (!r) continue;
        if (r.oldEl) r.oldEl.style.display = nowOpen ? "" : "none";
        if (r.newEl) r.newEl.style.display = nowOpen ? "" : "none";
      }
      if (foldHandle) {
        foldHandle.element.style.display = nowOpen ? "none" : "";
        if (foldHandle.placeholder) foldHandle.placeholder.style.display = nowOpen ? "none" : "";
        if (!nowOpen) foldHandle.resize();
      }
      if (!nowOpen && !region.summary && foldHandle
          && canRequestFoldSummary(fileIdx, region)) {
        requestFoldSummary(fileIdx, region, foldHandle);
      }
      window.ScrAnnotations.reflow(anchor);
    });

    const contentCell = anchor && anchor.children[1];
    if (contentCell) contentCell.prepend(marker);
    return { marker, foldHandle };
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
    window.ScrProgress.init(DATA);
    installSessionEvents();
  }

  // Subscribe to the server's SSE channel (wire + parse handled in
  // sse.ts via window.ScrSse) and patch the viewer state as events
  // arrive. The handlers are responsible for the viewer-side effects:
  // updating the progress strip, mutating DATA, and re-rendering the
  // affected DOM. sse.ts itself stays diff-agnostic.
  function installSessionEvents() {
    if (!SESSION_ENDPOINT) return;
    const Progress = window.ScrProgress;
    window.ScrSse.connect(SESSION_ENDPOINT, {
      overviewStart: () => Progress.setOverviewState("running"),
      overviewFailed: () => Progress.setOverviewState("failed"),
      overview: (payload) => {
        Progress.setOverviewState("ok");
        applyOverviewPatch(payload);
      },
      hunkStart: (payload) => {
        const hunkId = `H${payload.file_idx}_${payload.hunk_idx}`;
        Progress.setHunkState(hunkId, "running");
        // Flip the per-hunk intent slot from "queued" to "analysing…"
        // without waiting for the completion event.
        repaintHunkHeader(hunkId);
      },
      hunk: (payload) => {
        Progress.setHunkState(
          `H${payload.file_idx}_${payload.hunk_idx}`,
          payload.ok ? "ok" : "failed",
        );
        applyHunkPatch(payload);
      },
      done: () => finaliseStreaming(),
      foldSummary: (payload) => applyFoldSummary(payload),
    });
  }

  // --- Fold-summary RPC ----------------------------------------------------

  function canRequestFoldSummary(fileIdx, region) {
    if (!SESSION_ENDPOINT) return false;
    if (fileIdx == null) return false;
    return foldAddress(region) !== null;
  }

  // Resolve a fold region to its v2 request shape — file-level
  // identifiers plus either or both `right_*`/`left_*` line ranges
  // depending on context. Returns null when the region isn't
  // addressable (shouldn't happen — compute_fold_regions always
  // produces a populated range matching the context).
  function foldAddress(region) {
    const context = region.context || "right";
    const addr = { context };
    if (context === "right" || context === "both") {
      if (region.right_start == null || region.right_end == null) return null;
      addr.right_start = region.right_start;
      addr.right_end = region.right_end;
    }
    if (context === "left" || context === "both") {
      if (region.left_start == null || region.left_end == null) return null;
      addr.left_start = region.left_start;
      addr.left_end = region.left_end;
    }
    return addr;
  }

  function requestFoldSummary(fileIdx, region, foldHandle) {
    if (region._inflight || region.summary) return;
    const addr = foldAddress(region);
    if (!addr) return;
    region._inflight = true;
    setFoldBoxContent(foldHandle, "summarising…", {pending: true});
    fetch(SESSION_ENDPOINT + "/fold-summary", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ file_idx: fileIdx, ...addr }),
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
    if (!payload || payload.summary == null) return;
    if (payload.file_idx == null) return;
    const f = DATA.files && DATA.files[payload.file_idx];
    if (!f) return;
    const ctx = payload.context || "right";
    const rs = payload.right_start || 0, re_ = payload.right_end || 0;
    const ls = payload.left_start || 0, le = payload.left_end || 0;
    // Regions live on individual hunks but are addressed at the file
    // level; walk every hunk's fold_regions for the matching key.
    let region = null;
    let hostHunk = null;
    for (const h of f.hunks || []) {
      for (const r of h.fold_regions || []) {
        if (
          (r.context || "right") === ctx
          && (r.right_start || 0) === rs && (r.right_end || 0) === re_
          && (r.left_start || 0) === ls && (r.left_end || 0) === le
        ) {
          region = r; hostHunk = h; break;
        }
      }
      if (region) break;
    }
    if (!region || !hostHunk) return;
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
    // Cross-tab path: drop the cached diff and let the next render
    // rebuild with the summary. Replacing the hunk DOM also blows
    // away its fold chevrons — re-run the file-level fold pass so
    // they come back attached to the freshly-rendered rows.
    delete STATE.renderedDiffs[hostHunk.id];
    const existing = document.querySelector('.hunk[data-id="' + cssEscape(hostHunk.id) + '"]');
    if (existing && existing.parentNode) {
      const fresh = renderHunk(hostHunk, f);
      existing.parentNode.replaceChild(fresh, existing);
    }
    const fileEl = document.querySelector('.file[data-id="' + cssEscape(f.id) + '"]');
    if (fileEl) attachFileFolds(fileEl, f);
  }

  // --- Per-hunk DOM repaint -----------------------------------------------
  // The progress strip itself lives in progress.ts (`window.ScrProgress`).
  // viewer.js only owns the per-hunk intent-slot repaint — the strip
  // module shouldn't reach into the hunk DOM, and we don't want to
  // re-render the whole hunk when only the placeholder copy changes.
  function repaintHunkHeader(hunkId) {
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
    window.ScrProgress.finalise();
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
