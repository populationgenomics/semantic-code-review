# scr — semantic code review

LLM-augmented local-diff reviewer. Opens an interactive side-by-side
diff in the browser with semantic-group navigation, fold-level "what
did this block change" annotations, and inline comments. When invoked
as a Claude Code plugin, it round-trips those comments back into the
session so the agent can walk through them with you.

## Usage as a Claude Code plugin

Prerequisites on the user's machine:

- Python 3.11 or newer on `PATH`
- Node.js 20+ and `npm` on `PATH` (used to compile the viewer's
  TypeScript module on first run; gitignored output is cached under
  `$CLAUDE_PLUGIN_DATA`)
- `git` on `PATH`
- Either `ANTHROPIC_API_KEY` in the environment (or a `.env` in the
  repo you run `/scr:review` from), **or** a logged-in `claude` CLI
  on `PATH` for OAuth-based fallback (no API key required)

Optional:

- `gh` — only needed if you ever run `scr fetch` / `scr run` against
  a GitHub PR URL
- `ripgrep` (`rg`) — speeds up the LLM's code-search tool; `git grep`
  is used as a fallback

Install:

```
/plugin marketplace add populationgenomics/semantic-code-review
/plugin install scr
```

The first time you run `/scr:review`, the plugin's `bin/scr` wrapper:

1. Creates a hash-pinned Python virtualenv under
   `$CLAUDE_PLUGIN_DATA/venv` (every dependency installed via
   `pip install --require-hashes` so any tampered tarball fails the
   install instead of silently landing).
2. Runs `npm ci --ignore-scripts` for the viewer's TypeScript build
   toolchain (typescript, vitest, @playwright/test pinned via
   `package-lock.json`).
3. Compiles `annotations.ts` → `annotations.js` into the data dir
   via `tsc --outDir`.

All three steps are stamped by sha256 of their inputs and only re-run
when something changes. Subsequent invocations exec the cached venv
and start immediately.

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
- Otherwise, `claude` on `PATH` → falls back to a `claude -p`
  subprocess with a stdio MCP server exposing the same repo-tools
  the SDK path uses. No API key needed; works with any logged-in
  Claude Code installation.
- Neither available → fails fast with a clear error.

Force a backend with `--backend <name>`. Run `scr config show` for
the registered names — builtins plus anything you've added under
`[backends.<name>]` in your config.

Both Gemini backends are opt-in only — never picked by `auto`.

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
`[backends.<name>]` block in your config; see `scr config edit`.

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
scr review HEAD~1..HEAD --spec SPEC.md
```

From a source checkout (for development, or to pin the exact hashed
dependency set), build the viewer bundle yourself:

```
pip install --require-hashes -r requirements.lock
pip install --no-deps --no-build-isolation .
npm ci --ignore-scripts && npm run build
scr review HEAD~1..HEAD --spec SPEC.md
```

Subcommands:

- `scr review <ref-or-range> [--spec SPEC.md]` — local git diff,
  runs LLM augment, opens viewer, prints reviewer comments as
  markdown when you click Done.
- `scr pr <owner/repo> [<number>]` — same flow against a GitHub PR.
  Omit the number to enumerate open PRs requesting your review;
  on Done, posts the inline comments back to GitHub as a single
  COMMENT-event review. Confirms before posting unless `--yes`.
- `scr fetch <pr-url>` — fetch a GitHub PR into a run directory.
- `scr augment <run-dir>` — run the LLM augmentation pass on a run
  directory.
- `scr render <run-dir>` — render the HTML viewer from an augmented
  run.
- `scr run <pr-url>` — fetch + augment + render (no viewer server).
- `scr show <run-dir>` — print the augmented diff to stdout.
- `scr strip <augmented.diff>` — write a plain unified diff to stdout.
- `scr lint <augmented.diff>` — validate the augmented-diff format.
- `scr runs path` — print the runs root resolved for the current cwd.

`scr pr` reuses everything `scr review` does (same fetch, augment,
viewer, server, comment store) plus a thin GitHub-side helper that
groups the inline comments into one review object via `gh api`. The
`gh` CLI must be on `PATH` and authenticated.

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
(`review`, `pr`, `fetch`, `run`).

## Development

```
python3 -m venv .venv
.venv/bin/pip install --require-hashes -r build-requirements.lock
.venv/bin/pip install --require-hashes -r requirements-dev.lock
.venv/bin/pip install --no-deps --no-build-isolation -e .

npm ci --ignore-scripts
npm run build           # tsc → semantic_code_review/viewer/assets/annotations.js
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

One-time setup (recorded here for a fresh fork): register a
[Trusted Publisher](https://docs.pypi.org/trusted-publishers/) on the
PyPI project pointing at this repository, workflow filename
`release.yml`, and environment `pypi`; and create a `pypi` environment
in the repo's GitHub settings.

## What's where

- `semantic_code_review/augment/` — overview + per-hunk LLM pipeline,
  prompts, schemas, MCP tool wrapper.
- `semantic_code_review/viewer/` — Python side: `build_json.py`,
  `hunk_layout.py`. Frontend assets in `viewer/assets/`: eight
  TypeScript modules (boot/render/sidebar/annotations/comments/
  folds/progress/sse) bundled by esbuild into a single
  `viewer.js`, `viewer.css`, the static `index.html` served by
  the review server, and vendored `highlight.js` under
  `assets/vendor/`.
- `semantic_code_review/review/` — local HTTP server that
  back-channels reviewer comments to the calling process, the
  shared `serve_review` helper used by both `scr review` and
  `scr pr`, and `github.py` for the GitHub-PR round-trip via
  `gh api`.
- `commands/review.md` — the Claude Code slash-command prompt.
- `bin/scr` — bootstrap wrapper; preflights deps, maintains the
  Python venv + Node build cache, execs the real `scr`.
- `TREE_SITTER.md` — design notes on a possible future structural
  pass (symbol grouping, AST-driven fold regions, semantic hunk
  splitting). Speculative; nothing in tree depends on it.

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
