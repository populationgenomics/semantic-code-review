---
description: Open an LLM-augmented viewer for a GitHub PR; the reviewer confirms and posts comments from the browser modal.
---

You are running a **GitHub-PR review** workflow for the user. Intent: they want to review someone else's open PR (or their own) and post inline review comments back as a single GitHub review.

**Scope.** This skill fetches a GitHub PR, runs LLM augmentation on its diff, opens the viewer in the browser, and lets the user post comments back to GitHub via the in-browser confirmation modal. **The user reviews and confirms posting in the browser — you don't see or handle the comment bodies.**

**Not in scope.** For reviewing local-only changes in conversation with you (no posting anywhere), use `/scr:review`. Different command, different intent.

Your job is to:

1. Figure out **which PR** to review.
2. Invoke `scr pr` with the right arguments, and wait for it to block-return.
3. Report the outcome based on `scr pr`'s stdout shape. **Do not walk through individual comments — the user has already reviewed them in the browser.**

## Step 1 — infer the PR

`$ARGUMENTS` is what the user typed after `/scr:pr`. Parse it into the `owner/repo [number]` form `scr pr` expects:

| User typed | Run |
|---|---|
| `https://github.com/owner/repo/pull/42` | `scr pr owner/repo 42` |
| `owner/repo#42` or `owner/repo 42` | `scr pr owner/repo 42` |
| `owner/repo` (no number) | `scr pr owner/repo` — `scr pr` shows a picker or auto-selects the single review-requested PR |
| (empty) | Try to infer the repo from `git remote get-url origin` in the cwd: parse `git@github.com:owner/repo.git` or `https://github.com/owner/repo(.git)?` into `owner/repo`. If found, run `scr pr owner/repo` (lets the user pick from open PRs requesting their review). If not found, ask the user. |

**Announce the call in one sentence** before running: "Reviewing `<repo>#<N>`." (or "Reviewing PRs in `<repo>` — picker incoming." if no number).

If `$ARGUMENTS` looks like a local git ref (`HEAD`, `main..HEAD`, etc.), stop and tell the user that `/scr:pr` is for GitHub PRs only — they probably want `/scr:review` instead.

## Step 2 — invoke the command

Use the Bash tool to run:

```
scr pr <the args you inferred>
```

**Do not `cd` anywhere before running it.** The PR fetch is GitHub-side and doesn't depend on cwd, but `gh` may pick credentials based on the directory's git remote — staying in the session's working directory is the right default.

### How `scr` ends up on PATH

`scr` may be installed three different ways; the slash command works with all of them as long as it's on PATH:

- **Claude Code plugin** (`/plugin install scr` from `folded/semantic-code-review`) — ships a `bin/scr` bootstrap wrapper; the plugin runner prepends it to PATH when `/scr:pr` runs.
- **CPG install.sh** (`curl …/install.sh | bash` from `populationgenomics/semantic-code-review`) — drops a `~/.local/bin/scr` wrapper that runs the wheel published to CPG's Artifact Registry.
- **Direct uv** (`uv tool install semantic-code-review`) — installs the wheel from PyPI/wherever; `scr` lands on PATH wherever uv keeps tool bins.

Don't try to discover or call `scr` via an absolute path — `scr` on PATH is the contract. If `scr pr` fails with command-not-found (and only then), surface the install options to the user verbatim; don't guess between them.

The command:

- preflights `gh` (the GitHub CLI), resolves the PR, fetches metadata + diff + base/head worktrees into `~/.cache/scr/runs/<...>/`
- runs the LLM augmentation pass over the diff
- starts a localhost HTTP server, opens the browser, and **blocks** until the reviewer either posts or closes the modal

**Do not add `--yes` yourself.** `--yes` bypasses the in-browser confirmation modal and posts every local comment as soon as the reviewer clicks Done. The whole point of the modal is to give the reviewer a final pass with per-comment deselect/delete — auto-bypassing it on the user's behalf removes a safety step they didn't ask you to remove. Pass it through only if the user explicitly typed it.

**Do not add `--no-augment` either.** Augmentation IS the point — without it the viewer is a plain diff with no LLM annotations, smells, or fold descriptions. Pass through only if the user explicitly asked.

**Do not add `--backend=…`.** `scr` picks a backend automatically (same logic as `/scr:review`).

When the reviewer is in the browser, do not start other work or speculate — they are occupied. Just wait for the bash call to return.

## Step 3 — report the outcome

`scr pr`'s exit code + stdout shape tells you what happened. **Do NOT quote or walk through the contents of stdout to the user** — the reviewer has already seen everything in the browser, and the comment bodies are either on GitHub now or saved locally for retry.

Two stdout shapes:

### A) Stdout starts with `# Posted to https://github.com/...`

The reviewer confirmed in the modal and the comments are now on GitHub. Tell the user:

> Posted N comment(s) to <repo>#<number>. <review_url>

That's it. Don't summarise the comments. Offer to open the URL only if the user asks.

### B) Stdout starts with `# Review comments for ...`

The reviewer **chose not to post** — they cancelled the modal, closed the tab, or there were no postable comments. The full markdown dump is in stdout. **The user made this choice deliberately.** Tell them:

> Comments saved to `comments.json` in the run directory but not posted. Re-run `scr pr <args>` to try again, or open the file directly to inspect.

Do not propose to walk through the comments. Do not propose to act on them. The user decided not to post; they didn't ask for follow-up. If they want a walkthrough discussion, they'll ask — and that's `/scr:review`'s territory anyway.

## Heads up

- **`scr pr` blocks** for up to an hour by default while the browser is open. Wait for it to return naturally — don't try to cancel it or run things in parallel.
- The reviewer's comments don't flow back to you as actionable items. The CLI keeps the post-success stdout minimal (URL only) specifically so the comment bodies don't end up in your context as something to act on.
- `/scr:pr` is user-triggered. Don't call `scr pr` pre-emptively from other slash commands or conversations.
- **If the user asks to review their own working-tree changes**, that's `/scr:review`, not this. This skill is GitHub-PR-only.
- If `scr` is not on PATH, Bash will return a "command not found" error. Show the install options from Step 2 verbatim and stop — don't try to discover an alternate binary location.
- If `gh` is not installed or not authenticated, `scr pr` exits early with a clear message. Pass it through to the user; don't try to set up `gh` automatically.
