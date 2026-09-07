// The per-tab record, exercised without a rendered document: the shape
// round-trips, another run's or another version's record is not this
// one's, and the two failure kinds part ways — storage that will not
// cooperate degrades quietly to memory, a stored record that does not
// describe view state is logged and read as empty. The viewer tests
// cover what the renderer does with the record.

import { describe, test, expect, beforeEach, vi } from "vitest";
import { ViewState, type RegionRef } from "../../semantic_code_review/viewer/assets/view_state";

const RUN = "local-main-abc12345";
const GAP: RegionRef = { old: [1, 4], new: [1, 4] };
const KEY = "right:10-20:-";

function plant(record: unknown, run = RUN): void {
  sessionStorage.setItem(ViewState.storageKey(run), JSON.stringify(record));
}

function stored(run = RUN): unknown {
  const raw = sessionStorage.getItem(ViewState.storageKey(run));
  return raw === null ? null : JSON.parse(raw);
}

beforeEach(() => {
  sessionStorage.clear();
});

describe("round trip", () => {
  test("a fresh run starts empty and writes nothing until something changes", () => {
    ViewState.init(RUN);
    expect(ViewState.isRevealed("F0", GAP)).toBe(false);
    expect(ViewState.isFolded("F0", KEY)).toBe(false);
    expect(ViewState.pill()).toBeNull();
    expect(ViewState.section()).toBeNull();
    expect(stored()).toBeNull();
  });

  test("every part of the record comes back on the next init", () => {
    ViewState.init(RUN);
    ViewState.reveal("F0", GAP);
    ViewState.reveal("F1", { old: [8, 7], new: [8, 12] });   // no base lines: an insertion's region
    ViewState.setFolded("F0", KEY, true);
    ViewState.setPill("symbols:SY0");
    ViewState.setSection("background");

    ViewState.init(RUN);
    expect(ViewState.isRevealed("F0", GAP)).toBe(true);
    expect(ViewState.isRevealed("F1", { old: [8, 7], new: [8, 12] })).toBe(true);
    expect(ViewState.isRevealed("F1", GAP)).toBe(false);   // the same boundaries in another file
    expect(ViewState.isFolded("F0", KEY)).toBe(true);
    expect(ViewState.isFolded("F1", KEY)).toBe(false);
    expect(ViewState.pill()).toBe("symbols:SY0");
    expect(ViewState.section()).toBe("background");
  });

  test("the stored shape is versioned and names its run", () => {
    ViewState.init(RUN);
    ViewState.reveal("F0", GAP);
    ViewState.setFolded("F0", KEY, true);
    expect(stored()).toEqual({
      v: 1, run: RUN,
      reveals: [{ file: "F0", old: [1, 4], new: [1, 4] }],
      folds: [{ file: "F0", key: KEY }],
      pill: null, section: null,
    });
  });

  test("unreveal and unfold take their entries out, and a null pill or section clears the slot", () => {
    ViewState.init(RUN);
    ViewState.reveal("F0", GAP);
    ViewState.setFolded("F0", KEY, true);
    ViewState.setPill("files:BF0");
    ViewState.setSection("map");
    ViewState.unreveal("F0", GAP);
    ViewState.setFolded("F0", KEY, false);
    ViewState.setPill(null);
    ViewState.setSection(null);
    expect(stored()).toEqual({ v: 1, run: RUN, reveals: [], folds: [], pill: null, section: null });
  });

  test("another run's record is not this run's", () => {
    ViewState.init("local-other-99999999");
    ViewState.reveal("F0", GAP);
    ViewState.init(RUN);
    expect(ViewState.isRevealed("F0", GAP)).toBe(false);
    // Nor is it disturbed.
    expect((stored("local-other-99999999") as { reveals: unknown[] }).reveals).toHaveLength(1);
  });

  test("a record under this run's key that names another run is discarded", () => {
    plant({ v: 1, run: "someone-else", reveals: [{ file: "F0", old: [1, 4], new: [1, 4] }], folds: [], pill: "x:y", section: null });
    ViewState.init(RUN);
    expect(ViewState.isRevealed("F0", GAP)).toBe(false);
    expect(ViewState.pill()).toBeNull();
  });

  test("a record from another schema version is discarded, not migrated", () => {
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    plant({ v: 2, run: RUN, reveals: [{ file: "F0", old: [1, 4], new: [1, 4] }], folds: [], pill: null, section: null });
    ViewState.init(RUN);
    expect(ViewState.isRevealed("F0", GAP)).toBe(false);
    expect(error).not.toHaveBeenCalled();   // stale, not corrupt
  });

  test("an empty run id is a broken shell, not a key", () => {
    expect(() => ViewState.init("")).toThrow(/run id/);
  });

  test("the transient ledger is its own, and touches no storage", () => {
    ViewState.init(RUN);
    const panel = ViewState.transient();
    panel.reveal("F0", GAP);
    panel.setFolded("F0", KEY, true);
    expect(panel.isRevealed("F0", GAP)).toBe(true);
    expect(panel.isFolded("F0", KEY)).toBe(true);
    expect(ViewState.isRevealed("F0", GAP)).toBe(false);
    expect(ViewState.isFolded("F0", KEY)).toBe(false);
    expect(stored()).toBeNull();
  });
});

describe("a corrupt record is loud, and read as empty", () => {
  test.each([
    ["not JSON", "{oh no"],
    ["reveals that are not a list", JSON.stringify({ v: 1, run: RUN, reveals: {}, folds: [], pill: null, section: null })],
    ["a reveal with a non-numeric bound", JSON.stringify({
      v: 1, run: RUN, reveals: [{ file: "F0", old: ["1", 4], new: [1, 4] }], folds: [], pill: null, section: null,
    })],
    ["a fold with no key", JSON.stringify({ v: 1, run: RUN, reveals: [], folds: [{ file: "F0" }], pill: null, section: null })],
    ["a pill that is not a string", JSON.stringify({ v: 1, run: RUN, reveals: [], folds: [], pill: 7, section: null })],
  ])("%s", (_name, raw) => {
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    sessionStorage.setItem(ViewState.storageKey(RUN), raw);
    expect(() => ViewState.init(RUN)).not.toThrow();
    expect(error).toHaveBeenCalledTimes(1);
    expect(ViewState.pill()).toBeNull();
    expect(ViewState.isRevealed("F0", GAP)).toBe(false);
    // The next write replaces it.
    ViewState.setPill("files:BF0");
    expect((stored() as { pill: string }).pill).toBe("files:BF0");
  });
});

describe("storage that will not cooperate degrades to memory", () => {
  test("a refused write warns once and keeps the record in memory", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const real = sessionStorage;
    Object.defineProperty(globalThis, "sessionStorage", {
      configurable: true,
      value: { ...real, getItem: () => null, setItem: () => { throw new DOMException("quota", "QuotaExceededError"); } },
    });
    try {
      ViewState.init(RUN);
      ViewState.reveal("F0", GAP);
      ViewState.setFolded("F0", KEY, true);
      ViewState.setPill("files:BF0");
      expect(ViewState.isRevealed("F0", GAP)).toBe(true);
      expect(ViewState.isFolded("F0", KEY)).toBe(true);
      expect(ViewState.pill()).toBe("files:BF0");
      expect(warn).toHaveBeenCalledTimes(1);
    } finally {
      Object.defineProperty(globalThis, "sessionStorage", { configurable: true, value: real });
    }
  });

  test("a denied sessionStorage reads as empty and swallows the writes", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const real = sessionStorage;
    Object.defineProperty(globalThis, "sessionStorage", {
      configurable: true,
      get() { throw new DOMException("denied", "SecurityError"); },
    });
    try {
      expect(() => ViewState.init(RUN)).not.toThrow();
      expect(() => ViewState.reveal("F0", GAP)).not.toThrow();
      expect(ViewState.isRevealed("F0", GAP)).toBe(true);
      expect(warn).toHaveBeenCalledTimes(1);
    } finally {
      Object.defineProperty(globalThis, "sessionStorage", { configurable: true, value: real });
    }
  });
});

describe("before init", () => {
  test("reading or writing the record is a wiring bug, said so", async () => {
    // A fresh module instance: the one the file shares has been
    // initialised by the tests above.
    vi.resetModules();
    const fresh = (await import("../../semantic_code_review/viewer/assets/view_state")).ViewState;
    expect(() => fresh.pill()).toThrow(/init/);
    expect(() => fresh.isRevealed("F0", GAP)).toThrow(/init/);
    expect(() => fresh.setFolded("F0", KEY, true)).toThrow(/init/);
  });
});
