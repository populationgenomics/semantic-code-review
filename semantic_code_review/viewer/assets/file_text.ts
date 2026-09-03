// The full source of one changed file, both sides, fetched lazily from
// /file-text and cached per file for the session.
//
// Two consumers read it: rendered mode (rendered.ts), which parses base
// and head into blocks, and the collapsible regions of the text diff
// (render.ts), which take their unchanged context from it. One cache
// serves both so whichever asks first pays the round trip and the other
// reads it back; a second asker while the request is in flight shares
// the request. Module-global rather than per pane: the text is
// pane-independent (only view state splits by pane), and never
// invalidated — the base/head worktrees are pinned for the run.
//
// A leaf: nothing here imports another viewer module, so both consumers
// import it without render.ts → rendered.ts becoming a cycle.

/** The /file-text payload. A side is null when that side has no file:
 *  an added file's base, a deleted file's head. */
interface FileText {
  file_idx: number;
  path: string;
  base: string | null;
  head: string | null;
}

// Prefixed onto the fetch; the session endpoint boot.ts resolves for
// every back-channel route. Empty string = same origin.
let _endpoint = "";
let _cache: Record<string, FileText> = Object.create(null);
let _inflight: Record<string, Promise<FileText>> = Object.create(null);

/** Set the endpoint and start the cache empty, so a re-boot (tests,
 *  future hot reload) starts fresh. */
function init(endpoint: string): void {
  _endpoint = endpoint;
  _cache = Object.create(null);
  _inflight = Object.create(null);
}

/** The text already fetched for a file, or undefined. Synchronous, for
 *  a render pass that must not wait. */
function cached(fileId: string): FileText | undefined {
  return _cache[fileId];
}

/** The file's text, fetching once per file per session.
 *
 *  Rejects when the route fails; nothing is cached for the file, so the
 *  next call retries. */
function load(f: FileBlock): Promise<FileText> {
  const hit = _cache[f.id];
  if (hit) return Promise.resolve(hit);
  const pending = _inflight[f.id];
  if (pending) return pending;
  const p = _fetch(f)
    .then((text) => { _cache[f.id] = text; return text; })
    .finally(() => { delete _inflight[f.id]; });
  _inflight[f.id] = p;
  return p;
}

async function _fetch(f: FileBlock): Promise<FileText> {
  const idx = _fileIdx(f);
  const r = await fetch(`${_endpoint}/file-text?file_idx=${idx}`, { cache: "no-store" });
  if (!r.ok) throw new Error(`GET /file-text -> ${r.status}`);
  return (await r.json()) as FileText;
}

/** Recover the file index from the "F<idx>" id build_json assigns. */
function _fileIdx(f: FileBlock): number {
  const n = Number.parseInt(f.id.replace(/^F/, ""), 10);
  if (Number.isNaN(n)) throw new Error(`unexpected file id ${f.id}`);
  return n;
}

/** One entry per line, numbered as the diff's rows number them: line n
 *  is `splitLines(text)[n - 1]`. A trailing newline closes the last
 *  line rather than opening an empty one. */
function splitLines(text: string): string[] {
  const lines = text.split("\n");
  if (lines[lines.length - 1] === "") lines.pop();
  return lines;
}

export const FileTextCache = { init, cached, load, splitLines };
export type { FileText };
