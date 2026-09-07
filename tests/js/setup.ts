// Shared Vitest setup for the annotations module.
//
// jsdom implements neither `ResizeObserver` nor `scrollIntoView` — we
// install stubs so
// `attach()` can hook into a ResizeObserver without exploding. Tests
// that want to simulate a resize call `triggerResizeObservers()`
// themselves.
//
// jsdom's requestAnimationFrame is backed by setTimeout(0), which is
// good enough for testing reflow coalescing as long as we flush with
// `await flushRaf()` after scheduling.
//
// Web Storage: node 25 ships localStorage and sessionStorage as
// on-by-default globals, but they're inert without `--localstorage-file`
// (accessing one yields a methodless stub) and shadow jsdom's working
// Storage on the shared global object. We install real in-memory
// Storages so getItem/setItem/removeItem/clear behave and stay isolated
// per run.

import { afterEach, vi } from "vitest";

function memoryStorage(): Storage {
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

(globalThis as unknown as { localStorage: Storage }).localStorage = memoryStorage();
(globalThis as unknown as { sessionStorage: Storage }).sessionStorage = memoryStorage();

// jsdom implements no layout, so Element.scrollIntoView is absent
// entirely (not a no-op). The viewer calls it to bring a Map row's file
// or a document section's heading into view; without a stub the click
// handler throws.
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = function scrollIntoView(): void { /* no layout in jsdom */ };
}

// jsdom implements no pointer capture either. A divider drag claims the
// pointer so the stream keeps arriving once it outruns the 8px strip;
// with the events dispatched on the divider itself, a no-op is the whole
// of what a test needs from it.
if (!Element.prototype.setPointerCapture) {
  Element.prototype.setPointerCapture = function setPointerCapture(): void { /* no pointer capture in jsdom */ };
  Element.prototype.releasePointerCapture = function releasePointerCapture(): void { /* ditto */ };
}

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
