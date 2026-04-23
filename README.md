# semantic-code-review (`scr`)

LLM-augmented local-diff reviewer. Opens an interactive side-by-side diff in
the browser with fold-level "what did this block change" annotations, lets you
leave inline comments, and (when invoked as a Claude Code plugin) round-trips
those comments back into the session so the agent can walk through them with
you.

## Usage as a Claude Code plugin

Prerequisites on the user's machine:

- Python 3.11 or newer on `PATH`
- `git` on `PATH`
- `ANTHROPIC_API_KEY` in the environment (or a `.env` in the repo you run `/review` from)

Optional:

- `gh` — only needed if you ever run `scr fetch` / `scr run` against a GitHub PR URL
- `ripgrep` (`rg`) — speeds up the LLM's code-search tool; `git grep` is used as a fallback

Install:

```
/plugin marketplace add folded/semantic-code-review
/plugin install semantic-code-review
```

The first time you run `/review`, the plugin's `bin/scr` wrapper creates a
dedicated Python virtualenv under `$CLAUDE_PLUGIN_DATA/venv` and installs the
package from the plugin tree. Subsequent invocations exec the already-built
venv and start immediately. When the plugin is updated and `pyproject.toml`
changes, the wrapper detects it via a sha256 stamp and rebuilds the venv in
the background on the next run — no user action required.

### The `/review` slash command

```
/review HEAD~1               # diff working tree vs one commit back
/review main..HEAD           # committed-only diff of the current branch
/review HEAD --spec SPEC.md  # with a spec markdown as LLM ground truth
```

The command opens a browser viewer. Leave inline comments by clicking a line
number on either side of the diff. When you click **Done**, the viewer closes,
the command returns the comments as structured markdown, and Claude Code walks
through them with you one at a time.

See `.claude/commands/review.md` in this repo for the full prompt.

## Usage as a standalone CLI

```
pip install -e .
scr review HEAD~1..HEAD --spec SPEC.md
```

Subcommands:

- `scr review <ref-or-range>` — local git diff, runs LLM augment, opens viewer
- `scr fetch <pr-url>` — fetch a GitHub PR into a run directory
- `scr augment <run-dir>` — run the LLM augmentation pass on a run directory
- `scr render <run-dir>` — render the HTML viewer from an augmented run
- `scr run <pr-url>` — fetch + augment + render (no viewer server)
- `scr strip <augmented.diff>` — write a plain unified diff to stdout
- `scr lint <augmented.diff>` — validate the augmented-diff format

## Development

```
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
```

The design plan lives in `/Users/tjs/.claude/plans/plan-a-tool-that-bubbly-treehouse.md`.
