# ADR 0009 — Comments are sent, not dumped: a two-way review loop

- Status: Proposed
- Date: 2026-09-07
- Resolves: #20

## Context

A review ends with a dump. In review mode `scr review` blocks until the
reviewer clicks Done, then prints every comment as markdown for the
Claude Code plugin to walk through; in PR mode Done opens a modal that
posts every selected comment as one GitHub review and then ends the
session. Three consequences, filed as #20:

- Done kills the server and leaves the tab inert.
- Posting is welded to ending: there is no "send these and keep reading".
- Nothing reaches the counterpart until the end, so a reviewer who finds
  something big mid-review cannot hand it over without also handing over
  every note they have made so far, with no signal as to which matters.

The plugin's blocking call is also fragile: the Bash tool caps one call
at ten minutes, and `scr review` blocks for up to an hour.

Facts that shape the decision. The Claude Code harness wakes the model
only when a command it started exits; nothing else injects into a turn.
GitHub reviews have a *pending* state — a draft only its author sees,
whose comments can be added, edited and deleted (`addPullRequestReview-
Comment`, `updatePullRequestReviewComment`, `deletePullRequestReview-
Comment`) until `submitPullRequestReview` publishes it with an event
(`COMMENT`, `APPROVE`, `REQUEST_CHANGES`); one pending review per user
per PR. The viewer already shows upstream thread resolution but cannot
set it, and the GitHub layer submits `COMMENT` reviews only. Comment
anchors already propagate across commits for ingested PR comments
(`head_line`, `anchor_status`). The viewer already re-renders a hunk from
an SSE event, and augmentation is per hunk.

## Decision

**One lifecycle, two counterparts.** A comment is a *draft* — the
reviewer's own, editable, persisted, never lost — until the reviewer
*sends* it. Send is explicit, per comment, with *Send all drafts*. The
selection is the priority signal: one comment sent alone means "this,
now"; drafts are notes. A sent comment is *delivered* once the
counterpart has it; until then an edit just replaces the text. After
delivery an edit makes it dirty again and a re-Send delivers it as
*revised*; deleting a delivered comment delivers *withdrawn*; deleting an
undelivered draft delivers nothing. Nothing is read-only in review mode.
Done, the modal and `--yes` go.

**Review mode: Claude is the counterpart.**

- `scr review <args>` augments, serves, opens the browser, prints the
  run id and detaches; the server records how to reach it in the
  [[run-directory]]. Claude runs `scr review --wait <run_id>` as a
  background task. A call returns a *batch* (what one gesture sent: one
  comment for Send, all drafts for Send all), or `nothing yet` before the
  tool's timeout, or `ended` with the remaining drafts as the final list.
  A batch landing wakes Claude even mid-task; the skill treats a single
  sent comment as "look now".
- A batch carries the run id and batch number and, per comment: id,
  state (new / revised / withdrawn / reply), anchor, body, and the
  anchored code with two lines either side.
- Claude answers into the viewer: `scr comment reply <run_id> <id>` adds
  a `claude`-authored entry to the thread, shown live; `scr comment
  resolve` resolves it. The reviewer's follow-up is a reply they Send.
- The session ends when the tab has been gone for the idle period. The
  viewer shows whether Claude is *listening* (a `--wait` attached). After
  a few empty polls Claude stops re-arming and says the review is still
  open; it re-attaches when asked.
- The review follows the code. When the right endpoint changes (the
  working tree, or the ref Claude commits to) the diff re-renders live;
  a comment whose line changed goes *outdated* and re-anchors where
  propagation can; it becomes *addressed* only by an explicit resolve.
  Annotations on changed hunks are marked stale and re-augmented by one
  background worker, debounced — never per keystroke.

**PR mode: GitHub is the counterpart.**

- Send adds the comment to the reviewer's pending review, created on the
  first Send; an existing pending review is resumed and its comments
  shown as sent-but-unsubmitted, so nothing is submitted sight-unseen. A
  failed Send leaves the comment a draft marked *unsent*, retried.
- *Submit* publishes the pending review as Comment, Approve or Request
  changes, with an optional body. Approve with no comments is the LGTM.
  Submit refuses while any comment is unsent, and names them.
- Resolving or unresolving an upstream thread fires immediately, as
  GitHub's own UI does; a failure reverts the badge and says so.
- Claude is not in the loop: no stream, no replies. The `/scr:pr` skill's
  stance — the reviewer sees and posts everything in the browser — stands.
- The session ends when the tab has been gone for the idle period.

## Consequences

- The `/scr:review` command prompt is rewritten around the loop: start,
  wait in the background, discuss each batch as it arrives, answer in
  the thread, wait again, stop after empty polls, resume on request.
  `/scr:pr` changes only where it described Done and the modal.
- `--wait` is re-armed rather than long-lived because of the Bash tool's
  cap; that is a harness fact, not a design preference.
- A reviewer running `scr review` in a plain terminal gets the same
  detached server; with no `--wait` attached the drafts accumulate and
  `--wait` after the tab closes prints today's list.
- Following the code is the most expensive slice and the one the plugin
  exists for (`docs/motivation.md`, the short loop): comment, fix, see
  the fix under the comment. It is decided here and built last.
- Unsent PR-mode drafts live only in the run directory; a new head SHA
  is a new run and does not carry them. Sent comments live on GitHub in
  the pending review and survive.

Slices: [`docs/slices/two-way-review-loop.md`](../slices/two-way-review-loop.md).

## Alternatives considered

**Send on save.** One fewer click, nothing to forget. Rejected: every
save becomes a delivery, so the reviewer cannot group three related
notes or hold one back, and the edit-after-delivery question collapses
into streaming every keystroke to Claude.

**Sent is immutable.** Causally clean. Rejected: a typo in a comment
Claude has not yet reached could not be fixed; GitHub's own pending
review is editable until submitted, so review mode would be stricter
than PR mode for no reason.

**Every edit streams as an amendment.** Rejected in favour of
draft-until-delivered: the reviewer, not the tool, should decide when a
correction goes, and the badge tells them which of the two cases they
are in.

**A priority flag per comment** instead of Send-as-signal. Rejected: a
field on every comment that the model must honour, where the arriving
batch already says it.

**One review per Send** in PR mode. Rejected: N "reviewed" events on the
PR timeline. The pending review is the buffer GitHub provides for this.

**A separate `scr comments wait` command** with `scr review` still
blocking to the end. Rejected: the same loop with worse ergonomics.

**A debounce grouping rapid Sends.** Rejected: an invisible rule where
Send all is an explicit one.

**Claude in PR mode** (`--with-claude`). Not adopted now; the skill's
privacy stance is deliberate and the shared lifecycle leaves the door
open.
