"""The comment lifecycle in the store: draft → sent → delivered (ADR 0009).

The store is the single owner of the transitions. Each edge is covered
here against `CommentStore` directly, plus the tombstone a delivered
comment leaves when deleted and the migration of a `comments.json`
written before the lifecycle existed.
"""

from __future__ import annotations

import json

import pytest

from semantic_code_review import paths
from semantic_code_review.review import comments
from semantic_code_review.review.comments import CommentStore


def _payload(cid: str, body: str = "note", line: int = 3, **extra: object) -> dict:
    return {"id": cid, "file": "a.py", "side": "new", "line": line, "body": body, **extra}


def _by_id(store: CommentStore) -> dict[str, comments.Comment]:
    return {c.id: c for c in store.all()}


# --- draft ----------------------------------------------------------------


def test_a_new_comment_is_a_draft(run_dir: paths.RunDir) -> None:
    store = CommentStore(run_dir.comments)
    c = store.upsert(_payload("c1", delivery="delivered", deliveries=4, batch_no=9, withdrawn=True))
    # Lifecycle claims on the wire are ignored, like the source claim.
    assert (c.delivery, c.deliveries, c.batch_no, c.withdrawn) == ("draft", 0, None, False)
    assert c.is_draft


def test_an_old_comments_json_loads_as_drafts(run_dir: paths.RunDir) -> None:
    """A file from before the lifecycle has no `delivery`, `deliveries`,
    `batch_no`, `withdrawn` or `last_batch_no`: every local comment is a
    draft and numbering starts at 1."""
    run_dir.comments.write_text(
        json.dumps(
            {
                "comments": [
                    {"id": "old", "file": "a.py", "side": "new", "line": 1, "body": "x"},
                    {"id": "gh", "file": "a.py", "side": "new", "line": 2, "body": "y", "source": "github"},
                ]
            }
        ),
        encoding="utf-8",
    )
    store = CommentStore(run_dir.comments)
    by_id = _by_id(store)
    assert by_id["old"].is_draft
    assert not by_id["gh"].is_draft  # not local: outside the lifecycle
    assert store.last_batch_no == 0
    batch_no, _ = store.send("old")
    assert batch_no == 1


# --- send -----------------------------------------------------------------


def test_send_makes_a_draft_sent_in_its_own_batch(run_dir: paths.RunDir) -> None:
    store = CommentStore(run_dir.comments)
    store.upsert(_payload("c1"))
    store.upsert(_payload("c2", line=5))

    n1, sent1 = store.send("c1")
    n2, sent2 = store.send("c2")

    assert (n1, n2) == (1, 2)
    assert sent1.delivery == "sent" and sent1.batch_no == 1
    assert sent2.delivery == "sent" and sent2.batch_no == 2
    assert store.pending_batches() == [(1, [sent1]), (2, [sent2])]


def test_send_refuses_what_is_not_a_draft(run_dir: paths.RunDir) -> None:
    store = CommentStore(run_dir.comments)
    store.upsert(_payload("c1"))
    store.send("c1")
    with pytest.raises(comments.CommentStateError):
        store.send("c1")
    with pytest.raises(comments.CommentNotFound):
        store.send("nope")


def test_send_refuses_a_comment_that_is_not_the_reviewers(run_dir: paths.RunDir) -> None:
    run_dir.comments.write_text(
        json.dumps(
            {"comments": [{"id": "gh", "file": "a.py", "side": "new", "line": 2, "body": "y", "source": "github"}]}
        ),
        encoding="utf-8",
    )
    store = CommentStore(run_dir.comments)
    with pytest.raises(comments.ReadOnlyCommentError):
        store.send("gh")


def test_send_all_puts_every_draft_in_one_batch_and_skips_the_rest(run_dir: paths.RunDir) -> None:
    store = CommentStore(run_dir.comments)
    store.upsert(_payload("c1"))
    store.upsert(_payload("c2", line=5))
    store.upsert(_payload("c3", line=7))
    store.send("c1")  # already sent: not a draft any more

    batch_no, sent = store.send_all()

    assert batch_no == 2
    assert [c.id for c in sent] == ["c2", "c3"]
    assert all(c.delivery == "sent" and c.batch_no == 2 for c in sent)


def test_send_all_with_no_drafts_sends_nothing(run_dir: paths.RunDir) -> None:
    store = CommentStore(run_dir.comments)
    assert store.send_all() == (None, [])
    assert store.last_batch_no == 0


# --- sent -----------------------------------------------------------------


def test_editing_a_sent_comment_replaces_the_text_and_keeps_it_sent(run_dir: paths.RunDir) -> None:
    store = CommentStore(run_dir.comments)
    store.upsert(_payload("c1", "typo"))
    store.send("c1")

    edited = store.upsert(_payload("c1", "fixed"))

    assert edited.body == "fixed"
    assert edited.delivery == "sent"
    assert edited.batch_no == 1
    # The batch delivers the text as it stands at delivery.
    batch = store.deliver(1)
    assert batch.entries[0].comment.body == "fixed"
    assert batch.entries[0].state == "new"


def test_deleting_a_sent_undelivered_comment_leaves_nothing(run_dir: paths.RunDir) -> None:
    store = CommentStore(run_dir.comments)
    store.upsert(_payload("c1"))
    store.send("c1")

    assert store.delete("c1") is None

    assert store.all() == []
    assert store.pending_batches() == []


def test_deleting_a_draft_leaves_nothing(run_dir: paths.RunDir) -> None:
    store = CommentStore(run_dir.comments)
    store.upsert(_payload("c1"))
    assert store.delete("c1") is None
    assert store.all() == []
    with pytest.raises(comments.CommentNotFound):
        store.delete("c1")


# --- delivered ------------------------------------------------------------


def test_deliver_marks_the_batch_delivered_and_snapshots_it(run_dir: paths.RunDir) -> None:
    store = CommentStore(run_dir.comments)
    store.upsert(_payload("c1"))
    store.upsert(_payload("c2", line=5))
    store.send_all()

    batch = store.deliver(1)

    assert batch.batch_no == 1
    assert [(e.comment.id, e.state) for e in batch.entries] == [("c1", "new"), ("c2", "new")]
    by_id = _by_id(store)
    assert all(by_id[i].delivery == "delivered" and by_id[i].deliveries == 1 for i in ("c1", "c2"))
    assert store.pending_batches() == []
    with pytest.raises(comments.CommentNotFound):
        store.deliver(1)


def test_editing_a_delivered_comment_makes_it_a_draft_and_its_next_delivery_revised(
    run_dir: paths.RunDir,
) -> None:
    store = CommentStore(run_dir.comments)
    store.upsert(_payload("c1", "first"))
    store.send("c1")
    store.deliver(1)

    dirty = store.upsert(_payload("c1", "second"))

    assert dirty.delivery == "draft"
    assert dirty.deliveries == 1  # what the badge reads "needs re-send" off
    assert dirty.is_draft
    batch_no, _ = store.send("c1")
    batch = store.deliver(batch_no)
    assert batch.entries[0].state == "revised"
    assert batch.entries[0].comment.body == "second"
    assert _by_id(store)["c1"].deliveries == 2


def test_deleting_a_delivered_comment_leaves_a_withdrawn_tombstone_to_deliver(run_dir: paths.RunDir) -> None:
    store = CommentStore(run_dir.comments)
    store.upsert(_payload("c1"))
    store.send("c1")
    store.deliver(1)

    tombstone = store.delete("c1")

    assert tombstone is not None
    assert tombstone.withdrawn and tombstone.delivery == "sent" and tombstone.batch_no == 2
    # Invisible to the reviewer, pending for the counterpart.
    assert store.all() == []
    assert [(n, [c.id for c in cs]) for n, cs in store.pending_batches()] == [(2, ["c1"])]
    with pytest.raises(comments.CommentStateError):
        store.upsert(_payload("c1", "resurrect"))

    batch = store.deliver(2)

    assert [(e.comment.id, e.state) for e in batch.entries] == [("c1", "withdrawn")]
    assert store.pending_batches() == []
    assert "c1" not in json.dumps(json.loads(run_dir.comments.read_text(encoding="utf-8")))


def test_deleting_a_dirty_delivered_comment_still_withdraws(run_dir: paths.RunDir) -> None:
    """Delivered once, then edited back to draft: the counterpart holds
    the first text, so the deletion still has to reach it."""
    store = CommentStore(run_dir.comments)
    store.upsert(_payload("c1", "first"))
    store.send("c1")
    store.deliver(1)
    store.upsert(_payload("c1", "second"))

    tombstone = store.delete("c1")

    assert tombstone is not None and tombstone.withdrawn


# --- replies and resolution --------------------------------------------------


def test_a_reviewers_reply_delivers_as_reply(run_dir: paths.RunDir) -> None:
    store = CommentStore(run_dir.comments)
    store.upsert(_payload("c1"))
    store.upsert(_payload("c2", "follow-up", in_reply_to_id="c1"))
    store.send("c2")

    batch = store.deliver(1)

    assert batch.entries[0].state == "reply"
    assert batch.entries[0].comment.in_reply_to_id == "c1"


def test_a_claude_reply_is_anchored_on_its_parent_and_read_only(run_dir: paths.RunDir) -> None:
    store = CommentStore(run_dir.comments)
    store.upsert(_payload("c1", line=12))
    store.send("c1")
    store.deliver(1)

    reply = store.add_reply({"in_reply_to_id": "c1", "body": "done in 3f2a"}, source="claude", author="claude")

    assert (reply.file, reply.side, reply.line) == ("a.py", "new", 12)
    assert reply.source == "claude" and reply.author == "claude"
    assert reply.in_reply_to_id == "c1"
    assert not reply.is_writable and not reply.is_draft and not reply.is_undelivered
    with pytest.raises(comments.ReadOnlyCommentError):
        store.upsert({**_payload(reply.id, "edited")})
    with pytest.raises(comments.ReadOnlyCommentError):
        store.delete(reply.id)
    with pytest.raises(comments.ReadOnlyCommentError):
        store.send(reply.id)
    # Outside the lifecycle: never in a batch.
    assert store.send_all() == (None, [])


@pytest.mark.parametrize(
    "payload",
    [{"body": "no parent"}, {"in_reply_to_id": "c1", "body": "  "}],
)
def test_a_reply_needs_a_parent_and_a_body(run_dir: paths.RunDir, payload: dict) -> None:
    store = CommentStore(run_dir.comments)
    store.upsert(_payload("c1"))
    with pytest.raises(comments.CommentStateError):
        store.add_reply(payload, source="claude", author="claude")


def test_a_reply_to_an_unknown_comment_is_not_found(run_dir: paths.RunDir) -> None:
    store = CommentStore(run_dir.comments)
    with pytest.raises(comments.CommentNotFound):
        store.add_reply({"in_reply_to_id": "ghost", "body": "hi"}, source="claude", author="claude")


def test_resolving_a_thread_flags_every_member_from_any_member(run_dir: paths.RunDir) -> None:
    store = CommentStore(run_dir.comments)
    store.upsert(_payload("c1"))
    reply = store.add_reply({"in_reply_to_id": "c1", "body": "fixed"}, source="claude", author="claude")
    store.upsert(_payload("c3", "thanks", in_reply_to_id=reply.id))
    store.upsert(_payload("other", line=40))

    changed = store.set_thread_resolved(reply.id, True)

    assert sorted(c.id for c in changed) == sorted(["c1", reply.id, "c3"])
    by_id = _by_id(store)
    assert by_id["c1"].thread_resolved and by_id["c3"].thread_resolved
    assert not by_id["other"].thread_resolved
    # Resolution is not an edit: a delivered comment stays delivered.
    assert store.set_thread_resolved("c1", True) == []
    reopened = store.set_thread_resolved("c1", False)
    assert len(reopened) == 3 and not any(c.thread_resolved for c in reopened)


def test_resolving_an_ingested_thread_is_refused(run_dir: paths.RunDir) -> None:
    run_dir.comments.write_text(
        json.dumps(
            {"comments": [{"id": "gh", "file": "a.py", "side": "new", "line": 2, "body": "y", "source": "github"}]}
        ),
        encoding="utf-8",
    )
    store = CommentStore(run_dir.comments)
    store.upsert(_payload("mine", "reply", in_reply_to_id="gh"))
    with pytest.raises(comments.ReadOnlyCommentError):
        store.set_thread_resolved("mine", True)
    # Unless the caller has flipped it on GitHub and is recording that.
    changed = store.set_thread_resolved("mine", True, upstream=True)
    assert sorted(c.id for c in changed) == ["gh", "mine"]
    assert store.thread_root("mine").id == "gh"


def test_resolution_does_not_dirty_a_delivered_comment(run_dir: paths.RunDir) -> None:
    store = CommentStore(run_dir.comments)
    store.upsert(_payload("c1"))
    store.send("c1")
    store.deliver(1)
    store.set_thread_resolved("c1", True)
    assert _by_id(store)["c1"].delivery == "delivered"


# --- the pending review (PR mode) --------------------------------------------
# GitHub takes one comment at a time: a delivery lands or is refused per
# comment, an existing pending review is adopted, a submit flips what it
# held to upstream.


def test_mark_delivered_records_the_ids_github_gave(run_dir: paths.RunDir) -> None:
    store = CommentStore(run_dir.comments)
    store.upsert(_payload("c1"))
    store.send("c1")
    assert [c.id for c in store.undelivered()] == ["c1"]

    delivered = store.mark_delivered("c1", node_id="PRRC_1", thread_id="PRRT_1")

    assert delivered is not None
    assert (delivered.delivery, delivered.deliveries, delivered.node_id, delivered.thread_id) == (
        "delivered",
        1,
        "PRRC_1",
        "PRRT_1",
    )
    assert store.undelivered() == []
    assert store.node_index() == {"PRRC_1": "c1"}


def test_mark_delivered_refuses_what_was_not_sent(run_dir: paths.RunDir) -> None:
    store = CommentStore(run_dir.comments)
    store.upsert(_payload("c1"))
    with pytest.raises(comments.CommentStateError):
        store.mark_delivered("c1", node_id="x")
    with pytest.raises(comments.CommentNotFound):
        store.mark_delivered("ghost", node_id="x")


def test_a_refused_delivery_leaves_the_comment_sent_and_unsent(run_dir: paths.RunDir) -> None:
    store = CommentStore(run_dir.comments)
    store.upsert(_payload("c1"))
    store.send("c1")

    failed = store.mark_send_failed("c1", "HTTP 502")

    assert (failed.delivery, failed.send_error) == ("sent", "HTTP 502")
    assert [c.id for c in store.undelivered()] == ["c1"]
    # A later delivery clears the refusal.
    delivered = store.mark_delivered("c1", node_id="PRRC_1")
    assert delivered is not None and delivered.send_error is None
    with pytest.raises(comments.CommentStateError):
        store.mark_send_failed("c1", "again")


def test_an_edit_of_a_delivered_comment_keeps_its_node_id_for_the_update(run_dir: paths.RunDir) -> None:
    store = CommentStore(run_dir.comments)
    store.upsert(_payload("c1"))
    store.send("c1")
    store.mark_delivered("c1", node_id="PRRC_1", thread_id="PRRT_1")

    edited = store.upsert(_payload("c1", body="edited"))
    _, sent = store.send("c1")

    assert (edited.delivery, edited.deliveries, edited.node_id) == ("draft", 1, "PRRC_1")
    assert (sent.delivery, sent.node_id, sent.thread_id, sent.send_error) == ("sent", "PRRC_1", "PRRT_1", None)
    delivered = store.mark_delivered("c1")
    assert delivered is not None and (delivered.deliveries, delivered.node_id) == (2, "PRRC_1")


def test_a_withdrawn_tombstone_is_dropped_when_its_deletion_lands(run_dir: paths.RunDir) -> None:
    store = CommentStore(run_dir.comments)
    store.upsert(_payload("c1"))
    store.send("c1")
    store.mark_delivered("c1", node_id="PRRC_1")

    tombstone = store.delete("c1")
    assert tombstone is not None and tombstone.node_id == "PRRC_1" and tombstone.send_error is None
    assert [c.id for c in store.undelivered()] == ["c1"]
    failed = store.mark_send_failed("c1", "HTTP 502")
    assert failed.withdrawn and failed.send_error == "HTTP 502"

    assert store.mark_delivered("c1") is None
    assert store.undelivered() == [] and store.all() == []


def test_adopt_takes_a_pending_comment_in_as_delivered(run_dir: paths.RunDir) -> None:
    store = CommentStore(run_dir.comments)
    pending = comments.Comment(
        id="gh-11", file="a.py", side="new", line=3, body="theirs", node_id="PRRC_11", delivery="draft", send_error="x"
    )

    adopted = store.adopt(pending)

    assert (adopted.source, adopted.delivery, adopted.deliveries, adopted.send_error) == ("local", "delivered", 1, None)
    assert adopted.is_writable and not adopted.is_draft
    # Editable and deletable like anything the reviewer sent from here.
    assert store.upsert(_payload("gh-11", body="edited")).delivery == "draft"
    store.send("gh-11")
    assert store.mark_delivered("gh-11") is not None
    tombstone = store.delete("gh-11")
    assert tombstone is not None and tombstone.node_id == "PRRC_11"


def test_adopt_refuses_a_taken_id_and_a_comment_without_a_node(run_dir: paths.RunDir) -> None:
    store = CommentStore(run_dir.comments)
    store.upsert(_payload("c1"))
    with pytest.raises(comments.CommentStateError, match="already exists"):
        store.adopt(comments.Comment(id="c1", file="a.py", side="new", line=3, body="x", node_id="N"))
    with pytest.raises(comments.CommentStateError, match="no node_id"):
        store.adopt(comments.Comment(id="c2", file="a.py", side="new", line=3, body="x"))
    with pytest.raises(comments.CommentStateError, match="only the reviewer's"):
        store.adopt(comments.Comment(id="c3", file="a.py", side="new", line=3, body="x", node_id="N", source="github"))


def test_mark_submitted_flips_what_the_review_held_and_leaves_drafts(run_dir: paths.RunDir) -> None:
    store = CommentStore(run_dir.comments)
    store.upsert(_payload("sent", line=1))
    store.upsert(_payload("draft", line=2))
    store.upsert(_payload("unsent", line=3))
    store.send("sent")
    store.mark_delivered("sent", node_id="PRRC_1", thread_id="PRRT_1")
    store.send("unsent")
    store.mark_send_failed("unsent", "HTTP 502")

    changed = store.mark_submitted()

    assert [c.id for c in changed] == ["sent"]
    by_id = _by_id(store)
    assert (by_id["sent"].source, by_id["sent"].node_id, by_id["sent"].thread_id) == ("github", "PRRC_1", "PRRT_1")
    assert not by_id["sent"].is_writable
    assert by_id["draft"].is_draft and by_id["unsent"].delivery == "sent"
    assert store.mark_submitted() == []
    # Survives a reload.
    assert _by_id(CommentStore(run_dir.comments))["sent"].source == "github"


def test_reset_to_draft_undoes_a_delivery_github_no_longer_holds(run_dir: paths.RunDir) -> None:
    store = CommentStore(run_dir.comments)
    store.upsert(_payload("c1"))
    store.send("c1")
    store.mark_delivered("c1", node_id="PRRC_1", thread_id="PRRT_1")

    draft = store.reset_to_draft("c1", notice="gone from GitHub")

    assert (draft.delivery, draft.deliveries, draft.node_id, draft.thread_id) == ("draft", 0, None, None)
    assert draft.notice == "gone from GitHub" and draft.is_draft and draft.send_error is None
    assert store.with_node_ids() == []
    # The next Send clears the notice.
    _, sent = store.send("c1")
    assert sent.notice is None
    with pytest.raises(comments.CommentNotFound):
        store.reset_to_draft("ghost", notice="x")


def test_with_node_ids_lists_what_github_was_told_about_tombstones_included(run_dir: paths.RunDir) -> None:
    store = CommentStore(run_dir.comments)
    store.upsert(_payload("delivered", line=1))
    store.upsert(_payload("draft", line=2))
    store.upsert(_payload("gone", line=3))
    store.send_all()
    store.mark_delivered("delivered", node_id="N1")
    store.mark_delivered("gone", node_id="N3")
    store.delete("gone")
    assert [c.id for c in store.with_node_ids()] == ["delivered", "gone"]
    assert store.find("gone") is not None and store.find("gone").withdrawn  # type: ignore[union-attr]
    assert store.find("nope") is None


def test_mark_submitted_by_ids_takes_the_body_github_published(run_dir: paths.RunDir) -> None:
    store = CommentStore(run_dir.comments)
    store.upsert(_payload("kept", "local text", line=1))
    store.upsert(_payload("edited", "my edit", line=2))
    store.upsert(_payload("other", line=3))
    store.send_all()
    for cid, node in (("kept", "N1"), ("edited", "N2"), ("other", "N3")):
        store.mark_delivered(cid, node_id=node)

    changed = store.mark_submitted({"kept": None, "edited": "what GitHub has"})

    assert sorted(c.id for c in changed) == ["edited", "kept"]
    by_id = _by_id(store)
    assert by_id["kept"].source == "github" and by_id["kept"].body == "local text"
    assert by_id["edited"].source == "github" and by_id["edited"].body == "what GitHub has"
    assert by_id["other"].source == "local"


# --- the end of a review -----------------------------------------------------


def test_deliver_remaining_lists_drafts_and_sent_and_drops_tombstones(run_dir: paths.RunDir) -> None:
    store = CommentStore(run_dir.comments)
    store.upsert(_payload("draft", line=1))
    store.upsert(_payload("sent", line=2))
    store.send("sent")
    store.upsert(_payload("gone", line=3))
    store.send("gone")
    store.deliver(2)
    store.delete("gone")  # tombstone
    store.upsert(_payload("done", line=4))
    store.send("done")
    store.deliver(4)

    remaining = store.deliver_remaining()

    assert [c.id for c in remaining] == ["draft", "sent"]
    by_id = _by_id(store)
    assert all(by_id[i].delivery == "delivered" for i in ("draft", "sent", "done"))
    assert "gone" not in by_id
    assert store.pending_batches() == []
    assert store.deliver_remaining() == []


# --- persistence ------------------------------------------------------------


def test_the_lifecycle_survives_a_reload(run_dir: paths.RunDir) -> None:
    store = CommentStore(run_dir.comments)
    store.upsert(_payload("c1"))
    store.upsert(_payload("c2", line=5))
    store.send("c1")
    store.deliver(1)
    store.send("c2")

    reloaded = CommentStore(run_dir.comments)

    by_id = _by_id(reloaded)
    assert by_id["c1"].delivery == "delivered" and by_id["c1"].deliveries == 1
    assert by_id["c2"].delivery == "sent" and by_id["c2"].batch_no == 2
    assert reloaded.last_batch_no == 2
    assert [(n, [c.id for c in cs]) for n, cs in reloaded.pending_batches()] == [(2, ["c2"])]


def test_batch_numbers_never_restart(run_dir: paths.RunDir) -> None:
    store = CommentStore(run_dir.comments)
    store.upsert(_payload("c1"))
    store.send("c1")
    store.delete("c1")  # every trace of batch 1 is gone
    assert CommentStore(run_dir.comments).last_batch_no == 1
    store.upsert(_payload("c2"))
    assert store.send("c2")[0] == 2


# --- the batch markdown ------------------------------------------------------


def test_excerpt_marks_the_anchor_with_two_lines_either_side() -> None:
    text = "\n".join(f"line {n}" for n in range(1, 11)) + "\n"
    assert comments.excerpt(text, 5) == "\n".join(
        ["  3 | line 3", "  4 | line 4", "> 5 | line 5", "  6 | line 6", "  7 | line 7"]
    )
    assert comments.excerpt(text, 1) == "\n".join(["> 1 | line 1", "  2 | line 2", "  3 | line 3"])
    assert comments.excerpt(text, 11) is None
    assert comments.excerpt(None, 1) is None


def test_format_batch_markdown_carries_run_batch_and_every_field() -> None:
    root = comments.Comment(id="c1", file="a.py", side="new", line=5, body="why?\n\nsecond para")
    reply = comments.Comment(id="c2", file="a.py", side="new", line=5, body="because", in_reply_to_id="c1")
    md = comments.format_batch_markdown(
        3,
        [
            {"comment": root.model_dump(), "state": "revised", "excerpt": "> 5 | x = 1"},
            {"comment": reply.model_dump(), "state": "reply", "excerpt": None},
        ],
        run_slug="local-main-abc",
    )
    assert md.startswith("# Batch 3 for local-main-abc\n")
    assert "## c1 — revised — a.py:5 (new)\n" in md
    assert "> why?\n>\n> second para\n" in md
    assert "```\n> 5 | x = 1\n```" in md
    assert "## c2 — reply — a.py:5 (new) — in reply to c1\n" in md
    assert md.count("```") == 2  # no fence for the entry without an excerpt
    assert "_2 comments in this batch._" in md
