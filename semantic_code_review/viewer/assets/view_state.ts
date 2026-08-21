// Per-tab persistence for view state — what is folded, and at what
// collapse level (ADR 0006).
//
// `sessionStorage`, and it cannot be anything better. Every browser
// storage tier is origin-scoped and the review server binds `--port 0`,
// so the origin changes on every run and nothing written under one run's
// origin is reachable from the next. Within a run the port is fixed, so
// `sessionStorage` covers what is achievable: a tab's lifetime, reload
// included. Two tabs disagreeing about folds is accepted.
//
// The run id in the key is not what separates two tabs — `sessionStorage`
// is already per-tab. It guards the one way a record can outlive its run:
// the kernel hands a later run the same ephemeral port, the reviewer
// points the same tab at it, and the origin matches. Spans address
// absolute file lines, so restoring another run's record would hide
// arbitrary lines of a different diff.
//
// Two failure modes, deliberately handled differently:
//
//   - **Storage is unavailable or refuses the write** (disabled by
//     policy, over quota). Soft: the viewer keeps its state in memory and
//     loses it on reload. Nothing is wrong with the state itself.
//   - **A stored record is malformed.** Loud: `ViewStateError`. Coercing
//     a half-readable record into a plausible span set is how a reviewer
//     ends up with lines hidden for no reason they can see.
//
// A record from a *different schema version* is neither: it is a known
// stale artefact of an older scr in the same tab, discarded with a
// warning. Recognised version plus a payload that does not validate is
// corruption.

import {
  type CollapseLevel,
  type FileSnapshot,
  type HiddenSpan,
  type LineRange,
  type SpanKind,
  type SpanOwner,
} from "./visibility";

/** Bump when the record shape changes; an older record is then
 *  discarded rather than read as corrupt. */
const _VERSION = 1;

const _LEVELS: CollapseLevel[] = ["files", "hunks", "segments", "off"];
const _OWNERS: SpanOwner[] = ["level", "user", "gap"];
const _KINDS: SpanKind[] = ["file", "hunk", "segment", "codefold", "context"];

/** Thrown when a stored record parses but does not describe view state.
 *  Not caught by the viewer: a corrupt record is a bug in what wrote it,
 *  and limping on with a partially-read span set hides lines silently. */
export class ViewStateError extends Error {}

export interface StoredViewState {
  collapseLevel: CollapseLevel;
  files: FileSnapshot[];
}

function storageKey(runId: string): string {
  if (!runId) {
    throw new Error("view state: empty run id (data.json carries no run_id)");
  }
  return `scr-view-state:${runId}`;
}

// One-shot latch: `_storage` is probed on every render, so an
// unavailable store would otherwise fill the console.
let _warnedUnavailable = false;

/** The tab's `sessionStorage`, or null when the browser denies it.
 *  Accessing the property itself throws under a blocking cookie policy,
 *  which is why this is not a plain reference. */
function _storage(): Storage | null {
  try {
    return sessionStorage;
  } catch (e) {
    if (!_warnedUnavailable) {
      _warnedUnavailable = true;
      console.warn("view state: sessionStorage unavailable, state is in-memory only", e);
    }
    return null;
  }
}

// --- Validation -----------------------------------------------------------

function _fail(what: string): never {
  throw new ViewStateError(`stored view state: ${what}`);
}

function _isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function _range(v: unknown, where: string): LineRange | null {
  if (v === null) return null;
  if (!_isObject(v)) _fail(`${where} is neither null nor a range`);
  const { start, end } = v;
  if (typeof start !== "number" || typeof end !== "number") {
    _fail(`${where} has non-numeric bounds`);
  }
  return { start, end };
}

function _span(v: unknown, fileId: string): HiddenSpan {
  if (!_isObject(v)) _fail(`span of ${fileId} is not an object`);
  const { id, owner, kind } = v;
  if (typeof id !== "string" || !id) _fail(`span of ${fileId} has no id`);
  if (v.fileId !== fileId) _fail(`span ${id} is filed under ${fileId}`);
  if (!_OWNERS.includes(owner as SpanOwner)) _fail(`span ${id} has owner ${String(owner)}`);
  if (!_KINDS.includes(kind as SpanKind)) _fail(`span ${id} has kind ${String(kind)}`);
  return {
    id,
    fileId,
    owner: owner as SpanOwner,
    kind: kind as SpanKind,
    right: _range(v.right, `span ${id} right`),
    left: _range(v.left, `span ${id} left`),
  };
}

function _marks(v: unknown, fileId: string): [string, SpanOwner][] {
  if (!Array.isArray(v)) _fail(`marks of ${fileId} are not an array`);
  return v.map((entry) => {
    if (!Array.isArray(entry) || entry.length !== 2) {
      _fail(`mark of ${fileId} is not an [id, owner] pair`);
    }
    const [id, owner] = entry as unknown[];
    if (typeof id !== "string" || !id) _fail(`mark of ${fileId} has no id`);
    if (!_OWNERS.includes(owner as SpanOwner)) {
      _fail(`mark ${id} has owner ${String(owner)}`);
    }
    return [id, owner as SpanOwner];
  });
}

function _fileSnapshot(v: unknown): FileSnapshot {
  if (!_isObject(v)) _fail("file entry is not an object");
  const fileId = v.fileId;
  if (typeof fileId !== "string" || !fileId) _fail("file entry has no fileId");
  if (!Array.isArray(v.spans)) _fail(`spans of ${fileId} are not an array`);
  return {
    fileId,
    spans: v.spans.map((s) => _span(s, fileId)),
    marks: _marks(v.marks, fileId),
  };
}

function _validate(raw: string): StoredViewState | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (e) {
    throw new ViewStateError(`stored view state is not JSON: ${String(e)}`);
  }
  if (!_isObject(parsed)) _fail("record is not an object");
  if (parsed.version !== _VERSION) {
    console.warn(
      `view state: discarding a record written by schema v${String(parsed.version)}`,
    );
    return null;
  }
  const level = parsed.collapseLevel;
  if (!_LEVELS.includes(level as CollapseLevel)) {
    _fail(`collapse level ${String(level)} is not a level`);
  }
  if (!Array.isArray(parsed.files)) _fail("files is not an array");
  return {
    collapseLevel: level as CollapseLevel,
    files: parsed.files.map(_fileSnapshot),
  };
}

// --- Read / write ---------------------------------------------------------

/** This run's stored state, or null when there is none to restore
 *  (nothing stored, storage denied, or a record from another schema
 *  version).
 *
 *  Raises:
 *    ViewStateError: the record exists at this schema version but does
 *      not describe view state. */
function load(runId: string): StoredViewState | null {
  const store = _storage();
  if (!store) return null;
  const raw = store.getItem(storageKey(runId));
  if (raw === null) return null;
  return _validate(raw);
}

/** Write this run's state, or degrade to in-memory on refusal. Called
 *  from `render()`, so a throw here would take the viewer down over a
 *  full quota. */
function save(runId: string, collapseLevel: CollapseLevel, files: FileSnapshot[]): void {
  const store = _storage();
  if (!store) return;
  // Outside the try: a missing run id is a broken shell, not a refused
  // write, and must not be swallowed as one.
  const key = storageKey(runId);
  const record = { version: _VERSION, collapseLevel, files };
  try {
    store.setItem(key, JSON.stringify(record));
  } catch (e) {
    console.warn("view state: sessionStorage write refused; folds will not survive a reload", e);
  }
}

export const ViewState = {
  storageKey,
  load,
  save,
};
