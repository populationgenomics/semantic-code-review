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
  };

  // The augmentation progress strip lives in progress.ts (window.ScrProgress).
  // The sidebar (themes + files axes, active pill, filter logic) lives in
  // sidebar.ts (window.ScrSidebar). viewer.js calls into both; the per-
  // hunk intent-slot repaint stays here because it's about the hunk DOM.
  window.ScrSidebar.init(DATA);

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
    window.ScrSidebar.render();
    window.ScrSidebar.applyFilter();
    updateStatus();
    syncHash();
    updateSliderButtons();
    // Re-attach any loaded comments to freshly-rendered rows.
    window.ScrComments.renderAll();
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
      window.ScrFolds.attachFileFolds(div, f);
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
      if (fileEl) window.ScrFolds.attachFileFolds(fileEl, f);
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
      if (fileEl) window.ScrFolds.attachFileFolds(fileEl, f);
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

  // The viewer's fold story (file-level row stream + indent detection
  // + on-demand summary requests) lives in folds.ts as
  // `window.ScrFolds`. viewer.js calls attachFileFolds on every
  // structural change to a file body (initial render, gap
  // expand/collapse, hunk DOM replacement from applyFoldSummary).

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

  // --- Fold detection + summary (moved to folds.ts) -----------------------
  // attachFileFolds / computeFoldRegions / requestFoldSummary etc.
  // live in folds.ts (window.ScrFolds). viewer.js's only job is to
  // call attachFileFolds at the right times (initial render, gap
  // expand/collapse, applyFoldSummary cross-tab path).

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

  // Reviewer comments live in comments.ts (window.ScrComments).
  // Boot wires the gutter + loads existing; render() calls renderAll.


  // --- Done button ---------------------------------------------------------
  // Tells the review server we're finished. The server exits after
  // this fires; comments accumulated via window.ScrComments have
  // already round-tripped on each mutation. Single fetch, kept here
  // rather than in comments.ts to avoid coupling "I'm done" to the
  // comment storage layer.

  function installDoneButton() {
    if (!SESSION_ENDPOINT) return;
    const bar = document.querySelector(".pr-bar");
    if (!bar) return;
    const btn = el("button", "done-btn", "Done");
    btn.title = "Finish review and return comments to the caller";
    btn.addEventListener("click", () => {
      btn.disabled = true;
      btn.textContent = "Sending…";
      fetch(`${SESSION_ENDPOINT}/exit`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      }).catch(() => { /* server may exit before responding */ })
        .finally(() => { btn.textContent = "Done ✓"; });
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
    window.ScrComments.init(DATA);     // wires gutter + loads existing
    installDoneButton();
    restoreHash();
    render();
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

  // --- Fold-summary SSE patcher --------------------------------------------
  // The POST + retry plumbing is in folds.ts; this handler is what
  // viewer.js does when the *server* broadcasts a fold-summary back.
  // Same-tab paths short-circuit via `region._inflight` (the local
  // fetch handler in folds.ts owns the DOM update). Cross-tab paths
  // mutate DATA + re-render the affected hunk, then ask folds.ts to
  // re-attach chevrons over the freshly-rendered rows.
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
    if (fileEl) window.ScrFolds.attachFileFolds(fileEl, f);
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
      // Themes axis lives in sidebar.ts; refreshThemes mutates the
      // axis state in place. Keep DATA.groups in sync for any
      // consumer that still reads it directly.
      window.ScrSidebar.refreshThemes(payload.groups);
      DATA.groups = payload.groups;
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
