// CodeFold detection + on-demand fold-summary requests.
//
// A CodeFold is a `> def foo(): …` collapse of a structural region.
// Detection and presentation are separate concerns here:
//
//   Identity is a side-tagged span of absolute file lines —
//   `(context, right_start..right_end, left_start..left_end)` — and is
//   detected from the *file's own content*: the definition spans in
//   `fold_symbols` plus a whole-file row stream synthesised from
//   `head_lines`. It therefore does not move when the reviewer reveals
//   context, which is what keeps the persistent `FoldRegion` record —
//   and the summary and collapse state hanging off it — attached to the
//   region the reviewer folded (#10).
//
//   Presentation is the row indices a span lands on in *this* render,
//   recomputed by `_placeRegion` every time `attachFileFolds` runs.
//   Nothing durable is keyed on them.
//
// `attachFileFolds(fileEl, file)` walks every visible row in the file
// body in DOM order — across hunks and adjacent expanded context blocks
// — places each detected region onto that row list, and attaches one
// chevron per placed region. Folds whose body spans a hunk boundary
// collapse the right rows in every container because each row carries
// its own DOM refs.
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
  // Indices into the row sequence detection ran over — the whole-file
  // stream, not the rendered rows. Kept because the cross-language
  // fixture pins them; `_placeRegion` is what the renderer uses.
  header_idx: number;
  body_start_idx: number;
  body_end_idx: number;
  context: FoldContext;
  right_start: number | null;
  right_end: number | null;
  left_start: number | null;
  left_end: number | null;
  has_changes: boolean;
  // Identity of the definition the region snapped to; null for an
  // indentation-fallback region.
  qualified_name: string | null;
  kind: string | null;
}

// Where a detected region's span lands in the current render's row list.
interface FoldPlacement {
  headerIdx: number;
  bodyStartIdx: number;
  bodyEndIdx: number;
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

// The file's whole row stream: unchanged head context interleaved with
// the hunks' own rows, in file order. This is the detection input — it
// depends only on the file's content, so the regions it yields (and the
// absolute line spans that identify them) are the same no matter which
// part of the file is currently rendered.
//
// Returns null when the payload carries no head content: `head_lines` is
// null for generated / binary / deleted files and for any file over
// build_json's `_HEAD_LINES_CAP` (5,000 lines). Those files fall back to
// detecting over the rendered rows — today's behaviour, #10 included.
// Slice 6 replaces `head_lines` with the lazy `/file-text` route and
// removes both the cap and the head-side-only limit.
function _fileRowStream(file: FileBlock): RowBlock[] | null {
  const hl = file.head_lines;
  if (!hl) return null;
  const rows: RowBlock[] = [];
  let cn = 1;
  let co = 1;
  const ctxTo = (upTo: number): void => {
    while (cn < upTo) {
      const t = hl[cn - 1] ?? "";
      rows.push({ kind: "ctx", old_line: co, new_line: cn, old_text: t, new_text: t });
      co++; cn++;
    }
  };
  for (const h of file.hunks || []) {
    ctxTo(h.new_start);
    for (const r of h.rows || []) rows.push(r);
    cn = h.new_start + h.new_count;
    co = h.old_start + h.old_count;
  }
  ctxTo(hl.length + 1);
  return rows;
}

// Detection is O(rows x definition spans) over the whole file, so it is
// memoised per FileBlock and only re-run when the content it reads
// changes: an SSE `hunk` event swaps the HunkBlock object (DataStore
// .replaceHunk), which the identity check below catches. The rendered-
// rows fallback is deliberately not cached — it is reveal-dependent.
interface DetectionCache {
  hunks: HunkBlock[];
  regions: DetectedRegion[];
}

const _DETECTION_CACHE = new WeakMap<FileBlock, DetectionCache>();

function _detectFileRegions(
  file: FileBlock, renderedRows: RowBlock[],
): DetectedRegion[] {
  const syms = file.fold_symbols || { head: [], base: [] };
  const stream = _fileRowStream(file);
  if (stream === null) return _computeFoldRegions(renderedRows, syms.head, syms.base);
  const hunks = file.hunks || [];
  const cached = _DETECTION_CACHE.get(file);
  if (cached
      && cached.hunks.length === hunks.length
      && cached.hunks.every((h, i) => h === hunks[i])) {
    return cached.regions;
  }
  const regions = _computeFoldRegions(stream, syms.head, syms.base);
  _DETECTION_CACHE.set(file, { hunks: hunks.slice(), regions });
  return regions;
}

// Where a region's absolute span lands in the rows this render produced:
// the first and last rendered row inside the span on either side. The
// header is the first such row even when the definition's own opening
// line is not rendered — the chevron has to hang off a row that exists.
// Null when the span shows one row or fewer, leaving nothing to fold.
function _placeRegion(
  det: DetectedRegion, rows: RowBlock[],
): FoldPlacement | null {
  let first = -1;
  let last = -1;
  for (let i = 0; i < rows.length; i++) {
    if (!_rowInRegion(rows[i], det)) continue;
    if (first < 0) first = i;
    last = i;
  }
  if (first < 0 || last <= first) return null;
  return { headerIdx: first, bodyStartIdx: first + 1, bodyEndIdx: last };
}

function _rowInRegion(row: RowBlock, det: DetectedRegion): boolean {
  if (det.right_start != null && det.right_end != null && row.new_line != null
      && row.new_line >= det.right_start && row.new_line <= det.right_end) {
    return true;
  }
  return det.left_start != null && det.left_end != null && row.old_line != null
    && row.old_line >= det.left_start && row.old_line <= det.left_end;
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

function _upsertFoldRegion(file: FileBlock, det: DetectedRegion): FoldRegion {
  // The local POST handler and the SSE updater both mutate the
  // region object's `summary` field — they need to point at the
  // same reference. Find a matching persistent record if one
  // exists, refresh its detected fields, and return it. Otherwise
  // create a new one and stash it on the file's first hunk so the
  // next render picks it up. The record's row indices are not touched:
  // they are the server's per-hunk detection indices, and placement in
  // this render is `_placeRegion`'s job.
  const existing = _findExistingFoldRecord(file, det);
  if (existing) {
    existing.has_changes = det.has_changes;
    existing.qualified_name = det.qualified_name;
    existing.kind = det.kind;
    return existing;
  }
  const candidate: FoldRegion = {
    context: det.context,
    right_start: det.right_start, right_end: det.right_end,
    left_start: det.left_start, left_end: det.left_end,
    has_changes: det.has_changes,
    qualified_name: det.qualified_name, kind: det.kind,
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
  rows: RowBlock[], start: number, end: number,
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

function _indentRawRegions(rows: RowBlock[]): Array<[number, number]> {
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
  return raw;
}

// Definition spans enclosing a row, outermost-first: the row maps by
// line number into one side's tree — new_line into head spans (ctx /
// pair / ins rows), else old_line into base spans (del-only rows).
function _rowSymbols(
  row: RowBlock, headSpans: FoldSymbolSpan[], baseSpans: FoldSymbolSpan[],
): FoldSymbolSpan[] {
  let line: number | null;
  let spans: FoldSymbolSpan[];
  if (row.new_line != null) { line = row.new_line; spans = headSpans; }
  else if (row.old_line != null) { line = row.old_line; spans = baseSpans; }
  else return [];
  return spans
    .filter((s) => s.start_line <= line! && line! <= s.end_line)
    .sort((a, b) => a.depth - b.depth);
}

// A detected region before its side addresses are resolved.
// `rightRange` / `leftRange` are the definition's own declared line span
// on that side, present only on a symbol-snapped region (and only for a
// side whose tree actually holds the definition). An indentation-fallback
// region carries neither: its extent is knowable only from the rows it
// was detected over.
interface RawRegion {
  headerIdx: number;
  bodyEndIdx: number;
  qualifiedName: string | null;
  kind: string | null;
  rightRange: [number, number] | null;
  leftRange: [number, number] | null;
}

// `qualified_name` -> that definition's declared (start, end) lines;
// first occurrence wins, matching the first-seen region ordering.
function _spanRanges(spans: FoldSymbolSpan[]): Map<string, [number, number]> {
  const out = new Map<string, [number, number]>();
  for (const s of spans) {
    if (!out.has(s.qualified_name)) out.set(s.qualified_name, [s.start_line, s.end_line]);
  }
  return out;
}

// Regions snapped to definition spans, plus the set of row indices inside
// any definition. Every definition with >=1 present row becomes a region
// from its first to its last present row carrying that definition's
// identity; nested defs nest because a row carries its whole enclosing
// chain. Addresses come from the definition's declared span, never from
// the rows — a row-derived address moves when context is revealed.
function _symbolRawRegions(
  rows: RowBlock[], headSpans: FoldSymbolSpan[], baseSpans: FoldSymbolSpan[],
): { raw: RawRegion[]; covered: Set<number> } {
  const headRanges = _spanRanges(headSpans);
  const baseRanges = _spanRanges(baseSpans);
  const runs = new Map<string, [number, number]>();
  const kinds = new Map<string, string>();
  const order: string[] = [];
  const covered = new Set<number>();
  for (let i = 0; i < rows.length; i++) {
    for (const s of _rowSymbols(rows[i], headSpans, baseSpans)) {
      covered.add(i);
      const run = runs.get(s.qualified_name);
      if (run === undefined) {
        runs.set(s.qualified_name, [i, i]);
        kinds.set(s.qualified_name, s.kind);
        order.push(s.qualified_name);
      } else {
        run[1] = i;
      }
    }
  }
  const raw: RawRegion[] = order.map((qn) => {
    const run = runs.get(qn)!;
    return {
      headerIdx: run[0], bodyEndIdx: run[1],
      qualifiedName: qn, kind: kinds.get(qn)!,
      rightRange: headRanges.get(qn) ?? null,
      leftRange: baseRanges.get(qn) ?? null,
    };
  });
  return { raw, covered };
}

// Detect regions over a row sequence. A snapped region is addressed by
// its definition's declared span, so it reads the same whichever rows it
// was detected over; an indentation region has no such span and is
// addressed by the rows. Mirrors `compute_fold_regions` in
// viewer/hunk_layout.py — see tests/fixtures/fold_regions_cases.json.
function _computeFoldRegions(
  rows: RowBlock[],
  headSpans: FoldSymbolSpan[] = [],
  baseSpans: FoldSymbolSpan[] = [],
): DetectedRegion[] {
  let raw: RawRegion[];
  if (headSpans.length || baseSpans.length) {
    const sym = _symbolRawRegions(rows, headSpans, baseSpans);
    // Keep an indentation region only where no row it spans is already
    // covered by a definition — the snapped region owns that stretch.
    raw = sym.raw.concat(
      _indentRawRegions(rows)
        .filter(([h, e]) => {
          for (let j = h; j <= e; j++) if (sym.covered.has(j)) return false;
          return true;
        })
        .map(([h, e]): RawRegion => _indentRawRegion(h, e)),
    );
  } else {
    raw = _indentRawRegions(rows).map(([h, e]): RawRegion => _indentRawRegion(h, e));
  }
  raw.sort((a, b) => a.headerIdx - b.headerIdx || a.bodyEndIdx - b.bodyEndIdx);
  const regions: DetectedRegion[] = [];
  for (const rr of raw) {
    const header_idx = rr.headerIdx, body_end = rr.bodyEndIdx;
    const body_start = header_idx + 1;
    if (body_start > body_end) continue;
    const snapped = rr.qualifiedName !== null;
    const right_start = snapped
      ? (rr.rightRange ? rr.rightRange[0] : null)
      : _firstLine(rows, header_idx, body_end, "new_line");
    const right_end = snapped
      ? (rr.rightRange ? rr.rightRange[1] : null)
      : _lastLine(rows, header_idx, body_end, "new_line");
    const left_start = snapped
      ? (rr.leftRange ? rr.leftRange[0] : null)
      : _firstLine(rows, header_idx, body_end, "old_line");
    const left_end = snapped
      ? (rr.leftRange ? rr.leftRange[1] : null)
      : _lastLine(rows, header_idx, body_end, "old_line");
    const hasChanges = _anyChangesInRange(rows, header_idx, body_end);
    let context: FoldContext;
    if (right_start != null && left_start != null && hasChanges) context = "both";
    else if (right_start != null) context = "right";
    else context = "left";
    regions.push({
      header_idx, body_start_idx: body_start, body_end_idx: body_end,
      context, right_start, right_end, left_start, left_end,
      has_changes: hasChanges,
      qualified_name: rr.qualifiedName, kind: rr.kind,
    });
  }
  return regions;
}

function _indentRawRegion(headerIdx: number, bodyEndIdx: number): RawRegion {
  return {
    headerIdx, bodyEndIdx,
    qualifiedName: null, kind: null, rightRange: null, leftRange: null,
  };
}

function _firstLine(
  rows: RowBlock[], start: number, end: number, attr: "new_line" | "old_line",
): number | null {
  for (let j = start; j <= end; j++) {
    const v = rows[j][attr];
    if (v != null) return v;
  }
  return null;
}

function _lastLine(
  rows: RowBlock[], start: number, end: number, attr: "new_line" | "old_line",
): number | null {
  for (let j = end; j >= start; j--) {
    const v = rows[j][attr];
    if (v != null) return v;
  }
  return null;
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
// e.g. "function Foo.bar — ". Empty for an indentation-fallback region
// (no symbol), which keeps today's unlabelled placeholder.
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
  _setFoldBoxContent(foldHandle, label + "summarising…", { pending: true });
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
        _setFoldBoxContent(foldHandle, label + body.summary, {});
      } else {
        _setFoldBoxContent(
          foldHandle, label + "(summary failed — click to retry)",
          { failed: true }, retry,
        );
      }
    })
    .catch(() => {
      region._inflight = false;
      _setFoldBoxContent(
        foldHandle, label + "(summary failed — click to retry)",
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
  rows: RowWithEls[], region: FoldRegion, place: FoldPlacement, fileIdx: number,
): AttachedFold | null {
  const bodyStart = place.bodyStartIdx;
  const bodyEnd = place.bodyEndIdx;

  const headerRow = rows[place.headerIdx];
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
    // Seed the placeholder with the symbol identity (if any) followed by
    // the summary or its pending/run-augment stand-in.
    const label = _foldLabel(region);
    const pending = !region.summary && canSummarise;
    const bodyText = region.summary
      || (canSummarise
        ? "summarising…"
        : "(changes here; run augment to generate a description)");
    foldHandle = Annotations.attach({
      anchor, shadowAnchor: shadow,
      variant: "fold", content: label + bodyText,
    });
    if (!region.summary) {
      const box = foldHandle.element.querySelector(".annot-box");
      if (box) box.classList.add("missing");
      if (pending && box) box.classList.add("pending");
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
  const handles: AnnotationHandle[] = [];
  const chevrons: SVGElement[] = [];
  for (const det of _detectFileRegions(file, rows)) {
    const place = _placeRegion(det, rows);
    if (!place) continue;   // nothing of this region is on screen
    const region = _upsertFoldRegion(file, det);
    const attached = _attachOneFold(rows, region, place, fileIdx);
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

// Exposed for the cross-language lockstep fixture (tests/js/folds.test.ts):
// the same (rows, spans) input must yield the same regions as the Python
// `compute_fold_regions`. Not used by the runtime bundle.
export { _computeFoldRegions };
