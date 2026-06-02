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
//   - semantic_code_review/viewer/hunk_layout.py (hunk + fold_regions block)
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
  symbols_added: OverviewSymbol[];
  symbols_modified: OverviewSymbol[];
  symbols_removed: OverviewSymbol[];
  callgraph_edges: OverviewEdge[];
}

interface OverviewSymbol {
  kind: string;
  name: string;
  path: string;
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
  /** Full post-image content split into lines, or null when not
   *  shipped (large file, deleted/binary/generated, etc.). */
  head_lines: string[] | null;
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
  fold_regions: FoldRegion[];
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

interface FoldRegion {
  header_idx: number;
  body_start_idx: number;
  body_end_idx: number;
  context: FoldContext;
  /** 1-indexed line numbers in head/<path>. Null when context is "left". */
  right_start: number | null;
  right_end: number | null;
  /** 1-indexed line numbers in base/<path>. Null when context is "right". */
  left_start: number | null;
  left_end: number | null;
  has_changes: boolean;
  summary: string;
  /** Viewer-runtime only (not on the wire): set by folds.ts while a
   *  local POST /fold-summary is in flight, honoured by DataStore so
   *  an echoing SSE event doesn't stomp the in-flight fetch handler's
   *  DOM update. */
  _inflight?: boolean;
}

// --- Sidebar groups ---------------------------------------------------------

interface GroupBlock {
  /** Stable id. Themes axis uses "G<i>"; files axis uses "BF<file_idx>". */
  id: string;
  title: string;
  rationale: string;
  /** Hunk ids — matching ids in DATA.files[*].hunks[*].id. */
  hunk_ids: string[];
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
 *  trips between the viewer and the review server's /comments route;
 *  also persisted to localStorage when no session endpoint is set.
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
}
