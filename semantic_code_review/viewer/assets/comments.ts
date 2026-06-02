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

// Per-session override of resolved-thread collapse state. Thread ids
// the user has manually expanded sit here; clicking the header again
// removes the override so the thread re-collapses.
const _expandedResolved = new Set<string>();

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
  /** When set, the new comment is saved as a reply (in_reply_to_id pinned
   *  to this id). Ignored for edits of an existing comment. */
  replyTo?: string | null;
}

function _openEditor({ rowEl, side, line, file, existing, replyTo }: EditorOpts): void {
  const bodyWrap = _el("div", "comment-editor-body");
  const ta = _el("textarea", "comment-editor-input") as HTMLTextAreaElement;
  ta.rows = 1;
  ta.placeholder = existing
    ? "Edit comment… (Enter to save, Shift-Enter for newline, Esc to cancel)"
    : replyTo
      ? "Write a reply… (Enter to save, Shift-Enter for newline, Esc to cancel)"
      : "Write a comment… (Enter to save, Shift-Enter for newline, Esc to cancel)";
  ta.value = existing ? existing.body : "";
  bodyWrap.appendChild(ta);
  const bar = _el("div", "comment-editor-bar");
  const save = _el("button", "comment-btn comment-btn-save",
                   existing ? "Update" : (replyTo ? "Reply" : "Save"));
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
      in_reply_to_id: existing
        ? existing.in_reply_to_id ?? null
        : (replyTo ?? null),
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

// --- Thread building ----------------------------------------------------

/** A thread is one root comment plus its replies in chronological order.
 *  Local comments and ingested comments are grouped together when the
 *  reply chain matches up — a local reply to an ingested root joins
 *  that thread rather than starting its own. */
interface Thread {
  /** Identity of the thread for DOM lookup + remove. */
  id: string;
  /** Root + replies, in display order (root first). */
  entries: ReviewerComment[];
}

function _buildThreads(comments: ReviewerComment[]): Thread[] {
  // Sort by creation time so iteration order matches display order.
  const sorted = [...comments].sort(
    (a, b) => (a.created_at || 0) - (b.created_at || 0),
  );
  const byId = new Map<string, ReviewerComment>();
  for (const c of sorted) byId.set(c.id, c);

  /** Walk up the reply chain to find the thread's effective root.
   *  An in_reply_to_id pointing outside this anchor's set is treated
   *  as no parent — the comment becomes its own root. */
  function rootIdOf(c: ReviewerComment): string {
    let cur = c;
    const seen = new Set<string>();
    while (cur.in_reply_to_id && byId.has(cur.in_reply_to_id) && !seen.has(cur.id)) {
      seen.add(cur.id);
      cur = byId.get(cur.in_reply_to_id)!;
    }
    return cur.id;
  }

  const groups = new Map<string, ReviewerComment[]>();
  for (const c of sorted) {
    const k = rootIdOf(c);
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k)!.push(c);
  }
  return Array.from(groups, ([id, entries]) => ({ id, entries }));
}

// --- Per-entry chrome --------------------------------------------------

function _buildEntryHeader(c: ReviewerComment): HTMLElement | null {
  if (!c.author) return null;
  const header = _el("div", "comment-header");
  if (c.author_avatar_url) {
    const avatar = document.createElement("img");
    avatar.className = "comment-avatar";
    avatar.src = c.author_avatar_url;
    avatar.alt = "";
    avatar.referrerPolicy = "no-referrer";
    header.appendChild(avatar);
  }
  header.appendChild(_el("span", "comment-author", `@${c.author}`));
  if (c.html_url) {
    const link = document.createElement("a");
    link.className = "comment-permalink";
    link.href = c.html_url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.title = "open on GitHub";
    link.textContent = "↗";
    header.appendChild(link);
  }
  return header;
}

function _buildEntryBody(c: ReviewerComment): HTMLElement {
  const body = _el("div", "comment-body");
  if (c.body_html) {
    body.classList.add("comment-body-html");
    body.innerHTML = c.body_html;
  } else {
    body.textContent = c.body;
  }
  return body;
}

function _buildEntry(
  c: ReviewerComment,
  isReply: boolean,
  onEdit: () => void,
  onDelete: () => void,
): HTMLElement {
  const entry = _el("div", "comment-thread-entry");
  if (isReply) entry.classList.add("comment-thread-reply");
  if (_isIngested(c)) entry.classList.add("comment-thread-entry-ingested");
  entry.dataset.commentId = c.id;

  const header = _buildEntryHeader(c);
  if (header) entry.appendChild(header);
  entry.appendChild(_buildEntryBody(c));

  if (!_isIngested(c)) {
    const bar = _el("div", "comment-actions");
    const editBtn = _el("button", "comment-btn comment-btn-edit", "edit");
    const delBtn = _el("button", "comment-btn comment-btn-del", "delete");
    bar.appendChild(editBtn);
    bar.appendChild(delBtn);
    editBtn.addEventListener("click", (e) => { e.stopPropagation(); onEdit(); });
    delBtn.addEventListener("click", (e) => { e.stopPropagation(); onDelete(); });
    entry.appendChild(bar);
  }
  return entry;
}

// --- Thread row --------------------------------------------------------

function _isThreadResolved(thread: Thread): boolean {
  // Resolution is denormalised onto every member, but the root's flag is
  // the canonical one — a local reply that re-opens the thread doesn't
  // carry the flag, and we still want to honour it.
  return Boolean(thread.entries[0]?.thread_resolved);
}

function _buildThreadRow(
  thread: Thread, anchor: Anchor, anchorRowEl: HTMLElement,
): AnnotationHandle {
  const root = thread.entries[0];
  // Reply target: prefer an ingested root so a reply nests on GitHub
  // if we ever post it back. For local-only threads the root id is
  // still the right anchor for in-session grouping.
  const replyTarget = thread.entries.find(_isIngested)?.id ?? root.id;
  const ingestedThread = thread.entries.some(_isIngested);
  const resolved = _isThreadResolved(thread);
  const expanded = !resolved || _expandedResolved.has(thread.id);

  const container = _el("div", "comment-thread");
  if (resolved) container.classList.add("comment-thread-resolved");

  let handle: AnnotationHandle | null = null;
  const refresh = (): void => {
    handle?.remove();
    _refreshForAnchor(anchorRowEl, anchor);
  };

  if (resolved) {
    // Collapsed header sits at the top of every resolved thread; the
    // body below is hidden until the user expands it.
    const header = _buildResolvedHeader(thread, expanded, () => {
      if (_expandedResolved.has(thread.id)) _expandedResolved.delete(thread.id);
      else _expandedResolved.add(thread.id);
      refresh();
    });
    container.appendChild(header);
  }

  if (expanded) {
    thread.entries.forEach((c, idx) => {
      const entry = _buildEntry(
        c, idx > 0,
        () => {
          handle?.remove();
          _openEditor({
            rowEl: anchorRowEl, side: c.side, line: c.line,
            file: c.file, existing: c,
          });
        },
        () => _store.delete(c.id).then(refresh),
      );
      container.appendChild(entry);
    });

    if (ingestedThread) {
      const actions = _el("div", "comment-thread-actions");
      const reply = _el("button", "comment-btn comment-btn-reply", "Reply");
      reply.addEventListener("click", (e) => {
        e.stopPropagation();
        handle?.remove();
        _openEditor({
          rowEl: anchorRowEl, side: anchor.side, line: anchor.line,
          file: anchor.file, replyTo: replyTarget,
        });
      });
      actions.appendChild(reply);
      container.appendChild(actions);
    }
  }

  handle = Annotations.attach({
    anchor: anchorRowEl,
    shadowAnchor: (anchorRowEl as { _scrPair?: HTMLElement | null })._scrPair || null,
    variant: "comment",
    content: container,
    onInsert: (elRoot) => {
      elRoot.dataset.threadId = thread.id;
      if (ingestedThread) elRoot.classList.add("annot-comment-ingested");
      if (resolved) elRoot.classList.add("annot-comment-resolved");
      if (resolved && !expanded) elRoot.classList.add("annot-comment-collapsed");
      const box = elRoot.querySelector(".annot-box");
      if (box) box.classList.add("comment-display");
    },
  });
  return handle;
}

function _buildResolvedHeader(
  thread: Thread, expanded: boolean, onToggle: () => void,
): HTMLElement {
  const header = _el("button", "comment-thread-resolved-header");
  header.setAttribute("type", "button");
  const chev = _el("span", "comment-thread-chev", expanded ? "▾" : "▸");
  header.appendChild(chev);
  header.appendChild(_el("span", "comment-thread-resolved-tag", "✓ Resolved"));
  const n = thread.entries.length;
  const noun = n === 1 ? "comment" : "comments";
  const root = thread.entries[0];
  const meta = root.author ? `${n} ${noun} · @${root.author}` : `${n} ${noun}`;
  header.appendChild(_el("span", "comment-thread-resolved-meta", meta));
  header.addEventListener("click", (e) => {
    e.stopPropagation();
    onToggle();
  });
  return header;
}

interface Anchor { file: string; side: "old" | "new"; line: number; }

function _refreshForAnchor(anchorRowEl: HTMLElement, anchor: Anchor): void {
  _removeReviewerCommentRowsAfter(anchorRowEl);
  const relevant = _commentsFor(anchor.file, anchor.side, anchor.line);
  const threads = _buildThreads(relevant);
  for (const thread of threads) {
    _buildThreadRow(thread, anchor, anchorRowEl);
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
    const el = n as HTMLElement;
    const isReviewerCommentRow = n.nodeType === 1
      && el.classList.contains("row-annotation")
      && el.classList.contains("annot-comment")
      && !el.classList.contains("annot-editor")
      && (el.dataset.threadId || el.dataset.commentId);
    if (!isReviewerCommentRow) break;
    Annotations.detach(el);
    n = next;
  }
}

export const Comments = {
  init,
  renderAll,
};
