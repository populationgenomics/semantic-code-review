// Reviewer comments — line-anchored, round-tripped via the live
// review server (PUT/DELETE per mutation) or persisted to
// localStorage when no session endpoint is set.
//
// Each comment is anchored to {file, side, line}; the gutter
// click-handler opens an inline editor, save persists the comment,
// re-rendering re-attaches the existing rows via renderAll().
//
// Compiled by tsc to `comments.js`. Concatenated into the rendered
// HTML by `render_html.py`; viewer.js calls into window.ScrReviewerComments.

// `module: "none"` puts every top-level declaration in the shared
// global namespace, so an IIFE here keeps this module's internals
// from colliding with the other Scr* modules. Only the final
// window.ScrComments registration escapes.

(() => {

// --- State ---------------------------------------------------------------

let _lsKey = "scr-comments:local";
const _comments: Record<string, ReviewerComment> = Object.create(null);

interface AnnotationHandleLike {
  element: HTMLElement;
  placeholder: HTMLElement | null;
  resize(): void;
  remove(): void;
}

interface AnnotationsFacade {
  attach(opts: {
    anchor: HTMLElement;
    shadowAnchor?: HTMLElement | null;
    variant: string;
    content: Node | string;
    onInsert?: (el: HTMLElement) => void;
  }): AnnotationHandleLike;
  detach(row: HTMLElement): void;
  reflow(anchor: HTMLElement): void;
}

function _annotations(): AnnotationsFacade {
  return (window as unknown as { ScrAnnotations: AnnotationsFacade }).ScrAnnotations;
}

function _sessionEndpoint(): string {
  if (typeof document === "undefined") return "";
  const m = document.querySelector('meta[name="scr-session-endpoint"]');
  return m ? (m.getAttribute("content") || "") : "";
}

function _el(tag: string, className: string | null, text?: string): HTMLElement {
  const n = document.createElement(tag);
  if (className) n.className = className;
  if (text !== undefined) n.textContent = text;
  return n;
}

// --- Public API ----------------------------------------------------------

function init(data: ViewerData): void {
  _lsKey =
    "scr-comments:"
    + (data.pr && data.pr.head_sha ? data.pr.head_sha : "local");
  const app = document.getElementById("app");
  if (app) _installGutter(app);
  _storageLoad();
}

/** Re-attach comment rows for the currently-rendered DOM. Called
 *  from viewer.js's render() after every full re-render so saved
 *  comments survive. No-op when there are no comments to render. */
function renderAll(): void {
  if (Object.keys(_comments).length === 0) return;
  const byAnchor: Record<string, ReviewerComment[]> = Object.create(null);
  for (const c of Object.values(_comments)) {
    const k = `${c.file}|${c.side}|${c.line}`;
    (byAnchor[k] ||= []).push(c);
  }
  document.querySelectorAll(".file").forEach((fileEl) => {
    const pathEl = fileEl.querySelector(".file-path");
    const filePath = pathEl ? (pathEl.textContent || "") : "";
    fileEl.querySelectorAll(".row").forEach((row) => {
      const linenoCell = row.children[0] as HTMLElement | undefined;
      if (!linenoCell || !linenoCell.classList.contains("cell-lineno")) return;
      if (linenoCell.classList.contains("empty")) return;
      const side: "old" | "new" =
        linenoCell.classList.contains("cell-lineno-old") ? "old" : "new";
      const n = parseInt((linenoCell.textContent || "").trim(), 10);
      if (isNaN(n)) return;
      const relevant = byAnchor[`${filePath}|${side}|${n}`];
      if (!relevant) return;
      _refreshForAnchor(row as HTMLElement, { file: filePath, side, line: n });
    });
  });
}

// --- Storage (server-mediated, localStorage fallback) -------------------

function _storageLoad(): void {
  const endpoint = _sessionEndpoint();
  if (endpoint) {
    fetch(`${endpoint}/comments`)
      .then((r) => (r.ok ? r.json() : { comments: [] as ReviewerComment[] }))
      .then((d: { comments?: ReviewerComment[] }) => {
        for (const c of d.comments || []) _comments[c.id] = c;
        renderAll();
      })
      .catch(() => { /* server may have exited; ignore */ });
    return;
  }
  try {
    const raw = localStorage.getItem(_lsKey);
    if (!raw) return;
    const data = JSON.parse(raw) as { comments?: ReviewerComment[] };
    for (const c of data.comments || []) _comments[c.id] = c;
    renderAll();
  } catch (_) { /* ignore */ }
}

function _storageFlush(): void {
  if (_sessionEndpoint()) return;  // server round-trips per-mutation
  const payload = { comments: Object.values(_comments) };
  try { localStorage.setItem(_lsKey, JSON.stringify(payload)); } catch (_) { /* ignore */ }
}

function _save(c: ReviewerComment): Promise<ReviewerComment | null> {
  _comments[c.id] = c;
  const endpoint = _sessionEndpoint();
  if (endpoint) {
    return fetch(`${endpoint}/comments`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(c),
    })
      .then((r) => (r.ok ? r.json() as Promise<ReviewerComment> : null))
      .catch(() => null);
  }
  _storageFlush();
  return Promise.resolve(c);
}

function _delete(id: string): Promise<void> {
  delete _comments[id];
  const endpoint = _sessionEndpoint();
  if (endpoint) {
    return fetch(`${endpoint}/comments/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }).then(() => undefined).catch(() => undefined);
  }
  _storageFlush();
  return Promise.resolve();
}

// --- Anchor key + lookup ------------------------------------------------

function _commentsFor(file: string, side: "old" | "new", line: number): ReviewerComment[] {
  const k = `${file}|${side}|${line}`;
  return Object.values(_comments).filter(
    (c) => `${c.file}|${c.side}|${c.line}` === k,
  );
}

// --- Gutter affordance + click-to-comment ------------------------------

function _installGutter(appEl: HTMLElement): void {
  appEl.addEventListener("click", (e) => {
    const target = e.target as HTMLElement | null;
    if (!target) return;
    const cell = target.closest(".cell-lineno") as HTMLElement | null;
    if (!cell || cell.classList.contains("empty")) return;
    const row = cell.parentElement;
    if (!row || !row.classList.contains("row")) return;
    const side: "old" | "new" =
      cell.classList.contains("cell-lineno-old") ? "old" : "new";
    const line = parseInt((cell.textContent || "").trim(), 10);
    if (isNaN(line)) return;
    const fileEl = row.closest(".file") as HTMLElement | null;
    const pathEl = fileEl && fileEl.querySelector(".file-path");
    const filePath = pathEl ? (pathEl.textContent || "") : "";
    _openEditor({ rowEl: row as HTMLElement, side, line, file: filePath });
    e.stopPropagation();
  });
}

// --- Editor + display row ----------------------------------------------

interface EditorOpts {
  rowEl: HTMLElement;
  side: "old" | "new";
  line: number;
  file: string;
  existing?: ReviewerComment;
}

function _openEditor({ rowEl, side, line, file, existing }: EditorOpts): void {
  const bodyWrap = _el("div", "comment-editor-body");
  const ta = _el("textarea", "comment-editor-input") as HTMLTextAreaElement;
  ta.rows = 1;
  ta.placeholder = "Write a comment… (Enter to save, Shift-Enter for newline, Esc to cancel)";
  ta.value = existing ? existing.body : "";
  bodyWrap.appendChild(ta);
  const bar = _el("div", "comment-editor-bar");
  const save = _el("button", "comment-btn comment-btn-save", existing ? "Update" : "Save");
  const cancel = _el("button", "comment-btn comment-btn-cancel", "Cancel");
  bar.appendChild(save);
  bar.appendChild(cancel);
  bodyWrap.appendChild(bar);

  const handle = _annotations().attach({
    anchor: rowEl,
    shadowAnchor: (rowEl as { _scrPair?: HTMLElement | null })._scrPair || null,
    variant: "comment",
    content: bodyWrap,
    onInsert: (el) => {
      el.classList.add("annot-editor");
      const box = el.querySelector(".annot-box");
      if (box) box.classList.add("comment-editor-box");
    },
  });

  function autosize(): void {
    ta.style.height = "auto";
    ta.style.height = ta.scrollHeight + "px";
  }
  function close(): void { handle.remove(); }
  function submit(): void {
    const body = ta.value.trim();
    if (!body) { close(); return; }
    const id = (existing && existing.id) || `c-${Math.random().toString(36).slice(2, 10)}`;
    const now = Date.now() / 1000;
    const c: ReviewerComment = {
      id, file, side, line, body,
      created_at: existing ? existing.created_at : now,
      updated_at: now,
    };
    _save(c).then(() => {
      close();
      _refreshForAnchor(rowEl, { file, side, line });
    });
  }

  save.addEventListener("click", (e) => { e.stopPropagation(); submit(); });
  cancel.addEventListener("click", (e) => { e.stopPropagation(); close(); });
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); }
    else if (e.key === "Escape") { e.preventDefault(); close(); }
    e.stopPropagation();
  });
  ta.addEventListener("input", autosize);
  requestAnimationFrame(() => {
    autosize();
    ta.focus();
    ta.setSelectionRange(ta.value.length, ta.value.length);
  });
}

function _buildReviewerCommentRow(comment: ReviewerComment, anchorRowEl: HTMLElement): AnnotationHandleLike {
  const bodyWrap = _el("div", "comment-display-body");
  const body = _el("div", "comment-body");
  body.textContent = comment.body;
  bodyWrap.appendChild(body);
  const bar = _el("div", "comment-actions");
  const edit = _el("button", "comment-btn comment-btn-edit", "edit");
  const del = _el("button", "comment-btn comment-btn-del", "delete");
  bar.appendChild(edit);
  bar.appendChild(del);
  bodyWrap.appendChild(bar);

  const handle = _annotations().attach({
    anchor: anchorRowEl,
    shadowAnchor: (anchorRowEl as { _scrPair?: HTMLElement | null })._scrPair || null,
    variant: "comment",
    content: bodyWrap,
    onInsert: (elRoot) => {
      elRoot.dataset.commentId = comment.id;
      const box = elRoot.querySelector(".annot-box");
      if (box) box.classList.add("comment-display");
    },
  });

  edit.addEventListener("click", (e) => {
    e.stopPropagation();
    handle.remove();
    _openEditor({
      rowEl: anchorRowEl, side: comment.side, line: comment.line,
      file: comment.file, existing: comment,
    });
  });
  del.addEventListener("click", (e) => {
    e.stopPropagation();
    _delete(comment.id).then(() => handle.remove());
  });
  return handle;
}

interface Anchor { file: string; side: "old" | "new"; line: number; }

function _refreshForAnchor(anchorRowEl: HTMLElement, anchor: Anchor): void {
  _removeReviewerCommentRowsAfter(anchorRowEl);
  const relevant = _commentsFor(anchor.file, anchor.side, anchor.line)
    .sort((a, b) => (a.created_at || 0) - (b.created_at || 0));
  for (const c of relevant) {
    _buildReviewerCommentRow(c, anchorRowEl);
  }
  // Any LLM annotations (line_notes, fold summaries) that also
  // anchor at this row now sit further from it — reflow re-measures
  // their arrows to stretch past the newly-inserted comments.
  _annotations().reflow(anchorRowEl);
}

function _removeReviewerCommentRowsAfter(anchorRowEl: HTMLElement): void {
  let n: ChildNode | null = anchorRowEl.nextSibling;
  while (n) {
    const next = n.nextSibling;
    const isReviewerCommentRow = n.nodeType === 1
      && (n as HTMLElement).classList.contains("row-annotation")
      && (n as HTMLElement).classList.contains("annot-comment")
      && !(n as HTMLElement).classList.contains("annot-editor")
      && (n as HTMLElement).dataset
      && (n as HTMLElement).dataset.commentId;
    if (!isReviewerCommentRow) break;
    _annotations().detach(n as HTMLElement);
    n = next;
  }
}

const Comments = {
  init,
  renderAll,
};

if (typeof window !== "undefined") {
  (window as unknown as { ScrComments: typeof Comments }).ScrComments = Comments;
}

})();
