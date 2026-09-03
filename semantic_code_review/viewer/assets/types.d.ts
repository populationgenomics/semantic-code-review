// Viewer data contract between the Python build_json + hunk_layout
// emitters and the JS that consumes the inline `scr-data` block.
//
// Declarations only — tsc emits no .js for `.d.ts`. The types are
// available to every `.ts` file in this directory (matched by the
// `**/*.ts` include glob in tsconfig.json) and become the source of
// truth as `viewer.js` is migrated to TypeScript file-by-file.
//
// Mirror of:
//   - semantic_code_review/viewer/build_json.py  (top-level shape)
//   - semantic_code_review/viewer/hunk_layout.py (hunk block)
//   - semantic_code_review/viewer/fold_regions.py (fold_regions)
//   - semantic_code_review/augment/schemas.py    (FoldDescription, Smell, etc.)
// Keep these in lockstep when fields shift.

// --- Top-level --------------------------------------------------------------

interface ViewerData {
  version: string;
  /** Pre-augment marker: true while the page is open before the
   *  augmentation pass produced any annotations. Cleared once the
   *  `done` SSE event arrives (see installSessionEvents in viewer.js). */
  pending?: boolean;
  pr: PRBlock;
  smells_catalogue: Record<string, SmellCatalogueEntry>;
  files: FileBlock[];
  groups: GroupBlock[];
  /** Deterministic tree-sitter symbol delta, one block per changed
   *  symbol, mapped to overlapping hunk ids (ADR 0001 Symbols axis).
   *  Present (possibly empty) whenever a worktree was available. */
  symbols: GroupBlock[];
  /** Server runtime debug flag (--debug / SCR_DEBUG). When true the
   *  viewer mounts the raw-log drawer and subscribes to `debug-log`. */
  debug?: boolean;
  /** Whether the change explainer is available for this review (ADR
   *  0007). False for `--no-augment` and when `[augment].explainer` is
   *  off; the overview-mode button is then not mounted at all. Rides
   *  /data.json because the button is decided before augment finishes. */
  explainer?: boolean;
}

interface SmellCatalogueEntry {
  label: string;
  severity: SmellSeverity;
  color: string;
}

type SmellSeverity = "critical" | "major" | "minor" | "info";

// --- PR header --------------------------------------------------------------

interface PRBlock {
  title: string;
  number: number | null;
  repo: string;
  base_sha: string;
  head_sha: string;
  author: string;
  url: string;
  summary: string;
  themes: string[];
  callgraph_edges: OverviewEdge[];
}

interface OverviewEdge {
  /** dump_by_alias=True so the wire format uses the original `from`
   *  / `to` keys rather than the Python-side `src` / `dst` attrs. */
  from: string;
  to: string;
}

// --- Files ------------------------------------------------------------------

type FileRole =
  | "modified"
  | "added"
  | "deleted"
  | "renamed"
  | "generated"
  | "binary";

interface FileBlock {
  /** Stable id of the form "F<file_idx>". */
  id: string;
  path: string;
  old_path: string | null;
  status: FileRole;
  language: string;
  adds: number;
  dels: number;
  summary: string;
  symbols: FileSymbols;
  /** Every fold region of the file, both sides, computed by the server
   *  from the AST (indentation where no grammar exists); the viewer
   *  hangs chevrons on the rows it has rendered and derives nothing.
   *  Enclosing regions precede the regions they enclose. */
  fold_regions: FoldRegion[];
  /** Lines in the post-image, or null when there is none (a deleted
   *  file, or no head worktree). Bounds the collapsible region below
   *  the last hunk; the text itself comes from /file-text on demand. */
  head_line_count: number | null;
  hunks: HunkBlock[];
}

interface FileSymbols {
  added: string[];
  modified: string[];
  removed: string[];
}

// --- Hunks ------------------------------------------------------------------

interface HunkBlock {
  /** Stable id of the form "H<file_idx>_<hunk_idx>". */
  id: string;
  header: string;
  old_start: number;
  old_count: number;
  new_start: number;
  new_count: number;
  adds: number;
  dels: number;
  intent: string;
  smells: Smell[];
  confidence: number | null;
  context: string;
  refs: Ref[];
  line_notes: LineNote[];
  segments: SegmentBlock[];
  rows: RowBlock[];
  /** Viewer-runtime only (not on the wire): set by DataStore when the
   *  augment pass reported a hunk-level failure, so the renderer can
   *  show "couldn't produce annotations" instead of the pending
   *  spinner. */
  _failed?: boolean;
}

interface SegmentBlock {
  id: string;
  new_start: number;
  new_count: number;
  intent: string;
  smells: Smell[];
  context: string;
  refs: Ref[];
}

interface Smell {
  tag: string;
  note: string;
}

interface Ref {
  path: string;
  line: number;
  reason: string;
}

interface LineNote {
  line: number;
  body: string;
}

// --- Rows -------------------------------------------------------------------

type RowKind = "ctx" | "ins" | "del" | "pair";

interface RowBlock {
  kind: RowKind;
  /** Pre-image line number. Null on `ins`-only rows. */
  old_line: number | null;
  /** Post-image line number. Null on `del`-only rows. */
  new_line: number | null;
  old_text: string;
  new_text: string;
}

// --- Fold regions -----------------------------------------------------------

type FoldContext = "right" | "left" | "both";

/** A foldable stretch of a file, addressed by line range per side — the
 *  address `/fold-summary` takes. A row is in the region when its line on
 *  a covered side falls in that side's range. */
interface FoldRegion {
  context: FoldContext;
  /** 1-indexed line numbers in head/<path>. Null when context is "left". */
  right_start: number | null;
  right_end: number | null;
  /** 1-indexed line numbers in base/<path>. Null when context is "right". */
  left_start: number | null;
  left_end: number | null;
  /** A hunk changes a line inside the region. */
  has_changes: boolean;
  /** Identity of the definition this region is (e.g. "Foo.bar" /
   *  "function"); null on an indentation stanza. The viewer labels the
   *  collapsed placeholder with these when present. */
  qualified_name: string | null;
  kind: string | null;
  summary: string;
  /** Viewer-runtime only (not on the wire): set by folds.ts while a
   *  local POST /fold-summary is in flight, honoured by DataStore so
   *  an echoing SSE event doesn't stomp the in-flight fetch handler's
   *  DOM update. */
  _inflight?: boolean;
}

// --- Sidebar groups ---------------------------------------------------------

interface GroupBlock {
  /** Stable id. Themes axis uses "G<i>"; files axis uses "BF<file_idx>";
   *  symbols axis uses "SY<i>". */
  id: string;
  title: string;
  rationale: string;
  /** Hunk ids — matching ids in DATA.files[*].hunks[*].id. For a nested
   *  symbols-axis node this is the subtree union (own + every
   *  descendant's), so the count is the distinct hunks under it. */
  hunk_ids: string[];
  /** Nested children (symbols axis only — class ▸ method). Absent for
   *  flat axes and for leaf symbol nodes. */
  children?: GroupBlock[];
}

// --- SSE event payloads -----------------------------------------------------
// The /events stream broadcasts these in addition to the standard
// open/close lifecycle. Both viewer.js and (future) viewer.ts consume
// them as JSON via JSON.parse(messageEvent.data).

interface SseHunkStartEvent {
  file_idx: number;
  hunk_idx: number;
}

interface SseHunkEvent {
  file_idx: number;
  hunk_idx: number;
  ok: boolean;
  /** Present when ok=true; replacement HunkBlock to splice into DATA. */
  block?: HunkBlock;
  /** Present when ok=false. */
  error?: string;
}

interface SseOverviewEvent {
  pr: Partial<PRBlock>;
  files?: Array<{
    file_idx: number;
    summary?: string;
    language?: string;
    symbols?: FileSymbols;
    status?: FileRole;
  }>;
  groups?: GroupBlock[];
}

interface SseFoldSummaryEvent {
  file_idx: number;
  context: FoldContext;
  right_start: number;
  right_end: number;
  left_start: number;
  left_end: number;
  summary: string;
}

interface SseDoneEvent {
  reason: string;
}

// --- Change explainer (ADR 0007) --------------------------------------------
// The document served by GET /explainer, produced by POST
// /explainer/skeleton, and fanned out as the `explainer` SSE frame so a
// second tab picks up a document the first one paid for.

/** A pointer from the document into the diff: a whole file or a whole
 *  hunk, never a line range. Validated server-side by membership in the
 *  viewer's id space, so a reference that arrives here resolves. */
interface ExplainerRef {
  kind: "file" | "hunk";
  id: string;
}

/** One step of the reading order. `ref` is always a file. */
interface ExplainerMapRow {
  ref: ExplainerRef;
  why: string;
}

/** A diagram in a structured slot. `svg` has already been through the
 *  server-side sanitiser; the renderer runs the same rules again because
 *  a document can reach a browser from a run dir this server never
 *  wrote. `stripped` is what sanitisation removed — rendered, not
 *  swallowed. `alt` is required and becomes the SVG's `aria-label`. */
interface ExplainerFigure {
  svg: string;
  alt: string;
  caption: string;
  stripped: number;
}

type ExplainerSectionKind = "background" | "intuition" | "code" | "map";
type ExplainerSectionState = "pending" | "ready" | "failed";

/** One entry of a section's term list. */
interface ExplainerTerm {
  term: string;
  definition: string;
}

/** Background's escape hatch past its first layer. `target_section_id`
 *  is validated server-side, so a box that arrives here resolves. */
interface ExplainerSkipBox {
  body: string;
  target_section_id: string;
}

interface ExplainerSection {
  id: string;
  kind: ExplainerSectionKind;
  title: string;
  /** Which server-side call writes this section. Sections do not map
   *  one-to-one onto calls — Intuition and Code share one — so this is
   *  what keeps one press from buying one call twice, and what groups
   *  the citation line under the prose it accounts for. A subsection
   *  carries its parent's. The Map's is `"skeleton"`. */
  pass_id: string;
  state: ExplainerSectionState;
  /** Markdown prose. Empty until the section's own pass runs. */
  body: string;
  refs: ExplainerRef[];
  map_rows: ExplainerMapRow[];
  terms: ExplainerTerm[];
  skip_box: ExplainerSkipBox | null;
  figures: ExplainerFigure[];
  /** Repo paths this section's pass actually opened, recorded from the
   *  tool surface rather than claimed by the model. It is the *pass's*
   *  read list, so every section one call wrote carries the same one
   *  and the viewer renders it once, under the last of them. */
  sources: string[];
  subsections: ExplainerSection[];
}

interface ExplainerDocument {
  version: number;
  base_sha: string;
  head_sha: string;
  /** `not_warranted` is a real answer — the document is then the
   *  verdict note plus, at most, the Map. */
  verdict: "narrate" | "not_warranted";
  verdict_note: string;
  figure_family: string;
  cast: string[];
  toy_data: boolean;
  /** Model requests the document's prose calls have spent between them,
   *  against one shared budget. Persisted, so a reload does not
   *  re-grant it. */
  turns_used: number;
  sections: ExplainerSection[];
  /** References the model emitted that addressed nothing, dropped
   *  server-side. Rendered, because references thinning out unnoticed
   *  is the failure the count exists to prevent. */
  dropped_refs: number;
}

interface SseExplainerEvent extends ExplainerDocument {}

// --- Console stream events --------------------------------------------------
// Emitted by the background console worker (Slice 2). Every frame is
// tagged with `console_id` so a tab ignores streams from other tabs;
// they are unbuffered (no `id:` line) so a reload starts fresh.

interface SseConsoleDeltaEvent {
  console_id: string;
  /** A chunk of assistant text to append to the in-flight answer. */
  text: string;
}

interface SseConsoleToolEvent {
  console_id: string;
  /** Human-readable tool-activity label, e.g. "grep RepoTools". */
  label: string;
}

interface SseConsoleDoneEvent {
  console_id: string;
  /** Present on a clean finish — the full answer text. */
  answer?: string;
  /** True when the turn was cancelled mid-flight (Stop / Esc). */
  cancelled?: boolean;
}

interface SseConsoleErrorEvent {
  console_id: string;
  error: string;
}

// One raw CLI-backend subprocess spawn (--debug). Emitted per `claude -p`
// invocation across console turns and augment passes; consumed by the debug
// drawer. Buffered (unlike the console frames) so a freshly-loaded drawer
// replays the session's spawns.
interface SseDebugLogEvent {
  provider: string;
  model: string;
  /** True for a console (free-form) turn; false for an augment pass. */
  free_form: boolean;
  returncode: number;
  duration_ms: number;
  /** Spawn argv, with the `--system-prompt` value truncated. */
  argv: string[];
  stdin_preview: string;
  stderr_tail: string;
  envelope: {
    subtype?: string | null;
    is_error?: boolean | null;
    stop_reason?: string | null;
    num_turns?: number | null;
    session_id?: string | null;
    usage?: unknown;
    result_preview?: string | null;
  };
}

// --- /fold-summary HTTP request --------------------------------------------

interface FoldSummaryRequest {
  file_idx: number;
  context: FoldContext;
  right_start?: number;
  right_end?: number;
  left_start?: number;
  left_end?: number;
}

interface FoldSummaryResponse extends SseFoldSummaryEvent {}

// --- /comments wire format -------------------------------------------------

/** Reviewer comment anchored to a specific (file, side, line). Round-
 *  trips between the viewer and the review server's /comments route.
 *
 *  Named ReviewerComment rather than Comment because lib.dom's
 *  `Comment` interface (a Node subtype) is in the global namespace
 *  and the unqualified name would shadow / be shadowed by it. */
interface ReviewerComment {
  id: string;
  file: string;
  side: "old" | "new";
  line: number;
  body: string;
  created_at: number;
  updated_at: number;
  /** Where the comment came from. "local" → authored in this session
   *  (editable). "github" → ingested from the PR (read-only). */
  source?: "local" | "github";
  /** Display name of the author. Null for local comments (the
   *  reviewer is implicit). */
  author?: string | null;
  author_avatar_url?: string | null;
  /** Parent comment id when this is a reply within a thread. */
  in_reply_to_id?: string | null;
  /** Upstream commit SHA the comment was anchored to. May predate
   *  the run's head_sha when the PR has advanced since. */
  commit_id?: string | null;
  /** Permalink to the comment on the upstream provider. */
  html_url?: string | null;
  /** Provider-rendered HTML of the body. When present the viewer
   *  injects this verbatim instead of treating `body` as markdown. */
  body_html?: string | null;
  /** True when the upstream review thread containing this comment is
   *  marked resolved. Denormalised onto every member of the thread —
   *  the viewer reads it from the root entry. */
  thread_resolved?: boolean;
  /** Head-side line number after diff-based propagation. Null when
   *  no propagation could be computed (commit_unavailable / file_gone).
   *  The viewer prefers this over `line` when present. */
  head_line?: number | null;
  /** Result of propagating the original anchor through to head:
   *  - `anchored`: same line at head, nothing changed.
   *  - `shifted`: same line content at head, different number.
   *  - `orphaned`: line removed at head; head_line is the next surviving
   *    line below.
   *  - `file_gone`: path no longer exists at head_sha.
   *  - `commit_unavailable`: commit_id couldn't be fetched (e.g. an old
   *    force-pushed-over commit). */
  anchor_status?:
    | "anchored" | "shifted" | "orphaned"
    | "file_gone" | "commit_unavailable" | null;
  /** Stable id of the LLM annotation this comment was promoted from.
   *  When set, the viewer hides the source annotation so the comment
   *  visibly replaces it. Examples: "H0_3:line_note:42",
   *  "H0_3:smell:perf". */
  derived_from?: string | null;
}
