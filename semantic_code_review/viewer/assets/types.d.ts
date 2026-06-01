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
