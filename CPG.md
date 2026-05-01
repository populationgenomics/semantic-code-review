# scr at CPG

This is the populationgenomics fork of [folded/semantic-code-review].
The code is identical to upstream — see [README.md] for what `scr`
is, what the slash command does, the architecture overview, and the
supply-chain story.

This document covers only **what's different at CPG**: how the CLI
gets distributed internally via Artifact Registry, how to install it,
how releases are cut, and how to keep this fork in sync with upstream.

[folded/semantic-code-review]: https://github.com/folded/semantic-code-review
[README.md]: ./README.md

## Install

One command sets up everything — Application Default Credentials, the
keyring backend that lets `uv`/`pip` exchange ADC for an Artifact
Registry token, and a thin `scr` wrapper at `~/.local/bin/scr`.

While this repo is private (pre-approval), authenticate the install
script download with `gh`:

```sh
gh api -H "Accept: application/vnd.github.raw" \
    repos/populationgenomics/semantic-code-review/contents/install.sh | bash
```

After the repo flips to public, the bare form works:

```sh
curl -fsSL https://raw.githubusercontent.com/populationgenomics/semantic-code-review/main/install.sh | bash
```

Then:

```sh
scr --help
scr review HEAD~1
scr pr populationgenomics/<repo>
```

The wrapper auto-resolves the latest published version from Artifact
Registry on every invocation, so you don't run the installer again
to upgrade — just keep using `scr`.

### Prerequisites

- **uv** — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- **gcloud SDK** — for the one-time `gcloud auth application-default
  login`
- **Python 3.11+** — for whatever uv builds the tool venv from
- A `@populationgenomics.org.au` Google account (this is what grants
  read access to the Artifact Registry repo)

The installer checks all three and explains what's missing if it
can't find them.

### Optional, used by `scr` itself once installed

- **Node 20+ / npm** — only needed if you build from source. The
  published wheel already contains the compiled `annotations.js`,
  so `uvx`/`uv tool run` users don't need Node.
- **`gh` CLI** — needed for `scr fetch`, `scr run`, and `scr pr`.
- **`ripgrep` (`rg`)** — speeds up the LLM's code-search tool;
  `git grep` is used as a fallback.
- **`ANTHROPIC_API_KEY`** *or* a logged-in `claude` CLI — see the
  "LLM backend selection" section in the upstream README.

## How auth works

```
┌─────────────┐  ADC token   ┌─────────────────────────┐
│  your dev   │ ──────────►  │  GCP Artifact Registry  │
│   machine   │              │  (aasgard-dev,          │
│             │  wheel       │   australia-southeast1, │
│             │ ◄─────────── │   scr-python)           │
└─────────────┘              └─────────────────────────┘
                                  ▲
                                  │ wheel uploads
                                  │
                             ┌─────────────────────────┐
                             │  GitHub Actions release │
                             │  workflow on tag push   │
                             │  (WIF, no SA keys)      │
                             └─────────────────────────┘
```

Reader access is granted at the AR repo level to
`domain:populationgenomics.org.au`, so any active CPG Google
account can install. Non-CPG accounts get a 403 from AR.

The release workflow uses Workload Identity Federation — there are
no service-account JSON keys in GitHub Secrets. The GitHub OIDC token
is exchanged for a short-lived GCP access token at run time, scoped
to a publisher SA that has writer (and only writer) on the AR repo.

## Cutting a release (maintainers)

1. Sync from upstream (if there are upstream changes worth pulling):

   ```sh
   git fetch upstream
   git merge upstream/main          # fast-forward most of the time
   git push origin main
   ```

2. Bump the version in `pyproject.toml` (and
   `.claude-plugin/plugin.json` if it's also used as a Claude Code
   plugin). Commit.

3. Tag and push:

   ```sh
   git tag v0.7.1
   git push origin v0.7.1
   ```

   The `release.yml` workflow fires on `v*` tags. It compiles the
   TypeScript module, builds the wheel + sdist, authenticates to GCP
   via WIF, uploads to Artifact Registry, and attaches the artifacts
   to the GitHub release.

   The first thing the workflow checks is that the tag matches
   `pyproject.toml`'s version — it'll fail fast if you forget step 2.

4. Watch it:

   ```sh
   gh run watch --repo populationgenomics/semantic-code-review
   ```

   Once green, `uv tool run --from semantic-code-review scr` (which
   the wrapper does on every invocation) will resolve the new
   version automatically.

If the workflow fails partway, you can re-run the failed step from
the Actions UI; the AR upload step is the one that's idempotent-
unsafe (it'll 409 on a duplicate version), so if it succeeded once
already, skip it on the re-run.

### What lives where

- **GCP project**: `aasgard-dev`
- **AR repo**: `australia-southeast1-python.pkg.dev/aasgard-dev/scr-python/`
- **Publisher SA**: `scr-publisher@aasgard-dev.iam.gserviceaccount.com`
  (writer on AR, no other roles)
- **WIF pool**: `github-actions` in `aasgard-dev` (global), provider
  `github`, pinned to `assertion.repository_owner == "populationgenomics"`
- **WIF binding**: only workflows from `populationgenomics/semantic-code-review`
  can impersonate the publisher SA

## Sync from upstream

Upstream remote is set to `https://github.com/folded/semantic-code-review`.
For routine sync:

```sh
git fetch upstream
git merge upstream/main          # or rebase if you prefer linear history
git push origin main
```

The CPG-only files (`install.sh`, `CPG.md`, `.github/workflows/release.yml`)
don't exist upstream, so syncing never conflicts on them. The only
source of conflict would be if upstream later commits a file at the
same path — resolve once, move on.

## Roadmap to public

Once internal review approves opening the source, flipping
`populationgenomics/semantic-code-review` to public unlocks the
unauthenticated `curl | bash` install URL. No infrastructure changes
needed — the AR repo stays domain-gated either way (only CPG accounts
can `pip install` from it; anyone else gets a 403 even if they can
read the source).
