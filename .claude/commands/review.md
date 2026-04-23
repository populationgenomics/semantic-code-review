---
description: Open an LLM-augmented viewer for a local git diff; walk through the reviewer's comments when they hit Done.
---

You are running a review workflow for the user. Their intent: have you (Claude) implement or modify some code, then open an external viewer where they can leave inline comments, then walk through those comments together.

Usage examples the user will give you:
- "review the last commit against SPEC.md" → `scr review HEAD~1 --spec SPEC.md`
- "review my branch vs main" → `scr review main..HEAD`
- "review what you just did" → `scr review HEAD` (working tree vs HEAD)

## How to run it

1. Pick the right `scr review` invocation from the user's prompt. Defaults:
   - No spec → drop `--spec`.
   - Single ref (e.g. `HEAD`, `main`) → diffs the working tree against that ref. The user can add `--no-staged` or `--no-unstaged` if they want to narrow it.
   - Range (`main..HEAD`, `HEAD~3...HEAD`) → committed-only.
2. Run `scr review $ARGUMENTS` via the Bash tool. This:
   - builds the local diff into `.scr/runs/<slug>/`,
   - runs the LLM augmentation pass (unless `--no-augment` was passed),
   - starts an ephemeral localhost HTTP server, opens the browser, and **blocks** until the user clicks "Done" in the viewer.
3. When the command returns, its stdout is a markdown list of the reviewer's comments. Read it.

## After the command returns

For each comment in the markdown:

1. Print the comment location (`path:line (side)`) and the comment body back to the user so they know which one you're discussing.
2. Read the code around that line.
3. Respond to the reviewer's comment:
   - If it's a question, answer it directly, referring to the code.
   - If it's a concern or bug claim, investigate and report findings.
   - If it's a request for change, propose a diff (don't apply it yet — ask the user to confirm first).
4. Only move to the next comment when the user says to move on or acknowledges your response.

If the comments list is empty ("The reviewer had no concerns"), thank the user and stop — don't volunteer further changes.

## Heads up

- The command blocks for up to an hour by default; wait for it to return naturally.
- If the user aborts (closes the browser without clicking Done), the command exits with code 2 and stdout is still a valid (possibly empty) markdown list. Treat that the same way — read the markdown and act on what's there.
- Don't pre-emptively invoke the command without the user asking for a review. `/review` is the trigger.
