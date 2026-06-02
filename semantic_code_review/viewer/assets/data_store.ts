// Typed mutators for the viewer's in-memory [[viewer-data]] tree.
//
// Before this module existed, boot.ts owned a few hundred lines of
// SSE-handler logic that walked DATA in place, mutated nested slots,
// and silently augmented HunkBlock / FoldRegion with private
// `_failed` / `_inflight` flags via local `... extends HunkBlock`
// interfaces. The renderer and folds.ts independently re-declared
// the same augmentations — three places, no shared contract.
//
// This module owns those mutations. boot.ts's SSE handlers shrink to
// "translate payload into a DataStore call, then ask the right module
// to repaint." The viewer-runtime fields (`_failed`, `_inflight`) are
// now part of HunkBlock / FoldRegion in types.d.ts; DataStore is
// their canonical writer.
//
// Functional API: every mutator takes the `ViewerData` reference as
// its first argument. boot.ts still holds the reference (it owns the
// `/data.json` fetch result); DataStore is stateless. That keeps the
// existing pattern where Comments.init / Sidebar.init / Render.init
// each receive the same reference and see mutations through it.

export interface FoldRegionAddress {
  file_idx: number;
  context: FoldContext;
  right_start: number;
  right_end: number;
  left_start: number;
  left_end: number;
}

export interface ResolvedFoldRegion {
  file: FileBlock;
  /** Index into `file.hunks` of the hunk whose `fold_regions` list
   *  carries the addressed region. Regions are addressed at the file
   *  level but persisted on individual hunks (today, the first hunk
   *  of the file — see CONTEXT.md `Fold region`). */
  hostHunkIdx: number;
  region: FoldRegion;
}

export type FoldSummaryResult = "applied" | "noop" | "inflight" | "not-found";

export interface OverviewApplyResult {
  /** True iff `payload.groups` was non-empty; tells boot.ts whether
   *  to refresh the themes axis in addition to a full re-render. */
  groupsChanged: boolean;
}

export const DataStore = {
  // --- Overview / per-file metadata ----------------------------------

  applyOverview(data: ViewerData, payload: SseOverviewEvent): OverviewApplyResult {
    if (payload.pr) {
      Object.assign(data.pr || (data.pr = {} as PRBlock), payload.pr);
    }
    if (Array.isArray(payload.files)) {
      for (const fp of payload.files) {
        const f = data.files && data.files[fp.file_idx];
        if (!f) continue;
        if (fp.summary !== undefined) f.summary = fp.summary;
        if (fp.language) f.language = fp.language;
        if (fp.symbols) f.symbols = fp.symbols;
        if (fp.status) f.status = fp.status;
      }
    }
    const groupsChanged = Array.isArray(payload.groups);
    if (groupsChanged) {
      data.groups = payload.groups!;
    }
    return { groupsChanged };
  },

  // --- Per-hunk replacement ------------------------------------------

  /** Splice an augmented HunkBlock into the tree. Returns the file
   *  whose hunk was replaced (so the caller can hand it to
   *  Render.renderHunkReplace), or null if the address is invalid. */
  replaceHunk(
    data: ViewerData, file_idx: number, hunk_idx: number, block: HunkBlock,
  ): FileBlock | null {
    const file = data.files && data.files[file_idx];
    if (!file || !file.hunks || !file.hunks[hunk_idx]) return null;
    file.hunks[hunk_idx] = block;
    return file;
  },

  /** Mark a hunk slot as failed: the renderer reads `_failed` to show
   *  "couldn't produce annotations" instead of the pending spinner. */
  markHunkFailed(
    data: ViewerData, file_idx: number, hunk_idx: number,
  ): FileBlock | null {
    const file = data.files && data.files[file_idx];
    if (!file || !file.hunks || !file.hunks[hunk_idx]) return null;
    const hunk = file.hunks[hunk_idx];
    hunk.intent = "";
    hunk._failed = true;
    return file;
  },

  // --- Fold regions --------------------------------------------------

  /** Look up the fold region at `addr`, walking every hunk in the
   *  addressed file. Returns null if no matching region exists. */
  findFoldRegion(data: ViewerData, addr: FoldRegionAddress): ResolvedFoldRegion | null {
    const f = data.files && data.files[addr.file_idx];
    if (!f) return null;
    for (let hi = 0; hi < (f.hunks || []).length; hi++) {
      const h = f.hunks[hi];
      for (const r of h.fold_regions || []) {
        if (_foldKeyMatches(r, addr)) {
          return { file: f, hostHunkIdx: hi, region: r };
        }
      }
    }
    return null;
  },

  /** Apply a server-side fold summary to the matching region.
   *
   *  Outcomes:
   *    "applied"   — summary written; caller should re-render.
   *    "noop"      — same summary already present; nothing to do.
   *    "inflight"  — region's own local POST is racing this; caller
   *                  must NOT re-render (the in-flight handler owns
   *                  the DOM update, and a re-render would pop the
   *                  user's just-closed fold back open).
   *    "not-found" — the address doesn't match any region. */
  applyFoldSummary(
    data: ViewerData, addr: FoldRegionAddress, summary: string,
  ): FoldSummaryResult {
    const resolved = this.findFoldRegion(data, addr);
    if (!resolved) return "not-found";
    if (resolved.region.summary === summary) return "noop";
    resolved.region.summary = summary;
    if (resolved.region._inflight) return "inflight";
    return "applied";
  },

  // --- Stream lifecycle ----------------------------------------------

  finalisePending(data: ViewerData): void {
    // Hunks the server never sent an event for (filtered, skipped,
    // crashed mid-pass) now render the failure copy instead of the
    // spinner on the next paint.
    data.pending = false;
  },
};


function _foldKeyMatches(r: FoldRegion, addr: FoldRegionAddress): boolean {
  return (r.context || "right") === addr.context
    && (r.right_start || 0) === addr.right_start
    && (r.right_end || 0) === addr.right_end
    && (r.left_start || 0) === addr.left_start
    && (r.left_end || 0) === addr.left_end;
}
