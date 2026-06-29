// Cross-language lockstep for the symbol-aware fold detector.
//
// The same (rows, spans) cases in tests/fixtures/fold_regions_cases.json
// drive this vitest case and the pytest case in tests/test_hunk_layout.py
// (test_fold_regions_lockstep_fixture). Both detectors must produce the
// regions baked into the fixture, so the server's wire `fold_regions` and
// the viewer's client-side detection stay reconcilable.

import fs from "node:fs";
import path from "node:path";
import { describe, test, expect } from "vitest";
import { _computeFoldRegions } from "../../semantic_code_review/viewer/assets/folds";

interface FoldCase {
  name: string;
  rows: RowBlock[];
  head_spans: FoldSymbolSpan[];
  base_spans: FoldSymbolSpan[];
  expected: Array<Record<string, unknown>>;
}

const REGION_KEYS = [
  "header_idx", "body_start_idx", "body_end_idx", "context",
  "right_start", "right_end", "left_start", "left_end",
  "qualified_name", "kind",
] as const;

const CASES: FoldCase[] = JSON.parse(
  fs.readFileSync(
    path.resolve(process.cwd(), "tests/fixtures/fold_regions_cases.json"),
    "utf-8",
  ),
);

describe("fold detector lockstep fixture", () => {
  for (const c of CASES) {
    test(c.name, () => {
      // _computeFoldRegions only reads row line numbers / kind / text, so
      // the DOM-less fixture rows stand in for RowWithEls.
      const detected = _computeFoldRegions(
        c.rows as never[], c.head_spans, c.base_spans,
      );
      const got = detected.map((r) =>
        Object.fromEntries(REGION_KEYS.map((k) => [k, (r as Record<string, unknown>)[k]])),
      );
      expect(got).toEqual(c.expected);
    });
  }
});
