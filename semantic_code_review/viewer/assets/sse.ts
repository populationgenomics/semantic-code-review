// Server-Sent Events wiring.
//
// Opens the /events stream on the review server, parses each
// per-type JSON payload, and dispatches to typed handlers the
// caller registers. The wire format is one EventSource frame per
// pipeline phase (overview-start, overview, overview-failed,
// hunk-start, hunk, fold-summary, done); payload shapes live in
// `types.d.ts`. Replay on reconnect is handled by the browser's
// EventSource implementation (it sends Last-Event-ID automatically;
// the server replays from its buffer).
//
// Compiled by tsc to `sse.js`. The compiled output is concatenated
// into the viewer HTML by `render_html.py` and must expose
// `window.ScrSse` for the classic-script `viewer.js` to call into.

// `module: "none"` — no `export`s, top-level decls only. The viewer
// data contract (SseOverviewEvent / SseHunkEvent / SseFoldSummaryEvent
// / SseDoneEvent / SseHunkStartEvent) lives in `types.d.ts` and is in
// scope without an import.

interface SseHandlers {
  overviewStart?: () => void;
  overviewFailed?: () => void;
  overview?: (payload: SseOverviewEvent) => void;
  hunkStart?: (payload: SseHunkStartEvent) => void;
  hunk?: (payload: SseHunkEvent) => void;
  foldSummary?: (payload: SseFoldSummaryEvent) => void;
  done?: (payload: SseDoneEvent) => void;
}

/** Subscribe to `<endpoint>/events`. Returns the EventSource so
 *  callers can close it explicitly if needed (rare — the connection
 *  stays open for the review session). Returns `null` if the browser
 *  doesn't support EventSource or the connection couldn't be opened.
 */
function connect(endpoint: string, handlers: SseHandlers): EventSource | null {
  if (!endpoint || typeof EventSource === "undefined") return null;
  let es: EventSource;
  try {
    es = new EventSource(endpoint + "/events");
  } catch (_) {
    return null;
  }

  // Frames without a JSON body (the server sends no `data:` for
  // overview-start / overview-failed) just fire the handler.
  const wireBare = (type: string, fn: () => void): void => {
    es.addEventListener(type, fn);
  };
  // Frames with a JSON body: parse + dispatch with the typed
  // payload. JSON.parse errors are swallowed so a corrupt frame
  // doesn't take down the whole channel.
  const wireJson = <T>(type: string, fn: (payload: T) => void): void => {
    es.addEventListener(type, (e: MessageEvent) => {
      let parsed: T;
      try {
        parsed = JSON.parse(e.data) as T;
      } catch (_) {
        return;
      }
      fn(parsed);
    });
  };

  if (handlers.overviewStart) wireBare("overview-start", handlers.overviewStart);
  if (handlers.overviewFailed) wireBare("overview-failed", handlers.overviewFailed);
  if (handlers.overview) wireJson<SseOverviewEvent>("overview", handlers.overview);
  if (handlers.hunkStart) wireJson<SseHunkStartEvent>("hunk-start", handlers.hunkStart);
  if (handlers.hunk) wireJson<SseHunkEvent>("hunk", handlers.hunk);
  if (handlers.foldSummary) wireJson<SseFoldSummaryEvent>("fold-summary", handlers.foldSummary);
  if (handlers.done) wireJson<SseDoneEvent>("done", handlers.done);
  return es;
}

const Sse = { connect };

if (typeof window !== "undefined") {
  (window as unknown as { ScrSse: typeof Sse }).ScrSse = Sse;
}
