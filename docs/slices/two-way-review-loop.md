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
