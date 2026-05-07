# Refactor plan — deepening opportunities

Six candidates, ordered to respect dependencies. Each step is sized to land
as one PR; tests must pass before the next step starts.

## Vocabulary

- **Module** — anything with an interface and an implementation.
- **Interface** — everything a caller must know to use the module.
- **Seam** — where an interface lives; a place behaviour can be altered
  without editing in place.
- **Adapter** — a concrete thing satisfying an interface at a seam.
- **Depth** — leverage at the interface (a lot of behaviour, a small
  surface).
- **Locality** — change/bug/knowledge concentrated in one place.

## Ordering rationale

```
#3 (tool surface)     ✓──┐
                         ├──► #1 (backend registry) ──► #6 (git ops)
#2 (subprocess model) ✓──┘

#4 (staged diff types) ──► #5 (viewer transform)
```

- **#2 before #1**: the registry stores adapters that produce a `Model`.
  If two of those Models are near-clones (former state), the registry
  bakes in the duplication. Collapse first.
- **#3 before #1**: each adapter has to hand tools to its backend. If
  `RepoTools` is the single source of truth, the adapter doesn't have to
  know about both pydantic-ai and MCP shapes.
- **#4 before #5**: the viewer transform's input contract is "an
  `AugmentedDiff` in some unknown stage". Sharpen the type first, then
  collapse the transform.
- **#6 is independent** but easiest to do once the augment pipeline's
  input type is settled.

**Status**: #3, #2, and #1 are done. #4 is the next step on the
augment-pipeline track. #5 follows #4; #6 is independent and can
land alongside.

---

## Step 1 — Unify the tool surface (#3) — DONE (ebb4a72)

**Files**: `semantic_code_review/augment/tools.py` (152),
`semantic_code_review/augment/repo_tool_fns.py` (147),
`semantic_code_review/augment/mcp_server.py` (148).

**Goal**: `RepoTools` becomes the single source of truth. Pydantic-ai
tool functions and the MCP schema are derived from its public methods,
not maintained by hand.

**Steps**

1. Audit `RepoTools` methods and confirm each one's signature, docstring,
   and parameter types are sufficient for both pydantic-ai and MCP
   schema generation. Add `Annotated`/typed-dict parameter descriptions
   where descriptions currently live only in the wrapper functions.
2. Introduce a small decorator `@tool` (or use existing pydantic-ai
   metadata) on `RepoTools` methods to mark which are exported.
3. Replace `repo_tool_fns.TOOL_FUNCTIONS` with a generator that walks
   `RepoTools` and produces pydantic-ai tool functions from the
   decorated methods.
4. Replace `repo_tool_fns.mcp_dispatch` and `mcp_server.mcp_tool_schemas`
   with introspection-driven equivalents over the same set.
5. Delete the hand-written wrappers; collapse `repo_tool_fns.py` and the
   schema-half of `mcp_server.py` into `tools.py` (or a thin `tools_export.py`).
6. Add a test that asserts the pydantic-ai and MCP tool surfaces match
   (same names, same parameter shapes).

**Done when**: renaming a `RepoTools` method updates both the pydantic-ai
agent and the MCP server with no other edits, and the new test catches
drift.

**Test surface**: `RepoTools` methods are tested directly; existing
`tests/test_repo_tool_fns.py` and `tests/test_mcp_server.py` continue to
pass against the introspected surfaces.

---

## Step 2 — Collapse the duplicated subprocess Model skeleton (#2) — DONE (bfd40d3)

**Outcome**: `SubprocessModel(Model, ABC)` owns `request()`, the
validation-retry loop, and `_spawn()`; subclasses implement four hooks
plus an optional usage normaliser:

- `_build_invocation(...) -> _Invocation` — argv + env + stdin in one
  return type. (Subsumed the originally-planned separate `argv` /
  `build_prompt` hooks; prompt construction is a per-subclass private
  helper.)
- `_parse_envelope(stdout, stderr, returncode) -> dict` — raises the
  typed error on hard failures.
- `_envelope_to_structured(envelope, schema, submit_tool_name) -> dict`
  — raises `_ValidationFailure` for retry-eligible errors.
- `_envelope_to_usage(envelope) -> dict` (optional; default identity).
- `_validation_exhausted_error(...)` — typed exception for the
  retries-exhausted case.

Retry count is parameterised via `max_validation_retries` on the base
(`ClaudeCLIModel`=0 because `--json-schema` enforces shape server-side;
`GeminiCLIModel`=1 because gemini's CLI doesn't expose `responseSchema`).

**Retro on the line-count goal**: the plan predicted "drop 200+ lines";
the file actually grew from 851 → 1000 lines. Base-class scaffolding
(abstract method declarations, `_Invocation` dataclass, `_spawn`, retry
loop) costs more lines than the inline duplication it replaced. The
*structural* goal — one request loop, one spawn helper, two focused
adapters, third backend = only the differences — is met. Future
"collapse N-way duplication" steps in this plan should treat line-count
predictions as soft.

**Test surface**: existing `tests/test_cli_models.py` exercises both
adapters end-to-end through `Agent.run(...)`. Three new tests
(`test_subprocess_model_*`) drive `SubprocessModel.request` directly via
a stub subclass, locking in the retry/feedback/stdin contract
independent of either real adapter.

---

## Step 3 — Backend registry (#1) — DONE

**Outcome**: the dispatch lives in
`semantic_code_review/backends/`. One adapter class per
`BackendType`; the registry maps `BackendType → adapter class` and
constructs an instance bound to a `(name, BackendDef)` pair on
lookup. `cli._select_client` is now a four-line shim:

```python
if backend == "auto":
    backend = backends.resolve_auto(config=_CONFIG)
return backends.get(backend, config=_CONFIG).resolve(model=model)
```

Auto resolution is a deterministic walk: each adapter declares
`auto_priority` (None = excluded; lower = preferred), and the
registry sorts candidates that report `supports_auto() is True`.

**Side effect**: SDK adapters (Anthropic, Google) construct their
`Model` with explicit `provider=AnthropicProvider(api_key=...)` /
`GoogleProvider(api_key=..., vertexai=...)` instead of mutating
`os.environ`. The key only lives on the model.

**Renames**: the old `Backend` dataclass in `augment/agents.py` (the
"client handle" — `(model, is_subprocess_backend)`) is now `Client`,
freeing the `Backend` name for the new adapter ABC. All call sites
(`pipeline.py`, `overview.py`, `hunks.py`, `review/runner.py`, two
test files) updated.

**Test surface**:
- `tests/test_backend_select.py` is now registry-level (lookup,
  unknown-name reporting, auto walk with stub adapters).
- `tests/backends/test_<name>.py` covers each adapter's
  credential-resolution and Model-wiring contract.
- `tests/backends/test_base.py` covers the shared `resolve_api_key`
  helper.

**Retro**: `cli.py` shrank from 965 → 674 lines (-291). The new
`backends/` package adds ~360 lines, but each file owns one
backend's behaviour and is independently testable — the structural
goal is met. Eliminating `os.environ` mutation as a side channel
(per the plan) fell out of switching to explicit pydantic-ai
provider constructors.

---

## Step 4 — Staged diff types (#4)

**Files**: `semantic_code_review/augment/schemas.py` (286),
`semantic_code_review/augment/pipeline.py` (289),
`semantic_code_review/format/parse.py` (417),
`semantic_code_review/format/emit.py` (158).

**Goal**: stage-tagged types — `ParsedDiff` → `AnnotatedDiff`. The
pipeline becomes a function, not a series of in-place mutations. `emit`
takes only the annotated form.

**Steps**

1. Inventory every read/write of `AugmentedDiff.overview` and
   per-hunk annotation fields. Record at which call site each becomes
   non-None.
2. Split the current dataclass:
   - `ParsedDiff` — hunks present, annotations always absent.
   - `AnnotatedDiff` — hunks plus required annotation fields. Overview
     is optional but typed (`Overview | SkippedOverview`, not
     `Overview | None`).
3. Update `format/parse.py` to return `ParsedDiff`.
4. Update `format/emit.py` to require `AnnotatedDiff` (or generalise
   over both with an explicit `emit_parsed` / `emit_annotated` if both
   are valid emit inputs).
5. Rewrite `pipeline.apply_hunk_annotations` and
   `apply_overview_to_diff` as pure functions returning a new
   `AnnotatedDiff` (use `dataclasses.replace` per hunk). The pipeline
   entry becomes `ParsedDiff -> AnnotatedDiff`.
6. Update `tests/test_augment_pipeline.py` to construct typed inputs
   instead of fabricating intermediate states by hand.
7. Add a round-trip test: `emit(parse(x))` where `x` is a hand-written
   AnnotatedDiff fixture, asserting the output equals the canonical form.

**Done when**: no caller can pass a half-baked diff to `emit`; the
pipeline doesn't mutate; tests don't reach into internal fields.

---

## Step 5 — Collapse the viewer hunk transform (#5)

**Files**: `semantic_code_review/viewer/build_json.py` (221),
`semantic_code_review/viewer/rows.py` (227),
`semantic_code_review/viewer/render_html.py` (138).

**Goal**: one module owns hunk → viewer-block. `Row` and `FoldRegion`
become internal; `build_json.py` becomes a thin file/hunk loop.

**Steps**

1. Move `build_rows` and `compute_fold_regions` into a new
   `viewer/hunk_layout.py` (or rename `rows.py`); make `Row`,
   `FoldRegion` module-private.
2. Introduce `build_hunk_viewer_block(hunk, file_idx, hunk_idx, meta)
   -> dict` that does row construction + fold detection + addition/
   deletion counting + output-block assembly.
3. Move the addition/deletion scan currently in `build_json.py:97-98`
   and the fold-region-to-line-range mapping in `build_json.py:141-149`
   into `build_hunk_viewer_block`.
4. `build_viewer_json` becomes `for file in diff.files: for hunk in
   file.hunks: blocks.append(build_hunk_viewer_block(...))`.
5. `render_html.py` keeps using `build_viewer_json`; no caller of
   `Row`/`FoldRegion` should remain.
6. Update `tests/test_viewer_json.py` and `tests/test_rows.py`. The
   `Row` tests survive as private-module tests of `hunk_layout.py`;
   the public tests target the block shape only.

**Done when**: `Row`/`FoldRegion` are imported only inside
`viewer/hunk_layout.py`. `build_json.py` is under ~80 lines.

---

## Step 6 — `GitOps` module (#6)

**Files**: `semantic_code_review/fetch/gh.py` (153),
`semantic_code_review/fetch/worktree.py` (54),
`semantic_code_review/fetch/__init__.py` (70),
`semantic_code_review/review/git.py` (247),
`semantic_code_review/review/github.py` (241).

**Goal**: one git surface. `fetch/` and `review/` adapt to it instead
of running their own helpers.

**Steps**

1. Inventory every `subprocess.run(["git", ...])` call across both
   trees. Catalogue: repo root, diff, sha resolution, worktree create/
   destroy, log, show.
2. Create `semantic_code_review/git_ops.py` with one class or namespace
   exposing the catalogued operations as methods. Use a single `GitError`
   typed exception (subclasses for the cases callers actually care about).
3. Migrate `review/git.py` to use `GitOps`. Delete its private
   `_git`, `_slug`, `_synthesise_head_sha` helpers (or move them into
   `GitOps`).
4. Migrate `fetch/worktree.py` to use `GitOps`. Likewise delete
   private helpers.
5. Harmonise the result types: `FetchResult` and `LocalDiff` should
   share a base (e.g., `DiffSource`) with `worktree_path`, `base_sha`,
   `head_sha`, `raw_diff_path`, plus a stage-specific `provenance`
   field.
6. Confirm the runner, the `pr` command, and tests in
   `tests/test_local_diff.py`, `tests/test_fetch_url.py`,
   `tests/test_github_pr_review.py` still work.

**Done when**: `subprocess.run(["git", ...])` and
`subprocess.run(["gh", ...])` appear only inside `git_ops.py`.

---

## Notes

- Each step ends with `pytest` and a manual smoke (`scr review HEAD~1`
  on this repo).
- Commit boundaries follow the steps; do not bundle.
- Step 4 can land as a single PR. Steps 3, 5, 6 are larger; if any
  exceeds ~600 lines of diff, split along the per-backend or
  per-call-site axis listed in the step.
- Line-count targets in step bodies are soft — see step 2's retro for
  why "collapse duplication" can grow a file rather than shrink it.
