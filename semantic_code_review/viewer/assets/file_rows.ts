// Typed handle from the renderer to the fold-detector for the per-file
// row stream.
//
// Why this module exists. The fold detector (folds.ts) needs to walk
// every row in a file — across hunks, across expanded gap-context — in
// DOM order, with access to both the `RowBlock` records and the
// per-side DOM elements those rows materialised into. The renderer
// (render.ts) is the only place that has all three pieces at the
// moment of construction.
//
// The previous shape stashed three optional properties (`_scrRows`,
// `_scrRowElsOld`, `_scrRowElsNew`) directly on the `.diff` /
// `.gap-expansion` HTMLElements via three independently-declared
// HTMLElement augmentation interfaces. That worked, but the contract
// was triplicated, stringly-typed, and silent: a rename on the
// producing side broke the consuming side at runtime, not at compile
// time.
//
// This module owns the contract instead. `record` is called by the
// renderer for every `.diff` / `.gap-expansion` it builds; `get` is
// called by the fold detector for each candidate container as it walks
// `.file-body`. Storage is a `WeakMap` keyed by container reference,
// so entries vanish when a container is replaced (collapsed back to a
// chip, re-rendered after an SSE hunk replace, etc.).

export interface RowWithEls extends RowBlock {
  oldEl: HTMLElement;
  newEl: HTMLElement;
}

export interface FileRowsEntry {
  rows: RowBlock[];
  oldEls: HTMLElement[];
  newEls: HTMLElement[];
}

const _storage = new WeakMap<HTMLElement, FileRowsEntry>();

export const FileRows = {
  /** Record the row stream for a `.diff` or `.gap-expansion` container. */
  record(container: HTMLElement, entry: FileRowsEntry): void {
    _storage.set(container, entry);
  },

  /** Look up a container's recorded rows, or undefined if none was set. */
  get(container: HTMLElement): FileRowsEntry | undefined {
    return _storage.get(container);
  },
};
