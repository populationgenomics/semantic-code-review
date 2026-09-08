# Slices — Comments are sent, not dumped

Pairs with [ADR 0009](../adr/0009-comments-are-sent-not-dumped.md). Three
slices, strictly ordered: each is usable on its own and the next builds on
it.

## Slice 1 — Send, and the stream to Claude

The comment lifecycle (draft → sent → delivered; revised; withdrawn) in
the store and the viewer: a Send button per comment, Send all drafts,
badges. `scr review` detaches after printing the run id and records the
server's address in the run directory. `scr review --wait <run_id>`
returns a batch, `nothing yet`, or `ended`. `scr comment reply` and
`scr comment resolve` write into the thread over the same server. The
viewer shows whether Claude is listening. Done goes from review mode.
`/scr:review` is rewritten around the loop.

**Gate:** with the browser open, a Send lands in a background `--wait`;
an edit before delivery replaces the text and after delivery arrives as
`revised` on re-Send; a deletion arrives as `withdrawn`; a `claude` reply
appears in the thread without a reload; closing the tab makes the next
`--wait` return `ended` with the remaining drafts.

**Landed as:** `scr review <spec>` materialises the run, spawns the
server as a detached child (the same interpreter re-executing the
invocation's argv with `--runs-root <resolved> --serve-run <slug>`, a new
session, stdio on the run dir's `server.log`), waits for `server.json`
and returns; stdout ends `viewer: <url>` then `run_id: <slug>`; exit 0,
or 2 with the log tail when the child dies before binding. The server
writes `server.json` (`{port, pid, started_at, url}`) once bound and
removes it on exit; it exits after `--timeout` idle seconds with no
request, no open viewer and no `--wait` attached. `scr review --wait
<run_id> [--wait-timeout 540] [--runs-root …]` long-polls `GET
/wait?timeout=S`; the first stdout line is `status: batch` (then `# Batch
<n> for <run_id>` and per comment `## <id> — new|revised|withdrawn|reply —
<file>:<line> (<side>)[ — in reply to <id>]`, the body quoted, the
anchored code ±2 lines in a fence), `status: nothing-yet`, or `status:
ended` (then `# Review ended — remaining comments for <run_id>` over
every undelivered comment, marked delivered by the CLI writing the
store); exit 0 for all three, 2 for an unknown run id or a live server
refusing `/wait`. `scr comment reply <run_id> <comment_id> [BODY]`
(stdin when omitted), `scr comment resolve|unresolve <run_id>
<comment_id>`: exit 0, or 2 with no live server or on a refusal. Routes:
`POST /comments/<id>/send`, `POST /comments/send-all`, `POST
/comments/<id>/{resolve,unresolve}`, `POST /comments` with `source:
"claude"` for a reply, `GET /wait`; SSE frames `comment`,
`comment-deleted`, `listening`; `/data.json` carries `counterpart` and
`listening`. Batches are numbered by the store at Send (one per Send,
one per Send all, one per deletion of a delivered comment) and persisted
in `comments.json` as `last_batch_no`; a batch whose comments were all
deleted before delivery is skipped. PR mode keeps Done and the modal
until slice 2.

## Slice 2 — The pending review

PR mode adopts the lifecycle with GitHub as the sink: Send adds to the
pending review (created or resumed; inherited comments shown as sent),
`unsent` on failure with retry. Submit replaces Done and the modal:
Comment / Approve / Request changes, optional body, refused while
anything is unsent. Resolve and unresolve on upstream threads fire
immediately. `--yes` goes. `/scr:pr` loses its Done and modal text.

**Gate:** a Send appears as a pending comment on GitHub within the same
review; Submit publishes one review with the chosen event; a second
`scr pr` on the same PR shows the pending comments it inherited; a
resolve toggles the thread on GitHub and reverts visibly on failure.

**Landed as:** `scr pr <repo> [<number>]` materialises the run and
detaches the server exactly as `scr review` does (`runner.detach_server`,
shared; the child is `scr pr … --runs-root <resolved> --serve-run <slug>`
and reads the PR off `meta.json`, so the picker's choice need not be in
its argv); stdout ends `viewer: <url>` then `run_id: <slug>`; exit 0, 1
with no PR picked, 2 on a gh/fetch failure or a child that dies before
binding. No `--wait`, no `--yes`, no Done, no modal; the session ends
when the tab has been gone for the idle period. The server is built with
`counterpart="github"` and a `ReviewSink` (`review/pending_review.
PendingReview`, over `review/github_graphql.py`), and before binding
calls `resume_pending_review`: `reviews(states: PENDING)` for the viewer,
then the review's `comments`, adopted into the store as `source: local`,
`delivery: delivered`, `deliveries: 1` with their `node_id` (id `gh-
<databaseId>`, side read off the last line of `diffHunk`, anchors
propagated to head like an ingested comment's; a file-level comment is
counted as `unanchored` and not shown); a refused lookup is logged and
the first Send looks again. Routes: `POST /comments/<id>/send` and `POST
/comments/send-all` mark sent then flush — every undelivered comment,
one `gh api graphql` call each, under one lock: `addPullRequestReview`
on the first (created here, never at start), `addPullRequestReviewThread`
(anchor resolved against `raw.diff`; returns thread and comment ids),
`addPullRequestReviewComment` for a reply to an upstream comment,
`updatePullRequestReviewComment` when the comment carries a `node_id`,
`deletePullRequestReviewComment` for a withdrawn tombstone — and answer
`{batch_no, comment_ids, pending_review}`. A refusal (`GitHubRefused`,
502) marks that comment `send_error` and the rest go on. `POST /comments/
retry` flushes again; the viewer fires it every 30 s while anything is
unsent, and every Send flushes too. **Edit in place:** an edit to a
delivered comment is sent again by the session on save (`upsert` →
`send` → flush) and stays *pending* — the ADR's *revised* is review
mode's mechanism, where Claude must be told; a refused update reads
*unsent*. A deletion of a delivered comment deletes the pending comment;
refused, the tombstone stays for the retry (named in `unsent` as
`deleted: true`). `POST /submit {event: COMMENT|APPROVE|REQUEST_CHANGES,
body?}` → 409 `{error, unsent: [{id, file, side, line, body, deleted,
error}]}` while anything is undelivered, else `submitPullRequestReview`
(an empty review is created first when none is pending — Approve with no
comments is the LGTM), `mark_submitted` flips what it held to `source:
github`, and the answer is `{review_url, event, submitted}`; the next
Send opens a new pending review. `POST /comments/<id>/{resolve,
unresolve}` on an upstream thread fires `resolveReviewThread` /
`unresolveReviewThread` on its `thread_id` (recorded by the ingest and on
delivery) and records the flag only once it lands; a thread still in the
pending review is refused 409, as is `GET /wait`. `/data.json` carries
`pending_review: {unsent, submitted_url, unanchored}`, republished as
the `pending-review` SSE frame after every flush and on submit. The bar
(`send_bar.ts`, one module for both counterparts) shows Send all drafts,
`N unsent — retrying`, Submit with its chooser (a panel, not a modal),
and the review's link once submitted; badges read *draft* / *sending* /
*unsent* / *pending*; Resolve / Unresolve sit on every thread in PR
mode, disabled with the reason on a pending one. The pending review's id
lives on the sink in memory, re-discovered at start. GitHub is
authoritative for its draft, so the store *reconciles* to it on any sign
of divergence: one `nodes(ids:)` look (with the PR's pending review in
the same query, `POST /reconcile`) per comment GitHub was told about —
still pending: nothing; in a published review: upstream now, with the
body GitHub holds, `submitted_from: "github"` in the state and the link
in the bar; unknown: a draft again with a `notice` under its row, cleared
by the next Send (a deletion tombstone is dropped). Triggers: a
NOT_FOUND-class refusal (`GitHubRefused.not_found`, off gh's stderr or
the typed GraphQL error; other refusals stay unsent-and-retry) —
reconcile once per flush and re-apply that delivery once, a second
refusal is unsent, never a loop; the chooser opening (it lists what
Submit would publish from the answer, or the last known state marked
stale when GitHub is unreachable); Submit, before deciding (refused 409
with `submitted_url` when the look finds the review published from the
web; nothing pending and nothing unsent still publishes an empty review);
the 30 s retry tick; and the start-up resume.

## Slice 3 — The review follows the code

When the right endpoint changes, the diff re-renders the changed hunks
over SSE; comments on changed lines go `outdated` and re-anchor where
propagation can; annotations on changed hunks are marked stale and
re-augmented by one debounced background worker. Resolve makes a comment
`addressed`.

**Gate:** Claude edits a file under review; the hunk updates without a
reload; a comment on a changed line shows `outdated` and still reads
against the old text; its annotation is marked stale, then refreshed
once, after the edits stop; `scr comment resolve` on it shows
`addressed`.
