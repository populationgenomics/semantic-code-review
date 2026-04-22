// Semantic Code Review — viewer.
// Single foldable diff with IDE-style code folding at PR/file/hunk/segment levels.

(function () {
  "use strict";

  const DATA = JSON.parse(document.getElementById("scr-data").textContent);
  const SMELLS = DATA.smells_catalogue || {};

  const STATE = {
    fold: "segments",   // 'files' | 'hunks' | 'segments' | 'off'
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

  function chev(folded) {
    const c = el("span", "chevron", folded ? "▸" : "▾");
    return c;
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
      body.appendChild(renderFileOverview(f));
      for (const h of f.hunks) body.appendChild(renderHunk(h, f));
      div.appendChild(body);
    }
    return div;
  }

  function renderFileHeader(f, folded) {
    const hdr = el("div", "file-header");
    hdr.appendChild(chev(folded));
    hdr.appendChild(el("span", "file-path", f.path));
    const s = el("span", "file-summary", f.summary || "");
    hdr.appendChild(s);
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
    const div = el("div", "file-overview");
    const parts = [];
    const sym = f.symbols || {};
    if (sym.added && sym.added.length)    parts.push(`<span class="label">added:</span>${esc(sym.added.join(", "))}`);
    if (sym.modified && sym.modified.length) parts.push(`<span class="label">modified:</span>${esc(sym.modified.join(", "))}`);
    if (sym.removed && sym.removed.length) parts.push(`<span class="label">removed:</span>${esc(sym.removed.join(", "))}`);
    div.innerHTML = parts.join("&nbsp;&nbsp;") || "<span class='label'>no symbol changes detected</span>";
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
      const diffEl = renderHunkDiff(h);
      div.appendChild(diffEl);
      // Segments fold _within_ the diff: each segment's rows can collapse
      // behind a single anchor row carrying the intent + smells.
      attachSegmentFolds(diffEl, h);
      if (h.context) {
        const c = el("div", "context-note");
        c.innerHTML = `<strong>context:</strong> ${esc(h.context)}`;
        div.appendChild(c);
      }
      if (h.refs && h.refs.length) div.appendChild(renderRefs(h.refs));
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
    const r = el("div", "refs");
    r.appendChild(el("strong", null, "refs: "));
    for (const ref of refs) {
      const link = buildRefLink(ref);
      r.appendChild(link);
      if (ref.reason) {
        const reason = el("span", "ref-reason", " " + ref.reason);
        r.appendChild(reason);
      }
      r.appendChild(document.createTextNode("  "));
    }
    return r;
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

  // --- Segment fold, overlaid on the d2h diff rows -------------------------
  function attachSegmentFolds(diffEl, hunk) {
    if (!hunk.segments || hunk.segments.length === 0) return;
    const tbody = diffEl.querySelector(".d2h-diff-tbody") || diffEl.querySelector("tbody");
    if (!tbody) return;

    // If we've already attached anchors for this hunk in a previous render,
    // just re-apply visibility based on current state. We don't want to keep
    // re-inserting anchor rows.
    const existingAnchors = tbody.querySelectorAll("tr.segment-anchor");
    if (existingAnchors.length === hunk.segments.length) {
      for (const anchor of existingAnchors) {
        const segId = anchor.dataset.segId;
        const members = tbody.querySelectorAll(`tr.segment-member-${cssEsc(segId)}`);
        const folded = isFolded(segId, defaultSegmentFolded());
        applySegmentVisibility(anchor, members, folded);
      }
      return;
    }

    // First attachment: classify each row by new-side line number, then
    // group into segments.
    const rows = Array.from(tbody.querySelectorAll("tr")).filter(
      tr => !tr.classList.contains("d2h-info") && !tr.classList.contains("segment-anchor")
    );
    for (const tr of rows) {
      const linenos = tr.querySelectorAll(".d2h-code-side-linenumber");
      const cell = linenos.length >= 2 ? linenos[1] : linenos[0];
      const n = cell ? parseInt(cell.textContent.trim(), 10) : NaN;
      tr.dataset.newLine = isNaN(n) ? "" : String(n);
    }

    let segIdx = 0;
    let inSegment = null;
    const memberMap = {};  // segId -> [tr]
    for (const tr of rows) {
      const raw = tr.dataset.newLine;
      const n = raw === "" ? NaN : parseInt(raw, 10);
      while (segIdx < hunk.segments.length) {
        const seg = hunk.segments[segIdx];
        const segEnd = seg.new_start + seg.new_count - 1;
        if (!isNaN(n) && n > segEnd) { segIdx++; inSegment = null; continue; }
        if (!isNaN(n) && n < seg.new_start) { inSegment = null; break; }
        inSegment = seg;  // line is in seg (or row has no new line & we're mid-seg)
        break;
      }
      if (inSegment) {
        (memberMap[inSegment.id] ||= []).push(tr);
        tr.classList.add("segment-member", `segment-member-${cssClassSafe(inSegment.id)}`);
      }
    }

    for (const seg of hunk.segments) {
      const members = memberMap[seg.id];
      if (!members || members.length === 0) continue;
      const anchor = buildSegmentAnchorRow(seg, members[0].cells.length);
      members[0].parentNode.insertBefore(anchor, members[0]);
      const folded = isFolded(seg.id, defaultSegmentFolded());
      applySegmentVisibility(anchor, members, folded);
      anchor.addEventListener("click", e => {
        e.stopPropagation();
        toggleFold(seg.id, defaultSegmentFolded());
      });
    }
  }

  function buildSegmentAnchorRow(seg, colspan) {
    const tr = document.createElement("tr");
    tr.className = "segment-anchor";
    tr.dataset.segId = seg.id;
    const td = document.createElement("td");
    td.colSpan = colspan || 4;
    const inner = el("div", "segment-anchor-inner");
    const chevNode = chev(true);
    chevNode.classList.add("segment-chev");
    inner.appendChild(chevNode);
    inner.appendChild(el("span", "segment-range",
      `+${seg.new_start}..+${seg.new_start + seg.new_count - 1}`));
    inner.appendChild(el("span", seg.intent ? "segment-intent" : "segment-intent empty",
      seg.intent || "(no intent)"));
    for (const sm of seg.smells || []) inner.appendChild(smellPill(sm));
    td.appendChild(inner);
    tr.appendChild(td);
    return tr;
  }

  function applySegmentVisibility(anchor, members, folded) {
    anchor.classList.toggle("folded", folded);
    const chevNode = anchor.querySelector(".segment-chev");
    if (chevNode) chevNode.textContent = folded ? "▸" : "▾";
    for (const m of members) m.style.display = folded ? "none" : "";
  }

  function cssClassSafe(s) { return String(s).replace(/[^\w-]/g, "_"); }
  function cssEsc(s) { return (window.CSS && CSS.escape) ? CSS.escape(cssClassSafe(s)) : cssClassSafe(s); }

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

  function renderHunkDiff(h) {
    if (STATE.renderedDiffs[h.id]) return STATE.renderedDiffs[h.id];
    // `d2h-auto-color-scheme` flips diff2html's palette to match
    // prefers-color-scheme (dark + light built-in).
    const container = el("div", "hunk-diff d2h-auto-color-scheme");
    try {
      const html = window.Diff2Html.html(h.diff_text, {
        outputFormat: "side-by-side",
        drawFileList: false,
        matching: "lines",
      });
      container.innerHTML = html;
      if (window.hljs) {
        container.querySelectorAll(".d2h-code-line-ctn").forEach(node => {
          try { window.hljs.highlightElement(node); } catch (_) {}
        });
      }
    } catch (err) {
      container.textContent = "render error: " + err.message;
    }
    STATE.renderedDiffs[h.id] = container;
    return container;
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
    // Wire the slider buttons.
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
