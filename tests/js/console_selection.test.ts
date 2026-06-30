import { describe, test, expect, afterEach } from "vitest";
import { resolveSelection } from "../../semantic_code_review/viewer/assets/console_selection";

// jsdom's Selection.toString() doesn't reflect the selected range, so we
// drive resolveSelection with a minimal fake Selection — it only reads
// isCollapsed / anchorNode / focusNode / toString(), and exercising the
// DOM walk is the whole point.
function fakeSelection(
  anchorNode: Node | null,
  text: string,
  focusNode: Node | null = anchorNode,
): Selection {
  return {
    isCollapsed: text.length === 0,
    anchorNode,
    focusNode,
    toString: () => text,
  } as unknown as Selection;
}

// A diff row: <div class="row"><div class="cell-lineno cell-lineno-new">N</div>
//             <div class="cell-code">…content…</div></div>
function makeRow(side: "old" | "new", line: number, content: string): HTMLElement {
  const row = document.createElement("div");
  row.className = "row";
  const lineno = document.createElement("div");
  lineno.className = `cell-lineno cell-lineno-${side}`;
  lineno.textContent = String(line);
  const code = document.createElement("div");
  code.className = "cell-code";
  code.textContent = content;
  row.appendChild(lineno);
  row.appendChild(code);
  return row;
}

afterEach(() => {
  document.body.innerHTML = "";
});

describe("resolveSelection", () => {
  test("returns null for a collapsed or empty selection", () => {
    expect(resolveSelection(null)).toBeNull();
    expect(resolveSelection(fakeSelection(document.body, ""))).toBeNull();
  });

  test("resolves a code selection to file / side / hunk_id / line_range", () => {
    const file = document.createElement("div");
    file.className = "file";
    const path = document.createElement("div");
    path.className = "file-path";
    path.textContent = "src/users.py";
    const hunk = document.createElement("div");
    hunk.className = "hunk";
    hunk.setAttribute("data-id", "H0_1");
    const r1 = makeRow("new", 10, "def deactivate(user):");
    const r2 = makeRow("new", 12, "    user.active = False");
    hunk.appendChild(r1);
    hunk.appendChild(r2);
    file.appendChild(path);
    file.appendChild(hunk);
    document.body.appendChild(file);

    // Anchor in r1's code cell, focus in r2's — a two-line drag.
    const sel = fakeSelection(
      r1.querySelector(".cell-code")!.firstChild,
      "def deactivate(user):\n    user.active = False",
      r2.querySelector(".cell-code")!.firstChild,
    );
    const out = resolveSelection(sel);
    expect(out).toEqual({
      selection_text: "def deactivate(user):\n    user.active = False",
      selection_kind: "code",
      side: "new",
      file: "src/users.py",
      hunk_id: "H0_1",
      line_range: [10, 12],
    });
  });

  test("collapses to a single-line range within one row", () => {
    const file = document.createElement("div");
    file.className = "file";
    const path = document.createElement("div");
    path.className = "file-path";
    path.textContent = "a.py";
    const hunk = document.createElement("div");
    hunk.className = "hunk";
    hunk.setAttribute("data-id", "H0_0");
    const row = makeRow("old", 7, "raise ValueError");
    hunk.appendChild(row);
    file.appendChild(path);
    file.appendChild(hunk);
    document.body.appendChild(file);

    const node = row.querySelector(".cell-code")!.firstChild;
    const out = resolveSelection(fakeSelection(node, "ValueError"));
    expect(out?.selection_kind).toBe("code");
    expect(out?.side).toBe("old");
    expect(out?.line_range).toEqual([7, 7]);
  });

  test("classifies a selection inside a comment thread as comment", () => {
    const thread = document.createElement("div");
    thread.className = "comment-thread";
    const body = document.createElement("div");
    body.className = "comment-body";
    body.textContent = "is this intentional?";
    thread.appendChild(body);
    document.body.appendChild(thread);

    const out = resolveSelection(
      fakeSelection(body.firstChild, "is this intentional?"),
    );
    expect(out).toEqual({
      selection_text: "is this intentional?",
      selection_kind: "comment",
    });
  });

  test("treats plain prose (no row/hunk) as a plain selection", () => {
    const p = document.createElement("p");
    p.textContent = "overview text";
    document.body.appendChild(p);
    const out = resolveSelection(fakeSelection(p.firstChild, "overview text"));
    expect(out).toEqual({
      selection_text: "overview text",
      selection_kind: "plain",
    });
  });

  test("ignores selections inside the console UI", () => {
    const drawer = document.createElement("div");
    drawer.className = "console-drawer";
    const t = document.createElement("div");
    t.textContent = "previous answer";
    drawer.appendChild(t);
    document.body.appendChild(drawer);
    expect(resolveSelection(fakeSelection(t.firstChild, "previous answer"))).toBeNull();
  });
});
