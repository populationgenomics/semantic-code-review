// One visibility model — the union primitive behind every kind of hiding
// in the viewer (ADR 0006).
//
// Three mechanisms used to decide whether a line renders — a `CodeFold`
// collapse, a file / hunk / segment header collapse, and the unchanged
// context outside a hunk — each with its own identity, its own storage,
// and its own rules. Here they are one thing: a `HiddenSpan` over
// side-tagged absolute file lines. A line is visible iff no span covers
// it, so visibility is the complement of the union of the spans and is
// answerable from state alone, with no DOM read. Revealing is removing a
// span, never a DOM mutation, so a re-render cannot lose it and nested
// state survives its container: dropping the outer span leaves the inner
// one standing.
//
// Presentation stays polymorphic and belongs to the renderer, not here: a
// `context` span shows an expand chip, a `hunk` span its `@@` header, a
// `codefold` span keeps the run's first rendered line as a header row and
// drops the rest (`planRows`).
//
// Two ledgers per file, not one. `spans` is what is hidden now; `marks`
// records every span id ever asserted and who asserted it. Seeding a
// marked id is a no-op, which is what makes a reveal stick: the renderer
// re-seeds every gap it lays out on every render, and a bulk action
// forgets only the marks it owns.

// --- Types ----------------------------------------------------------------

/** The global collapse depth, `files` (deepest) → `off` (no default
 *  hiding). Not a fold: it seeds the `level`-owned spans. */
export type CollapseLevel = "files" | "hunks" | "segments" | "off";

/** Who asserted a span, and therefore what may retract it.
 *
 *  - `level` — the global collapse level. Picking a level is
 *    authoritative over its own spans and nothing else: it drops every
 *    level-owned span *and mark* and re-seeds at the new depth, so a
 *    reviewer who expanded one hunk gets it folded back, and a reviewer
 *    who folded `uv.lock` away keeps it folded.
 *  - `user` — asserted by a click. Survives every bulk action; only
 *    another click or Reset retracts it.
 *  - `gap` — unchanged context outside a hunk, seeded by the renderer as
 *    it lays each region out. Also survives a level change: dropping to
 *    `off` expands the hunks, not every gap in the diff. */
export type SpanOwner = "level" | "user" | "gap";

/** What the span hides, which is what the renderer stands in its place. */
export type SpanKind = "file" | "hunk" | "segment" | "codefold" | "context";

/** 1-indexed inclusive line numbers. An empty range (`end < start`, e.g.
 *  the base side of a pure-insertion hunk) covers nothing. */
export interface LineRange {
  start: number;
  end: number;
}

export interface HiddenSpan {
  /** Unique across the viewer; every minter embeds the file id. */
  id: string;
  fileId: string;
  owner: SpanOwner;
  kind: SpanKind;
  /** Absolute lines in head/<path>, or null when the span has no head
   *  side (a deletion-only `CodeFold`). */
  right: LineRange | null;
  /** Absolute lines in base/<path>, or null when it has no base side. */
  left: LineRange | null;
}

/** The side-tagged address a `CodeFold` is identified by — satisfied by
 *  both the wire `FoldRegion` and folds.ts's freshly detected regions. */
export interface FoldAddress {
  context: FoldContext;
  right_start: number | null;
  right_end: number | null;
  left_start: number | null;
  left_end: number | null;
}

interface FileState {
  spans: Map<string, HiddenSpan>;
  marks: Map<string, SpanOwner>;
}

// --- Store ----------------------------------------------------------------

const _files = new Map<string, FileState>();

function _state(fileId: string): FileState {
  let s = _files.get(fileId);
  if (!s) {
    s = { spans: new Map(), marks: new Map() };
    _files.set(fileId, s);
  }
  return s;
}

/** Drop every span and every mark. The Reset button, and boot. */
function reset(): void {
  _files.clear();
}

/** Assert a span the reviewer asked for. Re-marks the id with this
 *  span's owner, so a level-seeded hide the reviewer re-asserts by hand
 *  becomes theirs and stops answering to the level. */
function hide(span: HiddenSpan): void {
  const s = _state(span.fileId);
  s.spans.set(span.id, span);
  s.marks.set(span.id, span.owner);
}

/** Assert a span unless its id has been asserted before. The renderer
 *  seeds the same gaps and level spans on every render; the mark is what
 *  stops a re-seed from undoing a reveal. */
function seed(span: HiddenSpan): void {
  const s = _state(span.fileId);
  if (s.marks.has(span.id)) return;
  s.spans.set(span.id, span);
  s.marks.set(span.id, span.owner);
}

/** Reveal: remove the span. The mark stays, recording that this id has
 *  been decided, so no re-seed puts it back. */
function reveal(fileId: string, id: string): void {
  _state(fileId).spans.delete(id);
}

/** Flip a span's presence. Returns the new hidden state. */
function toggle(span: HiddenSpan): boolean {
  if (isHidden(span.fileId, span.id)) {
    reveal(span.fileId, span.id);
    return false;
  }
  hide(span);
  return true;
}

function isHidden(fileId: string, id: string): boolean {
  return _state(fileId).spans.has(id);
}

function spansOf(fileId: string): HiddenSpan[] {
  return Array.from(_state(fileId).spans.values());
}

/** Who last asserted this id, or undefined if it never has been. */
function markOwner(fileId: string, id: string): SpanOwner | undefined {
  return _state(fileId).marks.get(id);
}

/** Drop every span and mark with this owner, across every file. */
function dropOwned(owner: SpanOwner): void {
  for (const s of _files.values()) {
    for (const [id, o] of Array.from(s.marks.entries())) {
      if (o !== owner) continue;
      s.marks.delete(id);
      s.spans.delete(id);
    }
  }
}

// --- The visibility function ----------------------------------------------

function _inRange(range: LineRange | null, line: number | null): boolean {
  if (range === null || line === null) return false;
  return line >= range.start && line <= range.end;
}

/** Every span covering an absolute line on one side. The union rule: the
 *  line is visible iff this is empty. */
function covering(
  fileId: string, side: "old" | "new", line: number,
): HiddenSpan[] {
  return spansOf(fileId).filter(
    (s) => _inRange(side === "new" ? s.right : s.left, line),
  );
}

/** Whether an absolute line on one side is hidden. Slice 5's manifest
 *  asks this of a comment's (side, line). */
function lineHidden(fileId: string, side: "old" | "new", line: number): boolean {
  return covering(fileId, side, line).length > 0;
}

/** Every span covering a row, optionally narrowed to one kind. A row
 *  carries up to two line numbers and is covered when either side is. */
function coveringRow(
  fileId: string, row: RowBlock, kind?: SpanKind,
): HiddenSpan[] {
  return spansOf(fileId).filter((s) => {
    if (kind !== undefined && s.kind !== kind) return false;
    return _inRange(s.right, row.new_line) || _inRange(s.left, row.old_line);
  });
}

/** Which of a container's rows this render emits, as indices into `rows`.
 *
 *  Only `codefold` spans are consulted: they are the sole hiding source
 *  whose affordance lives *inside* a row stream. A hidden hunk or region
 *  never reaches here — its own chrome stands in for the whole stream.
 *
 *  A collapsed `CodeFold` keeps its first rendered line as the header the
 *  chevron and summary hang off, and drops the rest; a definition whose
 *  own opening line is off screen therefore still gets a header. `headed`
 *  carries which folds have already placed one across the containers
 *  rendered so far in this file, so a fold straddling a hunk boundary
 *  places exactly one. */
function planRows(
  fileId: string, rows: RowBlock[], headed: Set<string>,
): number[] {
  const out: number[] = [];
  for (let i = 0; i < rows.length; i++) {
    const folds = coveringRow(fileId, rows[i], "codefold");
    if (folds.length === 0) {
      out.push(i);
      continue;
    }
    // A fold that has already placed its header owns everything below it,
    // including any fold nested inside.
    if (folds.some((f) => headed.has(f.id))) continue;
    out.push(i);
    for (const f of folds) headed.add(f.id);
  }
  return out;
}

// --- Span minters ---------------------------------------------------------

// A whole file: the header is the only affordance, so the span covers
// every line on both sides.
const _WHOLE_FILE: LineRange = { start: 1, end: Number.MAX_SAFE_INTEGER };

function fileSpanId(fileId: string): string {
  return `file:${fileId}`;
}

function fileSpan(f: FileBlock, owner: SpanOwner): HiddenSpan {
  return {
    id: fileSpanId(f.id), fileId: f.id, owner, kind: "file",
    right: _WHOLE_FILE, left: _WHOLE_FILE,
  };
}

function hunkSpanId(hunkId: string): string {
  return `hunk:${hunkId}`;
}

function hunkSpan(f: FileBlock, h: HunkBlock, owner: SpanOwner): HiddenSpan {
  return {
    id: hunkSpanId(h.id), fileId: f.id, owner, kind: "hunk",
    right: { start: h.new_start, end: h.new_start + h.new_count - 1 },
    left: { start: h.old_start, end: h.old_start + h.old_count - 1 },
  };
}

function segmentSpanId(segmentId: string): string {
  return `seg:${segmentId}`;
}

function segmentSpan(
  f: FileBlock, s: SegmentBlock, owner: SpanOwner,
): HiddenSpan {
  return {
    id: segmentSpanId(s.id), fileId: f.id, owner, kind: "segment",
    right: { start: s.new_start, end: s.new_start + s.new_count - 1 },
    left: null,
  };
}

function _addressKey(a: FoldAddress): string {
  return `${a.context}:${a.right_start ?? ""}-${a.right_end ?? ""}`
    + `:${a.left_start ?? ""}-${a.left_end ?? ""}`;
}

function codeFoldSpanId(fileId: string, a: FoldAddress): string {
  return `cf:${fileId}:${_addressKey(a)}`;
}

/** A `CodeFold`'s span is the definition's own absolute extent — the same
 *  address the region record and `/fold-summary` are keyed on, so folding
 *  and re-detecting a region agree without a translation step. */
function codeFoldSpan(
  fileId: string, a: FoldAddress, owner: SpanOwner,
): HiddenSpan {
  return {
    id: codeFoldSpanId(fileId, a), fileId, owner, kind: "codefold",
    right: a.right_start != null && a.right_end != null
      ? { start: a.right_start, end: a.right_end } : null,
    left: a.left_start != null && a.left_end != null
      ? { start: a.left_start, end: a.left_end } : null,
  };
}

function contextSpanId(
  fileId: string, right: LineRange | null, left: LineRange | null,
): string {
  const r = right ? `${right.start}-${right.end}` : "";
  const l = left ? `${left.start}-${left.end}` : "";
  return `ctx:${fileId}:${r}:${l}`;
}

/** A run of lines the renderer stands an expand chip in front of. Its id
 *  is its extent, so the same gap in the same layout is the same span
 *  across renders. */
function contextSpan(
  fileId: string, right: LineRange | null, left: LineRange | null,
): HiddenSpan {
  return {
    id: contextSpanId(fileId, right, left), fileId, owner: "gap",
    kind: "context", right, left,
  };
}

// --- Collapse level -------------------------------------------------------

/** The segments a hunk's body folds to at the `segments` level: its own,
 *  or one synthetic segment spanning it so a segment-less hunk still
 *  folds to a single summary. Minted here because the ids are what the
 *  spans are keyed on. */
function displaySegments(h: HunkBlock): SegmentBlock[] {
  if (h.segments && h.segments.length > 0) return h.segments;
  return [{
    id: `${h.id}_whole`,
    new_start: h.new_start,
    new_count: h.new_count,
    intent: h.intent || "",
    smells: h.smells || [],
    context: h.context || "",
    refs: h.refs || [],
  }];
}

/** Every span a collapse level asserts: files at `files`, hunks at
 *  `files` and `hunks`, segments at anything but `off`. Pure — the
 *  caller decides whether to seed or replace. */
function levelSpans(data: ViewerData, level: CollapseLevel): HiddenSpan[] {
  const out: HiddenSpan[] = [];
  for (const f of data.files) {
    if (level === "files") out.push(fileSpan(f, "level"));
    for (const h of f.hunks) {
      if (level === "files" || level === "hunks") out.push(hunkSpan(f, h, "level"));
      if (level !== "off") {
        for (const s of displaySegments(h)) out.push(segmentSpan(f, s, "level"));
      }
    }
  }
  return out;
}

/** Pick a level. A bulk action, not a reset: it retracts what the level
 *  asserted and re-asserts at the new depth, and leaves `user` and `gap`
 *  spans alone. */
function setLevel(data: ViewerData, level: CollapseLevel): void {
  dropOwned("level");
  for (const span of levelSpans(data, level)) seed(span);
}

/** Give nodes that have appeared since the last bulk action the current
 *  level's default — an SSE hunk patch can introduce segments the level
 *  never saw. Seeding, so a reveal is not undone. */
function syncLevel(data: ViewerData, level: CollapseLevel): void {
  for (const span of levelSpans(data, level)) seed(span);
}

// --- Public surface -------------------------------------------------------

export const Visibility = {
  reset,
  hide,
  seed,
  reveal,
  toggle,
  isHidden,
  spansOf,
  markOwner,
  dropOwned,
  covering,
  lineHidden,
  coveringRow,
  planRows,
  fileSpan,
  fileSpanId,
  hunkSpan,
  hunkSpanId,
  segmentSpan,
  segmentSpanId,
  codeFoldSpan,
  codeFoldSpanId,
  contextSpan,
  contextSpanId,
  displaySegments,
  levelSpans,
  setLevel,
  syncLevel,
};
