---
description: Open an LLM-augmented viewer for the work in progress in this session; walk through the reviewer's comments when they hit Done.
---

You are running a review workflow for the user. Intent: they (or you, in this session) have just implemented or modified some code, and they want a structured review of that work before moving on.

Your job is to:

1. Figure out **what** to review and **what spec** (if any) to treat as ground truth.
2. Invoke `scr review` with the right arguments, and wait for it to block-return.
3. Walk the user through the returned comments one at a time.

## Step 1 — infer the review scope

`$ARGUMENTS` is what the user typed after `/review`. Use it as the authoritative override when non-empty, otherwise infer from the session.

### If `$ARGUMENTS` is non-empty

Pass it through verbatim as the CLI args to `scr review`. Examples:
- `/review HEAD~1` → `scr review HEAD~1`
- `/review main..HEAD --spec docs/spec.md` → `scr review main..HEAD --spec docs/spec.md`
- `/review` (empty) → infer (see below)

### If `$ARGUMENTS` is empty

Pick the scope using this ladder, **running the relevant git commands yourself via Bash** to decide:

1. **Working-tree changes exist?** Run `git status --porcelain=v1`. If it's non-empty, the user is reviewing WIP. Default to `scr review HEAD` (which diffs everything — unstaged + staged + ahead-of-HEAD — against HEAD, ignoring HEAD as the base… actually: run `scr review HEAD` meaning "diff from HEAD to current working state" — covers staged + unstaged). **Announce your choice to the user in one sentence** before running, so they can redirect: "Reviewing your working-tree changes (staged + unstaged) against HEAD. Say `/review main..HEAD` if you meant your branch instead."

2. **Clean tree but committed ahead of the main-branch tip?** Run `git rev-parse --abbrev-ref HEAD` to get the current branch, and `git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null || echo refs/remotes/origin/main` to guess the default branch. If current != default and `git rev-list --count <default>..HEAD` > 0, use `scr review <default>..HEAD`. Announce: "Reviewing your branch <current> against <default> (<N> commits)."

3. **Clean tree, on default branch, not ahead of remote?** Look at the most recent commit: `scr review HEAD~1..HEAD`. Announce: "Reviewing the last commit <sha> <subject>."

4. **None of the above make sense?** Ask the user: "I can't find obvious work to review in this repo. What range or ref would you like?" and stop.

### Spec markdown inference

Separately from the range, check if there's an obvious spec file to pass as `--spec`:

- If the user provided a `--spec` in `$ARGUMENTS`, use it.
- If there's a SPEC, TASK, PLAN, or DESIGN markdown file that has been **mentioned or read in this conversation**, prefer it. Prefer the most recently referenced.
- Otherwise scan for `SPEC.md`, `docs/spec*.md`, `PLAN.md`, `TASK.md`, `ROADMAP.md` in the repo. If exactly one matches, use it. If multiple, show the user the list and ask which (or none).
- If none found, run without `--spec`.

**Announce the spec choice in the same sentence as the range**: "Reviewing <range> against spec `<path>`."

## Step 2 — invoke the command

Use the Bash tool to run:

```
scr review <the args you inferred>
```

The command:
- builds the local diff into `.scr/runs/<slug>/`
- runs the LLM augmentation pass (unless `--no-augment` was passed)
- starts a localhost HTTP server, opens the browser, and **blocks** until the user clicks "Done" (or the 1-hour default timeout)

**Do not add `--no-augment` yourself.** Augmentation IS the point — without it the viewer shows a plain diff with no LLM annotations, smells, fold descriptions, or context. Pass it through only if the user explicitly asked for it.

**Do not add `--backend=api` either.** `scr` picks a backend automatically: if `ANTHROPIC_API_KEY` is set it uses the Anthropic SDK directly; otherwise it falls back to the `claude` CLI subprocess (assuming the user is logged into Claude Code). The absence of an API key is not a reason to disable augmentation.

When the user is reviewing in the browser, do not start other work or speculate — they are occupied. Just wait for the bash call to return.

## Step 3 — walk through the comments

When `scr review` returns, its stdout is a markdown list of reviewer comments. Read it.

For each comment:

1. Print the comment location (`path:line (side)`) and the body back to the user so they know which one you're on.
2. Read the code around that line (use the Read tool on the file at the given line number).
3. Respond:
   - **Question** → answer directly, referring to the code.
   - **Concern / bug claim** → investigate and report findings. Don't defend reflexively; investigate first.
   - **Request for change** → propose a diff. Don't apply it yet — ask the user to confirm before editing.
4. Move to the next comment only when the user says to move on or clearly acknowledges your response.

If the comments list is empty ("The reviewer had no concerns"), thank the user and stop — don't volunteer further changes.

## Heads up

- **`scr review` blocks** for up to an hour by default while the browser is open. Wait for it to return naturally — don't try to cancel it or run things in parallel.
- If the user closes the browser without clicking Done, the command exits code 2 and stdout is still a valid markdown list (possibly empty). Treat it the same way.
- `/review` is user-triggered. Don't call `scr review` pre-emptively from other slash commands or conversations.
- If the tool hasn't been installed yet, running `scr review` will print a clear bootstrap message; pass it through to the user verbatim.
