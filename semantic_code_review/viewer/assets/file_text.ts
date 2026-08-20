// The viewer's one source of file *content* — the lazy `/file-text`
// route (ADR 0004), fetched per file and cached for the run.
//
// Two consumers, one cache. Rendered markdown mode needs both sides'
// full source to parse; the text diff needs it for everything the diff
// itself never mentions — the unchanged lines between hunks, and the
// whole-file row stream `folds.ts` detects `CodeFold`s over (ADR 0006).
// Both used to be served differently: rendered mode fetched here,
// detection read an eager `FileBlock.head_lines` capped at 5,000 lines
// and shipped for the head side only. One source, one bound.
//
// The bound is the route's, not the payload's: `_FILE_TEXT_CAP_BYTES`
// (2 MB) in review/server.py, per side. A side over it comes back null,
// which is *not* an empty file — an empty file would detect every fold
// out of existence and hide every gap. `hasContent` is false for it and
// callers stand a band in the lines' place instead.
//
// Loading is asynchronous, and that is safe because a persisted
// `HiddenSpan` is self-describing: restoring one needs no content, only
// *offering* a new fold does. So view state is on screen at first paint
// and fold affordances arrive with the content, one coalesced repaint
// per batch of arrivals.

interface Entry {
  state: "pending" | "loaded" | "failed";
  /** Full source per side, or null when the route served that side no
   *  content (added/deleted file, over the cap, unreadable). */
  base: string | null;
  head: string | null;
  /** Split-on-demand line views of the above; the row synthesisers want
   *  lines and the markdown renderer wants the text. */
  baseLines: string[] | null;
  headLines: string[] | null;
  inflight: Promise<Entry> | null;
}

export interface FileSource {
  base: string | null;
  head: string | null;
}

interface FileTextResponse {
  file_idx: number;
  path: string;
  base: string | null;
  head: string | null;
}

// Prefixed onto the fetch; empty string = same origin (the production
// path). Set by boot.
let _endpoint = "";
// Full re-render, injected by boot. Called once per batch of arrivals
// rather than once per file: a review is many files and each would
// otherwise repaint the whole app.
let _onLoaded: () => void = () => {};
let _repaintScheduled = false;

const _cache: Record<string, Entry> = Object.create(null);

function init(endpoint: string, onLoaded: () => void): void {
  _endpoint = endpoint;
  _onLoaded = onLoaded;
}

/** Drop every cached source. Boot only — the base/head worktrees are
 *  pinned for a run, so nothing invalidates mid-session. */
function reset(): void {
  for (const k of Object.keys(_cache)) delete _cache[k];
}

/** Recover the file index from the "F<idx>" id build_json assigns. */
function fileIdx(f: FileBlock): number {
  const n = Number.parseInt(f.id.replace(/^F/, ""), 10);
  if (Number.isNaN(n)) throw new Error(`unexpected file id ${f.id}`);
  return n;
}

/** Whether this file's content is worth asking the route for. A binary
 *  file has no text: the route would answer with the replacement-char
 *  transcoding of its bytes, and synthesising context rows out of that
 *  is worse than saying the lines are not loaded. */
function _servable(f: FileBlock): boolean {
  return f.status !== "binary";
}

/** Ask for a file's content if it has not been asked for. Fire and
 *  forget: the arrival repaints. Called by the renderer for every file
 *  whose body it lays out, so a file the reviewer never opens costs
 *  nothing. */
function request(f: FileBlock): void {
  if (_cache[f.id] || !_servable(f)) return;
  void load(f).catch(() => { /* the entry records the failure */ });
}

/** Fetch (or join the in-flight fetch for) a file's content. Rejects if
 *  the route cannot be reached; rendered mode awaits this before it
 *  flips, so the failure has to reach the caller rather than being
 *  swallowed into an empty pane. */
function load(f: FileBlock): Promise<FileSource> {
  const existing = _cache[f.id];
  if (existing) {
    if (existing.inflight) return existing.inflight.then(_source);
    if (existing.state === "failed") {
      return Promise.reject(new Error(`/file-text failed for ${f.path}`));
    }
    return Promise.resolve(_source(existing));
  }
  if (!_servable(f)) {
    return Promise.reject(new Error(`no text for ${f.status} file ${f.path}`));
  }
  const entry: Entry = {
    state: "pending", base: null, head: null,
    baseLines: null, headLines: null, inflight: null,
  };
  _cache[f.id] = entry;
  entry.inflight = _fetchText(f).then(
    (r) => {
      entry.state = "loaded";
      entry.base = r.base;
      entry.head = r.head;
      entry.baseLines = r.base === null ? null : _splitLines(r.base);
      entry.headLines = r.head === null ? null : _splitLines(r.head);
      entry.inflight = null;
      _scheduleRepaint();
      return entry;
    },
    (e) => {
      entry.state = "failed";
      entry.inflight = null;
      console.warn(`/file-text failed for ${f.path}`, e);
      _scheduleRepaint();
      throw e;
    },
  );
  return entry.inflight.then(_source);
}

function _source(e: Entry): FileSource {
  return { base: e.base, head: e.head };
}

/** Python's `str.splitlines`, which is what the line numbers in the diff
 *  are counted against: a trailing newline ends the last line rather
 *  than starting an empty one. */
function _splitLines(text: string): string[] {
  const lines = text.split(/\r\n|\r|\n/);
  if (lines.length > 0 && lines[lines.length - 1] === "") lines.pop();
  return lines;
}

async function _fetchText(f: FileBlock): Promise<FileTextResponse> {
  const r = await fetch(
    `${_endpoint}/file-text?file_idx=${fileIdx(f)}`, { cache: "no-store" },
  );
  if (!r.ok) throw new Error(`GET /file-text -> ${r.status}`);
  return (await r.json()) as FileTextResponse;
}

function _scheduleRepaint(): void {
  if (_repaintScheduled) return;
  _repaintScheduled = true;
  setTimeout(() => {
    _repaintScheduled = false;
    _onLoaded();
  }, 0);
}

/** The file's source, or null while it is still on its way (or never
 *  coming). Rendered mode's own guard against painting a blank pane. */
function get(fileId: string): FileSource | null {
  const e = _cache[fileId];
  if (!e || e.state !== "loaded") return null;
  return _source(e);
}

/** Whether this file's lines can be rendered: the route served at least
 *  one side. False while pending, on failure, and for a file over the
 *  cap on both sides. */
function hasContent(fileId: string): boolean {
  return _lines(fileId) !== null;
}

/** The side the row synthesisers read. Head when it was served (the
 *  common case, and the side line numbers are quoted in), base
 *  otherwise — unchanged context is by definition the same text on both
 *  sides, so either serves both halves of a `ctx` row exactly. */
function _lines(fileId: string): { lines: string[]; side: "old" | "new" } | null {
  const e = _cache[fileId];
  if (!e || e.state !== "loaded") return null;
  if (e.headLines) return { lines: e.headLines, side: "new" };
  if (e.baseLines) return { lines: e.baseLines, side: "old" };
  return null;
}

/** Where a hunk starts and resumes on each side, in absolute lines.
 *
 *  Not `start + count`: git writes a zero-count side as *the line
 *  before* the hunk — a pure deletion of base 10-12 after head line 9 is
 *  `@@ -10,3 +9,0 @@`, so the head side resumes at 10, not at 9. Reading
 *  the header literally there misnumbers every synthesised context row
 *  from the hunk to the end of the file, pairing head lines against the
 *  wrong base ones.
 */
function hunkBounds(h: HunkBlock): {
  firstNew: number; firstOld: number; nextNew: number; nextOld: number;
} {
  const firstNew = h.new_count > 0 ? h.new_start : h.new_start + 1;
  const firstOld = h.old_count > 0 ? h.old_start : h.old_start + 1;
  return {
    firstNew,
    firstOld,
    nextNew: h.new_count > 0 ? h.new_start + h.new_count : firstNew,
    nextOld: h.old_count > 0 ? h.old_start + h.old_count : firstOld,
  };
}

/** Unchanged context rows for `[fromNew, toNew)` on the head side,
 *  running from `fromOld` on the base side. Empty when the file has no
 *  content — the caller stands a band in their place rather than
 *  quietly rendering nothing.
 *
 *  Both cursors advance together: context is unchanged by definition, so
 *  a run of it is the same number of lines on both sides. */
function contextRows(
  fileId: string, fromNew: number, toNew: number, fromOld: number,
): RowBlock[] {
  const content = _lines(fileId);
  if (content === null) return [];
  const rows: RowBlock[] = [];
  let cn = fromNew;
  let co = fromOld;
  while (cn < toNew) {
    const t = content.lines[(content.side === "new" ? cn : co) - 1] ?? "";
    rows.push({ kind: "ctx", old_line: co, new_line: cn, old_text: t, new_text: t });
    cn++; co++;
  }
  return rows;
}

/** The file's last head-side line, given where the last hunk left both
 *  cursors — the bound on the trailing run of context. Null when the
 *  file has no content.
 *
 *  Derived from the base side when head is the unserved one: the file's
 *  tail is the same number of lines on both sides, so the last head line
 *  is `curNew` plus however much base is left. */
function tailEnd(fileId: string, curNew: number, curOld: number): number | null {
  const e = _cache[fileId];
  if (!e || e.state !== "loaded") return null;
  if (e.headLines) return e.headLines.length;
  if (e.baseLines) return curNew + (e.baseLines.length - curOld);
  return null;
}

export const FileText = {
  init,
  hunkBounds,
  reset,
  request,
  load,
  get,
  hasContent,
  contextRows,
  tailEnd,
  fileIdx,
};
