// The tab's view of one run — what the reviewer has disclosed and where
// they are — kept so a repaint and a reload put it back.
//
// One sessionStorage entry per run id, holding the diff pane's ledger of
// revealed regions and collapsed definitions, the active sidebar pill and
// the open explainer section. Per tab by nature (the ids mean nothing in
// another run, and sessionStorage is the tab's), so the record is module
// state behind functions, as `Prefs` is for the cross-run preferences —
// see CONTEXT.md's viewer-preference entry for the split.
//
// The record is a hint, not a contract: a reveal whose region no longer
// exists is never consulted and never thrown on; a stored record from
// another run or another schema version is discarded, not migrated. A
// write the browser refuses degrades to in-memory only, with one
// warning; a stored record that does not parse or does not describe
// view state is logged as an error and read as empty — it must not brick
// the viewer.
//
// A leaf: nothing here imports another viewer module.

/** A collapsible region by its boundaries on both sides, 1-indexed and
 *  inclusive; a side the region has no lines on is `[start, start - 1]`.
 *  Boundaries, not row indices: the rows a region renders as depend on
 *  the text fetched for it, the boundaries only on the diff. */
export interface RegionRef {
  old: [number, number];
  new: [number, number];
}

/** What one pane records of the reviewer's disclosure: the regions
 *  revealed and the definitions folded. `file` is the `F<idx>` id;
 *  `key` is folds.ts's `foldKey`. The diff pane's is the stored record;
 *  the explainer panel's (`transient()`) is in memory for the session. */
export interface Ledger {
  isRevealed(file: string, region: RegionRef): boolean;
  reveal(file: string, region: RegionRef): void;
  unreveal(file: string, region: RegionRef): void;
  isFolded(file: string, key: string): boolean;
  setFolded(file: string, key: string, folded: boolean): void;
}

interface Reveal extends RegionRef {
  file: string;
}

interface Fold {
  file: string;
  key: string;
}

/** The stored shape. `v` and `run` gate the read. */
interface StoredRecord {
  v: 1;
  run: string;
  reveals: Reveal[];
  folds: Fold[];
  pill: string | null;
  section: string | null;
}

const VERSION = 1;
const KEY_PREFIX = "scr-view-state:";

interface LedgerState {
  reveals: Map<string, Reveal>;
  folds: Map<string, Fold>;
}

let _runId: string | null = null;
// Cleared, never replaced: the stored ledger below closes over it.
const _state: LedgerState = { reveals: new Map(), folds: new Map() };
let _pill: string | null = null;
let _section: string | null = null;
// False once a write has been refused: the record then lives in memory
// for the rest of the session, and the warning is not repeated.
let _storageOk = true;

function _revealKey(file: string, r: RegionRef): string {
  return `${file}|${r.old[0]}-${r.old[1]}:${r.new[0]}-${r.new[1]}`;
}

function _foldKey(file: string, key: string): string {
  return `${file}|${key}`;
}

function _ledger(state: LedgerState, changed: () => void): Ledger {
  return {
    isRevealed: (file, region) => state.reveals.has(_revealKey(file, region)),
    reveal: (file, region) => {
      state.reveals.set(_revealKey(file, region), { file, old: region.old, new: region.new });
      changed();
    },
    unreveal: (file, region) => {
      if (state.reveals.delete(_revealKey(file, region))) changed();
    },
    isFolded: (file, key) => state.folds.has(_foldKey(file, key)),
    setFolded: (file, key, folded) => {
      const k = _foldKey(file, key);
      const was = state.folds.has(k);
      if (folded === was) return;
      if (folded) state.folds.set(k, { file, key });
      else state.folds.delete(k);
      changed();
    },
  };
}

function storageKey(runId: string): string {
  return KEY_PREFIX + runId;
}

/** Load the tab's record for `runId`, or start empty. Call before any
 *  module reads its part of the record — the sidebar its pill, the
 *  explainer its section, the renderer its reveals and folds. */
function init(runId: string): void {
  if (!runId) throw new Error("view state: run id missing");
  _runId = runId;
  _state.reveals.clear();
  _state.folds.clear();
  _pill = null;
  _section = null;
  _storageOk = true;
  const stored = _read(runId);
  if (stored === null) return;
  for (const r of stored.reveals) _state.reveals.set(_revealKey(r.file, r), r);
  for (const f of stored.folds) _state.folds.set(_foldKey(f.file, f.key), f);
  _pill = stored.pill;
  _section = stored.section;
}

function _read(runId: string): StoredRecord | null {
  let raw: string | null;
  try {
    raw = sessionStorage.getItem(storageKey(runId));
  } catch (e) {
    _storageOk = false;
    console.warn("view state: sessionStorage unavailable, keeping the record in memory", e);
    return null;
  }
  if (raw === null) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (e) {
    console.error("view state: stored record is not JSON, starting empty", e);
    return null;
  }
  // Another version or another run: stale, not corrupt.
  if (!_isObject(parsed) || parsed.v !== VERSION || parsed.run !== runId) return null;
  try {
    return _validate(parsed);
  } catch (e) {
    console.error("view state: stored record is malformed, starting empty", e);
    return null;
  }
}

function _isObject(x: unknown): x is Record<string, unknown> {
  return typeof x === "object" && x !== null && !Array.isArray(x);
}

function _isRange(x: unknown): x is [number, number] {
  return Array.isArray(x) && x.length === 2 && x.every((n) => Number.isInteger(n));
}

function _validate(o: Record<string, unknown>): StoredRecord {
  if (!Array.isArray(o.reveals)) throw new Error("reveals is not a list");
  if (!Array.isArray(o.folds)) throw new Error("folds is not a list");
  if (o.pill !== null && typeof o.pill !== "string") throw new Error("pill is not a string or null");
  if (o.section !== null && typeof o.section !== "string") throw new Error("section is not a string or null");
  const reveals: Reveal[] = o.reveals.map((r: unknown) => {
    if (!_isObject(r) || typeof r.file !== "string" || !_isRange(r.old) || !_isRange(r.new)) {
      throw new Error(`malformed reveal ${JSON.stringify(r)}`);
    }
    return { file: r.file, old: r.old, new: r.new };
  });
  const folds: Fold[] = o.folds.map((f: unknown) => {
    if (!_isObject(f) || typeof f.file !== "string" || typeof f.key !== "string") {
      throw new Error(`malformed fold ${JSON.stringify(f)}`);
    }
    return { file: f.file, key: f.key };
  });
  return { v: VERSION, run: o.run as string, reveals, folds, pill: o.pill, section: o.section };
}

function _require(): string {
  if (_runId === null) throw new Error("view state: init(runId) has not been called");
  return _runId;
}

function _persist(): void {
  const runId = _require();
  if (!_storageOk) return;
  const record: StoredRecord = {
    v: VERSION,
    run: runId,
    reveals: Array.from(_state.reveals.values()),
    folds: Array.from(_state.folds.values()),
    pill: _pill,
    section: _section,
  };
  try {
    sessionStorage.setItem(storageKey(runId), JSON.stringify(record));
  } catch (e) {
    _storageOk = false;
    console.warn("view state: could not write the record, keeping it in memory", e);
  }
}

// The diff pane's ledger, over the stored record.
const _stored = _ledger(_state, _persist);

function isRevealed(file: string, region: RegionRef): boolean {
  _require();
  return _stored.isRevealed(file, region);
}
function reveal(file: string, region: RegionRef): void { _require(); _stored.reveal(file, region); }
function unreveal(file: string, region: RegionRef): void { _require(); _stored.unreveal(file, region); }
function isFolded(file: string, key: string): boolean { _require(); return _stored.isFolded(file, key); }
function setFolded(file: string, key: string, folded: boolean): void {
  _require();
  _stored.setFolded(file, key, folded);
}

/** The active sidebar pill as `<axis>:<id>`, or null. */
function pill(): string | null { _require(); return _pill; }
function setPill(value: string | null): void {
  _require();
  _pill = value;
  _persist();
}

/** The open explainer section's id, or null. */
function section(): string | null { _require(); return _section; }
function setSection(value: string | null): void {
  _require();
  _section = value;
  _persist();
}

/** A ledger kept in memory alone, for a pane whose disclosure is its own
 *  and not the tab's — the explainer's detail panel. */
function transient(): Ledger {
  return _ledger({ reveals: new Map(), folds: new Map() }, () => {});
}

export const ViewState = {
  init,
  storageKey,
  isRevealed,
  reveal,
  unreveal,
  isFolded,
  setFolded,
  pill,
  setPill,
  section,
  setSection,
  transient,
};
