// Reviewer comments — line-anchored, round-tripped via the live
// review server or persisted to localStorage. Storage strategy lives
// in `comment_store.ts`; this module owns the gutter, the editor,
// and the DOM re-attach pass.
//
// Each comment is anchored to {file, side, line}; the gutter
// click-handler opens an inline editor, save persists the comment
// through the store, re-rendering re-attaches the existing rows via
// renderAll().
//
import { Annotations, type AnnotationHandle } from "./annotations";
import { type CommentStore, makeLocalStore, makeServerStore } from "./comment_store";

// --- State ---------------------------------------------------------------

// Picked once at init(); never re-resolved. Until init runs, every
// op short-circuits to a no-op store so jsdom unit tests that don't
// bother to init() don't crash on stray click-handlers.
let _store: CommentStore = makeLocalStore("scr-comments:uninit");

function _sessionEndpoint(): string | null {
  if (typeof document === "undefined") return null;
  const m = document.querySelector('meta[name="scr-session-endpoint"]');
  if (!m) return null;
  return m.getAttribute("content") || "";  // "" = same origin
}

function _el(tag: string, className: string | null, text?: string): HTMLElement {
  const n = document.createElement(tag);
  if (className) n.className = className;
  if (text !== undefined) n.textContent = text;
  return n;
}

// --- Public API ----------------------------------------------------------

function init(data: ViewerData): void {
  const endpoint = _sessionEndpoint();
  if (endpoint !== null) {
    _store = makeServerStore(endpoint);
  } else {
    const lsKey =
      "scr-comments:"
      + (data.pr && data.pr.head_sha ? data.pr.head_sha : "local");
    _store = makeLocalStore(lsKey);
  }
  const app = document.getElementById("app");
  if (app) _installGutter(app);
  _store.load().then(renderAll);
}

/** Re-attach comment rows for the currently-rendered DOM. Called
 *  from render() after every full re-render so saved comments
 *  survive. No-op when there are no comments to render. */
function renderAll(): void {
  const all = _store.getAll();
  if (all.length === 0) return;
  const byAnchor: Record<string, ReviewerComment[]> = Object.create(null);
  for (const c of all) {
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

// --- Anchor lookup ------------------------------------------------------

function _commentsFor(file: string, side: "old" | "new", line: number): ReviewerComment[] {
  const k = `${file}|${side}|${line}`;
  return _store.getAll().filter(
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

  const handle = Annotations.attach({
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
    _store.save(c).then(() => {
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

function _isIngested(comment: ReviewerComment): boolean {
  return (comment.source || "local") !== "local";
}

function _buildReviewerCommentRow(comment: ReviewerComment, anchorRowEl: HTMLElement): AnnotationHandle {
  const ingested = _isIngested(comment);

  const bodyWrap = _el("div", "comment-display-body");
  if (comment.author) {
    const header = _el("div", "comment-header");
    if (comment.author_avatar_url) {
      const avatar = document.createElement("img");
      avatar.className = "comment-avatar";
      avatar.src = comment.author_avatar_url;
      avatar.alt = "";
      avatar.referrerPolicy = "no-referrer";
      header.appendChild(avatar);
    }
    header.appendChild(_el("span", "comment-author", `@${comment.author}`));
    if (comment.html_url) {
      const link = document.createElement("a");
      link.className = "comment-permalink";
      link.href = comment.html_url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.title = "open on GitHub";
      link.textContent = "↗";  // ↗
      header.appendChild(link);
    }
    bodyWrap.appendChild(header);
  }

  const body = _el("div", "comment-body");
  if (comment.body_html) {
    // Trusted upstream rendering (the API call is authenticated as the
    // user and goes through their own gh CLI). Inject verbatim rather
    // than ship a markdown parser to the client.
    body.classList.add("comment-body-html");
    body.innerHTML = comment.body_html;
  } else {
    body.textContent = comment.body;
  }
  bodyWrap.appendChild(body);

  let editBtn: HTMLElement | null = null;
  let delBtn: HTMLElement | null = null;
  if (!ingested) {
    const bar = _el("div", "comment-actions");
    editBtn = _el("button", "comment-btn comment-btn-edit", "edit");
    delBtn = _el("button", "comment-btn comment-btn-del", "delete");
    bar.appendChild(editBtn);
    bar.appendChild(delBtn);
    bodyWrap.appendChild(bar);
  }

  const handle = Annotations.attach({
    anchor: anchorRowEl,
    shadowAnchor: (anchorRowEl as { _scrPair?: HTMLElement | null })._scrPair || null,
    variant: "comment",
    content: bodyWrap,
    onInsert: (elRoot) => {
      elRoot.dataset.commentId = comment.id;
      if (ingested) elRoot.classList.add("annot-comment-ingested");
      if (comment.in_reply_to_id) elRoot.classList.add("annot-comment-reply");
      const box = elRoot.querySelector(".annot-box");
      if (box) box.classList.add("comment-display");
    },
  });

  if (editBtn && delBtn) {
    editBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      handle.remove();
      _openEditor({
        rowEl: anchorRowEl, side: comment.side, line: comment.line,
        file: comment.file, existing: comment,
      });
    });
    delBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      _store.delete(comment.id).then(() => handle.remove());
    });
  }
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
  Annotations.reflow(anchorRowEl);
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
    Annotations.detach(n as HTMLElement);
    n = next;
  }
}

export const Comments = {
  init,
  renderAll,
};
