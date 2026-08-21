// The per-tab storage layer, exercised without a rendered document.
//
// Two things are asserted here that the viewer tests cannot isolate: the
// record shape round-trips both ledgers, and the split between the
// failures that degrade quietly (storage denied, write refused) and the
// one that must not — a stored record that parses but does not describe
// view state.

import { describe, test, expect, beforeEach, vi } from "vitest";
import { installTabStorage, makeStorage } from "./setup";
import { ViewState, ViewStateError } from "../../semantic_code_review/viewer/assets/view_state";
import { type FileSnapshot, type HiddenSpan } from "../../semantic_code_review/viewer/assets/visibility";

const RUN = "local-main-abc12345";

function span(id: string, overrides: Partial<HiddenSpan> = {}): HiddenSpan {
  return {
    id, fileId: "F0", owner: "user", kind: "hunk",
    right: { start: 10, end: 14 }, left: { start: 10, end: 13 },
    ...overrides,
  };
}

function snapshot(): FileSnapshot[] {
  return [{
    fileId: "F0",
    spans: [span("hunk:H0_0"), span("ctx:F0:1-4:1-4", { owner: "gap", kind: "context" })],
    // `hunk:H0_1` is marked but not hidden — a reveal.
    marks: [["hunk:H0_0", "user"], ["ctx:F0:1-4:1-4", "gap"], ["hunk:H0_1", "level"]],
  }];
}

/** Write a record straight to storage, bypassing the serialiser, so a
 *  test can plant something the serialiser would never produce. */
function plant(record: unknown): void {
  sessionStorage.setItem(ViewState.storageKey(RUN), JSON.stringify(record));
}

beforeEach(() => {
  installTabStorage();
});

describe("round trip", () => {
  test("nothing stored reads as nothing to restore", () => {
    expect(ViewState.load(RUN)).toBeNull();
  });

  test("the level and both ledgers come back as they went in", () => {
    ViewState.save(RUN, "segments", snapshot());
    const back = ViewState.load(RUN);
    expect(back).not.toBeNull();
    expect(back!.collapseLevel).toBe("segments");
    expect(back!.files).toEqual(snapshot());
  });

  test("a mark with no span survives — that is what a reveal is", () => {
    ViewState.save(RUN, "hunks", snapshot());
    const file = ViewState.load(RUN)!.files[0];
    expect(file.spans.map((s) => s.id)).not.toContain("hunk:H0_1");
    expect(file.marks).toContainEqual(["hunk:H0_1", "level"]);
  });

  test("a span with no side keeps its null rather than a zero range", () => {
    ViewState.save(RUN, "off", [{
      fileId: "F0", spans: [span("cf:F0:left", { left: null })], marks: [["cf:F0:left", "user"]],
    }]);
    expect(ViewState.load(RUN)!.files[0].spans[0].left).toBeNull();
  });

  test("another run's state is not this run's", () => {
    ViewState.save("local-other-99999999", "off", snapshot());
    expect(ViewState.load(RUN)).toBeNull();
  });

  test("an empty run id is a broken shell, not a key", () => {
    expect(() => ViewState.load("")).toThrow(/run id/);
    expect(() => ViewState.save("", "off", [])).toThrow(/run id/);
  });
});

describe("per-tab isolation", () => {
  test("a second tab starts empty and does not disturb the first", () => {
    ViewState.save(RUN, "files", snapshot());

    const firstTab = installTabStorage();
    expect(ViewState.load(RUN)).toBeNull();
    ViewState.save(RUN, "off", []);

    installTabStorage(firstTab!);
    expect(ViewState.load(RUN)!.collapseLevel).toBe("files");
  });

  test("the record goes to sessionStorage, not the shared localStorage", () => {
    localStorage.clear();
    ViewState.save(RUN, "hunks", snapshot());
    expect(sessionStorage.getItem(ViewState.storageKey(RUN))).not.toBeNull();
    expect(localStorage.getItem(ViewState.storageKey(RUN))).toBeNull();
  });
});

describe("storage that will not cooperate degrades to in-memory", () => {
  test("a denied sessionStorage reads as nothing and swallows the write", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    Object.defineProperty(globalThis, "sessionStorage", {
      configurable: true,
      get() { throw new DOMException("denied", "SecurityError"); },
    });
    try {
      expect(ViewState.load(RUN)).toBeNull();
      expect(() => ViewState.save(RUN, "off", snapshot())).not.toThrow();
      expect(warn).toHaveBeenCalled();
    } finally {
      installTabStorage();
    }
  });

  test("a refused write warns and returns rather than throwing", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    installTabStorage({
      ...makeStorage(),
      setItem: () => { throw new DOMException("quota", "QuotaExceededError"); },
    });
    expect(() => ViewState.save(RUN, "off", snapshot())).not.toThrow();
    expect(warn).toHaveBeenCalled();
  });
});

describe("a malformed record fails loud", () => {
  test.each([
    ["not JSON at all", "{oh no"],
    ["a JSON scalar", "42"],
  ])("%s", (_name, raw) => {
    sessionStorage.setItem(ViewState.storageKey(RUN), raw);
    expect(() => ViewState.load(RUN)).toThrow(ViewStateError);
  });

  test.each([
    ["a collapse level that is not one", { collapseLevel: "everything", files: [] }],
    ["files that are not an array", { collapseLevel: "off", files: {} }],
    ["a file entry with no id", { collapseLevel: "off", files: [{ spans: [], marks: [] }] }],
    ["a file entry with no marks ledger", {
      collapseLevel: "off", files: [{ fileId: "F0", spans: [] }],
    }],
    ["a span with an unknown owner", {
      collapseLevel: "off",
      files: [{ fileId: "F0", spans: [{ ...span("x"), owner: "someone" }], marks: [] }],
    }],
    ["a span with an unknown kind", {
      collapseLevel: "off",
      files: [{ fileId: "F0", spans: [{ ...span("x"), kind: "paragraph" }], marks: [] }],
    }],
    ["a span filed under another file", {
      collapseLevel: "off",
      files: [{ fileId: "F1", spans: [span("x")], marks: [] }],
    }],
    ["a range with non-numeric bounds", {
      collapseLevel: "off",
      files: [{
        fileId: "F0",
        spans: [{ ...span("x"), right: { start: "10", end: 14 } }],
        marks: [],
      }],
    }],
    ["a mark that is not an [id, owner] pair", {
      collapseLevel: "off", files: [{ fileId: "F0", spans: [], marks: ["hunk:H0_0"] }],
    }],
  ])("%s", (_name, record) => {
    plant({ version: 1, ...(record as object) });
    expect(() => ViewState.load(RUN)).toThrow(ViewStateError);
  });

  test("a record from a different schema version is stale, not corrupt", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    plant({ version: 99, anything: true });
    expect(ViewState.load(RUN)).toBeNull();
    expect(warn).toHaveBeenCalled();
  });
});
