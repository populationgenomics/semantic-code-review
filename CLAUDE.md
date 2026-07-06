# scr development notes

scr (semantic code review) is an LLM-augmented local-diff and GitHub-PR
reviewer: it opens an interactive side-by-side viewer with semantic-group
navigation, fold-level annotations, and inline comments that round-trip back
into a Claude Code session. See [`README.md`](README.md) for usage and
[`CONTEXT.md`](CONTEXT.md) for the architecture map.

## Working norms

Operating directives for Claude (and any agent) in this repo; they counteract default
model dispositions.

- **Resist the minimal-diff reflex.** Don't reach for the smallest change that hides the
  symptom (special-casing, papering over root causes). Aim for the correct fix at the
  right complexity level — not the smallest, not gold-plated.
- **Fail loudly and early.** Raise on a missing expected input or precondition; never fall
  back to a default/placeholder to limp along. A placeholder is an explicit caller input,
  never a code default.
- **Push back; don't just comply.** When a design, name, or approach seems worse —
  including a shortcut you're asked to take — say so with reasoning, unprompted. The
  author owns the final call.
- **Offer better alternatives with trade-offs.** When a materially better approach than
  the proposed one exists, present it and the trade-offs — don't just execute the ask.
- **Investigate before producing.** Read the code and verify constraints first. Don't
  treat a training-pattern convention as load-bearing unchecked; don't speculate about
  what you can read.
- **Explain non-obvious changes first.** For a change whose rationale isn't self-evident,
  give the why before showing or applying the diff.
- **Ask when unsure** rather than assume intent.
- **No intensifiers or emphasis filler.** Drop words and phrases that add emphasis but no
  information — "that's the key", "crucially", "importantly", "the key insight", "it's
  worth noting". State the point plainly. Applies to all prose: chat replies, PR/review
  comments, commit messages, and docs.

## Code style

@docs/style/general.md
@docs/style/python.md

## Docs

The primary audience for docs is a model reading them as context; humans second. Be
terse: state each decision, mechanism, and rationale once — no rhetorical emphasis, no
persuasion, no recaps. Every token written is re-paid on every future read.

## Committing

- **Stage explicit paths**, not `git add -A` / `.`.
- **Commit after each self-contained improvement** — never batch unrelated changes.
- **Run the suite before committing** (`uv run pytest`); it must be green. Use `uv run`,
  not bare `python` — bare `python` pulls a stale pydantic that breaks the tests.
- **Correct a pushed branch with a new commit on top**, not amend + force-push. Reserve
  force-push for rebasing a branch onto `main`.

## CI and review

- **Pin third-party GitHub Actions to the latest stable release**: the moving major tag
  (`@v3`) where the action publishes one, else the exact latest version (`@v8.2.0`). Verify
  against the action's releases when adding or bumping one.

## Live `claude-cli` contract tests

`tests/backends/test_claude_cli_live.py` spawns the real `claude` CLI to guard the
`claude -p` envelope + MCP handshake our subprocess driver depends on. The CLI is not a
versioned API surface, so Anthropic can change it under us; the mocked `claude-cli` tests
only check our *assumption* of that contract, so drift passes CI green and reaches users
first. These are the guard against that.

- **Local only** — the CLI needs paid auth (subscription OAuth or an API key); CI has
  neither, so there's no CI job. They're skipped unless `claude` is on `PATH` and
  `SCR_LIVE_CLI=1`.
- **Run periodically, and always when bumping the pinned `claude`, `anthropic`, or
  `pydantic-ai`**: `SCR_LIVE_CLI=1 uv run pytest tests/backends/test_claude_cli_live.py`.
  A failure means the CLI contract moved — update the driver/parser (and the mocked-test
  fakes) to match.
