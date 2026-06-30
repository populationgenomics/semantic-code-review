// Console selection resolution — "ask about *this*".
//
// Slice 4 (ADR 0002): turn a browser text selection into a structured
// hint the console can fold into a turn. We walk the selection's anchor
// up the rendered diff DOM, reusing the same `.cell-lineno` / `.hunk` /
// `.file` resolution the comment gutter uses (`comments.ts`), and
// classify the selection as code / comment / plain:
//
//   - code    — inside a diff row; resolves to (file, side, hunk_id,
//               line_range) so the server can inline the enclosing hunk.
//   - comment — inside a reviewer/LLM annotation; carries just the text.
//   - plain   — anything else (prose, the overview); carries just the text.
//
// Selections inside the console UI itself (the prompt, the drawer, the
// footer) resolve to null so caret moves in the textarea don't register
// as a pinned selection. This module is pure DOM → data; `console.ts`
// owns the chip, the live tracking, and the wire payload.

export interface ConsoleSelection {
  selection_text: string;
  selection_kind: "code" | "comment" | "plain";
  file?: string;
  side?: "old" | "new";
  hunk_id?: string;
  /** [start, end] line numbers on `side`, inclusive; collapsed to a
   *  single line when the selection stays within one row. */
  line_range?: [number, number];
}

/** The element a (possibly text) node lives in, for `.closest()` walks. */
function elementOf(node: Node | null): Element | null {
  if (!node) return null;
  return node.nodeType === Node.ELEMENT_NODE
    ? (node as Element)
    : node.parentElement;
}

/** Resolve a node's enclosing diff row to its (side, line) — the same
 *  shape `comments.ts` reads off `row.children[0]`. Null when the node
 *  isn't in a numbered code row. */
function rowLine(node: Node | null): { side: "old" | "new"; line: number } | null {
  const row = elementOf(node)?.closest(".row");
  if (!row) return null;
  const cell = row.children[0] as HTMLElement | undefined;
  if (!cell || !cell.classList.contains("cell-lineno")) return null;
  if (cell.classList.contains("empty")) return null;
  const side: "old" | "new" =
    cell.classList.contains("cell-lineno-old") ? "old" : "new";
  const line = parseInt((cell.textContent || "").trim(), 10);
  if (isNaN(line)) return null;
  return { side, line };
}

/** Resolve a live selection to a console hint, or null when there's
 *  nothing usable (collapsed, empty, or inside the console UI). */
export function resolveSelection(sel: Selection | null): ConsoleSelection | null {
  if (!sel || sel.isCollapsed) return null;
  const text = sel.toString().trim();
  if (!text) return null;

  const anchorEl = elementOf(sel.anchorNode);
  if (!anchorEl) return { selection_text: text, selection_kind: "plain" };

  // Ignore the console's own surfaces — a caret move in the prompt or a
  // drag across the transcript isn't a selection of the change.
  if (anchorEl.closest(".console-drawer, .console-input, #status-bar")) {
    return null;
  }

  // Comment / annotation text — reviewer threads and LLM annotations.
  if (anchorEl.closest(".comment-thread, .comment-editor-body, .row-annotation")) {
    return { selection_text: text, selection_kind: "comment" };
  }

  // Code — must sit inside a diff row of a hunk in a file.
  const hunkEl = anchorEl.closest(".hunk");
  const fileEl = anchorEl.closest(".file");
  const anchor = rowLine(sel.anchorNode);
  if (hunkEl && fileEl && anchor) {
    const hunk_id = hunkEl.getAttribute("data-id") || undefined;
    const pathEl = fileEl.querySelector(".file-path");
    const file = pathEl ? (pathEl.textContent || "").trim() : "";
    // The focus row may differ from the anchor row on a multi-line drag;
    // keep the range on the anchor's side and span the two line numbers.
    const focus = rowLine(sel.focusNode);
    const lines = [anchor.line];
    if (focus && focus.side === anchor.side) lines.push(focus.line);
    const lo = Math.min(...lines);
    const hi = Math.max(...lines);
    const out: ConsoleSelection = {
      selection_text: text,
      selection_kind: "code",
      side: anchor.side,
      line_range: [lo, hi],
    };
    if (file) out.file = file;
    if (hunk_id) out.hunk_id = hunk_id;
    return out;
  }

  return { selection_text: text, selection_kind: "plain" };
}
