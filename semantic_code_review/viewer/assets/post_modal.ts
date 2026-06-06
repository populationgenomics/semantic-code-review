// Confirmation modal for `scr pr` — replaces the y/N terminal prompt.
//
// On boot the viewer fetches /post-config; when `posting: true` the
// Done button's click handler is swapped from "POST /exit" to "open
// the modal". The modal pulls /post-preview, lists the comments that
// would be posted, lets the reviewer deselect with a checkbox or
// delete entirely with a per-row ×, then POSTs /post-review with the
// selected IDs. On success it swaps to a result view with the GitHub
// URL and a Close button that fires the real /exit.

interface PostConfig {
  posting: boolean;
  repo?: string;
  number?: number;
  head_sha?: string;
}

interface PostPreviewRow {
  id: string;
  file: string;
  side: string;
  line: number;
  body: string;
  is_reply: boolean;
}

interface PostResultResponse {
  posted: number;
  review_url: string;
  review_id: number;
}

interface InstallResult {
  /** Replacement handler for the Done button when posting is enabled.
   *  `null` means the Done button should keep its default /exit behaviour. */
  onDoneClick: (() => void) | null;
}

/** Fetch the post config and install the modal infrastructure when
 *  the server is in posting mode. Returns `{ onDoneClick }` so the
 *  caller (boot.ts) can decide how to wire the Done button. */
async function install(endpoint: string): Promise<InstallResult> {
  const cfg = await fetchConfig(endpoint);
  if (!cfg.posting) return { onDoneClick: null };

  const modal = buildModal();
  document.body.appendChild(modal.root);
  // Esc closes the modal — same affordance as the help overlay.
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !modal.root.classList.contains("hidden")) {
      hide(modal.root);
    }
  });

  return {
    onDoneClick: () => openModal(endpoint, cfg, modal),
  };
}

// --- modal lifecycle -----------------------------------------------------

interface ModalRefs {
  root: HTMLElement;
  meta: HTMLElement;
  list: HTMLElement;
  status: HTMLElement;
  cancel: HTMLButtonElement;
  confirm: HTMLButtonElement;
}

function buildModal(): ModalRefs {
  const root = el("div", "post-modal hidden");
  root.setAttribute("role", "dialog");
  root.setAttribute("aria-label", "Confirm post");
  const card = el("div", "post-modal-card");
  root.appendChild(card);

  const header = el("h3", "post-modal-header");
  header.textContent = "Post review";
  card.appendChild(header);

  const meta = el("p", "post-modal-meta");
  card.appendChild(meta);

  const list = el("ul", "post-modal-list");
  card.appendChild(list);

  const status = el("p", "post-modal-status");
  card.appendChild(status);

  const footer = el("div", "post-modal-footer");
  const cancel = el("button", "post-cancel") as HTMLButtonElement;
  cancel.type = "button";
  cancel.textContent = "Cancel";
  const confirm = el("button", "post-confirm") as HTMLButtonElement;
  confirm.type = "button";
  confirm.textContent = "Post";
  footer.appendChild(cancel);
  footer.appendChild(confirm);
  card.appendChild(footer);

  // Clicking the dim backdrop (root, but NOT the card) cancels.
  root.addEventListener("click", (e) => {
    if (e.target === root) hide(root);
  });

  return { root, meta, list, status, cancel, confirm };
}

async function openModal(endpoint: string, cfg: PostConfig, modal: ModalRefs): Promise<void> {
  // Reset every piece of state the prior open might have mutated —
  // the success block, the swapped Close-on-empty handler, the
  // hidden Cancel button. Otherwise reopening after a cancel
  // shows stale UI.
  const card = modal.root.firstElementChild as HTMLElement;
  const oldSuccess = card.querySelector(".post-modal-success");
  if (oldSuccess) oldSuccess.remove();
  if (!card.querySelector(".post-modal-header")) {
    const h = el("h3", "post-modal-header");
    h.textContent = "Post review";
    card.insertBefore(h, modal.meta);
  }
  modal.cancel.style.display = "";
  modal.cancel.disabled = false;
  modal.confirm.disabled = true;
  modal.confirm.textContent = "Post";
  modal.confirm.onclick = null;
  modal.meta.textContent = "Loading…";
  modal.list.replaceChildren();
  modal.status.textContent = "";
  show(modal.root);

  let rows: PostPreviewRow[];
  try {
    rows = await fetchPreview(endpoint);
  } catch (e) {
    modal.status.textContent = `Failed to load preview: ${e}`;
    return;
  }

  // Meta line: short, schematic. Repo / number / head SHA come from
  // /post-config; the modal labels itself so the reviewer can see at
  // a glance where the comments will land.
  const bits: string[] = [];
  if (cfg.repo) bits.push(cfg.repo);
  if (cfg.number != null) bits.push(`#${cfg.number}`);
  if (cfg.head_sha) bits.push(`@${cfg.head_sha.slice(0, 8)}`);
  modal.meta.textContent = bits.join(" · ");

  if (rows.length === 0) {
    // Nothing to post — there's no useful "Confirm" action here.
    // Collapse the footer to a single Close button that fires /exit
    // so the reviewer doesn't have to figure out that "Cancel" plus
    // closing the tab is the way out.
    const empty = el("li", "post-modal-empty");
    empty.textContent = "No comments to post.";
    modal.list.appendChild(empty);
    modal.cancel.style.display = "none";
    modal.confirm.disabled = false;
    modal.confirm.textContent = "Close";
    modal.confirm.onclick = () => {
      modal.confirm.disabled = true;
      modal.confirm.textContent = "Closing…";
      fetch(`${endpoint}/exit`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      }).catch(() => { /* server may exit before responding */ })
        .finally(() => { modal.confirm.textContent = "Closed"; });
    };
    return;
  }

  for (const row of rows) {
    modal.list.appendChild(renderRow(endpoint, row, modal));
  }
  updateConfirmLabel(modal);

  modal.confirm.onclick = () => onConfirm(endpoint, modal);
  modal.cancel.onclick = () => hide(modal.root);
}

function renderRow(endpoint: string, row: PostPreviewRow, modal: ModalRefs): HTMLElement {
  const li = el("li", "post-row");
  li.dataset.id = row.id;

  const label = el("label", "post-row-label");
  const cb = el("input", "post-row-check") as HTMLInputElement;
  cb.type = "checkbox";
  cb.checked = true;
  cb.addEventListener("change", () => updateConfirmLabel(modal));
  label.appendChild(cb);

  const loc = el("span", "post-row-loc");
  loc.textContent = `${row.file}:${row.line} (${row.side})`;
  label.appendChild(loc);

  if (row.is_reply) {
    const kind = el("span", "post-row-kind");
    kind.textContent = "reply";
    label.appendChild(kind);
  }

  const body = el("div", "post-row-body");
  body.textContent = row.body;
  label.appendChild(body);

  li.appendChild(label);

  // Per-row destructive delete — calls the same DELETE /comments/<id>
  // path the inline gutter uses. After it succeeds we re-fetch the
  // preview so deletions made elsewhere (or to threads with replies)
  // are reflected consistently.
  const del = el("button", "post-row-delete") as HTMLButtonElement;
  del.type = "button";
  del.title = "Delete this comment";
  del.textContent = "×";
  del.addEventListener("click", async () => {
    del.disabled = true;
    try {
      const r = await fetch(`${endpoint}/comments/${encodeURIComponent(row.id)}`, {
        method: "DELETE",
      });
      if (!r.ok) throw new Error(`DELETE -> ${r.status}`);
      // Cheap path: drop the row from the DOM and recount.
      li.remove();
      updateConfirmLabel(modal);
      if (modal.list.children.length === 0) {
        const empty = el("li", "post-modal-empty");
        empty.textContent = "No comments to post.";
        modal.list.appendChild(empty);
      }
    } catch (e) {
      modal.status.textContent = `Delete failed: ${e}`;
      del.disabled = false;
    }
  });
  li.appendChild(del);

  return li;
}

function selectedIds(modal: ModalRefs): string[] {
  const out: string[] = [];
  for (const li of Array.from(modal.list.children) as HTMLElement[]) {
    const id = li.dataset.id;
    if (!id) continue;
    const cb = li.querySelector(".post-row-check") as HTMLInputElement | null;
    if (cb && cb.checked) out.push(id);
  }
  return out;
}

function updateConfirmLabel(modal: ModalRefs): void {
  const n = selectedIds(modal).length;
  modal.confirm.disabled = n === 0;
  modal.confirm.textContent = n === 1 ? "Post 1 comment" : `Post ${n} comments`;
}

async function onConfirm(endpoint: string, modal: ModalRefs): Promise<void> {
  const ids = selectedIds(modal);
  if (ids.length === 0) return;

  modal.confirm.disabled = true;
  modal.cancel.disabled = true;
  modal.status.textContent = "Posting…";

  let resp: Response;
  try {
    resp = await fetch(`${endpoint}/post-review`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ comment_ids: ids }),
    });
  } catch (e) {
    modal.status.textContent = `Post failed: ${e}`;
    modal.confirm.disabled = false;
    modal.cancel.disabled = false;
    return;
  }

  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`;
    try {
      const err = await resp.json() as { error?: string };
      if (err.error) detail = err.error;
    } catch (_) { /* ignore */ }
    modal.status.textContent = `Post failed: ${detail}`;
    modal.confirm.disabled = false;
    modal.cancel.disabled = false;
    return;
  }

  const result = await resp.json() as PostResultResponse;
  showResult(endpoint, modal, result);
}

function showResult(endpoint: string, modal: ModalRefs, result: PostResultResponse): void {
  // Swap the modal body to the result view. List + meta + status all
  // become a single success block; the buttons collapse to one Close.
  modal.meta.textContent = "";
  modal.list.replaceChildren();
  modal.status.textContent = "";

  const card = modal.root.firstElementChild as HTMLElement;
  // Remove the prior header so the success block reads cleanly.
  const oldHeader = card.querySelector(".post-modal-header");
  if (oldHeader) oldHeader.remove();

  const ok = el("div", "post-modal-success");
  const tick = el("span", "post-modal-tick");
  tick.textContent = "✓";
  ok.appendChild(tick);
  const text = el("div", "post-modal-success-text");
  const n = result.posted;
  const heading = el("strong", "");
  heading.textContent = n === 1 ? "Posted 1 comment" : `Posted ${n} comments`;
  text.appendChild(heading);
  if (result.review_url) {
    text.appendChild(el("br", ""));
    const link = el("a", "post-modal-link") as HTMLAnchorElement;
    link.href = result.review_url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = "View on GitHub →";
    text.appendChild(link);
  }
  ok.appendChild(text);
  card.insertBefore(ok, card.querySelector(".post-modal-footer"));

  modal.cancel.style.display = "none";
  modal.confirm.disabled = false;
  modal.confirm.textContent = "Close";
  modal.confirm.onclick = () => {
    modal.confirm.disabled = true;
    modal.confirm.textContent = "Closing…";
    fetch(`${endpoint}/exit`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    }).catch(() => { /* server may exit before responding */ })
      .finally(() => { modal.confirm.textContent = "Closed"; });
  };
}

// --- network ------------------------------------------------------------

async function fetchConfig(endpoint: string): Promise<PostConfig> {
  try {
    const r = await fetch(`${endpoint}/post-config`, { cache: "no-store" });
    if (!r.ok) return { posting: false };
    return (await r.json()) as PostConfig;
  } catch (_) {
    return { posting: false };
  }
}

async function fetchPreview(endpoint: string): Promise<PostPreviewRow[]> {
  const r = await fetch(`${endpoint}/post-preview`, { cache: "no-store" });
  if (!r.ok) {
    let detail = `HTTP ${r.status}`;
    try {
      const err = await r.json() as { error?: string };
      if (err.error) detail = err.error;
    } catch (_) { /* ignore */ }
    throw new Error(detail);
  }
  const data = await r.json() as { comments?: PostPreviewRow[] };
  return data.comments || [];
}

// --- DOM helpers --------------------------------------------------------

function el(tag: string, cls: string): HTMLElement {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  return e;
}

function show(root: HTMLElement): void { root.classList.remove("hidden"); }
function hide(root: HTMLElement): void { root.classList.add("hidden"); }

export const PostModal = { install };
