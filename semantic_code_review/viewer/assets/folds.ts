// Indent-based fold detection + on-demand fold-summary requests.
//
// The viewer's fold story is unified per-file (not per-stretch):
// `attachFileFolds(fileEl, file)` walks every visible row in the
// file body in DOM order — across hunks and adjacent expanded
// context blocks — runs an indent-based fold detector over the
// unified sequence, and attaches one chevron per region. Folds
// whose body spans a hunk boundary collapse the right rows in
// every container because each row carries its own DOM refs.
//
// First time the reviewer collapses a region whose summary is
// empty, this module fires `POST /fold-summary` against the live
// review server. The response writes back into the region object
// (mutating DATA in place); the server's `fold-summary` SSE event
// is handled by `applyFoldSummary` in boot.ts.
//
import { Annotations, type AnnotationHandle } from "./annotations";
import { FileRows, type RowWithEls } from "./file_rows";

interface DetectedRegion {
  header_idx: number;
  body_start_idx: number;
  body_end_idx: number;
  context: FoldContext;
  right_start: number | null;
  right_end: number | null;
  left_start: number | null;
  left_end: number | null;
}

interface AttachedFold {
  marker: SVGElement;
  foldHandle: AnnotationHandle | null;
}

interface FoldRequestAddress {
  context: FoldContext;
  right_start?: number;
  right_end?: number;
  left_start?: number;
  left_end?: number;
}

interface FoldFileState {
  handles: AnnotationHandle[];
  chevrons: SVGElement[];
}

const _FILE_FOLD_STATE: Record<string, FoldFileState> = Object.create(null);

function _sessionEndpoint(): string | null {
  // Read at call time, not module init — the meta tag may be
  // injected after this module loads (tests set up the DOM
  // dynamically, and a future bootloader might too). Empty
  // string content means "same origin" (the production case);
  // a missing meta tag (null) means no server is available
  // and the route is wired off.
  if (typeof document === "undefined") return null;
  const m = document.querySelector('meta[name="scr-session-endpoint"]');
  if (!m) return null;
  return m.getAttribute("content") || "";
}

// --- DOM helpers (private, duplicated from viewer.js because the
// classic-script module boundary doesn't let us import them) ----------

const _SVG_NS = "http://www.w3.org/2000/svg";

function _chev(folded: boolean, extraClass: string): SVGElement {
  const svg = document.createElementNS(_SVG_NS, "svg") as unknown as SVGElement;
  svg.setAttribute("viewBox", "0 0 12 12");
  svg.setAttribute("aria-hidden", "true");
  svg.classList.add("chevron");
  svg.classList.add(extraClass);
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

function _isRowContentEmpty(rowEl: HTMLElement | undefined | null): boolean {
  if (!rowEl) return true;
  const content = rowEl.children[1];
  return !content || content.classList.contains("empty");
}

// --- File-level walker --------------------------------------------------

function _teardownFileFolds(fileId: string): void {
  const s = _FILE_FOLD_STATE[fileId];
  if (!s) return;
  for (const h of s.handles) {
    try { h.remove(); } catch (_) { /* ignore */ }
  }
  for (const c of s.chevrons) {
    try { c.remove(); } catch (_) { /* ignore */ }
  }
  delete _FILE_FOLD_STATE[fileId];
}

// Walk the file body's .diff / .gap-expansion containers in DOM order,
// pull each one's row stream out of `FileRows` (recorded by render.ts
// at construction time), and flatten into one indexable list so folds
// can straddle hunks and adjacent gap-context.
function _collectFileRows(fileEl: HTMLElement): RowWithEls[] {
  const body = fileEl.querySelector(".file-body");
  if (!body) return [];
  const out: RowWithEls[] = [];
  for (const child of Array.from(body.children) as HTMLElement[]) {
    const cls = child.classList;
    let source: HTMLElement | null = null;
    if (cls.contains("hunk")) {
      source = child.querySelector(".diff");
    } else if (cls.contains("gap-expansion")) {
      source = child;
    }
    if (!source) continue;
    const entry = FileRows.get(source);
    if (!entry) continue;
    for (let i = 0; i < entry.rows.length; i++) {
      out.push({
        ...entry.rows[i],
        oldEl: entry.oldEls[i], newEl: entry.newEls[i],
      });
    }
  }
  return out;
}

function _findExistingFoldRecord(
  file: FileBlock, det: DetectedRegion,
): FoldRegion | null {
  const rs = det.right_start || 0, re_ = det.right_end || 0;
  const ls = det.left_start || 0, le = det.left_end || 0;
  for (const h of file.hunks || []) {
    for (const r of h.fold_regions || []) {
      if (
        (r.context || "right") === det.context
        && (r.right_start || 0) === rs && (r.right_end || 0) === re_
        && (r.left_start || 0) === ls && (r.left_end || 0) === le
      ) {
        return r;
      }
    }
  }
  return null;
}

function _upsertFoldRegion(
  file: FileBlock, det: DetectedRegion, rows: RowWithEls[],
): FoldRegion {
  // The local POST handler and the SSE updater both mutate the
  // region object's `summary` field — they need to point at the
  // same reference. Find a matching persistent record if one
  // exists, refresh its detected fields, and return it. Otherwise
  // create a new one and stash it on the file's first hunk so the
  // next render picks it up.
  const hasChanges = _anyChangesInRange(rows, det.header_idx, det.body_end_idx);
  const existing = _findExistingFoldRecord(file, det);
  if (existing) {
    existing.header_idx = det.header_idx;
    existing.body_start_idx = det.body_start_idx;
    existing.body_end_idx = det.body_end_idx;
    existing.has_changes = hasChanges;
    return existing;
  }
  const candidate: FoldRegion = {
    header_idx: det.header_idx,
    body_start_idx: det.body_start_idx,
    body_end_idx: det.body_end_idx,
    context: det.context,
    right_start: det.right_start, right_end: det.right_end,
    left_start: det.left_start, left_end: det.left_end,
    has_changes: hasChanges,
    summary: "",
  };
  if (file.hunks && file.hunks.length > 0) {
    const h0 = file.hunks[0];
    if (!h0.fold_regions) h0.fold_regions = [];
    h0.fold_regions.push(candidate);
  }
  return candidate;
}

function _anyChangesInRange(
  rows: RowWithEls[], start: number, end: number,
): boolean {
  for (let i = start; i <= end; i++) {
    const k = rows[i].kind;
    if (k === "ins" || k === "del" || k === "pair") return true;
  }
  return false;
}

// --- Indent-based detection --------------------------------------------

function _rowIndent(row: RowBlock): number {
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

function _computeFoldRegions(rows: RowWithEls[]): DetectedRegion[] {
  const indents = rows.map(_rowIndent);
  const nextNonBlank = (i: number): number | null => {
    for (let j = i + 1; j < indents.length; j++) {
      if (indents[j] !== -1) return indents[j];
    }
    return null;
  };
  const raw: Array<[number, number]> = [];
  const stack: Array<[number, number]> = [];
  for (let i = 0; i < indents.length; i++) {
    const ind = indents[i];
    if (ind === -1) continue;
    while (stack.length && stack[stack.length - 1][0] >= ind) {
      const top = stack.pop()!;
      raw.push([top[1], i - 1]);
    }
    const ni = nextNonBlank(i);
    if (ni !== null && ni > ind) stack.push([ind, i]);
  }
  while (stack.length) {
    const top = stack.pop()!;
    raw.push([top[1], indents.length - 1]);
  }
  raw.sort((a, b) => a[0] - b[0]);
  const regions: DetectedRegion[] = [];
  for (const [header_idx, body_end] of raw) {
    const body_start = header_idx + 1;
    if (body_start > body_end) continue;
    const right_start = _firstLine(rows, header_idx, body_end, "new_line");
    const right_end = _lastLine(rows, header_idx, body_end, "new_line");
    const left_start = _firstLine(rows, header_idx, body_end, "old_line");
    const left_end = _lastLine(rows, header_idx, body_end, "old_line");
    const hasChanges = _anyChangesInRange(rows, header_idx, body_end);
    let context: FoldContext;
    if (right_start != null && left_start != null && hasChanges) context = "both";
    else if (right_start != null) context = "right";
    else context = "left";
    regions.push({
      header_idx, body_start_idx: body_start, body_end_idx: body_end,
      context, right_start, right_end, left_start, left_end,
    });
  }
  return regions;
}

function _firstLine(
  rows: RowWithEls[], start: number, end: number, attr: "new_line" | "old_line",
): number | null {
  for (let j = start; j <= end; j++) {
    const v = rows[j][attr];
    if (v != null) return v;
  }
  return null;
}

function _lastLine(
  rows: RowWithEls[], start: number, end: number, attr: "new_line" | "old_line",
): number | null {
  for (let j = end; j >= start; j--) {
    const v = rows[j][attr];
    if (v != null) return v;
  }
  return null;
}

// --- Attach + click ----------------------------------------------------

interface FoldRegionRuntime extends FoldRegion {
  _inflight?: boolean;
}

function _canRequestFoldSummary(
  fileIdx: number | null, region: FoldRegion,
): boolean {
  if (_sessionEndpoint() === null) return false;
  if (fileIdx == null) return false;
  return _foldAddress(region) !== null;
}

function _foldAddress(region: FoldRegion): FoldRequestAddress | null {
  const context = region.context || "right";
  const addr: FoldRequestAddress = { context };
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

function _requestFoldSummary(
  fileIdx: number, region: FoldRegionRuntime,
  foldHandle: AnnotationHandle,
): void {
  if (region._inflight || region.summary) return;
  const addr = _foldAddress(region);
  if (!addr) return;
  region._inflight = true;
  _setFoldBoxContent(foldHandle, "summarising…", { pending: true });
  const retry = (): void => _requestFoldSummary(fileIdx, region, foldHandle);
  fetch(_sessionEndpoint() + "/fold-summary", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ file_idx: fileIdx, ...addr }),
  })
    .then((r) => r.json().then((j: { summary?: string }) => ({ status: r.status, body: j })))
    .then(({ status, body }) => {
      region._inflight = false;
      if (status === 200 && body.summary) {
        region.summary = body.summary;
        _setFoldBoxContent(foldHandle, body.summary, {});
      } else {
        _setFoldBoxContent(
          foldHandle, "(summary failed — click to retry)",
          { failed: true }, retry,
        );
      }
    })
    .catch(() => {
      region._inflight = false;
      _setFoldBoxContent(
        foldHandle, "(summary failed — click to retry)",
        { failed: true }, retry,
      );
    });
}

function _setFoldBoxContent(
  foldHandle: AnnotationHandle, text: string,
  classes: { pending?: boolean; failed?: boolean },
  onClick?: () => void,
): void {
  if (!foldHandle || !foldHandle.element) return;
  const box = foldHandle.element.querySelector(".annot-box") as HTMLElement | null;
  if (!box) return;
  box.textContent = text;
  box.classList.remove("pending", "failed");
  if (classes.pending) box.classList.add("pending");
  if (classes.failed) box.classList.add("failed");
  if (onClick) {
    const clone = box.cloneNode(true) as HTMLElement;
    clone.style.cursor = "pointer";
    clone.addEventListener("click", onClick);
    box.replaceWith(clone);
  }
  foldHandle.resize();
}

function _attachOneFold(
  rows: RowWithEls[], region: FoldRegion, fileIdx: number,
): AttachedFold | null {
  const bodyStart = region.body_start_idx;
  const bodyEnd = region.body_end_idx;
  if (bodyStart > bodyEnd) return null;

  const headerRow = rows[region.header_idx];
  if (!headerRow) return null;
  const headerOld = headerRow.oldEl;
  const headerNew = headerRow.newEl;
  if (!headerOld && !headerNew) return null;

  const side = _isRowContentEmpty(headerNew) && !_isRowContentEmpty(headerOld)
    ? "old" : "new";
  const anchor = side === "new" ? headerNew : headerOld;
  const shadow = side === "new" ? headerOld : headerNew;

  const marker = _chev(false, "fold-chev");
  marker.setAttribute("role", "button");
  marker.setAttribute("tabindex", "0");

  let foldHandle: AnnotationHandle | null = null;
  const canSummarise = _canRequestFoldSummary(fileIdx, region);
  if (region.summary || region.has_changes || canSummarise) {
    const initialContent = region.summary
      || (canSummarise
        ? "summarising…"
        : "(changes here; run augment to generate a description)");
    foldHandle = Annotations.attach({
      anchor, shadowAnchor: shadow,
      variant: "fold", content: initialContent,
    });
    if (!region.summary) {
      const box = foldHandle.element.querySelector(".annot-box");
      if (box) box.classList.add("missing");
      if (initialContent === "summarising…" && box) box.classList.add("pending");
    }
    foldHandle.element.style.display = "none";
    if (foldHandle.placeholder) foldHandle.placeholder.style.display = "none";
  }

  marker.addEventListener("click", (e) => {
    e.stopPropagation();
    const nowOpen = marker.classList.toggle("open");
    for (let i = bodyStart; i <= bodyEnd; i++) {
      const r = rows[i];
      if (!r) continue;
      if (r.oldEl) r.oldEl.style.display = nowOpen ? "" : "none";
      if (r.newEl) r.newEl.style.display = nowOpen ? "" : "none";
    }
    if (foldHandle) {
      foldHandle.element.style.display = nowOpen ? "none" : "";
      if (foldHandle.placeholder) {
        foldHandle.placeholder.style.display = nowOpen ? "none" : "";
      }
      if (!nowOpen) foldHandle.resize();
    }
    if (!nowOpen && !region.summary && foldHandle
        && _canRequestFoldSummary(fileIdx, region)) {
      _requestFoldSummary(fileIdx, region, foldHandle);
    }
    Annotations.reflow(anchor);
  });

  const contentCell = anchor && (anchor.children[1] as HTMLElement | undefined);
  if (contentCell) contentCell.prepend(marker);
  return { marker, foldHandle };
}

function attachFileFolds(fileEl: HTMLElement, file: FileBlock): void {
  _teardownFileFolds(file.id);
  const fileIdx = Number(file.id.replace("F", ""));
  const rows = _collectFileRows(fileEl);
  if (rows.length === 0) return;
  const detected = _computeFoldRegions(rows);
  const handles: AnnotationHandle[] = [];
  const chevrons: SVGElement[] = [];
  for (const det of detected) {
    const region = _upsertFoldRegion(file, det, rows);
    const attached = _attachOneFold(rows, region, fileIdx);
    if (!attached) continue;
    if (attached.foldHandle) handles.push(attached.foldHandle);
    if (attached.marker) chevrons.push(attached.marker);
  }
  _FILE_FOLD_STATE[file.id] = { handles, chevrons };
}

// The single runtime surface. boot.ts calls attachFileFolds on
// initial render, after every gap expand/collapse, and from
// applyFoldSummary's cross-tab path.
export const Folds = { attachFileFolds };
