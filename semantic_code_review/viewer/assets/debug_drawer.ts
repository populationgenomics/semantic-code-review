// Debug raw-log drawer (--debug / SCR_DEBUG).
//
// A gated, always-available panel that lists every CLI-backend subprocess
// spawn the server fans out as a `debug-log` SSE frame — one per `claude -p`
// invocation across console turns and augment passes. Each entry shows the
// spawn's outcome inline (provider/model, envelope subtype, turns, timing)
// and expands to the raw argv, stdin, stderr, and envelope so a reviewer can
// see exactly what happened when a turn misbehaves.
//
// Mounted from boot.ts only when DATA.debug is true. Modelled on console.ts:
// a fixed panel toggled via a `.hidden` class, content built with
// `.textContent` (never HTML) so nothing here can inject markup.

let _drawer: HTMLElement | null = null;
let _log: HTMLElement | null = null;
let _toggle: HTMLButtonElement | null = null;
let _count = 0;

function init(): void {
  if (_drawer) return; // idempotent

  _toggle = document.createElement("button");
  _toggle.type = "button";
  _toggle.className = "debug-toggle";
  _toggle.title = "Show the raw backend spawn log (--debug)";
  renderToggle();
  _toggle.addEventListener("click", () => toggle());
  document.body.appendChild(_toggle);

  _drawer = document.createElement("div");
  _drawer.className = "debug-drawer hidden";
  const header = document.createElement("div");
  header.className = "debug-drawer-header";
  header.textContent = "Backend spawn log";
  const close = document.createElement("button");
  close.type = "button";
  close.className = "debug-drawer-close";
  close.textContent = "×";
  close.title = "Close (the log keeps recording)";
  close.addEventListener("click", () => collapse());
  header.appendChild(close);
  _log = document.createElement("div");
  _log.className = "debug-log";
  _drawer.appendChild(header);
  _drawer.appendChild(_log);
  document.body.appendChild(_drawer);
}

function renderToggle(): void {
  if (_toggle) _toggle.textContent = `Debug log (${_count})`;
}

function toggle(): void {
  if (!_drawer) return;
  if (_drawer.classList.contains("hidden")) reveal();
  else collapse();
}

function reveal(): void {
  _drawer?.classList.remove("hidden");
  scrollToEnd();
}

function collapse(): void {
  _drawer?.classList.add("hidden");
}

function scrollToEnd(): void {
  if (_log) _log.scrollTop = _log.scrollHeight;
}

// Append one spawn record. Called for every `debug-log` frame, including the
// buffered replay a freshly-loaded drawer receives.
function onLog(payload: SseDebugLogEvent): void {
  if (!_log) return;
  _count += 1;
  renderToggle();

  const env = payload.envelope || {};
  const entry = document.createElement("details");
  entry.className = "debug-entry";
  if (payload.returncode !== 0 || env.is_error) entry.classList.add("debug-entry-error");

  const summary = document.createElement("summary");
  summary.className = "debug-entry-summary";
  const kind = payload.free_form ? "console" : "augment";
  const bits = [
    `${_count}.`,
    kind,
    `${payload.provider}/${payload.model}`,
    env.subtype ? `· ${env.subtype}` : "",
    env.num_turns != null ? `· ${env.num_turns} turns` : "",
    `· ${payload.duration_ms}ms`,
    payload.returncode !== 0 ? `· rc=${payload.returncode}` : "",
  ].filter(Boolean);
  summary.textContent = bits.join(" ");
  entry.appendChild(summary);

  addField(entry, "argv", (payload.argv || []).join(" "));
  addField(entry, "stdin", payload.stdin_preview);
  addField(entry, "stderr", payload.stderr_tail);
  const envLines = [
    env.session_id != null ? `session_id: ${env.session_id}` : "",
    env.stop_reason != null ? `stop_reason: ${env.stop_reason}` : "",
    env.usage != null ? `usage: ${JSON.stringify(env.usage)}` : "",
  ].filter(Boolean);
  addField(entry, "envelope", envLines.join("\n"));
  addField(entry, "result", env.result_preview ?? "");

  _log.appendChild(entry);
  if (!_drawer?.classList.contains("hidden")) scrollToEnd();
}

// A labelled <pre> block inside an entry; skipped when the value is empty.
function addField(entry: HTMLElement, label: string, value: string): void {
  if (!value) return;
  const wrap = document.createElement("div");
  wrap.className = "debug-field";
  const lab = document.createElement("div");
  lab.className = "debug-field-label";
  lab.textContent = label;
  const pre = document.createElement("pre");
  pre.className = "debug-field-value";
  pre.textContent = value;
  wrap.appendChild(lab);
  wrap.appendChild(pre);
  entry.appendChild(wrap);
}

export const DebugDrawer = { init, onLog };
