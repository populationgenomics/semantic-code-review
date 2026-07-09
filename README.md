# scr — semantic code review

A tool for reviewing code you're accountable for but didn't write by
hand — typically an agent's implementation of a plan you agreed. It uses
an LLM to direct your attention rather than to gate: it clusters the diff
into semantic groups and annotates what each block changed, so you can
judge *whether* the change matches the intent and *how* it was built.
Nothing is posted or applied without you.

The same review runs at two radii. As a Claude Code plugin
(`/scr:review`), your inline comments return into the session when you
click Done and the authoring agent iterates on them. As `scr pr`, the
same flow runs against a GitHub PR and posts your comments as a single
review the author — or their agent — picks up later. Both open one
browser viewer: a side-by-side diff with semantic-group navigation,
fold-level "what did this block change" annotations, and inline comments
you leave by clicking a line number.

For a screenshot tour of the viewer — the sidebar axes, the fold ladder,
segments, comments, and the review console — see
[docs/walkthrough.md](docs/walkthrough.md).

## Usage as a Claude Code plugin

Prerequisites on the user's machine:

- Python 3.11 or newer on `PATH`
- `git` on `PATH`
- Either `ANTHROPIC_API_KEY` in the environment (or a `.env` in the
  repo you run `/scr:review` from), **or** a logged-in `claude` CLI
  on `PATH` for OAuth-based fallback (no API key required)

Optional:

- `gh` — only needed for GitHub-PR review (`scr pr`, or the
  lower-level `scr fetch`)
- `ripgrep` (`rg`) — speeds up the LLM's code-search tool; `git grep`
  is used as a fallback

Install:

```
/plugin marketplace add populationgenomics/semantic-code-review
/plugin install scr
```

The first time you run `/scr:review`, the plugin's `bin/scr` wrapper
creates a Python virtualenv under `$CLAUDE_PLUGIN_DATA/venv`, installs
the dependency closure with `pip install --require-hashes` (so any
tampered tarball fails the install instead of silently landing), then
installs `semantic-code-review` from PyPI at the version pinned in the
plugin's `pyproject.toml`. That published wheel carries the prebuilt
`viewer.js` bundle as package data, so the wrapper needs no Node, npm, or
build step — the git checkout is a thin launcher over a PyPI release. The
install is stamped (lockfile hash + version) and only re-runs when one
changes; subsequent invocations exec the cached venv and start
immediately.

### The `/scr:review` slash command

```
/scr:review HEAD~1               # diff working tree vs one commit back
/scr:review main..HEAD           # committed-only diff of the current branch
/scr:review HEAD --spec SPEC.md  # with a spec markdown as LLM ground truth
```

The command opens a browser viewer. The left sidebar lists semantic
groups (LLM-curated clusters of related hunks); click one to filter
the visible hunks to that group, click "Show all" to clear. Hunks
that no group claimed get a subtle dotted-border tell so you can
spot them at a glance.

Leave inline comments by clicking a line number on either side of
the diff. When you click **Done**, the viewer closes, the command
returns the comments as structured markdown, and Claude Code walks
through them with you one at a time.

See `commands/review.md` for the full slash-command prompt.

### LLM backend selection

`scr` picks a backend automatically:

- `ANTHROPIC_API_KEY` set → uses the Anthropic SDK directly (best
  performance: prompt caching, native concurrency, structured tool
  use).
- Otherwise, `claude` on `PATH` → falls back to `claude -p`
  subprocesses that connect to one warm HTTP MCP server `scr` hosts,
  exposing the same repo-tools the SDK path uses. No API key needed;
  works with any logged-in Claude Code installation.
- Neither available → fails fast with a clear error.

Force a backend with `--backend <name>`. Run `scr config show` for
the registered names — builtins plus anything you've added under
`[backends.<name>]` in your config.

The Gemini backend is opt-in only — never picked by `auto`.

- `--backend=gemini-api` uses Google's official `google-genai` SDK
  directly. Auth ladder: `GOOGLE_CLOUD_PROJECT` set → Vertex AI
  via Application Default Credentials; else `GEMINI_API_KEY` /
  `GOOGLE_API_KEY` → AI Studio. Native concurrency, full structured
  tool use, Gemini's implicit prompt caching surfaces as
  `cache_read_input_tokens` in our usage stats. Best Gemini path
  for production work.

### Trying it without paid API access

`scr` ships builtin profiles for several free-tier providers. None
require an Anthropic or Google paid plan. Pick one, set the listed
env var, pass `--backend <name>`:

| `--backend` | Auth | Notes |
|---|---|---|
| `gemini-api` | `GEMINI_API_KEY` from [aistudio.google.com](https://aistudio.google.com) | Free tier with rate limits; hits the Google SDK directly. |
| `groq` | `GROQ_API_KEY` from [console.groq.com](https://console.groq.com) | Free tier, very fast inference, Llama 3.3 70B by default. |
| `github` | `GITHUB_TOKEN` (any GitHub account) | Free quota across multiple model families; default is `openai/gpt-4o-mini`. |
| `cerebras` | `CEREBRAS_API_KEY` from [cloud.cerebras.ai](https://cloud.cerebras.ai) | Free tier; pass `--model` (catalogue rotates). |
| `openrouter` | `OPENROUTER_API_KEY` from [openrouter.ai](https://openrouter.ai) | Mixed paid + free models; pass `--model` (e.g. `meta-llama/llama-3.3-70b-instruct:free`). |
| `mistral` | `MISTRAL_API_KEY` from [console.mistral.ai](https://console.mistral.ai) | La Plateforme free tier; default is `codestral-latest`. |
| `ollama` | none — runs against `localhost:11434` | Local llama.cpp/Ollama; pass `--model` to name something you've pulled (e.g. `qwen2.5-coder:14b`). |

The non-Anthropic / non-Google entries all reach the provider via
the OpenAI Chat Completions wire format (`type = "openai-compat"`).
Override any builtin's model — or add a new provider — with a
`[backends.<name>]` block in your config; see `scr config edit` and
[docs/config.md](docs/config.md) for the full config structure.

If your bearer lives in a secret store rather than a long-lived env
var, use `api_key_command`: it runs a shell-free argv command and
reads the bearer from stdout. Built-in example: `--backend=github`
falls back to `gh auth token` when `GITHUB_TOKEN` isn't set. Custom
example, Anthropic key in a GCP Secret Manager entry:

```toml
[backends.claude-api]
api_key_command = ["gcloud", "secrets", "versions", "access",
                   "latest", "--secret=anthropic-api-key"]
```

The same hook works on `claude-api`, `gemini-api`, and any
`openai-compat` backend. For `gemini-api`, `GOOGLE_CLOUD_PROJECT`
takes precedence: when set, the Vertex/ADC path wins and
`api_key_command` is skipped.

Quality caveat: any non-frontier backend produces shallower hunk
intents and more spurious `refs[]`. The output is still useful as a
draft you skim, especially for small PRs, but it isn't a Claude /
Gemini Pro replacement.

## Usage as a standalone CLI

Install from PyPI. The published wheel already contains the compiled
viewer bundle, so no Node is needed at runtime:

```
uv tool install semantic-code-review
# or: pipx install semantic-code-review   /   pip install semantic-code-review
scr init                                  # interactive first-run setup
scr review HEAD~1..HEAD --spec SPEC.md
```

`scr init` is the quickest way to get configured: it detects which
backends are usable in your environment, lets you pick a default backend
and model (live-listed from the backend once a credential resolves), and
guides credential setup — offering, per backend, to set an env var, fetch
the key from a command (`api_key_command`), write it to a gitignored
`.env`, or (user scope only) store it in your user config's `[env]`. All
config and secret files are written `0600`.

From a source checkout (for development, or to pin the exact hashed
dependency set), build the viewer bundle yourself:

```
pip install --require-hashes -r requirements.lock
pip install --no-deps --no-build-isolation .
npm ci --ignore-scripts && npm run build
scr review HEAD~1..HEAD --spec SPEC.md
```

The three commands you'll actually use:

- `scr init` — interactive setup: detect usable backends, pick a
  default + model, guide credential setup, write the config. Run
  once.
- `scr review <ref-or-range> [--spec SPEC.md]` — review a local git
  diff. Runs the LLM augment pass, opens the viewer, and prints your
  inline comments as markdown when you click Done.
- `scr pr <owner/repo> [<number>]` — the same flow against a GitHub
  PR. Omit the number to pick from the open PRs requesting your
  review; on Done it posts your inline comments back as a single
  COMMENT-event review (confirms first unless `--yes`). Needs the `gh`
  CLI on `PATH` and authenticated.

`scr pr` is `scr review` plus a GitHub round-trip: same fetch, augment,
viewer, and comment store, with the comments grouped into one review
object via `gh api` at the end.

On a PR that already carries review comments, `scr pr` ingests them into
the viewer alongside your own. Each ingested comment is re-anchored from
the commit it was written against to the PR's current head by diff
propagation, so it stays pinned to the code it addressed rather than being
collapsed as "outdated" the way GitHub's own UI does; the viewer tags each
as `anchored`, `shifted`, `orphaned`, or gone, and carries thread
resolution state across. Across successive revisions this keeps a review's
prior judgments attached to the code as it converges, instead of losing
them on every push.

<details>
<summary>Plumbing — the review/pr pipeline split into stages, for
scripting or debugging</summary>

- `scr fetch <pr-url>` — materialise a GitHub PR into a run directory.
- `scr augment <run-dir>` — run the LLM augmentation pass on a run
  directory.
- `scr show <run-dir>` — print the augmented diff to stdout.
- `scr strip <augmented.diff>` — strip annotations back to a plain
  unified diff on stdout.
- `scr lint <augmented.diff>` — validate the augmented-diff format.
- `scr runs path` — print the runs root resolved for the current cwd.
- `scr config show | edit | path` — inspect or edit the config files.
</details>

### Where run artefacts live

`scr` writes per-review state — a `meta.json`, the raw and augmented
diffs, and `base/` / `head/` worktrees so the LLM (and the viewer)
can read pre- and post-change files — to a per-repo directory under
your XDG cache:

```
~/.cache/scr/runs/<sha256-of-git-common-dir>/<run-slug>/
```

Worktrees of the same repo share the directory; different repos get
different ones. Runs live outside the repo on purpose — a `.scr/` at
the repo root is a deploy-tool footgun (gcloud, docker, tar tend to
upload it unless every project remembers to ignore it), and the
worktrees inside contain real git history that no one wants to
upload by accident.

Override with `--runs-root <path>` on any command that creates runs
(`review`, `pr`, `fetch`).

## Development

```
python3 -m venv .venv
.venv/bin/pip install --require-hashes -r build-requirements.lock
.venv/bin/pip install --require-hashes -r requirements-dev.lock
.venv/bin/pip install --no-deps --no-build-isolation -e .

npm ci --ignore-scripts
npm run build           # tsc --noEmit typecheck, then esbuild bundle → semantic_code_review/viewer/assets/viewer.js
npm run test:js         # vitest
.venv/bin/python -m pytest
```

`tests/conftest.py` autobuilds the TypeScript module if it's missing
when pytest starts, so `python -m pytest` Just Works on a fresh
checkout (provided Node is available).

## Releasing

Releases publish to [PyPI](https://pypi.org/project/semantic-code-review/)
from GitHub Actions via Trusted Publishing (OIDC) — no API tokens, no
cloud credentials. To cut a release:

1. Bump `version` in `pyproject.toml` (and `.claude-plugin/plugin.json`
   to match).
2. Commit, then publish a GitHub Release whose tag is `vX.Y.Z` matching
   that version. `.github/workflows/release.yml` fires on the published
   release: it compiles the viewer bundle, builds the wheel + sdist with
   `uv build`, and uploads to PyPI. A guard fails the run loudly if the
   tag and `pyproject.toml` version disagree.

The plugin installs `semantic-code-review==<pyproject version>` from
PyPI, so the version on the default branch must always be one that is
published (or being published) there — bump it only as part of cutting a
release. A plugin update that lands a version PyPI doesn't yet carry will
fail its first-run install until the release finishes.

One-time setup (recorded here for a fresh fork): register a
[Trusted Publisher](https://docs.pypi.org/trusted-publishers/) on the
PyPI project pointing at this repository, workflow filename
`release.yml`, and environment `pypi`; and create a `pypi` environment
in the repo's GitHub settings.

## What's where

- `semantic_code_review/augment/` — overview + per-hunk LLM pipeline,
  prompts, schemas, MCP tool wrapper.
- `semantic_code_review/viewer/` — Python side: `build_json.py`,
  `hunk_layout.py`. Frontend assets in `viewer/assets/`: the
  TypeScript modules (`boot` entry, plus `render`, `sidebar`,
  `folds`, `annotations`, `comments`, `console`, `sse`, …) bundled
  by esbuild into a single `viewer.js`, alongside `viewer.css`, the
  static `index.html` served by the review server, and vendored
  `highlight.js` under `assets/vendor/`.
- `semantic_code_review/structural/` — deterministic tree-sitter layer:
  `parse.py` (grammars + tag queries → a `Symbol` tree), `symbols.py`,
  and `diff.py` (base→head `SymbolDelta`). Feeds the Symbols sidebar
  axis, AST fold regions, and the overview seed.
- `semantic_code_review/review/` — local HTTP server that
  back-channels reviewer comments to the calling process, the
  shared `serve_review` helper used by both `scr review` and
  `scr pr`, and `github.py` for the GitHub-PR round-trip via
  `gh api`.
- `commands/review.md` — the Claude Code slash-command prompt.
- `bin/scr` — bootstrap wrapper; preflights deps, maintains the
  Python venv, installs `scr` from PyPI at the pinned version, execs
  the real `scr`.
- `TREE_SITTER.md` — design history of the structural (tree-sitter)
  layer. Symbol grouping and AST fold regions are shipped; semantic
  hunk splitting is still future.

## Supply-chain hygiene

Every external dependency is pinned by exact version and SHA-256
hash. The `bin/scr` bootstrap installs with
`pip install --require-hashes` and `npm ci`, so any on-disk artifact
whose hash doesn't match the lockfile fails the install rather than
silently landing.

Lockfiles (all committed):

- `build-requirements.lock` — PEP 517 build backend (`setuptools`,
  `wheel`).
- `requirements.lock` — runtime Python deps (`anthropic`,
  `pydantic`, `typer`, …).
- `requirements-dev.lock` — runtime + `pytest` + `pytest-asyncio`.
- `package-lock.json` — Node toolchain (`typescript`, `vitest`,
  `@playwright/test`, `jsdom`, `@types/node`).

Frontend assets (`highlight.js` + light/dark stylesheets) are
vendored under `semantic_code_review/viewer/assets/vendor/` at a
pinned upstream version with the BSD-3-Clause LICENSE file alongside
them. The viewer HTML inlines those bytes — never loads anything
from a CDN at runtime. See `vendor/VENDOR.md` for provenance and
hashes; `vendor/refresh.sh` re-downloads at the pinned version and
fails loudly if any SHA-256 doesn't match.

Refreshing:

```
# Python
uv pip compile pyproject.toml -o requirements.lock --generate-hashes
uv pip compile pyproject.toml --extra dev -o requirements-dev.lock --generate-hashes
printf 'setuptools>=68\nwheel\n' | uv pip compile - --generate-hashes -o build-requirements.lock

# Node toolchain
npm install   # commits an updated package-lock.json with new integrity hashes

# Frontend (edit version constants in refresh.sh first if bumping)
./semantic_code_review/viewer/assets/vendor/refresh.sh
```
