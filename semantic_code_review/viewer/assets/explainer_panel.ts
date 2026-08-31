// Overview-mode detail panel — the referenced file's diff, beside the
// document.
//
// A reference in the prose (an inline chip, a Map row) opens the file it
// addresses here rather than leaving the mode: the reader checks the
// code with the sentence that sent them there still on screen, and the
// return trip is looking left rather than navigating back. "Open in
// diff" in the panel header is the way to the full ladder when the
// panel is not context enough.
//
// The mode's DOM is a split. The document cell belongs to the explainer
// and is replaced on every section write; this panel is a sibling cell
// that repaint never touches, which is what keeps it — and its scroll —
// where the reader left it across a repaint they did not ask for. The
// boundary between the two is the reader's to drag (layout_dividers.ts):
// the document's cell carries the width, the panel takes the remainder.
//
// Both cells are viewport-bound scrollers (viewer.css), so each column's
// scrollbar sits on its own right edge rather than the window's — and
// the document's needs the keyboard handed to it, which is what the
// focus on entry is for.
//
// The file is rendered through a callback injected by render.ts, so the
// dependency runs one way (render.ts → here, the `rendered.ts`
// precedent) and the render stays off `renderedDiffs`: that cache holds
// live nodes, and a node cannot be in two trees.

import { Annotations } from "./annotations";
import { Comments } from "./comments";
import { LayoutDividers } from "./layout_dividers";

// The panel's floor and the divider's strip mirror viewer.css; together
// they are what the document's ceiling is measured against. Below the
// document's own floor the prose is a column of two-word lines.
const PANEL_FLOOR = 380;
const DIVIDER_WIDTH = 8;
const DOC_FLOOR = 340;

export interface PanelHost {
  /** Fresh DOM for the reference's file, at the panel's own fold state.
   *  Throws for an id no file carries — references are validated by
   *  membership server-side, so that is a bug, not a reviewer's typo. */
  renderFile(ref: ExplainerRef): HTMLElement;
  /** Leave overview mode and land on the reference in the diff. */
  openInDiff(ref: ExplainerRef): void;
}

/** The split's elements, built together and dropped together. */
interface PanelDom {
  split: HTMLElement;
  docCell: HTMLElement;
  panel: HTMLElement;
  pathEl: HTMLElement;
  body: HTMLElement;
}

let _host: PanelHost | null = null;
let _dom: PanelDom | null = null;
// The reference the panel is showing, or null when it is closed.
let _ref: ExplainerRef | null = null;

/** Put the document in the mode's split, building the split (and the
 *  closed panel beside it) if this is the pass that entered the mode.
 *
 *  Only the document cell is written: an open panel outlives every
 *  repaint of the prose. */
function mount(app: HTMLElement, documentEl: HTMLElement, host: PanelHost): void {
  _host = host;
  const live = (_dom !== null && _dom.split.parentElement === app) ? _dom : null;
  const dom = live ?? _build(app);
  // The cell is the mode's scroller, and this replaces its whole child. A
  // layout flushed between the removal and the insertion would clamp the
  // offset against an empty content box; `replaceChildren` flushes none,
  // so this is what makes the reader's place the code's guarantee rather
  // than that call's.
  const top = dom.docCell.scrollTop;
  dom.docCell.replaceChildren(documentEl);
  dom.docCell.scrollTop = top;
  // The mode leaves the window nothing to scroll, so PgDn, Space and the
  // arrows reach the prose only through the cell that holds it. On entry
  // only: a repaint must not take focus off a comment editor in the panel
  // or the console prompt. `preventScroll` because focusing a box
  // otherwise scrolls every ancestor to reveal it.
  if (live === null) dom.docCell.focus({ preventScroll: true });
  // The chips were rebuilt with the document, so the mark on the ones
  // addressing what the panel holds has to go back on.
  if (_ref !== null) _markSource(dom, _ref);
}

function _build(app: HTMLElement): PanelDom {
  app.innerHTML = "";
  _ref = null;
  const split = _el("div", "explainer-split");
  const docCell = _el("div", "explainer-doc");
  // Focusable but not a tab stop: the focus is the keyboard's route to
  // the cell's own scroll, not a control the reader tabs to.
  docCell.tabIndex = -1;
  const panel = _el("aside", "explainer-detail");
  panel.hidden = true;
  const head = _el("div", "explainer-detail-head");
  const pathEl = _el("span", "explainer-detail-path");
  head.appendChild(pathEl);
  const openInDiff = _el("button", "explainer-detail-open", "Open in diff");
  openInDiff.title = "Leave the document and read this in the diff";
  openInDiff.addEventListener("click", () => {
    if (_ref !== null) _host?.openInDiff(_ref);
  });
  head.appendChild(openInDiff);
  const shut = _el("button", "explainer-detail-close", "×");
  shut.title = "Close (Esc)";
  shut.setAttribute("aria-label", "Close");
  shut.addEventListener("click", () => close());
  head.appendChild(shut);
  const body = _el("div", "explainer-detail-body");
  panel.appendChild(head);
  panel.appendChild(body);
  split.appendChild(docCell);
  split.appendChild(panel);
  app.appendChild(split);
  // After the split is on the page: the divider measures it to know how
  // far the document may go, and a detached box reports no width.
  docCell.insertAdjacentElement("afterend", _divider(split, docCell));
  _dom = { split, docCell, panel, pathEl, body };
  return _dom;
}

/** The boundary between the document and the panel.
 *
 *  The document's cell carries the width; the panel takes what is left,
 *  down to the floor its diff stays readable at — the far end of the
 *  same clamp. It is here rather than gated on an open panel because the
 *  document's right edge is the reader's either way: with nothing beside
 *  it, the drag is how they set the measure. */
function _divider(split: HTMLElement, docCell: HTMLElement): HTMLElement {
  return LayoutDividers.create({
    className: "layout-divider-doc",
    label: "Resize the document column",
    storageKey: "scr-explainer-doc-width",
    bounds: () => ({
      min: DOC_FLOOR,
      max: split.clientWidth - PANEL_FLOOR - DIVIDER_WIDTH,
    }),
    measure: () => docCell.getBoundingClientRect().width,
    apply: (w) => {
      // The class is what hands the measure over to the column: with a
      // width set, the prose fills what the reader gave it instead of
      // holding 72ch inside it.
      docCell.classList.toggle("explainer-doc-sized", w !== null);
      docCell.style.width = w === null ? "" : `${Math.round(w)}px`;
    },
  });
}

/** Drop the split. The caller is about to paint the diff over it, and
 *  re-entering the mode starts with a closed panel: what a reference
 *  opened belongs to the reading it was opened during. */
function unmount(): void {
  _dom = null;
  _ref = null;
}

/** Show `ref`'s file, swapping in place over whatever the panel held. */
function open(ref: ExplainerRef): void {
  const dom = _dom;
  if (dom === null || _host === null) return;
  const file = _host.renderFile(ref);
  _ref = ref;
  dom.panel.hidden = false;
  // The split packs the two cells together only while there is a second
  // one to pack against; the stylesheet reads the state off this class.
  dom.split.classList.add("panel-open");
  dom.body.replaceChildren(file);
  // The path off the rendered header rather than a second channel for
  // it: `.file-path` is the DOM's own answer to which file this is —
  // the comment gutter reads it there too.
  const path = file.querySelector(".file-path");
  dom.pathEl.textContent = path ? (path.textContent || "") : "";
  dom.pathEl.title = dom.pathEl.textContent;
  dom.body.scrollTop = 0;
  if (ref.kind === "hunk") _scrollToHunk(dom, ref.id);
  _markSource(dom, ref);
  _afterMount();
}

function close(): void {
  const dom = _dom;
  if (dom === null) return;
  dom.panel.hidden = true;
  dom.split.classList.remove("panel-open");
  dom.body.replaceChildren();
  _ref = null;
  _clearMarks();
}

/** Re-render the open reference's file — a fold toggle inside the panel
 *  lands here. The document is not touched, and the panel keeps its
 *  scroll: the reader flipped a chevron, not the page. */
function repaint(): void {
  const dom = _dom;
  if (dom === null || _ref === null || _host === null) return;
  const top = dom.body.scrollTop;
  dom.body.replaceChildren(_host.renderFile(_ref));
  dom.body.scrollTop = top;
  _afterMount();
}

/** Bring a hunk to the top of the panel's own scroller.
 *
 *  Not `scrollIntoView`: that scrolls every scrollable ancestor,
 *  including the page — which would move the reader's place in the
 *  document, the one thing opening beside it is for. */
function _scrollToHunk(dom: PanelDom, hunkId: string): void {
  const hunk = dom.body.querySelector(
    '.hunk[data-id="' + _cssEscape(hunkId) + '"]',
  ) as HTMLElement | null;
  if (!hunk) return;
  dom.body.scrollTop +=
    hunk.getBoundingClientRect().top - dom.body.getBoundingClientRect().top;
}

/** Comment threads and annotation arrows over the freshly mounted DOM.
 *  `renderAll` walks every `.file` on the page, and the panel's is one;
 *  the arrows were sized while the tree was detached, so the pass after
 *  paint is the one the diff path also runs. */
function _afterMount(): void {
  Comments.renderAll();
  Annotations.reflowAll();
  requestAnimationFrame(() => Annotations.reflowAll());
}

/** Mark the references addressing what the panel is showing. Every chip
 *  for the reference carries it: they all point at what is already
 *  open, and singling out the one that was clicked would claim a
 *  distinction the panel does not make. */
function _markSource(dom: PanelDom, ref: ExplainerRef): void {
  _clearMarks();
  dom.docCell
    .querySelectorAll<HTMLElement>('[data-ref-id="' + _cssEscape(ref.id) + '"]')
    .forEach((el) => el.classList.add("explainer-ref-open"));
}

function _clearMarks(): void {
  document
    .querySelectorAll<HTMLElement>(".explainer-ref-open")
    .forEach((el) => el.classList.remove("explainer-ref-open"));
}

function _el(tag: string, className: string, text?: string): HTMLElement {
  const n = document.createElement(tag);
  n.className = className;
  if (text !== undefined) n.textContent = text;
  return n;
}

function _cssEscape(s: string): string {
  const w = window as unknown as { CSS?: { escape?: (s: string) => string } };
  if (w.CSS && typeof w.CSS.escape === "function") return w.CSS.escape(s);
  return String(s).replace(/[^a-zA-Z0-9_-]/g, (c) => "\\" + c);
}

export const ExplainerPanel = {
  mount,
  unmount,
  open,
  close,
  repaint,
};
