// Semantic Code Review — viewer.
// Single foldable diff with IDE-style code folding at PR/file/hunk/segment levels.
// Custom side-by-side diff renderer driven by pre-paired rows from the Python
// build step (no diff2html dependency).

(function () {
  "use strict";

  const DATA = JSON.parse(document.getElementById("scr-data").textContent);
  const SMELLS = DATA.smells_catalogue || {};

  const STATE = {
    fold: "hunks",      // 'files' | 'hunks' | 'segments' | 'off'
    overrides: {},      // regionId -> bool (true = folded)
    renderedDiffs: {},  // hunkId -> pre-rendered <div>
  };

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
    updateStatus();
    syncHash();
    updateSliderButtons();
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

    const diff = el("div", "diff");
    const count = gap.new_end - gap.new_start + 1;
    for (let i = 0; i < count; i++) {
      const ol = gap.old_start + i;
      const nl = gap.new_start + i;
      const text = f.head_lines[nl - 1] ?? "";
      diff.appendChild(renderRow({
        kind: "ctx", old_line: ol, new_line: nl,
        old_text: text, new_text: text,
      }, f));
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
      if (h.line_notes && h.line_notes.length) {
        const ln = el("div", "line-notes");
        ln.innerHTML = "<strong>notes:</strong> ";
        for (const n of h.line_notes) {
          const s = el("span", "note", `+${n.line}: ${n.body}`);
          ln.appendChild(s);
        }
        div.appendChild(ln);
      }
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
    const intent = el("span", h.intent ? "hunk-intent" : "hunk-intent empty", h.intent || "(no intent — may need re-run)");
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
    const rowEls = [];
    for (const row of h.rows || []) {
      const re = renderRow(row, file);
      container.appendChild(re);
      rowEls.push(re);
    }
    attachIndentFolds(rowEls, h.rows || []);
    STATE.renderedDiffs[h.id] = container;
    return container;
  }

  // --- Indent-based code folding ------------------------------------------

  function attachIndentFolds(rowEls, rows) {
    const indents = rows.map(rowIndent);
    const regions = computeFoldRegions(indents);
    for (const r of regions) addFoldChevron(rowEls, r);
  }

  function rowIndent(row) {
    // Use the side whose content survives: new-side for ctx/ins/pair,
    // old-side for del rows. Blank lines (whitespace-only) don't open or
    // close fold regions.
    const text = (row.kind === "del") ? row.old_text : row.new_text;
    if (text === "" || !text.trim()) return -1;
    let ind = 0;
    for (let i = 0; i < text.length; i++) {
      const ch = text[i];
      if (ch === " ") ind++;
      else if (ch === "\t") ind += 4;
      else break;
    }
    return ind;
  }

  function computeFoldRegions(indents) {
    // A fold region opens at a row whose next non-blank row has deeper
    // indent. It closes at the next non-blank row whose indent is <= the
    // header's indent. Regions nest; each opens its own entry on the stack.
    const regions = [];
    const stack = [];
    function nextNonBlank(i) {
      for (let j = i + 1; j < indents.length; j++) {
        if (indents[j] !== -1) return indents[j];
      }
      return null;
    }
    for (let i = 0; i < indents.length; i++) {
      const ind = indents[i];
      if (ind === -1) continue;
      while (stack.length && stack[stack.length - 1].indent >= ind) {
        const top = stack.pop();
        regions.push({ headerIdx: top.headerIdx, bodyEnd: i - 1 });
      }
      const ni = nextNonBlank(i);
      if (ni !== null && ni > ind) {
        stack.push({ indent: ind, headerIdx: i });
      }
    }
    while (stack.length) {
      const top = stack.pop();
      regions.push({ headerIdx: top.headerIdx, bodyEnd: indents.length - 1 });
    }
    return regions;
  }

  function addFoldChevron(rowEls, region) {
    const headerEl = rowEls[region.headerIdx];
    if (!headerEl) return;
    const bodyStart = region.headerIdx + 1;
    const bodyEnd = region.bodyEnd;
    if (bodyStart > bodyEnd) return;

    // Folded state by default? We want the code to start expanded so the
    // reviewer sees full content; the chevron therefore starts in the
    // `.open` state and toggles back to collapsed on click.
    const marker = chev(/* folded */ false, "fold-chev");
    marker.setAttribute("role", "button");
    marker.setAttribute("tabindex", "0");
    marker.addEventListener("click", e => {
      e.stopPropagation();
      const nowOpen = marker.classList.toggle("open");
      for (let i = bodyStart; i <= bodyEnd; i++) {
        if (rowEls[i]) rowEls[i].style.display = nowOpen ? "" : "none";
      }
    });

    // Prepend to whichever content cell has visible text on the header row.
    // Children: [old-lineno, old-content, new-lineno, new-content].
    const children = headerEl.children;
    const newContent = children[3];
    const oldContent = children[1];
    if (newContent && !newContent.classList.contains("empty")) {
      newContent.prepend(marker);
    } else if (oldContent) {
      oldContent.prepend(marker);
    }
  }

  function renderRow(row, file) {
    const wrapper = el("div", `row row-${row.kind}`);
    const hasOld = row.old_line !== null && row.old_line !== undefined;
    const hasNew = row.new_line !== null && row.new_line !== undefined;
    wrapper.appendChild(renderLineno(row.old_line, "old", hasOld));
    wrapper.appendChild(renderContent(row.old_text, "old", hasOld, file));
    wrapper.appendChild(renderLineno(row.new_line, "new", hasNew));
    wrapper.appendChild(renderContent(row.new_text, "new", hasNew, file));
    return wrapper;
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
    restoreHash();
    render();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
