# CONTEXT — semantic-code-review

A glossary of domain terms used across the codebase. Each entry pins a
concept that recurs in source, tests, and docs so we can talk about it
without re-inventing vocabulary.

This file grows incrementally — add an entry when a refactor needs a
term, not all at once. Terms not yet listed but recurring in code
include: **pass** (overview / hunk / fold-summary), **annotation**,
**row**, **segment**, **smell**, **theme**. Pin these the next time a
refactor brushes against them.

## Terms

**Run directory**
The per-review on-disk state, one directory per (repo, slug). Default
location is `~/.cache/scr/runs/<sha256-of-git-common-dir>/<run-slug>/`;
overridable with `--runs-root`. Contents:

- `meta.json` — PR-shaped metadata (title, body, base/head SHAs, file
  list, mode).
- `raw.diff` — the unified diff before any LLM augmentation.
- `augmented.diff` + `augmented.scr.json` — the [[augmented-diff]]
  artefacts emitted by the augment pipeline (paired; same data, two
  shapes).
- `base/` and `head/` — git worktrees pinned to the diff's endpoints
  so `RepoTools` (the MCP-exposed read_file / grep) can resolve paths
  during the LLM passes.
- `comments.json` — reviewer comments persisted by the back-channel
  HTTP server; populated only when `scr review` is the entry point.

Each subsystem under `fetch/`, `review/`, `augment/`, and `viewer/`
takes a `run_dir: Path` and operates inside it. The implicit contract
is "everything I need to do my job lives under this one path". The
act of *producing* a run directory is named: see [[run-spec]].

**Augmented diff**
The output of the augment pipeline, kept on disk in two paired forms:

- `augmented.diff` — the unified diff with LLM annotations encoded as
  line-prefix metadata (`# intent: …`, `# refs: …`, `# fold: …`, etc).
  Grammar lives in `format/parse.py` ↔ `format/emit.py`. The text form
  is what the HTML viewer ultimately renders.
- `augmented.scr.json` — the same content as a Pydantic-shaped JSON
  sidecar (an `AnnotatedDiff` tree of `AnnotatedFile` → `AnnotatedHunk`
  → annotations). Round-tripped by `format/sidecar.py`. Used when code
  needs to manipulate annotations structurally (e.g. the fold-summary
  pass writing a new `FoldDescription` back into the tree).

The two are kept in sync — any code that mutates one rewrites the
other. The sidecar is the canonical structural shape; the unified-diff
form is the canonical wire shape.

**RunSpec**
The shared shape both [[run-directory]] sources hand to the
materialise step. A `RunSpec` (in `fetch/run_source.py`) carries
`slug`, `raw_diff`, `base_sha`, `head_sha`, `files`, `meta` (PR-shaped,
written verbatim to `meta.json`), and an optional `spec_md_text`.
`materialize_run_metadata(spec, runs_root) → Path` writes the shared
artefacts (`raw.diff`, `files.txt`, `meta.json`, optional `spec.md`).

Two sources today (`fetch/github.py`, `fetch/local.py`), each
producing a `RunSpec` plus per-source extras carried on a wrapper —
`GithubResolved` adds the `PRRef`; `LocalResolved` adds the cwd
`.git` location, the working-state flag, and the diagnostic mode
(`"range"`, `"ref-working"`, etc.). The wrapper is transient: once
materialise + per-source worktree setup are done, downstream
consumers see only `run_dir: Path`.

Worktree mechanics stay per-source on purpose — fresh bare clone +
remote fetch for GitHub, `worktree add` against the cwd repo (or a
symlink for working-state mode) for local. Unifying them would have
meant a multi-axis conditional inside `materialize_run_metadata` for
no callsite benefit.

**Hunk**
A contiguous range of changed lines in a diff, with its `@@` header
plus old/new start+count. Both on-disk forms — the
[[augmented-diff]] text and its sidecar — model files as ordered
lists of hunks. The augment pipeline runs the per-hunk LLM pass once
per hunk (`HunkAnnotations`); the [[viewer-data]] addresses each
hunk by a stable id of the form `"H<file_idx>_<hunk_idx>"`.

**Fold region**
A collapsible region within a [[hunk]] in the viewer. Addressed by
`(file_idx, context, right_range, left_range)`:

- `context = "right"` — unchanged-context fold (collapses lines that
  exist in the post-image only). Pure-context folds are the common
  case.
- `context = "left"` — deletion-only fold (lines present pre-image,
  removed in post).
- `context = "both"` — straddles changed content; the LLM sees a
  unified-diff view of the region.

Summaries are produced on demand by the fold-summary pass the first
time a region is collapsed, then persisted in the
`augmented.scr.json` sidecar as a `FoldDescription` on the file's
first hunk — a stable home pending a schema migration that lifts
fold descriptions up to `AnnotatedFile`.

**Viewer data**
The in-memory runtime data structure served as `/data.json` by the
review server and consumed by the TS viewer. Defined by the
`ViewerData` interface in `viewer/assets/types.d.ts`, with subtypes
`FileBlock`, `HunkBlock`, `RowBlock`, `FoldRegion`, etc. Built from
the [[augmented-diff]] sidecar by `viewer/build_json.py` +
`viewer/hunk_layout.py`, augmented with metadata from `meta.json`.

Distinct from the [[augmented-diff]] sidecar in two ways: (1) it
includes pre-rendered row layout (the diff's two-column structure
expanded into row objects) which the sidecar leaves implicit; (2)
it carries transient runtime flags (e.g. `pending` while the
augment pass is still streaming) that have no place on the
persisted sidecar.

The TS side has no single owner for the in-memory tree today —
`boot.ts` fetches it and mutates it in response to SSE events,
while every other module reads from the same global reference. A
deepening to give it a typed owner is in flight.

**Reviewer comment**
A reviewer-authored inline comment anchored to a specific
`(file, side, line)`. Round-trips between the viewer and the
review server's `/comments` route during a session; persisted to
`comments.json` in the [[run-directory]], or to browser
`localStorage` when no session endpoint is present (e.g. opening
a `scr render` output offline).

Named `ReviewerComment` in TypeScript and `Comment` in Python —
the TS name is qualified because `lib.dom.Comment` (a `Node`
subtype) is in the global namespace and an unqualified `Comment`
would shadow it.

**Backend**
A registered LLM provider that the CLI resolves a name to. Each backend
is a `Backend` subclass under `semantic_code_review/backends/`; the
registry (`backends/__init__.py`) maps `BackendType → Backend`. The
backend owns credential resolution and constructs the `Client` that
the augment pipeline drives.

**Client**
The handle the augment pipeline drives. Wraps either a pydantic-ai
model id string (for SDK backends) or a `pydantic_ai.models.Model`
instance (for CLI subprocess backends). Constructed by
`Backend.resolve(model=...)`. Defined in `augment/agents.py`.

**CLI driver**
A concrete `pydantic_ai.Model` subclass we author to wrap a specific
third-party LLM CLI. Two today: `ClaudeCLIModel` (wraps `claude -p`)
and `GeminiCLIModel` (wraps `gemini -p`). Each spawns the CLI on every
`request()`, parses its envelope, and returns a synthetic
`ModelResponse`; the multi-turn tool-call loop runs inside the
subprocess via MCP, not in pydantic-ai.

CLI drivers share `SubprocessModel` (in `backends/_cli_driver.py`) as
a base — not itself a driver, just the scaffolding they extend. Each
driver lives in its per-backend file alongside the `Backend` adapter
that constructs it.

Distinct from the `Model` subclasses pydantic-ai ships
(`AnthropicModel`, `GoogleModel`, …), which we instantiate but do not
author. pydantic-ai itself has no word for this distinction —
"`Model`" covers both — but our tree splits along it: drivers are
ours, other `Model`s come from pydantic-ai.
