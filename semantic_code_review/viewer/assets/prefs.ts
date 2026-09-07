// The reader's preferences — the settings that outlive one run.
//
// The review server binds an ephemeral port, so every run is a new
// origin and localStorage starts empty; what should carry across runs
// (the span gutter's fold, the divider widths) is fetched from `/prefs`
// at boot and written back with PATCH. This module is the in-memory
// copy: `load` fills it before the first paint, `get` reads it
// synchronously, `set`/`unset` update it at once and send the change
// behind a short delay so a drag's stream of widths goes as one request.
//
// Per-run view state — the selected pill, the open section, what the
// reviewer revealed and folded — is not a preference and does not come
// here: it is the tab's record of the run (view_state.ts).

export type PrefValue = string | number | boolean;

/** How long a burst of sets is held before one PATCH carries the last
 *  value of each key. A divider drag stores on release, but arrow-key
 *  nudges store per keypress. */
const FLUSH_MS = 200;

let _endpoint = "";
let _values: Record<string, PrefValue> = Object.create(null);
// Keys changed since the last flush; null is an unset.
let _pending: Record<string, PrefValue | null> = Object.create(null);
let _timer: ReturnType<typeof setTimeout> | null = null;
let _inflight: Promise<void> | null = null;

/** Fetch the preferences once. Resolves either way: a server without
 *  the route, or an unreachable one, leaves the defaults in place and
 *  says so once on the console — a preference is never worth a failed
 *  boot. Await it before the first paint so the stored fold and widths
 *  apply without a flash. */
async function load(endpoint: string): Promise<void> {
  _endpoint = endpoint;
  _values = Object.create(null);
  _pending = Object.create(null);
  if (_timer !== null) { clearTimeout(_timer); _timer = null; }
  try {
    const r = await fetch(`${endpoint}/prefs`, { cache: "no-store" });
    if (!r.ok) throw new Error(`GET /prefs -> ${r.status}`);
    const body = (await r.json()) as Record<string, unknown>;
    for (const [k, v] of Object.entries(body)) {
      if (typeof v === "string" || typeof v === "number" || typeof v === "boolean") _values[k] = v;
    }
  } catch (e) {
    console.warn("prefs: could not load, using defaults", e);
  }
}

function get(key: string): PrefValue | undefined {
  return _values[key];
}

/** Record `value` and send it. The in-memory copy changes now, so a
 *  reader of `get` in the same run sees it whether or not the PATCH
 *  lands. */
function set(key: string, value: PrefValue): void {
  _values[key] = value;
  _queue(key, value);
}

/** Forget `key` — back to the default — here and on the server. */
function unset(key: string): void {
  delete _values[key];
  _queue(key, null);
}

function _queue(key: string, value: PrefValue | null): void {
  _pending[key] = value;
  if (_timer === null) _timer = setTimeout(_flush, FLUSH_MS);
}

/** Send everything pending as one PATCH. Sets that arrive while a
 *  request is out wait for it, so two requests never race to the file
 *  in the wrong order. */
function _flush(): void {
  _timer = null;
  if (_inflight !== null) {
    _timer = setTimeout(_flush, FLUSH_MS);
    return;
  }
  const patch = _pending;
  _pending = Object.create(null);
  if (Object.keys(patch).length === 0) return;
  _inflight = fetch(`${_endpoint}/prefs`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  })
    .then((r) => {
      if (!r.ok) throw new Error(`PATCH /prefs -> ${r.status}`);
    })
    .catch((e) => {
      // The in-memory value stands: the reader has what they asked for
      // this run, and the next successful write carries the key again
      // if it changes.
      console.warn("prefs: could not save", patch, e);
    })
    .finally(() => {
      _inflight = null;
    });
}

export const Prefs = {
  load,
  get,
  set,
  unset,
};
