// Fold-region chrome: chevrons, collapse, and on-demand fold-summary
// requests, over the regions the server computed.
//
// What folds is decided once, in Python, over the whole file
// (viewer/fold_regions.py) and shipped as `FileBlock.fold_regions`,
// each region addressed by line range per side. Nothing here derives a
// region: `attachFileFolds(fileEl, file)` walks the rows this pane has
// rendered for the file — hunks and expanded context alike, in DOM
// order — and, for each region with two or more of its rows on screen,
// hangs a chevron on the first and folds the rest under it. A region
// whose opener the diff never carried still folds from whichever of its
// rows is showing; a region with one row showing has nothing to fold.
//
// First time the reviewer collapses a region whose summary is empty,
// this module fires `POST /fold-summary` against the live review
// server. The response writes back into the region object (mutating
// DATA in place); the server's `fold-summary` SSE event is handled by
// `applyFoldSummary` in boot.ts.
//
// A collapsed region's box is its summary line and, beneath it, the
// labels the fold hid (ADR 0008: folding a region shows its label). The
// labels are the renderer's — it knows the spans — and arrive through
// the `FoldLabels` callback; this module owns the box and the summary.
//
import { Annotations, type AnnotationHandle } from "./annotations";
import { FileRows, type RowWithEls } from "./file_rows";

interface AttachedFold {
  marker: SVGElement;
  foldHandle: AnnotationHandle | null;
}

/** What a collapsed region shows beneath its summary: the labels on the
 *  rows the fold hid. `headerRow` stays visible with the chevron;
 *  `bodyRows` are the rows that fold under it, in order; `open` opens
 *  the fold (for a label click that asks for the lines). Null when the
 *  body carries no label. */
export type FoldLabels = (
  headerRow: RowWithEls, bodyRows: RowWithEls[], open: () => void,
) => HTMLElement | null;

interface FoldRequestAddress {
  context: FoldContext;
  right_start?: number;
  right_end?: number;
  left_start?: number;
  left_end?: number;
}

interface FoldChrome {
  handles: AnnotationHandle[];
  chevrons: SVGElement[];
}

/** A rendered row with the container (`.diff` / `.gap-expansion`) that
 *  holds it. */
interface FileRow extends RowWithEls {
  container: HTMLElement;
}

/** Where a region sits among the rows this pane rendered: the chevron
 *  row and the rows that fold under it. Indices into the collected row
 *  list, inclusive. */
interface PlacedRegion {
  region: FoldRegion;
  headerIdx: number;
  bodyEndIdx: number;
}

// The chrome attached inside each row container, so a re-attach removes
// it first. Keyed by the container rather than the file id: a container
// belongs to one pane (a node cannot be in two trees), so the explainer
// panel's copy of a file and the diff pane's never tear down each
// other's chevrons; and the diff pane reuses a hunk's `.diff` across
// repaints while rebuilding the `.file` around it, so the chrome those
// cached rows carry is found by the node that carries it. Entries vanish
// with the container.
const _CONTAINER_FOLD_CHROME = new WeakMap<HTMLElement, FoldChrome>();

function _sessionEndpoint(): string {
  // Read at call time, not module init — the meta tag may be
  // injected after this module loads (tests set up the DOM
  // dynamically, and a future bootloader might too). Empty string
  // content means "same origin" (the production case). The review
  // server always injects the tag; a missing tag is a broken shell,
  // so fail loud rather than silently degrading.
  const m = document.querySelector('meta[name="scr-session-endpoint"]');
  if (!m) throw new Error("scr-session-endpoint meta tag missing");
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

function _teardownContainerFolds(container: HTMLElement): void {
  const s = _CONTAINER_FOLD_CHROME.get(container);
  if (!s) return;
  for (const h of s.handles) {
    try { h.remove(); } catch (_) { /* ignore */ }
  }
  for (const c of s.chevrons) {
    try { c.remove(); } catch (_) { /* ignore */ }
  }
  _CONTAINER_FOLD_CHROME.delete(container);
}

function _recordChrome(container: HTMLElement, attached: AttachedFold): void {
  let s = _CONTAINER_FOLD_CHROME.get(container);
  if (!s) {
    s = { handles: [], chevrons: [] };
    _CONTAINER_FOLD_CHROME.set(container, s);
  }
  if (attached.foldHandle) s.handles.push(attached.foldHandle);
  s.chevrons.push(attached.marker);
}

// Walk the file body's .diff / .gap-expansion containers in DOM order,
// pull each one's row stream out of `FileRows` (recorded by render.ts
// at construction time), and flatten into one indexable list so a fold
// can straddle hunks and adjacent gap-context.
function _collectFileRows(fileEl: HTMLElement): FileRow[] {
  const body = fileEl.querySelector(".file-body");
  if (!body) return [];
  const out: FileRow[] = [];
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
        oldEl: entry.oldEls[i], newEl: entry.newEls[i], container: source,
      });
    }
  }
  return out;
}

// --- Placing regions on rendered rows ------------------------------------

function _inRange(line: number | null | undefined, start: number | null, end: number | null): boolean {
  return line != null && start != null && end != null && line >= start && line <= end;
}

/** Whether a rendered row lies in a region: its line on a covered side
 *  falls in that side's range. The one placement rule, shared with the
 *  label tree in render.ts. */
function rowInRegion(row: RowBlock, region: FoldRegion): boolean {
  return _inRange(row.new_line, region.right_start, region.right_end)
    || _inRange(row.old_line, region.left_start, region.left_end);
}

// Which rendered rows each region covers: the chevron sits on the first
// row inside the region's ranges, and everything through the last such
// row folds under it — including any row between them that is on
// neither range (a deletion interleaved with unchanged lines). A region
// with fewer than two rows on screen has nothing to fold and is skipped.
// Two regions that land on the same row (a class and the method a hunk
// sits inside, neither opener rendered) keep the tighter one — on a tie,
// the later, since the server lists an enclosing region before the
// regions it encloses. It names what the rows are.
function _placeRegions(rows: RowWithEls[], regions: FoldRegion[]): PlacedRegion[] {
  const byHeader = new Map<number, PlacedRegion>();
  for (const region of regions) {
    let headerIdx = -1;
    let bodyEndIdx = -1;
    for (let i = 0; i < rows.length; i++) {
      if (!rowInRegion(rows[i], region)) continue;
      if (headerIdx < 0) headerIdx = i;
      bodyEndIdx = i;
    }
    if (headerIdx < 0 || bodyEndIdx === headerIdx) continue;
    const prior = byHeader.get(headerIdx);
    if (prior && prior.bodyEndIdx < bodyEndIdx) continue;
    byHeader.set(headerIdx, { region, headerIdx, bodyEndIdx });
  }
  return Array.from(byHeader.values()).sort((a, b) => a.headerIdx - b.headerIdx);
}

// --- Attach + click ----------------------------------------------------

function _canRequestFoldSummary(
  fileIdx: number | null, region: FoldRegion,
): boolean {
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

// Prefix the collapsed placeholder with the region's symbol identity,
// e.g. "function Foo.bar — ". Empty for an indentation stanza (no
// symbol), which keeps the unlabelled placeholder.
function _foldLabel(region: FoldRegion): string {
  if (!region.qualified_name) return "";
  const kind = region.kind ? `${region.kind} ` : "";
  return `${kind}${region.qualified_name} — `;
}

function _requestFoldSummary(
  fileIdx: number, region: FoldRegion,
  foldHandle: AnnotationHandle,
): void {
  if (region._inflight || region.summary) return;
  const addr = _foldAddress(region);
  if (!addr) return;
  region._inflight = true;
  const label = _foldLabel(region);
  _setFoldSummary(foldHandle, label + "summarising…", { pending: true });
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
        _setFoldSummary(foldHandle, label + body.summary, {});
      } else {
        _setFoldSummary(
          foldHandle, label + "(summary failed — click to retry)",
          { failed: true }, retry,
        );
      }
    })
    .catch(() => {
      region._inflight = false;
      _setFoldSummary(
        foldHandle, label + "(summary failed — click to retry)",
        { failed: true }, retry,
      );
    });
}

/** The fold box's content: the summary line, then the labels the fold
 *  hid when it has any. */
function _foldBoxContent(summary: string, labels: HTMLElement | null): HTMLElement {
  const wrap = document.createElement("div");
  wrap.className = "fold-body";
  const line = document.createElement("div");
  line.className = "fold-summary";
  line.textContent = summary;
  wrap.appendChild(line);
  if (labels) wrap.appendChild(labels);
  return wrap;
}

/** Rewrite the summary line alone; the labels beneath it stay. */
function _setFoldSummary(
  foldHandle: AnnotationHandle, text: string,
  classes: { pending?: boolean; failed?: boolean },
  onClick?: () => void,
): void {
  if (!foldHandle || !foldHandle.element) return;
  const box = foldHandle.element.querySelector(".annot-box") as HTMLElement | null;
  const line = box && (box.querySelector(".fold-summary") as HTMLElement | null);
  if (!box || !line) return;
  line.textContent = text;
  box.classList.remove("pending", "failed");
  if (classes.pending) box.classList.add("pending");
  if (classes.failed) box.classList.add("failed");
  if (onClick) {
    const clone = line.cloneNode(true) as HTMLElement;
    clone.style.cursor = "pointer";
    clone.addEventListener("click", onClick);
    line.replaceWith(clone);
  }
  foldHandle.resize();
}

// A fold's state lives in its rows: collapsed when every body row is
// hidden. Re-attaching after a gap expands or a summary lands rebuilds
// the chevron in the state the rows are already in, so it never pops a
// fold open or shows an open chevron over hidden rows.
function _isCollapsed(rows: RowWithEls[], bodyStart: number, bodyEnd: number): boolean {
  for (let i = bodyStart; i <= bodyEnd; i++) {
    const r = rows[i];
    if (!r) continue;
    if ((r.oldEl && r.oldEl.style.display !== "none")
        || (r.newEl && r.newEl.style.display !== "none")) {
      return false;
    }
  }
  return true;
}

// A body row folds with what hangs off it: the annotation rows, their
// placeholders and a span's label sit between it and the next recorded
// row in DOM order. Only what this fold hid is shown again, so a nested
// fold's own collapsed box and hidden rows stay as they were.
function _showRows(rows: RowWithEls[], start: number, end: number, show: boolean): void {
  const recorded = new Set<HTMLElement>();
  for (const r of rows) { recorded.add(r.oldEl); recorded.add(r.newEl); }
  for (let i = start; i <= end; i++) {
    const r = rows[i];
    if (!r) continue;
    for (const el of [r.oldEl, r.newEl]) {
      if (!el) continue;
      el.style.display = show ? "" : "none";
      _showAttachments(el, recorded, show);
    }
  }
}

function _showAttachments(rowEl: HTMLElement, recorded: Set<HTMLElement>, show: boolean): void {
  for (let s = rowEl.nextElementSibling as HTMLElement | null; s && !recorded.has(s);
       s = s.nextElementSibling as HTMLElement | null) {
    if (show) {
      if (s.dataset.foldHidden === undefined) continue;
      delete s.dataset.foldHidden;
      s.style.display = "";
    } else if (s.style.display !== "none") {
      s.dataset.foldHidden = "";
      s.style.display = "none";
    }
  }
}

function _attachOneFold(
  rows: RowWithEls[], placed: PlacedRegion, fileIdx: number, labels: FoldLabels,
): AttachedFold | null {
  const { region } = placed;
  const bodyStart = placed.headerIdx + 1;
  const bodyEnd = placed.bodyEndIdx;

  const headerRow = rows[placed.headerIdx];
  const headerOld = headerRow.oldEl;
  const headerNew = headerRow.newEl;
  if (!headerOld && !headerNew) return null;

  const side = _isRowContentEmpty(headerNew) && !_isRowContentEmpty(headerOld)
    ? "old" : "new";
  const anchor = side === "new" ? headerNew : headerOld;
  const shadow = side === "new" ? headerOld : headerNew;

  const collapsed = _isCollapsed(rows, bodyStart, bodyEnd);
  if (!collapsed) _showRows(rows, bodyStart, bodyEnd, true);   // a partly hidden body reads as open
  const marker = _chev(collapsed, "fold-chev");
  marker.setAttribute("role", "button");
  marker.setAttribute("tabindex", "0");

  let foldHandle: AnnotationHandle | null = null;
  const setOpen = (nowOpen: boolean): void => {
    marker.classList.toggle("open", nowOpen);
    _showRows(rows, bodyStart, bodyEnd, nowOpen);
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
  };
  const open = (): void => { if (!marker.classList.contains("open")) setOpen(true); };

  const hidden = labels(headerRow, rows.slice(bodyStart, bodyEnd + 1), open);
  const canSummarise = _canRequestFoldSummary(fileIdx, region);
  if (region.summary || region.has_changes || canSummarise || hidden) {
    // Seed the summary line with the symbol identity (if any) followed by
    // the summary or its pending/run-augment stand-in.
    const label = _foldLabel(region);
    const pending = !region.summary && canSummarise;
    const bodyText = region.summary
      || (canSummarise
        ? "summarising…"
        : "(changes here; run augment to generate a description)");
    foldHandle = Annotations.attach({
      anchor, shadowAnchor: shadow,
      variant: "fold", content: _foldBoxContent(label + bodyText, hidden),
      // The clamp is the summary line's (CSS), not the box's: the labels
      // beneath it must not be cut off.
      layout: { maxHeight: null },
    });
    if (!region.summary) {
      const box = foldHandle.element.querySelector(".annot-box");
      if (box) box.classList.add("missing");
      if (pending && box) box.classList.add("pending");
    }
    foldHandle.element.style.display = collapsed ? "" : "none";
    if (foldHandle.placeholder) foldHandle.placeholder.style.display = collapsed ? "" : "none";
    if (collapsed) foldHandle.resize();
  }

  marker.addEventListener("click", (e) => {
    e.stopPropagation();
    setOpen(!marker.classList.contains("open"));
  });

  const contentCell = anchor && (anchor.children[1] as HTMLElement | undefined);
  if (contentCell) contentCell.prepend(marker);
  return { marker, foldHandle };
}

function attachFileFolds(fileEl: HTMLElement, file: FileBlock, labels: FoldLabels): void {
  const fileIdx = Number(file.id.replace("F", ""));
  const rows = _collectFileRows(fileEl);
  for (const container of new Set(rows.map((r) => r.container))) {
    _teardownContainerFolds(container);
  }
  for (const placed of _placeRegions(rows, file.fold_regions || [])) {
    const attached = _attachOneFold(rows, placed, fileIdx, labels);
    if (attached) _recordChrome(rows[placed.headerIdx].container, attached);
  }
}

// The runtime surface. render.ts calls attachFileFolds after a file
// body is built, after every gap expand/collapse, and (via
// `Render.attachFileFolds`, which supplies the labels) when boot.ts
// hears a fold summary land from another tab.
export const Folds = { attachFileFolds, rowInRegion };
