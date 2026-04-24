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

import { afterEach, vi } from "vitest";

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
