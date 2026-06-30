// Review console — bottom-bar Q&A over the change under review.
//
// Slice 2 (ADR 0002): streaming turns over SSE, with cancel. A
// persistent, unobtrusive prompt input shares the footer with the
// status counts (right); `Ctrl-P` focuses it (intercepting the browser
// Print shortcut — acceptable on a dedicated localhost tab). On submit
// it POSTs /console/ask (which returns 202) and then drives the
// transcript off the SSE stream: `console-delta` chunks accumulate into
// the answer, `console-tool` frames surface tool activity, and a
// terminal `console-done` / `console-error` ends the turn. A Stop
// affordance (and `Esc`) cancels an in-flight turn via /console/cancel;
// a second `Esc` collapses the drawer and drops the conversation.
//
// The conversation `message_history` lives server-side and is
// ephemeral; this module only holds the visible transcript. Every
// console frame is tagged with a per-tab `console_id` so streams from
// other tabs are ignored, and the frames are unbuffered server-side, so
// a mid-turn reload starts the console fresh. Answers render as markdown
// (with inline mermaid) via `renderConsoleMarkdown`, with raw HTML
// disabled and the output sanitised, so `<script>`-laden model output is
// neutralised.
//
// Slice 4 adds selection-awareness: the reviewer's page selection is
// resolved (`console_selection.ts`) to a code / comment / plain hint,
// shown as a clearable chip, and folded once into the turn it's
// submitted with — turn-anchored, never re-injected.

import { renderConsoleMarkdown } from "./console_render";
import { resolveSelection, type ConsoleSelection } from "./console_selection";

let _endpoint = "";
let _consoleId = "";
let _drawer: HTMLElement | null = null;
let _transcript: HTMLElement | null = null;
let _input: HTMLTextAreaElement | null = null;
let _stop: HTMLButtonElement | null = null;
let _chip: HTMLElement | null = null;
let _busy = false;

// The reviewer's pinned selection (Slice 4), tracked live off the page's
// selection and folded into the next turn. Set when a usable selection
// is made over the change, replaced on re-select, cleared on submit (or
// via the chip's × ). Never auto-cleared on collapse — clicking into the
// prompt deselects the diff visually, but the pin persists.
let _selection: ConsoleSelection | null = null;

// The in-flight turn's DOM + accumulated answer text. Null between
// turns; the SSE handlers no-op when there's nothing in flight.
interface PendingTurn {
  answer: HTMLElement;
  activity: HTMLElement;
  text: HTMLElement;
  accumulated: string;
}
let _pending: PendingTurn | null = null;

const MAX_INPUT_ROWS = 6;

/** A per-tab id stamped on every /console request and matched against
 *  every console SSE frame, so a tab ignores another tab's stream. */
function genConsoleId(): string {
  const c = (globalThis as { crypto?: { randomUUID?: () => string } }).crypto;
  if (c && typeof c.randomUUID === "function") return c.randomUUID();
  return "c" + Math.random().toString(36).slice(2) + Date.now().toString(36);
}

/** Mount the console bar into the footer. No-op (returns) when the
 *  footer is absent — e.g. a DOM that doesn't include the status bar. */
function init(endpoint: string): void {
  _endpoint = endpoint;
  _consoleId = genConsoleId();
  const footer = document.getElementById("status-bar");
  if (!footer || _input) return; // idempotent: don't double-mount
  build(footer);
  wireGlobalKeys();
}

function build(footer: HTMLElement): void {
  footer.classList.add("has-console");

  // Transcript drawer: fixed above the footer, hidden until the first
  // turn. Grows upward (CSS caps its height, then it scrolls).
  _drawer = document.createElement("div");
  _drawer.className = "console-drawer hidden";
  _transcript = document.createElement("div");
  _transcript.className = "console-transcript";
  _drawer.appendChild(_transcript);
  document.body.appendChild(_drawer);

  // Selection chip: shows the reviewer's pinned selection, with a ×  to
  // clear it. Hidden until a selection is pinned; sits at the far left of
  // the footer, ahead of the prompt.
  _chip = document.createElement("div");
  _chip.className = "console-chip hidden";
  footer.insertBefore(_chip, footer.firstChild);

  // Prompt input on the left of the footer, ahead of the status counts.
  _input = document.createElement("textarea");
  _input.className = "console-input";
  _input.rows = 1;
  _input.placeholder = "Ask about this change…  (Ctrl-P)";
  _input.setAttribute("aria-label", "Review console prompt");
  footer.insertBefore(_input, _chip.nextSibling);

  // Stop affordance: hidden until a turn is in flight, sits between the
  // prompt and the status counts.
  _stop = document.createElement("button");
  _stop.className = "console-stop hidden";
  _stop.type = "button";
  _stop.textContent = "Stop";
  _stop.title = "Cancel the in-flight answer (Esc)";
  _stop.addEventListener("click", () => cancelTurn());
  footer.insertBefore(_stop, _input.nextSibling);

  _input.addEventListener("input", autogrow);
  _input.addEventListener("keydown", onInputKey);
  // Focusing the prompt reveals the chip for a selection made just
  // before the click; the selectionchange tracker keeps it current.
  _input.addEventListener("focus", renderChip);
  document.addEventListener("selectionchange", onSelectionChange);
}

// Track the page selection. A usable selection over the change pins
// itself (replacing any prior pin); a collapse — e.g. clicking into the
// prompt — is ignored so the pin survives until submit or an explicit
// clear. Selections inside the console UI resolve to null and are
// likewise ignored.
function onSelectionChange(): void {
  const sel = resolveSelection(
    typeof window.getSelection === "function" ? window.getSelection() : null,
  );
  if (!sel) return;
  _selection = sel;
  renderChip();
}

// (Re)paint the chip from `_selection`. Hidden when nothing is pinned.
function renderChip(): void {
  if (!_chip) return;
  if (!_selection) {
    _chip.classList.add("hidden");
    _chip.textContent = "";
    return;
  }
  _chip.classList.remove("hidden");
  _chip.textContent = "";
  const label = document.createElement("span");
  label.className = "console-chip-label";
  label.textContent = chipLabel(_selection);
  label.title = _selection.selection_text;
  const clear = document.createElement("button");
  clear.type = "button";
  clear.className = "console-chip-clear";
  clear.textContent = "×";
  clear.title = "Clear selection";
  clear.addEventListener("click", (e) => {
    e.preventDefault();
    clearSelection();
  });
  _chip.appendChild(label);
  _chip.appendChild(clear);
}

// A compact one-line description of the pinned selection for the chip.
function chipLabel(sel: ConsoleSelection): string {
  if (sel.selection_kind === "code" && sel.file) {
    const [lo, hi] = sel.line_range || [0, 0];
    const span = lo && hi ? (lo === hi ? `:${lo}` : `:${lo}–${hi}`) : "";
    return `${sel.file}${span}`;
  }
  const snippet = sel.selection_text.replace(/\s+/g, " ").slice(0, 40);
  const kind = sel.selection_kind === "comment" ? "comment" : "text";
  return `${kind}: “${snippet}${sel.selection_text.length > 40 ? "…" : ""}”`;
}

function clearSelection(): void {
  _selection = null;
  renderChip();
}

function wireGlobalKeys(): void {
  window.addEventListener("keydown", (e: KeyboardEvent) => {
    // Ctrl-P / Cmd-P focuses the prompt, suppressing the print dialog.
    if ((e.ctrlKey || e.metaKey) && (e.key === "p" || e.key === "P")) {
      e.preventDefault();
      reveal();
      _input?.focus();
    }
  });
}

function onInputKey(e: KeyboardEvent): void {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    void submit();
  } else if (e.key === "Escape") {
    e.preventDefault();
    // Esc cancels an in-flight turn first; only once nothing is running
    // does it collapse the drawer and drop the conversation.
    if (_busy) cancelTurn();
    else dismiss();
  }
}

// Grow the textarea with its content, 1 → MAX_INPUT_ROWS lines, then
// let it scroll. Measured off scrollHeight after a reset to auto.
function autogrow(): void {
  if (!_input) return;
  _input.style.height = "auto";
  const line = parseFloat(getComputedStyle(_input).lineHeight) || 18;
  const max = line * MAX_INPUT_ROWS;
  _input.style.height = Math.min(_input.scrollHeight, max) + "px";
}

async function submit(): Promise<void> {
  if (!_input || _busy) return;
  const question = _input.value.trim();
  if (!question) return;
  // The selection is turn-anchored: snapshot it for this turn, then drop
  // the pin so it never bleeds into the next question.
  const selection = _selection;
  _input.value = "";
  autogrow();
  clearSelection();
  reveal();
  appendQuestion(question);
  _pending = appendAnswer();
  setBusy(true);
  try {
    const body: Record<string, unknown> = { question, console_id: _consoleId };
    if (selection) body.selection = selection;
    const r = await fetch(`${_endpoint}/console/ask`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    // 202 means the turn was accepted and is streaming over SSE; the
    // answer arrives via onDelta/onDone, not this response body. Any
    // other status is an immediate failure (409 busy / unavailable).
    if (!r.ok && r.status !== 202) {
      failPending(await errorText(r));
    }
  } catch (e) {
    failPending(`request failed: ${e}`);
  } finally {
    scrollToEnd();
  }
}

// Stop / Esc-while-busy: ask the server to cancel. The turn ends when
// the worker emits a cancelled `console-done`; we keep the partial
// answer that already streamed.
function cancelTurn(): void {
  if (!_busy) return;
  if (_stop) _stop.disabled = true;
  fetch(`${_endpoint}/console/cancel`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ console_id: _consoleId }),
  }).catch(() => { /* server may be tearing down; cancel is best-effort */ });
}

// Esc with nothing in flight: drop the conversation and collapse. The
// server-side history is ephemeral; resetting it means the next turn
// re-seeds from scratch.
function dismiss(): void {
  _input?.blur();
  collapse();
  if (_transcript) _transcript.textContent = "";
  _pending = null;
  fetch(`${_endpoint}/console/reset`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  }).catch(() => { /* server may be tearing down; reset is best-effort */ });
}

// --- SSE handlers (filtered by console_id) -------------------------------

function mine(payload: { console_id?: string }): boolean {
  return !!payload && payload.console_id === _consoleId;
}

function onDelta(payload: SseConsoleDeltaEvent): void {
  if (!mine(payload) || !_pending) return;
  _pending.accumulated += payload.text || "";
  renderConsoleMarkdown(_pending.text, _pending.accumulated);
  scrollToEnd();
}

function onTool(payload: SseConsoleToolEvent): void {
  if (!mine(payload) || !_pending) return;
  const line = document.createElement("div");
  line.className = "console-tool";
  line.textContent = payload.label || "";
  _pending.activity.appendChild(line);
  scrollToEnd();
}

function onDone(payload: SseConsoleDoneEvent): void {
  if (!mine(payload)) return;
  const pending = _pending;
  if (pending) {
    // Backends that don't stream deltas (CLI, Slice 5) carry the whole
    // answer on `done`; fall back to it when nothing streamed.
    if (!pending.accumulated && payload.answer) {
      renderConsoleMarkdown(pending.text, payload.answer);
    }
    if (payload.cancelled) pending.answer.classList.add("console-cancelled");
    else if (!pending.accumulated && !payload.answer) {
      pending.text.textContent = "(empty answer)";
    }
  }
  endTurn();
}

function onError(payload: SseConsoleErrorEvent): void {
  if (!mine(payload)) return;
  failPending(payload.error || "console error");
  endTurn();
}

// --- transcript rendering ------------------------------------------------

function appendQuestion(text: string): void {
  if (!_transcript) return;
  const q = document.createElement("div");
  q.className = "console-q";
  q.textContent = text; // plain text — never interpreted as HTML
  _transcript.appendChild(q);
  scrollToEnd();
}

function appendAnswer(): PendingTurn {
  const answer = document.createElement("div");
  answer.className = "console-a console-pending";
  const activity = document.createElement("div");
  activity.className = "console-activity";
  const text = document.createElement("div");
  text.className = "console-text";
  text.textContent = "…";
  answer.appendChild(activity);
  answer.appendChild(text);
  _transcript?.appendChild(answer);
  scrollToEnd();
  return { answer, activity, text, accumulated: "" };
}

// Mark the in-flight answer as failed and show the error in place of
// whatever (if anything) had streamed.
function failPending(message: string): void {
  if (!_pending) return;
  _pending.answer.classList.add("console-error");
  _pending.text.textContent = message;
}

async function errorText(r: Response): Promise<string> {
  try {
    const body = (await r.json()) as { error?: string };
    if (body && body.error) return body.error;
  } catch (_) { /* fall through to status text */ }
  return `request failed (${r.status})`;
}

// Clear the busy state and detach the in-flight turn. The pending
// marker comes off so the answer renders in its final style.
function endTurn(): void {
  if (_pending) _pending.answer.classList.remove("console-pending");
  _pending = null;
  setBusy(false);
  scrollToEnd();
}

function setBusy(busy: boolean): void {
  _busy = busy;
  if (_input) _input.classList.toggle("busy", busy);
  if (_stop) {
    _stop.classList.toggle("hidden", !busy);
    _stop.disabled = false;
  }
}

function reveal(): void {
  _drawer?.classList.remove("hidden");
}

function collapse(): void {
  _drawer?.classList.add("hidden");
}

function scrollToEnd(): void {
  if (_drawer) _drawer.scrollTop = _drawer.scrollHeight;
}

export const Console = { init, onDelta, onTool, onDone, onError };
