"""The review session with GitHub as the counterpart (ADR 0009, slice 2).

A Send delivers into the pending review at once; a refusal leaves the
comment unsent and retried; an edit updates in place; a deletion deletes;
an existing pending review is resumed; Submit publishes and is refused
while anything is unsent; an upstream thread is resolved on GitHub first.
The GitHub side is a fake `ReviewSink` that records what it was asked
and refuses on demand; the store and the fan-out are real.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from semantic_code_review import errors, paths
from semantic_code_review.review import github_graphql, pending_review
from semantic_code_review.review.comments import Comment, CommentStore
from semantic_code_review.review.session import ReviewSession
from tests.test_review_session import _Frames


class FakeSink:
    """A `ReviewSink` that hands out ids and records every call.

    `refuse` holds the comment ids (or `"submit"`, `"resolve"`) whose
    next call GitHub refuses; `pending` is what `resume` returns.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.refuse: set[str] = set()
        self.pending: pending_review.Resumed | None = None
        self.resume_refused = False
        self._n = 0

    def _next(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n}"

    def resume(self, node_index: dict[str, str]) -> pending_review.Resumed | None:
        self.calls.append(("resume", dict(node_index)))
        if self.resume_refused:
            raise github_graphql.GitHubRefused("HTTP 401")
        return self.pending

    def deliver(self, c: Comment, by_id: dict[str, Comment]) -> pending_review.Delivered:
        kind = "delete" if c.withdrawn else "update" if c.node_id else "reply" if c.in_reply_to_id else "thread"
        self.calls.append((kind, c.id, c.body))
        if c.id in self.refuse:
            self.refuse.discard(c.id)
            raise github_graphql.GitHubRefused(f"HTTP 502 for {c.id}")
        if kind == "delete":
            return pending_review.Delivered(node_id=None, thread_id=None)
        if kind == "update":
            return pending_review.Delivered(node_id=c.node_id, thread_id=c.thread_id)
        if kind == "reply":
            parent = by_id[c.in_reply_to_id or ""]
            return pending_review.Delivered(node_id=self._next("C"), thread_id=parent.thread_id)
        return pending_review.Delivered(node_id=self._next("C"), thread_id=self._next("T"))

    def submit(self, event: str, body: str) -> github_graphql.SubmittedReview:
        self.calls.append(("submit", event, body))
        if "submit" in self.refuse:
            raise github_graphql.GitHubRefused("HTTP 422: review body required")
        return github_graphql.SubmittedReview(
            node_id="PRR_s", database_id=9, url="https://gh/o/r/pull/7#pullrequestreview-9"
        )

    def set_thread_resolved(self, thread_id: str, resolved: bool) -> None:
        self.calls.append(("resolve", thread_id, resolved))
        if "resolve" in self.refuse:
            raise github_graphql.GitHubRefused("HTTP 502")


class _Harness:
    def __init__(self, run_dir: paths.RunDir) -> None:
        self.frames = _Frames()
        self.sink = FakeSink()
        self.session = ReviewSession(
            run_dir=run_dir,
            viewer_json={"version": "1", "files": []},
            store=CommentStore(run_dir.comments),
            publish=self.frames.publish,
            counterpart="github",
            github=self.sink,
        )

    def by_id(self) -> dict[str, Comment]:
        return {c.id: c for c in self.session.store.all()}

    def kinds(self) -> list[str]:
        return [call[0] for call in self.sink.calls]

    def comment_frames(self, cid: str) -> list[tuple[str, str | None]]:
        """`(delivery, send_error)` of every `comment` frame for `cid`."""
        return [(p["delivery"], p["send_error"]) for p in self.frames.payloads("comment") if p["id"] == cid]


def _note(cid: str, body: str = "note", **extra: Any) -> dict[str, Any]:
    return {"id": cid, "file": "a.py", "side": "new", "line": 3, "body": body, **extra}


def _refused(op) -> errors.ScrError:
    with pytest.raises(errors.ScrError) as excinfo:
        op()
    return excinfo.value


def _ingested(cid: str = "gh-1", **extra: Any) -> Comment:
    base: dict[str, Any] = {"source": "github", "node_id": "PRRC_1", "thread_id": "PRRT_1"}
    base.update(extra)
    return Comment(id=cid, file="a.py", side="new", line=3, body="theirs", **base)


def _seeded(run_dir: paths.RunDir, *seed: Comment) -> _Harness:
    """A harness over a `comments.json` holding `seed` — what the ingest
    leaves for a PR that already carries review comments."""
    run_dir.comments.write_text(json.dumps({"comments": [c.model_dump() for c in seed]}), encoding="utf-8")
    return _Harness(run_dir)


# --- wiring -----------------------------------------------------------------


def test_the_github_counterpart_needs_a_sink_and_only_it_takes_one(run_dir: paths.RunDir) -> None:
    store = CommentStore(run_dir.comments)
    with pytest.raises(ValueError, match="review sink"):
        ReviewSession(run_dir=run_dir, viewer_json={}, store=store, publish=_Frames().publish, counterpart="github")
    with pytest.raises(ValueError, match="review sink"):
        ReviewSession(
            run_dir=run_dir,
            viewer_json={},
            store=store,
            publish=_Frames().publish,
            counterpart="claude",
            github=FakeSink(),
        )


def test_data_json_carries_the_pending_review_state(run_dir: paths.RunDir) -> None:
    h = _Harness(run_dir)
    data = h.session.data_json()
    assert data["counterpart"] == "github"
    assert data["pending_review"] == {"unsent": [], "submitted_url": None, "unanchored": 0}


def test_there_is_no_stream_to_wait_on(run_dir: paths.RunDir) -> None:
    err = _refused(lambda: _Harness(run_dir).session.wait_for_batch(timeout=0))
    assert err.status == 409 and "GitHub" in str(err)


# --- send -------------------------------------------------------------------


def test_a_send_is_delivered_into_the_pending_review_and_records_its_ids(run_dir: paths.RunDir) -> None:
    h = _Harness(run_dir)
    h.session.upsert_comment(_note("c1"))

    response = h.session.send_comment("c1")

    assert h.kinds() == ["thread"]
    c1 = h.by_id()["c1"]
    assert (c1.delivery, c1.deliveries, c1.node_id, c1.thread_id, c1.send_error) == ("delivered", 1, "C1", "T2", None)
    assert response["comment_ids"] == ["c1"]
    assert response["pending_review"] == {"unsent": [], "submitted_url": None, "unanchored": 0}
    # The tab sees it go sent, then delivered; the review's state follows.
    assert h.comment_frames("c1") == [("draft", None), ("sent", None), ("delivered", None)]
    assert h.frames.payloads("pending-review") == [{"unsent": [], "submitted_url": None, "unanchored": 0}]


def test_a_refused_send_leaves_the_comment_unsent_and_names_it(run_dir: paths.RunDir) -> None:
    h = _Harness(run_dir)
    h.session.upsert_comment(_note("c1", "the note"))
    h.sink.refuse.add("c1")

    response = h.session.send_comment("c1")

    c1 = h.by_id()["c1"]
    assert (c1.delivery, c1.node_id, c1.send_error) == ("sent", None, "HTTP 502 for c1")
    assert response["pending_review"]["unsent"] == [
        {
            "id": "c1",
            "file": "a.py",
            "side": "new",
            "line": 3,
            "body": "the note",
            "deleted": False,
            "error": "HTTP 502 for c1",
        }
    ]
    assert h.comment_frames("c1")[-1] == ("sent", "HTTP 502 for c1")
    assert h.session.data_json()["pending_review"]["unsent"][0]["id"] == "c1"


def test_an_unsent_comment_is_retried_on_the_next_send_and_by_retry(run_dir: paths.RunDir) -> None:
    h = _Harness(run_dir)
    h.session.upsert_comment(_note("c1"))
    h.session.upsert_comment(_note("c2", line=9))
    h.sink.refuse.add("c1")
    h.session.send_comment("c1")
    assert h.by_id()["c1"].send_error is not None

    # The next Send flushes everything sent, the refused one included.
    h.session.send_comment("c2")
    assert h.kinds() == ["thread", "thread", "thread"]
    assert h.by_id()["c1"].delivery == "delivered" and h.by_id()["c2"].delivery == "delivered"

    # And the interval retry, with nothing to do, touches GitHub not at all.
    state = h.session.retry_deliveries()
    assert state["unsent"] == [] and len(h.sink.calls) == 3


def test_send_all_delivers_each_draft_on_its_own(run_dir: paths.RunDir) -> None:
    h = _Harness(run_dir)
    for cid, line in (("c1", 1), ("c2", 2), ("c3", 3)):
        h.session.upsert_comment(_note(cid, line=line))
    h.sink.refuse.add("c2")

    response = h.session.send_all()

    assert response["comment_ids"] == ["c1", "c2", "c3"]
    assert [u["id"] for u in response["pending_review"]["unsent"]] == ["c2"]
    by_id = h.by_id()
    assert by_id["c1"].delivery == "delivered" and by_id["c3"].delivery == "delivered"
    assert by_id["c2"].delivery == "sent" and by_id["c2"].send_error
    assert h.session.send_all() == {"batch_no": None, "comment_ids": []}


def test_a_reply_to_an_upstream_comment_is_delivered_as_a_reply(run_dir: paths.RunDir) -> None:
    h = _seeded(run_dir, _ingested())
    h.session.upsert_comment(_note("r1", "ack", in_reply_to_id="gh-1"))

    h.session.send_comment("r1")

    assert h.kinds() == ["reply"]
    r1 = h.by_id()["r1"]
    assert (r1.delivery, r1.node_id, r1.thread_id) == ("delivered", "C1", "PRRT_1")


# --- edit and delete --------------------------------------------------------


def test_an_edit_of_a_delivered_comment_updates_it_in_place_and_stays_delivered(run_dir: paths.RunDir) -> None:
    h = _Harness(run_dir)
    h.session.upsert_comment(_note("c1"))
    h.session.send_comment("c1")

    edited = h.session.upsert_comment(_note("c1", "edited"))

    assert h.kinds() == ["thread", "update"]
    assert h.sink.calls[1] == ("update", "c1", "edited")
    assert (edited["delivery"], edited["deliveries"], edited["node_id"], edited["body"]) == (
        "delivered",
        2,
        "C1",
        "edited",
    )
    assert h.comment_frames("c1")[-2:] == [("sent", None), ("delivered", None)]


def test_a_refused_edit_leaves_the_comment_unsent_until_retried(run_dir: paths.RunDir) -> None:
    h = _Harness(run_dir)
    h.session.upsert_comment(_note("c1"))
    h.session.send_comment("c1")
    h.sink.refuse.add("c1")

    edited = h.session.upsert_comment(_note("c1", "edited"))

    assert (edited["delivery"], edited["send_error"], edited["node_id"]) == ("sent", "HTTP 502 for c1", "C1")
    assert h.session.pending_review_state()["unsent"][0]["body"] == "edited"
    h.session.retry_deliveries()
    assert h.kinds()[-1] == "update" and h.by_id()["c1"].delivery == "delivered"


def test_an_edit_of_a_draft_touches_github_not_at_all(run_dir: paths.RunDir) -> None:
    h = _Harness(run_dir)
    h.session.upsert_comment(_note("c1"))
    edited = h.session.upsert_comment(_note("c1", "edited"))
    assert edited["delivery"] == "draft" and h.sink.calls == []


def test_deleting_a_delivered_comment_deletes_the_pending_comment(run_dir: paths.RunDir) -> None:
    h = _Harness(run_dir)
    h.session.upsert_comment(_note("c1"))
    h.session.send_comment("c1")

    assert h.session.delete_comment("c1") == {"ok": True, "withdrawn": True}

    assert h.kinds() == ["thread", "delete"]
    assert h.by_id() == {} and h.session.store.undelivered() == []
    assert h.frames.payloads("comment-deleted") == [{"id": "c1"}]


def test_a_refused_deletion_keeps_the_tombstone_and_retries(run_dir: paths.RunDir) -> None:
    h = _Harness(run_dir)
    h.session.upsert_comment(_note("c1", "gone"))
    h.session.send_comment("c1")
    h.sink.refuse.add("c1")

    h.session.delete_comment("c1")

    assert h.by_id() == {}, "the row is gone from the viewer"
    (unsent,) = h.session.pending_review_state()["unsent"]
    assert (unsent["id"], unsent["deleted"], unsent["body"], unsent["error"]) == ("c1", True, "gone", "HTTP 502 for c1")
    # No `comment` frame resurrects a deleted row.
    assert h.comment_frames("c1")[-1] == ("delivered", None)

    h.session.retry_deliveries()
    assert h.kinds()[-1] == "delete" and h.session.pending_review_state()["unsent"] == []


def test_deleting_a_draft_or_an_unsent_comment_deletes_nothing_upstream(run_dir: paths.RunDir) -> None:
    h = _Harness(run_dir)
    h.session.upsert_comment(_note("c1"))
    h.session.upsert_comment(_note("c2", line=9))
    h.sink.refuse.add("c2")
    h.session.send_comment("c2")
    assert h.session.delete_comment("c1") == {"ok": True, "withdrawn": False}
    assert h.session.delete_comment("c2") == {"ok": True, "withdrawn": False}
    assert h.kinds() == ["thread"] and h.session.pending_review_state()["unsent"] == []


# --- resume -----------------------------------------------------------------


def test_resume_adopts_the_pending_review_as_delivered_comments(run_dir: paths.RunDir) -> None:
    h = _Harness(run_dir)
    h.session.upsert_comment(_note("mine"))
    h.sink.pending = pending_review.Resumed(
        comments=[
            Comment(id="gh-11", file="a.py", side="new", line=5, body="from before", node_id="C11", commit_id="abc")
        ],
        reclaimed=[],
        unanchored=2,
    )

    h.session.resume_pending_review()

    assert h.sink.calls == [("resume", {})]
    adopted = h.by_id()["gh-11"]
    assert (adopted.source, adopted.delivery, adopted.deliveries, adopted.node_id) == ("local", "delivered", 1, "C11")
    assert adopted.is_writable
    assert h.by_id()["mine"].is_draft
    assert h.session.data_json()["pending_review"]["unanchored"] == 2


def test_resume_reclaims_a_pending_comment_the_ingest_recorded_as_upstream(run_dir: paths.RunDir) -> None:
    h = _seeded(run_dir, _ingested("c-own", node_id="C_own"))
    assert not h.by_id()["c-own"].is_writable
    h.sink.pending = pending_review.Resumed(comments=[], reclaimed=["c-own"], unanchored=0)

    h.session.resume_pending_review()

    assert h.sink.calls == [("resume", {"C_own": "c-own"})]
    assert h.by_id()["c-own"].is_writable and h.by_id()["c-own"].delivery == "delivered"


def test_resume_with_nothing_pending_or_a_refusal_changes_nothing(run_dir: paths.RunDir, caplog) -> None:
    h = _Harness(run_dir)
    h.session.resume_pending_review()
    h.sink.resume_refused = True
    h.session.resume_pending_review()
    assert h.by_id() == {} and "could not look for a pending review" in caplog.text
    # The first Send then looks again, through the sink.
    h.session.upsert_comment(_note("c1"))
    h.session.send_comment("c1")
    assert h.kinds() == ["resume", "resume", "thread"]


# --- submit -----------------------------------------------------------------


def test_submit_is_refused_while_anything_is_unsent_and_names_it(run_dir: paths.RunDir) -> None:
    h = _Harness(run_dir)
    h.session.upsert_comment(_note("c1", "fine"))
    h.session.upsert_comment(_note("c2", "stuck", line=9))
    h.sink.refuse.add("c2")
    h.session.send_all()

    err = _refused(lambda: h.session.submit({"event": "APPROVE"}))

    assert err.status == 409
    body = err.body()
    assert "1 comment is unsent" in body["error"]
    assert [(u["id"], u["body"], u["line"]) for u in body["unsent"]] == [("c2", "stuck", 9)]
    assert not any(call[0] == "submit" for call in h.sink.calls)


def test_submit_publishes_and_the_comments_become_upstream(run_dir: paths.RunDir) -> None:
    h = _Harness(run_dir)
    h.session.upsert_comment(_note("c1", "fine"))
    h.session.upsert_comment(_note("draft", "kept", line=9))
    h.session.send_comment("c1")

    response = h.session.submit({"event": "REQUEST_CHANGES", "body": "please"})

    assert h.sink.calls[-1] == ("submit", "REQUEST_CHANGES", "please")
    assert response == {
        "review_url": "https://gh/o/r/pull/7#pullrequestreview-9",
        "event": "REQUEST_CHANGES",
        "submitted": 1,
    }
    by_id = h.by_id()
    assert by_id["c1"].source == "github" and not by_id["c1"].is_writable and by_id["c1"].node_id == "C1"
    assert by_id["draft"].is_draft
    assert h.frames.payloads("comment")[-1]["source"] == "github"
    state = h.frames.payloads("pending-review")[-1]
    assert state["submitted_url"] == "https://gh/o/r/pull/7#pullrequestreview-9"
    assert h.session.data_json()["pending_review"]["submitted_url"] == state["submitted_url"]


def test_approve_with_no_comments_is_the_lgtm(run_dir: paths.RunDir) -> None:
    h = _Harness(run_dir)
    response = h.session.submit({"event": "APPROVE"})
    assert h.sink.calls == [("submit", "APPROVE", "")]
    assert response["submitted"] == 0 and response["review_url"]


def test_a_refused_submit_changes_nothing(run_dir: paths.RunDir) -> None:
    h = _Harness(run_dir)
    h.session.upsert_comment(_note("c1"))
    h.session.send_comment("c1")
    h.sink.refuse.add("submit")
    err = _refused(lambda: h.session.submit({"event": "COMMENT"}))
    assert err.status == 502 and "review body required" in str(err)
    assert h.by_id()["c1"].source == "local"
    assert h.session.pending_review_state()["submitted_url"] is None


@pytest.mark.parametrize("payload", [{}, {"event": "SHIP_IT"}, {"event": "APPROVE", "body": 3}])
def test_submit_rejects_a_malformed_payload(run_dir: paths.RunDir, payload: dict) -> None:
    h = _Harness(run_dir)
    assert _refused(lambda: h.session.submit(payload)).status == 400
    assert h.sink.calls == []


def test_after_a_submit_the_next_send_opens_a_new_pending_review(run_dir: paths.RunDir) -> None:
    h = _Harness(run_dir)
    h.session.submit({"event": "APPROVE"})
    h.session.upsert_comment(_note("later"))
    h.session.send_comment("later")
    assert h.by_id()["later"].delivery == "delivered"
    assert h.session.pending_review_state()["submitted_url"] is not None


# --- resolve ----------------------------------------------------------------


def test_resolving_an_upstream_thread_fires_on_github_then_records_it(run_dir: paths.RunDir) -> None:
    h = _seeded(run_dir, _ingested())
    h.session.upsert_comment(_note("r1", "reply", in_reply_to_id="gh-1"))

    result = h.session.set_thread_resolved("r1", True)

    assert h.sink.calls == [("resolve", "PRRT_1", True)]
    assert result == {"ok": True, "resolved": True, "comment_ids": ["gh-1", "r1"]}
    assert all(c.thread_resolved for c in h.by_id().values())
    assert h.session.set_thread_resolved("gh-1", False)["comment_ids"] == ["gh-1", "r1"]
    assert h.sink.calls[-1] == ("resolve", "PRRT_1", False)


def test_a_refused_resolve_changes_nothing_locally(run_dir: paths.RunDir) -> None:
    h = _seeded(run_dir, _ingested())
    h.sink.refuse.add("resolve")
    err = _refused(lambda: h.session.set_thread_resolved("gh-1", True))
    assert err.status == 502
    assert not h.by_id()["gh-1"].thread_resolved
    assert h.frames.payloads("comment") == []


def test_a_pending_thread_cannot_be_resolved_on_github(run_dir: paths.RunDir) -> None:
    h = _Harness(run_dir)
    h.session.upsert_comment(_note("c1"))
    h.session.send_comment("c1")
    err = _refused(lambda: h.session.set_thread_resolved("c1", True))
    assert err.status == 409 and "pending review" in str(err)
    assert not any(call[0] == "resolve" for call in h.sink.calls)


def test_an_upstream_thread_without_a_recorded_id_is_refused(run_dir: paths.RunDir) -> None:
    h = _seeded(run_dir, _ingested(thread_id=None))
    err = _refused(lambda: h.session.set_thread_resolved("gh-1", True))
    assert err.status == 409 and "thread id" in str(err)
    assert h.sink.calls == []
