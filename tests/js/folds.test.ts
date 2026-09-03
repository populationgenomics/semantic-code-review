// Fold chrome over server-computed regions, exercised directly against
// hand-built row containers (the shape render.ts records in FileRows).
// The bundle-level behaviour — clicks, /fold-summary, SSE — is covered in
// viewer.test.ts; this file pins what the walker owes each pane.

import { afterEach, describe, expect, test } from "vitest";
import { FileRows } from "../../semantic_code_review/viewer/assets/file_rows";
import { Folds } from "../../semantic_code_review/viewer/assets/folds";
import { makeHunkFixture } from "./fixtures/hunk-dom";

const ROWS: RowBlock[] = [
  { kind: "ctx", old_line: 1, new_line: 1, old_text: "def foo():", new_text: "def foo():" },
  { kind: "pair", old_line: 2, new_line: 2, old_text: "    x = 1", new_text: "    x = 2" },
  { kind: "ctx", old_line: 3, new_line: 3, old_text: "    return x", new_text: "    return x" },
];

const FILE = {
  id: "F0", path: "a.py", old_path: null, status: "modified", language: "python",
  adds: 1, dels: 1, summary: "", head_line_count: 3,
  symbols: { added: [], modified: [], removed: [] },
  fold_regions: [{
    context: "both", right_start: 1, right_end: 3, left_start: 1, left_end: 3,
    has_changes: true, qualified_name: "foo", kind: "function", summary: "",
  }],
  hunks: [],
} as unknown as FileBlock;

/** One pane's copy of the file: `.file > .file-body > .hunk > .diff`, the
 *  `.diff` recorded in FileRows as render.ts does. */
function mountCopy(): { fileEl: HTMLElement; diff: HTMLElement } {
  const { container: diff, old, new: newEls } = makeHunkFixture(
    ROWS.map((r) => ({ old: r.old_text, new: r.new_text })),
  );
  FileRows.record(diff, { rows: ROWS, oldEls: old, newEls });
  const fileEl = document.createElement("div");
  fileEl.className = "file";
  fileEl.dataset.id = FILE.id;
  const body = document.createElement("div");
  body.className = "file-body";
  const hunk = document.createElement("div");
  hunk.className = "hunk";
  hunk.appendChild(diff);
  body.appendChild(hunk);
  fileEl.appendChild(body);
  document.body.appendChild(fileEl);
  return { fileEl, diff };
}

function ensureEndpointMeta(): void {
  if (document.querySelector('meta[name="scr-session-endpoint"]')) return;
  const m = document.createElement("meta");
  m.setAttribute("name", "scr-session-endpoint");
  m.setAttribute("content", "");
  document.head.appendChild(m);
}

afterEach(() => { document.body.innerHTML = ""; });

describe("attachFileFolds per pane", () => {
  test("two live copies of a file each keep their chevron", () => {
    // The diff pane and the explainer's detail panel can both show a
    // file. Attaching to one must not strip the other's chrome.
    ensureEndpointMeta();
    const a = mountCopy();
    const b = mountCopy();
    Folds.attachFileFolds(a.fileEl, FILE);
    Folds.attachFileFolds(b.fileEl, FILE);
    expect(a.fileEl.querySelectorAll(".fold-chev")).toHaveLength(1);
    expect(b.fileEl.querySelectorAll(".fold-chev")).toHaveLength(1);
    expect(a.fileEl.querySelectorAll(".annot-box")).toHaveLength(1);
    expect(b.fileEl.querySelectorAll(".annot-box")).toHaveLength(1);
  });

  test("re-attaching to a container replaces its chrome and keeps its fold state", () => {
    ensureEndpointMeta();
    const a = mountCopy();
    Folds.attachFileFolds(a.fileEl, FILE);
    const chev = a.fileEl.querySelector(".fold-chev") as SVGElement;
    chev.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    const bodyRows = Array.from(a.diff.querySelectorAll<HTMLElement>(".half-new .row:not(.row-annotation)")).slice(1);
    expect(bodyRows.map((r) => r.style.display)).toEqual(["none", "none"]);

    // A repaint rebuilds the `.file` around the same (cached) `.diff`.
    const fileEl2 = document.createElement("div");
    fileEl2.className = "file";
    const body2 = document.createElement("div");
    body2.className = "file-body";
    const hunk2 = document.createElement("div");
    hunk2.className = "hunk";
    hunk2.appendChild(a.diff);
    body2.appendChild(hunk2);
    fileEl2.appendChild(body2);
    document.body.appendChild(fileEl2);
    Folds.attachFileFolds(fileEl2, FILE);

    expect(fileEl2.querySelectorAll(".fold-chev")).toHaveLength(1);
    expect(fileEl2.querySelectorAll(".annot-box")).toHaveLength(1);
    expect((fileEl2.querySelector(".fold-chev") as SVGElement).classList.contains("open")).toBe(false);
    expect(bodyRows.map((r) => r.style.display)).toEqual(["none", "none"]);
  });

  test("a region with one row on screen attaches nothing", () => {
    ensureEndpointMeta();
    const a = mountCopy();
    const narrow = { ...FILE, fold_regions: [{ ...FILE.fold_regions[0], right_start: 3, right_end: 9, left_start: 3, left_end: 9 }] };
    Folds.attachFileFolds(a.fileEl, narrow as FileBlock);
    expect(a.fileEl.querySelectorAll(".fold-chev")).toHaveLength(0);
  });
});
