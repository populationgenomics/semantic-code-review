// Shared Vitest setup for the annotations module.
//
// jsdom does not implement `ResizeObserver` — we install a stub so
// `attach()` can hook into a ResizeObserver without exploding. Tests
// that want to simulate a resize call `triggerResizeObservers()`
// themselves.
//
// jsdom's requestAnimationFrame is backed by setTimeout(0), which is
// good enough for testing reflow coalescing as long as we flush with
// `await flushRaf()` after scheduling.
//
// local/sessionStorage: node 25 ships the Web Storage API as an
// on-by-default global, but it's inert without `--localstorage-file`
// (accessing it yields a methodless stub) and shadows jsdom's working
// Storage on the shared global object. We install real in-memory
// Storages so getItem/setItem/removeItem/clear behave and stay isolated
// per run. `sessionStorage` is per-tab in a browser; `installTabStorage`
// is how a test plays a second tab.

import { afterEach, vi } from "vitest";

/** A standalone in-memory `Storage`. One per simulated tab. */
export function makeStorage(): Storage {
  const store = new Map<string, string>();
  return {
    getItem: (k) => (store.has(k) ? store.get(k)! : null),
    setItem: (k, v) => { store.set(k, String(v)); },
    removeItem: (k) => { store.delete(k); },
    clear: () => { store.clear(); },
    key: (i) => Array.from(store.keys())[i] ?? null,
    get length() { return store.size; },
  };
}

/** Swap in a fresh `sessionStorage`, as opening a second tab on the same
 *  origin does. Returns the storage that was in place, so a test can
 *  switch back to the first tab — or null if what was in place was a
 *  test's throwing stand-in for a browser that denies storage.
 *
 *  `defineProperty` rather than assignment: a test may have replaced the
 *  property with a throwing getter, which plain assignment would not
 *  displace. */
export function installTabStorage(storage: Storage = makeStorage()): Storage | null {
  let previous: Storage | null = null;
  try {
    previous = (globalThis as unknown as { sessionStorage: Storage }).sessionStorage;
  } catch {
    previous = null;
  }
  Object.defineProperty(globalThis, "sessionStorage", {
    configurable: true, writable: true, value: storage,
  });
  return previous;
}

(globalThis as unknown as { localStorage: Storage }).localStorage = makeStorage();
installTabStorage();

type RoCallback = (entries: ResizeObserverEntry[]) => void;

interface StubResizeObserver {
  observe(target: Element): void;
  unobserve(target: Element): void;
  disconnect(): void;
  __callback: RoCallback;
  __targets: Set<Element>;
}

const observers = new Set<StubResizeObserver>();

class ResizeObserverStub implements StubResizeObserver {
  __callback: RoCallback;
  __targets = new Set<Element>();
  constructor(callback: RoCallback) {
    this.__callback = callback;
    observers.add(this);
  }
  observe(target: Element): void {
    this.__targets.add(target);
  }
  unobserve(target: Element): void {
    this.__targets.delete(target);
  }
  disconnect(): void {
    this.__targets.clear();
    observers.delete(this);
  }
}

(globalThis as unknown as { ResizeObserver: typeof ResizeObserver }).ResizeObserver =
  ResizeObserverStub as unknown as typeof ResizeObserver;

export function triggerResizeObservers(): void {
  for (const o of observers) {
    o.__callback([] as unknown as ResizeObserverEntry[]);
  }
}

export async function flushRaf(): Promise<void> {
  // Let the RAF-scheduled callbacks (backed by setTimeout(~16ms) in
  // jsdom) run and settle. Three waits cover chained RAFs (initial
  // sizing RAF → reflow RAF → follow-up).
  for (let i = 0; i < 3; i++) {
    await new Promise((r) => setTimeout(r, 20));
  }
}

afterEach(() => {
  // Reset the DOM between tests.
  document.body.innerHTML = "";
  observers.clear();
  vi.restoreAllMocks();
});
