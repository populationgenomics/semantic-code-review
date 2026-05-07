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

**Status**: all six steps done.

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

## Step 4 — Staged diff types (#4) — DONE

**Outcome**: composition-based stage tagging.

- `ParsedHunk`, `ParsedFile`, `ParsedDiff` carry only structural
  fields.
- `HunkAnnotations` (the wire-format type) and a new `FileAnnotations`
  carry the LLM-produced payloads.
- `AnnotatedHunk = {parsed: ParsedHunk, ann: HunkAnnotations}`;
  `AnnotatedFile` carries flat structural fields plus
  `ann: FileAnnotations` plus `hunks: list[AnnotatedHunk]`.
- `AnnotatedDiff.overview: Overview | SkippedOverview` (typed sentinel,
  no `None`).
- `parse_augmented_diff(text) -> AnnotatedDiff` (universal parser);
  `parse_raw_diff(text) -> ParsedDiff` (used by the pipeline; rejects
  any non-PR-info `#scr:` directives).
- `emit_augmented_diff` requires an `AnnotatedDiff`.
- `apply_overview_to_diff` and `apply_hunk_annotations` are pure
  functions returning new objects via `model_copy(update=...)`. The
  pipeline gathers per-hunk results into a `dict[(file_idx, hunk_idx),
  HunkAnnotations]` and folds them into a new `AnnotatedDiff` in one
  pass.

**Why composition over inheritance**: an `AnnotatedHunk
extends ParsedHunk` relationship would have been a DRY trick, not a
real "is-a" — the pipeline isn't polymorphic over hunk-likes; it
consumes a `ParsedHunk` and produces an `AnnotatedHunk`. Composition
makes the layering honest at the cost of `h.parsed.header` /
`h.ann.intent` accessors. The structural file fields stayed flat
(`f.path`, `f.diff_git_line`) because file-level callers are many; the
hunk-level extra hop is the bulk of the touching diff.

**Test surface**: existing `tests/test_format_roundtrip.py`,
`tests/test_segments.py`, `tests/test_overview_groups.py`,
`tests/test_augment_pipeline.py`, and `tests/test_parse.py` updated to
the new accessor pattern. New
`test_handwritten_annotated_diff_round_trips` builds an
`AnnotatedDiff` in code and asserts `parse(emit(x)).model_dump() ==
x.model_dump()`.

---

## Step 5 — Collapse the viewer hunk transform (#5) — DONE

**Outcome**: `viewer/hunk_layout.py` owns hunk → viewer-block.
`build_rows` and `compute_fold_regions` stay public (the augment-side
hunk prompt at `augment/hunks.py:43-44` walks fold regions to label
changed ones for the LLM); `_Row` and `_FoldRegion` are module-private
value types — no caller imports them.

`build_hunk_viewer_block(h, file_idx, hunk_idx) -> dict` does row
construction + fold detection + per-hunk add/del counting + segment +
fold-region-to-line-range mapping + output-block assembly. Per-hunk
`adds`/`dels` ride on the returned block; `_file_block` sums them.

`build_json.py` shrank from 227 → ~165 lines (38 lines of hunk-shaping
moved into `hunk_layout.py`). The plan's "<~80 lines" target wasn't
met — `build_json.py` still owns `_pr_block`, `_group_blocks`,
`_load_head_lines`, language detection, and the URL parsers, all of
which are file/PR-level concerns that don't belong in `hunk_layout.py`.
The structural goal (one module owns hunk → block) is met.

**Test surface**: `tests/test_rows.py` renamed to
`tests/test_hunk_layout.py` and re-pointed at the new module — the
existing `build_rows`/`compute_fold_regions` tests still cover the
private value types via the public functions' return shapes.
`tests/test_viewer_json.py` unchanged; the wire JSON is identical
except per-hunk blocks now carry `adds`/`dels` counts (additive,
unused by the viewer JS).

---

## Step 6 — `git_ops` module (#6) — DONE

**Outcome**: every git/gh subprocess invocation in the library now
goes through `semantic_code_review/git_ops.py`. The module exposes
two layers:

- Generic escape hatches `git()`, `gh()`, `git_capture()`,
  `gh_capture()` for one-off invocations — capture variants return
  `(rc, stdout, stderr)` for callers that need to translate specific
  stderr (e.g. `fetch_pr_meta` keying on "Unknown JSON field") or
  treat `rc=1` as success (e.g. `git grep` no-matches).
- Named helpers (`rev_parse`, `merge_base`, `diff`,
  `status_porcelain`, `common_dir`, `show`, `log_oneline`, `grep`,
  `init_dir`, `remote_add`, `fetch_depth1`, `worktree_add`,
  `gh_path`, `preflight_gh`, `gh_pr_view`, `gh_pr_diff`,
  `gh_pr_list`, `gh_api_post`) for the call patterns that already
  appear at two or more sites.

Errors are typed: `GitError`, `GhError`, and `GhMissingError` (a
`GhError` subclass for missing-binary / too-old-binary, so the CLI
can print an install hint without separate catches). Domain modules
keep their own typed wrappers (`LocalDiffError`, `GhFetchError`)
that re-raise from `GitError` / `GhError`; callers don't need to
know about `git_ops` exceptions.

**Migrated call sites**: `fetch/gh.py`, `fetch/worktree.py`,
`review/git.py`, `review/github.py`, `review/runner.py`,
`paths.py`, `augment/tools.py`. The remaining `subprocess.run`
usages in the library are non-git/gh: `augment/tools.py` runs `rg`
(ripgrep) for the fast-path search, `backends/base.py` spawns the
CLI-backend subprocess, and `cli.py` invokes `$EDITOR` for `scr
config edit`.

**Skipped** — `LocalDiff` / `FetchResult` harmonisation. The two
types live at different layers: `LocalDiff` is the pre-write output
of `build_local_diff` (carries the raw diff text in memory),
`FetchResult` is the post-write output of `fetch()` (carries paths
to artefacts already on disk). The shared "post-populated run dir"
shape that step 5 of this plan gestured at is already the
`run_dir: Path` that `serve_review` consumes — promoting it to a
typed value object would add a layer with one consumer. Left as
parked work; revisit if a third diff source (e.g. a Gerrit
fetcher) ever needs the same shape.

**Test surface**: existing `tests/test_local_diff.py`,
`tests/test_fetch_url.py`, and `tests/test_github_pr_review.py`
continue to cover the public API. Two adjustments:
- `test_fetch_url.py` now patches `git_ops.subprocess.run` /
  `git_ops.shutil.which` instead of the corresponding attrs in
  `fetch.gh` (the patches followed the moved code).
- `test_github_pr_review.py` dropped the `require_gh` shim that
  used to pre-resolve `/usr/bin/gh`; argv now starts with the bare
  `"gh"` since `subprocess.run` does its own PATH resolution. The
  cli-level `preflight_gh()` still gates the friendly missing-tool
  message before any other gh invocation.

**Retro**: `git_ops.py` adds ~260 lines; the seven migrated files
shrink by ~230 lines combined. Net library code roughly flat —
matches step 2's lesson that "collapse N-way duplication" usually
trades a thin layer for centralised typing/testability rather than
raw line count. The structural goal — one place to mock, one place
to add a new git subcommand — is met.

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
