// Semantic Code Review — viewer.
// Single foldable diff with IDE-style code folding at PR/file/hunk/segment levels.
// Custom side-by-side diff renderer driven by pre-paired rows from the Python
// build step (no diff2html dependency).

(function () {
  "use strict";

  const DATA = JSON.parse(document.getElementById("scr-data").textContent);
  const SMELLS = DATA.smells_catalogue || {};

  const SESSION_ENDPOINT = (() => {
    const m = document.querySelector('meta[name="scr-session-endpoint"]');
    return m ? m.getAttribute("content") : "";
  })();

  const STATE = {
    fold: "hunks",      // 'files' | 'hunks' | 'segments' | 'off'
    overrides: {},      // regionId -> bool (true = folded)
    renderedDiffs: {},  // hunkId -> pre-rendered <div>
    comments: {},       // id -> Comment
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
    // Re-attach any loaded comments to freshly-rendered rows.
    if (Object.keys(STATE.comments).length) renderAllExistingComments();
    // Annotation arrows attached during render were sized while the
    // tree was still detached (no layout). Re-size once now that
    // everything is in the document so line-note arrows land at the
    // right height.
    requestAnimationFrame(resizeAllAnnotArrows);
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
    attachIndentFolds(container, rowEls, h.fold_regions || []);
    attachLineNotes(container, rowEls, h.rows || [], h.line_notes || []);
    STATE.renderedDiffs[h.id] = container;
    return container;
  }

  // Inline line-note annotations: each {line, body} entry is placed as an
  // annotation row directly under the post-image row it describes, using
  // the same emacs-flycheck-style row builder as fold summaries. The
  // previous behaviour collected them into a single "notes:" block at
  // the bottom of the hunk, which made it hard to see which line each
  // note applied to.
  function attachLineNotes(container, rowEls, rows, notes) {
    if (!notes.length || !rows.length) return;
    // Map post-image line number -> row element (last occurrence wins
    // for safety, though duplicates shouldn't happen in a valid hunk).
    const byNewLine = new Map();
    for (let i = 0; i < rows.length; i++) {
      const ln = rows[i].new_line;
      if (ln !== null && ln !== undefined) byNewLine.set(ln, rowEls[i]);
    }
    for (const note of notes) {
      const anchor = byNewLine.get(note.line);
      if (!anchor) continue;  // note points at a line that isn't in the hunk
      const annotRow = buildAnnotationRow({
        anchorRowEl: anchor,
        side: pickAnnotationSide(anchor),
        text: note.body,
        missing: false,
        variant: "note",
      });
      if (anchor.nextSibling) {
        anchor.parentNode.insertBefore(annotRow, anchor.nextSibling);
      } else {
        anchor.parentNode.appendChild(annotRow);
      }
      // Measure arrow after the row is in the DOM. Deferred because the
      // annotation box needs layout to report its height.
      if (annotRow._scrSizeArrow) {
        requestAnimationFrame(annotRow._scrSizeArrow);
      }
    }
  }

  // --- Indent-based code folding ------------------------------------------
  // Region ranges + summaries come pre-computed from the Python pipeline
  // (so the LLM can describe each one). We just wire the chevron and the
  // summary row for the folded state.

  function attachIndentFolds(container, rowEls, regions) {
    for (const r of regions) attachOneFold(container, rowEls, r);
  }

  function attachOneFold(container, rowEls, region) {
    const headerEl = rowEls[region.header_idx];
    if (!headerEl) return;
    const bodyStart = region.body_start_idx;
    const bodyEnd = region.body_end_idx;
    if (bodyStart > bodyEnd) return;

    const marker = chev(/* folded */ false, "fold-chev");
    marker.setAttribute("role", "button");
    marker.setAttribute("tabindex", "0");

    // Emacs-flycheck-style boxed annotation: a dedicated row below the
    // anchored line, connected by an L-shaped SVG arrow pointing from
    // under the first non-whitespace character on the anchor line down
    // and right into the text box. Hidden while the fold is open.
    const annotRow = region.summary || region.has_changes
      ? buildAnnotationRow({
          anchorRowEl: headerEl,
          side: pickAnnotationSide(headerEl),
          text: region.summary,
          missing: !region.summary,
          variant: "fold",
        })
      : null;
    if (annotRow) {
      annotRow.style.display = "none";
      if (headerEl.nextSibling) {
        headerEl.parentNode.insertBefore(annotRow, headerEl.nextSibling);
      } else {
        headerEl.parentNode.appendChild(annotRow);
      }
    }

    marker.addEventListener("click", e => {
      e.stopPropagation();
      const nowOpen = marker.classList.toggle("open");
      for (let i = bodyStart; i <= bodyEnd; i++) {
        if (rowEls[i]) rowEls[i].style.display = nowOpen ? "" : "none";
      }
      if (annotRow) {
        annotRow.style.display = nowOpen ? "none" : "";
        if (!nowOpen && annotRow._scrSizeArrow) annotRow._scrSizeArrow();
      }
      // Showing/hiding the fold summary shifts every sibling annotation
      // below it; re-size any that share this fold's header as anchor.
      resizeAnnotSiblings(headerEl);
    });

    // Prepend chevron to whichever content cell has visible text on the
    // header row. Children: [old-lineno, old-content, new-lineno, new-content].
    const children = headerEl.children;
    const newContent = children[3];
    const oldContent = children[1];
    const contentCell = (newContent && !newContent.classList.contains("empty"))
      ? newContent : oldContent;
    if (!contentCell) return;
    contentCell.prepend(marker);
  }

  // --- Annotation rows (emacs flycheck look) ------------------------------
  // Used for fold descriptions now; cross-row annotations like line-notes and
  // ref pointers can reuse the same row builder once we wire them up.

  function pickAnnotationSide(anchorRowEl) {
    // We annotate whichever content cell is populated (new by default, old
    // for pure delete rows). The arrow anchors at the first non-whitespace
    // character in that cell's code text.
    const cells = anchorRowEl.children;
    const newCell = cells[3];
    if (newCell && !newCell.classList.contains("empty")) return "new";
    return "old";
  }

  function buildAnnotationRow(opts) {
    const { anchorRowEl, side, text, missing, variant } = opts;
    const row = el("div", `row row-annotation${variant ? ` annot-${variant}` : ""}`);
    const cell = el("div", `cell-annotation cell-annotation-${side}`);
    cell.appendChild(svgAnnotArrow());
    const box = el("div", "annot-box");
    if (missing) {
      box.classList.add("missing");
      box.textContent = "(changes here; run augment to generate a description)";
    } else {
      box.textContent = text || "";
    }
    cell.appendChild(box);
    row.appendChild(cell);
    // Anchor + side stashed so sizeAnnotArrow can (re-)measure on resize.
    row._scrAnchor = anchorRowEl;
    row._scrSide = side;
    row._scrSizeArrow = () => sizeAnnotArrow(row);
    return row;
  }

  // Position + size the SVG after the box is in the DOM. Margin-left is
  // measured in pixels so the arrow's vertical segment lands exactly under
  // the anchor line's first printing character. Margin-top lifts the arrow
  // into the row above. Arrow tip sits at the vertical midpoint of the box.
  function sizeAnnotArrow(annotRow) {
    const box = annotRow.querySelector(".annot-box");
    const svg = annotRow.querySelector("svg.annot-arrow");
    const cell = annotRow.querySelector(".cell-annotation");
    if (!box || !svg || !cell) return;
    const boxH = box.offsetHeight;
    if (boxH <= 0) return;

    // topOverrun = how far above this annotation row the arrow should
    // rise. We want it to terminate at the vertical midline of the
    // anchor row so the arrow visually lands on the code line's text,
    // not at the row's bottom border (which leaves a ~half-row gap
    // between the arrow head and the line). The same rule handles
    // stacked annotations: when a comment sits between this note and
    // its anchor, rowRect.top is farther down, so topOverrun gets
    // larger automatically — the two arrows end at the same midline,
    // rendering as concentric L-shapes pointing at one line.
    const anchor = annotRow._scrAnchor;
    const minOverrun = 6;
    let topOverrun = minOverrun;
    if (anchor) {
      const rowRect = annotRow.getBoundingClientRect();
      const anchorRect = anchor.getBoundingClientRect();
      const anchorMidY = (anchorRect.top + anchorRect.bottom) / 2;
      topOverrun = Math.max(minOverrun, rowRect.top - anchorMidY);
    }
    const totalH = topOverrun + boxH;
    const midY = topOverrun + boxH / 2;
    const tipX = 17;
    const head = 4;
    const svgW = 20;
    const vLineX = 2;   // x-coord of the arrow's vertical segment in SVG space
    svg.setAttribute("height", String(totalH));
    svg.setAttribute("width", String(svgW));
    svg.setAttribute("viewBox", `0 0 ${svgW} ${totalH}`);
    svg.style.marginTop = `-${topOverrun}px`;

    // Horizontal alignment: put the SVG's vLineX at the character
    // midpoint of the nth character in the anchor row (n = count of
    // annotation siblings stacked *below* this one for the same
    // anchor). This staggers each stacked arrow across one monospace
    // character so they don't overlap: the annotation closest to the
    // anchor (which has the shortest vertical span) sits one char to
    // the right of the annotation below it, two to the right of the
    // annotation below that, etc.
    const side = annotRow._scrSide || "new";
    if (anchor) {
      const offset = annotationsBelow(annotRow, anchor);
      const anchorX = charCenterAt(anchor, side, offset);
      if (anchorX !== null) {
        const cellRect = cell.getBoundingClientRect();
        const cs = window.getComputedStyle(cell);
        const padL = parseFloat(cs.paddingLeft) || 0;
        const marginL = anchorX - cellRect.left - padL - vLineX;
        svg.style.marginLeft = `${Math.max(0, marginL)}px`;
      }
    }

    const path = svg.querySelector("path");
    path.setAttribute(
      "d",
      `M ${vLineX} 0 L ${vLineX} ${midY} L ${tipX} ${midY} ` +
      `M ${tipX - head} ${midY - head} L ${tipX} ${midY} L ${tipX - head} ${midY + head}`,
    );
  }

  // Re-size all currently-visible annotation arrows on window resize, since
  // box text can reflow and change height.
  window.addEventListener("resize", resizeAllAnnotArrows);

  function resizeAllAnnotArrows() {
    document.querySelectorAll(".row-annotation").forEach(r => {
      if (r.style.display !== "none") sizeAnnotArrow(r);
    });
  }

  // Re-size every annotation row anchored to the same source row. Call
  // after inserting or removing a row so stacked arrows restretch to
  // reach the real anchor rather than a sibling annotation above them.
  function resizeAnnotSiblings(anchor) {
    if (!anchor || !anchor.parentNode) return;
    const all = anchor.parentNode.querySelectorAll(".row-annotation");
    all.forEach(r => {
      if (r._scrAnchor === anchor && r.style.display !== "none") {
        sizeAnnotArrow(r);
      }
    });
  }

  // Count annotation rows that sit below `annotRow` in the DOM and
  // share the same anchor. Used to stagger stacked arrow origins —
  // each arrow shifts right by one character per annotation below it.
  function annotationsBelow(annotRow, anchor) {
    let n = 0;
    let s = annotRow.nextSibling;
    while (s) {
      if (s.classList && s.classList.contains("row-annotation")
          && s.style.display !== "none"
          && s._scrAnchor === anchor) {
        n++;
      }
      s = s.nextSibling;
    }
    return n;
  }

  // Return the horizontal pixel midpoint of the nth character after
  // (and including) the first non-whitespace character on the anchor
  // row's content side. n=0 → first printing char's midpoint.
  // Measurement comes from Range.getBoundingClientRect rather than a
  // ch-based guess because hljs spans + ligatures make character width
  // arithmetic unreliable.
  function charCenterAt(anchorRowEl, side, n) {
    const idx = (side === "old") ? 1 : 3;
    const contentCell = anchorRowEl.children[idx];
    if (!contentCell) return null;
    const code = contentCell.querySelector("code");
    if (!code) return contentCell.getBoundingClientRect().left;
    // Collect every text node, in order.
    const texts = [];
    const walker = document.createTreeWalker(code, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) texts.push(node);

    // Flatten to (node, localOffset) entries, skipping leading whitespace.
    const chars = [];
    let seenPrinting = false;
    for (const t of texts) {
      const s = t.nodeValue;
      for (let i = 0; i < s.length; i++) {
        if (!seenPrinting) {
          if (/\s/.test(s[i])) continue;
          seenPrinting = true;
        }
        chars.push({ node: t, offset: i });
      }
    }
    if (chars.length === 0) return code.getBoundingClientRect().left;

    const target = chars[Math.min(n, chars.length - 1)];
    const range = document.createRange();
    range.setStart(target.node, target.offset);
    range.setEnd(target.node, target.offset + 1);
    const r = range.getBoundingClientRect();
    if (!r.width && !r.height) {
      // Zero-size range (line end or unusual node); fall back to the
      // first printing character.
      const first = chars[0];
      const r0 = document.createRange();
      r0.setStart(first.node, first.offset);
      r0.setEnd(first.node, first.offset + 1);
      const rr = r0.getBoundingClientRect();
      return rr.left + rr.width / 2;
    }
    return r.left + r.width / 2;
  }

  function svgAnnotArrow() {
    // L-shape with an arrowhead on the right-end. Viewbox 20x14; path:
    //   (2,0) -> (2,9) -> (17,9), plus a small chevron head at (13,5)(17,9)(13,13).
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("class", "annot-arrow");
    svg.setAttribute("viewBox", "0 0 20 14");
    svg.setAttribute("width", "20");
    svg.setAttribute("height", "14");
    svg.setAttribute("aria-hidden", "true");
    const p = document.createElementNS(SVG_NS, "path");
    p.setAttribute("d", "M 2 0 L 2 9 L 17 9 M 13 5 L 17 9 L 13 13");
    p.setAttribute("fill", "none");
    p.setAttribute("stroke", "currentColor");
    p.setAttribute("stroke-width", "1.4");
    p.setAttribute("stroke-linecap", "round");
    p.setAttribute("stroke-linejoin", "round");
    svg.appendChild(p);
    return svg;
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

  function openCommentEditor({ rowEl, side, line, file, existing }) {
    // Insert an editor row immediately after rowEl, reusing the annotation
    // arrow + box. The box contains a textarea.
    const editor = buildCommentEditorRow({ anchorRowEl: rowEl, side, line, file, existing });
    if (rowEl.nextSibling) {
      rowEl.parentNode.insertBefore(editor, rowEl.nextSibling);
    } else {
      rowEl.parentNode.appendChild(editor);
    }
    editor._scrSizeArrow();
    resizeAnnotSiblings(rowEl);
    const ta = editor.querySelector("textarea");
    if (ta) {
      ta.focus();
      ta.setSelectionRange(ta.value.length, ta.value.length);
    }
  }

  function buildCommentEditorRow(opts) {
    const { anchorRowEl, side, line, file, existing } = opts;
    const row = el("div", "row row-annotation annot-comment annot-editor");
    const cell = el("div", `cell-annotation cell-annotation-${side}`);
    cell.appendChild(svgAnnotArrow());
    const box = el("div", "annot-box comment-editor-box");
    const ta = el("textarea", "comment-editor-input");
    ta.rows = 1;
    ta.placeholder = "Write a comment… (Enter to save, Shift-Enter for newline, Esc to cancel)";
    ta.value = existing ? existing.body : "";
    box.appendChild(ta);
    // Auto-grow vertically so the editor stays at one line until the
    // user types past it, then expands as needed. Width is still user-
    // controllable via the drag handle (resize: horizontal).
    function autosizeTextarea() {
      ta.style.height = "auto";
      ta.style.height = ta.scrollHeight + "px";
    }
    const bar = el("div", "comment-editor-bar");
    const save = el("button", "comment-btn comment-btn-save", existing ? "Update" : "Save");
    const cancel = el("button", "comment-btn comment-btn-cancel", "Cancel");
    bar.appendChild(save);
    bar.appendChild(cancel);
    box.appendChild(bar);
    cell.appendChild(box);
    row.appendChild(cell);
    row._scrAnchor = anchorRowEl;
    row._scrSide = side;
    row._scrSizeArrow = () => sizeAnnotArrow(row);

    function close() {
      row.remove();
      resizeAnnotSiblings(anchorRowEl);
    }

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
        refreshCommentsForAnchor(anchorRowEl, { file, side, line });
      });
    }

    save.addEventListener("click", e => { e.stopPropagation(); submit(); });
    cancel.addEventListener("click", e => { e.stopPropagation(); close(); });
    ta.addEventListener("keydown", e => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); }
      else if (e.key === "Escape") { e.preventDefault(); close(); }
      e.stopPropagation();
    });
    // Resize arrow and grow textarea when content changes.
    ta.addEventListener("input", () => {
      autosizeTextarea();
      row._scrSizeArrow();
      resizeAnnotSiblings(anchorRowEl);
    });
    // Initial autosize — runs once the row is in the DOM.
    requestAnimationFrame(autosizeTextarea);
    return row;
  }

  function buildCommentRow(comment, anchorRowEl) {
    const row = el("div", "row row-annotation annot-comment");
    const cell = el("div", `cell-annotation cell-annotation-${comment.side}`);
    cell.appendChild(svgAnnotArrow());
    const box = el("div", "annot-box comment-display");
    const body = el("div", "comment-body");
    body.textContent = comment.body;
    box.appendChild(body);
    const bar = el("div", "comment-actions");
    const edit = el("button", "comment-btn comment-btn-edit", "edit");
    const del = el("button", "comment-btn comment-btn-del", "delete");
    bar.appendChild(edit);
    bar.appendChild(del);
    box.appendChild(bar);
    cell.appendChild(box);
    row.appendChild(cell);
    row.dataset.commentId = comment.id;
    row._scrAnchor = anchorRowEl;
    row._scrSide = comment.side;
    row._scrSizeArrow = () => sizeAnnotArrow(row);

    edit.addEventListener("click", e => {
      e.stopPropagation();
      row.remove();
      openCommentEditor({
        rowEl: anchorRowEl, side: comment.side, line: comment.line,
        file: comment.file, existing: comment,
      });
    });
    del.addEventListener("click", e => {
      e.stopPropagation();
      deleteComment(comment.id).then(() => row.remove());
    });
    return row;
  }

  function refreshCommentsForAnchor(anchorRowEl, anchor) {
    // Clear any comment-display rows immediately after the anchor, then
    // rebuild them from current state.
    removeCommentRowsAfter(anchorRowEl);
    const relevant = commentsFor(anchor.file, anchor.side, anchor.line)
      .sort((a, b) => (a.created_at || 0) - (b.created_at || 0));
    let insertAfter = anchorRowEl;
    for (const c of relevant) {
      const cr = buildCommentRow(c, anchorRowEl);
      if (insertAfter.nextSibling) {
        insertAfter.parentNode.insertBefore(cr, insertAfter.nextSibling);
      } else {
        insertAfter.parentNode.appendChild(cr);
      }
      cr._scrSizeArrow();
      insertAfter = cr;
    }
    // Any LLM annotations (line_notes, fold summaries) that also anchor
    // at this row now sit further from it — re-measure their arrows so
    // they stretch past the newly-inserted comments.
    resizeAnnotSiblings(anchorRowEl);
  }

  function removeCommentRowsAfter(anchorRowEl) {
    let n = anchorRowEl.nextSibling;
    while (n && n.classList && n.classList.contains("annot-comment")
           && !n.classList.contains("annot-editor")) {
      const next = n.nextSibling;
      n.remove();
      n = next;
    }
  }

  function renderAllExistingComments() {
    // On load, walk the DOM for every row and reattach comments.
    const byAnchor = {};  // anchorKey -> list
    for (const c of Object.values(STATE.comments)) {
      const k = `${c.file}|${c.side}|${c.line}`;
      (byAnchor[k] ||= []).push(c);
    }
    document.querySelectorAll(".file").forEach(fileEl => {
      const filePath = fileEl.querySelector(".file-path")
        ? fileEl.querySelector(".file-path").textContent : "";
      fileEl.querySelectorAll(".row").forEach(row => {
        const [oldCell, , newCell] = [row.children[0], row.children[1], row.children[2]];
        for (const [cell, side] of [[oldCell, "old"], [newCell, "new"]]) {
          if (!cell || cell.classList.contains("empty")) continue;
          const n = parseInt(cell.textContent.trim(), 10);
          if (isNaN(n)) continue;
          const relevant = byAnchor[`${filePath}|${side}|${n}`];
          if (!relevant) continue;
          refreshCommentsForAnchor(row, { file: filePath, side, line: n });
        }
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
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
