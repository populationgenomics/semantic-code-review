// Minimal DOM fixtures for annotation tests.
//
// The annotation module doesn't know about diffs, but our tests want
// realistic-shape anchors: a row with [lineno-cell, content-cell] where
// the content cell contains a <code> node with the line's text. The
// helpers here build that shape without pulling in anything else from
// the viewer.

export interface AnchorRowOptions {
  lineno?: number | string;
  text?: string;
  wrap?: "half-old" | "half-new" | "none";
}

export interface AnchorPair {
  container: HTMLElement;
  anchor: HTMLElement;
  shadow: HTMLElement | null;
}

/** Build a single anchor row (no half wrapper) for diff-agnostic tests. */
export function makeAnchorRow(opts: AnchorRowOptions = {}): HTMLElement {
  const row = document.createElement("div");
  row.className = "row row-ctx";
  const lineno = document.createElement("span");
  lineno.className = "cell cell-lineno cell-lineno-new";
  lineno.textContent = String(opts.lineno ?? 1);
  row.appendChild(lineno);
  const content = document.createElement("span");
  content.className = "cell cell-content cell-content-new";
  const code = document.createElement("code");
  code.textContent = opts.text ?? "hello world";
  content.appendChild(code);
  row.appendChild(content);
  return row;
}

/** Build a hunk fixture mirroring the viewer: .diff > .half-old + .half-new. */
export function makeHunkFixture(rows: Array<{ old: string; new: string }>): {
  container: HTMLElement;
  old: HTMLElement[];
  new: HTMLElement[];
} {
  const container = document.createElement("div");
  container.className = "diff";
  const halfOld = document.createElement("div");
  halfOld.className = "half half-old";
  const halfNew = document.createElement("div");
  halfNew.className = "half half-new";
  container.appendChild(halfOld);
  container.appendChild(halfNew);
  const oldRows: HTMLElement[] = [];
  const newRows: HTMLElement[] = [];
  rows.forEach((r, i) => {
    const oldRow = buildRow(i + 1, r.old, "old");
    const newRow = buildRow(i + 1, r.new, "new");
    halfOld.appendChild(oldRow);
    halfNew.appendChild(newRow);
    oldRows.push(oldRow);
    newRows.push(newRow);
  });
  document.body.appendChild(container);
  return { container, old: oldRows, new: newRows };
}

function buildRow(lineno: number, text: string, side: "old" | "new"): HTMLElement {
  const row = document.createElement("div");
  row.className = "row row-ctx";
  const linenoCell = document.createElement("span");
  linenoCell.className = `cell cell-lineno cell-lineno-${side}`;
  linenoCell.textContent = String(lineno);
  row.appendChild(linenoCell);
  const content = document.createElement("span");
  content.className = `cell cell-content cell-content-${side}`;
  const code = document.createElement("code");
  code.textContent = text;
  content.appendChild(code);
  row.appendChild(content);
  return row;
}
