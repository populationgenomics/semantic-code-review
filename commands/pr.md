---
description: Open an LLM-augmented viewer for a GitHub PR; the reviewer sends comments into their pending review and submits it from the browser.
---

You are running a **GitHub-PR review** workflow for the user. Intent: they want to review someone else's open PR (or their own) and post inline review comments back as one GitHub review.

**Scope.** This skill fetches a GitHub PR, runs LLM augmentation on its diff and opens the viewer in the browser. Everything after that happens in the browser: each comment the user *sends* goes into their pending review on GitHub, and *Submit* publishes it as Comment, Approve or Request changes. **The user reviews, sends and submits in the browser — you don't see or handle the comment bodies, and nothing comes back to you.**

**Not in scope.** For reviewing local-only changes in conversation with you (no posting anywhere), use `/scr:review`. Different command, different intent.

Your job is to:

1. Figure out **which PR** to review.
2. Invoke `scr pr` with the right arguments. It returns at once.
3. Tell the user the viewer is open and that Submit in the browser posts. **There is nothing to wait for and nothing to report from stdout.**

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

`scr` may be installed two ways; the slash command works with both as long as it's on PATH:

- **Claude Code plugin** (`/plugin install scr` from `populationgenomics/semantic-code-review`) — ships a `bin/scr` bootstrap wrapper; the plugin runner prepends it to PATH when `/scr:pr` runs.
- **PyPI** (`uv tool install semantic-code-review`, or `pipx`/`pip`) — installs the published wheel; `scr` lands on PATH wherever uv keeps tool bins.

Don't try to discover or call `scr` via an absolute path — `scr` on PATH is the contract. If `scr pr` fails with command-not-found (and only then), surface the install options to the user verbatim; don't guess between them.

The command:

- preflights `gh` (the GitHub CLI), resolves the PR, fetches metadata + diff + base/head worktrees into `~/.cache/scr/runs/<...>/`
- starts a detached review server that runs the LLM augmentation pass, serves the viewer and opens the browser; the server picks up any pending review the user already has on the PR, so its comments show as *pending* rather than being submitted sight-unseen
- prints `viewer: <url>` and `run_id: <slug>` and **returns at once** — the server keeps running until the tab has been closed for the idle period

**Do not add `--no-augment`.** Augmentation IS the point — without it the viewer is a plain diff with no LLM annotations, smells, or fold descriptions. Pass through only if the user explicitly asked.

**Do not add `--backend=…`.** `scr` picks a backend automatically (same logic as `/scr:review`).

## Step 3 — tell the user, then stop

`scr pr` exits 0 once the viewer is reachable; its stdout is the viewer URL and the run id, nothing about comments. Tell the user:

> The viewer for <repo>#<number> is open. Comments you **Send** go into your pending review on GitHub as you send them; **Submit** in the browser publishes the review (Comment, Approve or Request changes). Nothing comes back here.

That's it. Don't wait for anything, don't poll, don't offer to walk through comments — you never see them. Offer to open the URL only if the browser didn't.

A non-zero exit means the review did not start: `gh` missing or unauthenticated, no PR picked, a fetch failure, or the server failing to start (stderr carries the message and points at the server log). Pass the message through to the user; don't try to fix `gh` yourself.

## Heads up

- **There is no `--wait` for `scr pr`.** GitHub is the counterpart, not you: the review loop runs between the user and GitHub, and the `/scr:review` stream does not apply.
- The reviewer's comments don't flow back to you as actionable items, by design: the bodies never enter your context.
- `/scr:pr` is user-triggered. Don't call `scr pr` pre-emptively from other slash commands or conversations.
- **If the user asks to review their own working-tree changes**, that's `/scr:review`, not this. This skill is GitHub-PR-only.
- If `scr` is not on PATH, Bash will return a "command not found" error. Show the install options from Step 2 verbatim and stop — don't try to discover an alternate binary location.
- If `gh` is not installed or not authenticated, `scr pr` exits early with a clear message. Pass it through to the user; don't try to set up `gh` automatically.
- Running `scr pr` again on the same PR while the server is up prints the same URL and run id (`scr pr: a server already holds this run`); a new head SHA is a new run, and the pending review carries over from GitHub.
