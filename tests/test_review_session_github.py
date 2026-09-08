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

PENDING_URL = "https://gh/o/r/pull/7#pullrequestreview-1"
EMPTY_STATE = {"unsent": [], "submitted_url": None, "submitted_from": None, "unanchored": 0}


class FakeSink:
    """A `ReviewSink` that hands out ids and records every call.

    `refuse` holds the comment ids (or `"submit"`, `"resolve"`,
    `"reconcile"`) whose next call GitHub refuses; `not_found` the ones
    it refuses as not knowing the node, one entry per refusal. `pending` is what `resume`
    returns. GitHub's side of a reconciliation is `on_github`: per node
    id, a `CommentStanding` or None (gone); a node id the fake issued and
    was not told otherwise about is pending.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.refuse: set[str] = set()
        self.not_found: list[str] = []
        self.pending: pending_review.Resumed | None = None
        self.resume_refused = False
        self.on_github: dict[str, github_graphql.CommentStanding | None] = {}
        self.issued: set[str] = set()
        self._n = 0

    def _next(self, prefix: str) -> str:
        self._n += 1
        node = f"{prefix}{self._n}"
        if prefix == "C":
            self.issued.add(node)
        return node

    def _refusal(self, key: str) -> None:
        if key in self.not_found:
            self.not_found.remove(key)
            raise github_graphql.GitHubRefused(f"Could not resolve to a node for {key}", not_found=True)
        if key in self.refuse:
            self.refuse.discard(key)
            raise github_graphql.GitHubRefused(f"HTTP 502 for {key}")

    def reconcile(self, node_ids: list[str]) -> dict[str, github_graphql.CommentStanding | None]:
        self.calls.append(("reconcile", list(node_ids)))
        self._refusal("reconcile")
        out: dict[str, github_graphql.CommentStanding | None] = {}
        for node in node_ids:
            if node in self.on_github:
                out[node] = self.on_github[node]
            elif node in self.issued:
                out[node] = github_graphql.CommentStanding(review_id="PRR_fake", review_state="PENDING", review_url="")
            else:
                out[node] = None
        return out

    def resume(self, node_index: dict[str, str]) -> pending_review.Resumed | None:
        self.calls.append(("resume", dict(node_index)))
        if self.resume_refused:
            raise github_graphql.GitHubRefused("HTTP 401")
        if self.pending is not None:
            # What the pending review yielded is on GitHub, pending.
            self.issued.update(c.node_id for c in self.pending.comments if c.node_id)
            self.issued.update(node for node, cid in node_index.items() if cid in self.pending.reclaimed)
        return self.pending

    def deliver(self, c: Comment, by_id: dict[str, Comment]) -> pending_review.Delivered:
        kind = "delete" if c.withdrawn else "update" if c.node_id else "reply" if c.in_reply_to_id else "thread"
        self.calls.append((kind, c.id, c.body))
        self._refusal(c.id)
        if kind == "delete":
            self.issued.discard(c.node_id or "")
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
            self.refuse.discard("submit")
            raise github_graphql.GitHubRefused("HTTP 422: review body required")
        self._refusal("submit")
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

    def gone(self, cid: str) -> None:
        """GitHub no longer knows this comment's pending twin."""
        node = self.session.store.find(cid)
        assert node is not None and node.node_id
        self.sink.on_github[node.node_id] = None

    def published(self, cid: str, *, body: str | None = None) -> None:
        """The review holding this comment was submitted from GitHub's web UI."""
        node = self.session.store.find(cid)
        assert node is not None and node.node_id
        self.sink.on_github[node.node_id] = github_graphql.CommentStanding(
            review_id="PRR_web",
            review_state="APPROVED",
            review_url=PENDING_URL,
            body=body if body is not None else node.body,
        )

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
    assert data["pending_review"] == EMPTY_STATE


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
    assert response["pending_review"] == EMPTY_STATE
    # The tab sees it go sent, then delivered; the review's state follows.
    assert h.comment_frames("c1") == [("draft", None), ("sent", None), ("delivered", None)]
    assert h.frames.payloads("pending-review") == [EMPTY_STATE]


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

    # The interval retry reconciles, then has nothing to deliver.
    state = h.session.retry_deliveries()
    assert state["unsent"] == [] and h.kinds()[3:] == ["reconcile"]


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

    # Adopted, then what the store now believes is reconciled.
    assert h.sink.calls == [("resume", {}), ("reconcile", ["C11"])]
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

    assert h.sink.calls == [("resume", {"C_own": "c-own"}), ("reconcile", ["C_own"])]
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
    assert h.sink.calls == [("reconcile", []), ("submit", "APPROVE", "")]
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


# --- reconciliation ---------------------------------------------------------
# GitHub is authoritative for its draft. Whenever there is a sign the
# store's view diverged — a NOT_FOUND refusal, the chooser opening, a
# Submit, the retry tick — the store re-derives it from GitHub.

REMOVED = "removed from your pending review on GitHub — Send again to re-add"


def _delivered(h: _Harness, *cids: str) -> None:
    for cid in cids:
        h.session.upsert_comment(_note(cid, line=len(cid) * 7))
    h.session.send_all()
    assert all(h.by_id()[c].delivery == "delivered" for c in cids)


def test_reconcile_leaves_a_comment_still_pending_alone(run_dir: paths.RunDir) -> None:
    h = _Harness(run_dir)
    _delivered(h, "c1")
    before = h.by_id()["c1"]

    result = h.session.reconcile()

    assert h.sink.calls[-1] == ("reconcile", ["C1"])
    assert result["outcomes"] == {"c1": "pending"}
    assert h.by_id()["c1"] == before
    assert result["submitted_from"] is None


def test_reconcile_returns_a_comment_deleted_on_github_to_draft_with_a_notice(run_dir: paths.RunDir) -> None:
    h = _Harness(run_dir)
    _delivered(h, "c1", "c2")
    h.gone("c1")

    result = h.session.reconcile()

    assert result["outcomes"] == {"c1": "removed", "c2": "pending"}
    c1 = h.by_id()["c1"]
    assert (c1.delivery, c1.deliveries, c1.node_id, c1.thread_id, c1.send_error) == ("draft", 0, None, None, None)
    assert c1.notice == REMOVED and c1.is_draft
    assert h.frames.payloads("comment")[-1]["notice"] == REMOVED
    assert result["unsent"] == [], "a draft is not unsent; it does not block Submit"
    # Sending it again re-adds it, and the notice goes.
    h.session.send_comment("c1")
    again = h.by_id()["c1"]
    assert (again.delivery, again.notice) == ("delivered", None)
    assert again.node_id is not None and again.node_id != "C1"


def test_reconcile_completes_a_deletion_whose_target_is_already_gone(run_dir: paths.RunDir) -> None:
    h = _Harness(run_dir)
    _delivered(h, "c1")
    h.sink.refuse.add("c1")
    h.session.delete_comment("c1")
    assert h.session.pending_review_state()["unsent"][0]["deleted"] is True
    h.gone("c1")

    result = h.session.reconcile()

    assert result["outcomes"] == {"c1": "deleted"}
    assert h.session.store.find("c1") is None and result["unsent"] == []


def test_reconcile_finds_the_review_published_from_github(run_dir: paths.RunDir) -> None:
    h = _Harness(run_dir)
    _delivered(h, "c1", "c2")
    h.session.upsert_comment(_note("draft", line=99))
    h.published("c1")
    h.published("c2", body="as GitHub has it")

    result = h.session.reconcile()

    assert result["outcomes"] == {"c1": "submitted", "c2": "submitted"}
    assert (result["submitted_url"], result["submitted_from"]) == (PENDING_URL, "github")
    by_id = h.by_id()
    assert by_id["c1"].source == "github" and not by_id["c1"].is_writable
    assert by_id["c2"].body == "as GitHub has it", "an edit that never landed does not read as if it had"
    assert by_id["draft"].is_draft
    assert h.frames.payloads("pending-review")[-1]["submitted_from"] == "github"
    # The sink forgot the pending review: the next Send opens a new one.
    h.session.send_comment("draft")
    assert h.by_id()["draft"].delivery == "delivered"


def test_reconcile_relays_a_github_failure_and_changes_nothing(run_dir: paths.RunDir) -> None:
    h = _Harness(run_dir)
    _delivered(h, "c1")
    h.sink.refuse.add("reconcile")
    err = _refused(h.session.reconcile)
    assert err.status == 502
    assert h.by_id()["c1"].delivery == "delivered"


def test_reconcile_is_for_the_github_counterpart_only(run_dir: paths.RunDir) -> None:
    from tests.test_review_session import _Harness as _ClaudeHarness

    assert _refused(_ClaudeHarness(run_dir).session.reconcile).status == 409


# --- the triggers -------------------------------------------------------------


def test_a_not_found_on_update_reconciles_and_the_comment_is_a_draft_again(run_dir: paths.RunDir) -> None:
    """The live case: the pending comment was deleted on GitHub's web UI,
    then edited here. The update meets NOT_FOUND; instead of unsent-and-
    retry-forever the comment returns to draft with the notice."""
    h = _Harness(run_dir)
    _delivered(h, "c1")
    h.gone("c1")
    h.sink.not_found.append("c1")

    edited = h.session.upsert_comment(_note("c1", "edited"))

    assert h.kinds() == ["thread", "update", "reconcile"]
    assert (edited["delivery"], edited["notice"], edited["send_error"], edited["body"]) == (
        "draft",
        REMOVED,
        None,
        "edited",
    )
    assert h.session.pending_review_state()["unsent"] == []


def test_a_not_found_on_delete_reconciles_and_the_deletion_is_complete(run_dir: paths.RunDir) -> None:
    h = _Harness(run_dir)
    _delivered(h, "c1")
    h.gone("c1")
    h.sink.not_found.append("c1")

    h.session.delete_comment("c1")

    assert h.kinds() == ["thread", "delete", "reconcile"]
    assert h.session.store.find("c1") is None
    assert h.session.pending_review_state()["unsent"] == []


def test_a_not_found_on_add_reconciles_and_adds_into_a_fresh_review(run_dir: paths.RunDir) -> None:
    """The cached pending review was discarded on the web: the add meets
    NOT_FOUND, the reconcile drops the stale id, the add is applied once
    more into a review created afresh."""
    h = _Harness(run_dir)
    h.session.upsert_comment(_note("c1"))
    h.sink.not_found.append("c1")

    response = h.session.send_comment("c1")

    assert h.kinds() == ["thread", "reconcile", "thread"]
    assert h.by_id()["c1"].delivery == "delivered"
    assert response["pending_review"]["unsent"] == []


def test_a_second_refusal_after_the_reconcile_is_unsent_not_a_loop(run_dir: paths.RunDir) -> None:
    h = _Harness(run_dir)
    h.session.upsert_comment(_note("c1"))
    h.sink.not_found.extend({"c1"})
    h.sink.refuse.add("c1")  # consumed by the re-apply, after the not_found

    h.session.send_comment("c1")

    assert h.kinds() == ["thread", "reconcile", "thread"]
    c1 = h.by_id()["c1"]
    assert c1.delivery == "sent" and c1.send_error == "HTTP 502 for c1"
    # Even a second NOT_FOUND on the re-apply is unsent, not another round.
    h.session.upsert_comment(_note("c2", line=9))
    h.sink.not_found.extend(["c2", "c2"])
    h.session.send_comment("c2")
    # The flush retries the unsent c1 first (it lands), then c2 twice.
    assert h.kinds()[3:] == ["thread", "thread", "reconcile", "thread"]
    assert h.by_id()["c2"].delivery == "sent" and "Could not resolve" in (h.by_id()["c2"].send_error or "")
    # The next tick reconciles, then delivers it.
    h.session.retry_deliveries()
    assert h.kinds()[7:] == ["reconcile", "thread"]
    assert all(c.delivery == "delivered" for c in h.by_id().values())


def test_one_flush_reconciles_once_for_every_refused_comment(run_dir: paths.RunDir) -> None:
    h = _Harness(run_dir)
    for cid, line in (("c1", 1), ("c2", 2)):
        h.session.upsert_comment(_note(cid, line=line))
    h.sink.not_found.extend({"c1", "c2"})

    h.session.send_all()

    assert h.kinds() == ["thread", "reconcile", "thread", "thread", "thread"]
    assert all(c.delivery == "delivered" for c in h.by_id().values())


def test_other_refusals_do_not_reconcile(run_dir: paths.RunDir) -> None:
    """Network, auth, rate limit: the comment stays unsent for the retry;
    GitHub's view is not re-derived over a failure that says nothing
    about it."""
    h = _Harness(run_dir)
    h.session.upsert_comment(_note("c1"))
    h.sink.refuse.add("c1")
    h.session.send_comment("c1")
    assert h.kinds() == ["thread"]
    assert h.by_id()["c1"].send_error == "HTTP 502 for c1"


def test_the_retry_tick_reconciles_first_so_it_self_heals(run_dir: paths.RunDir) -> None:
    h = _Harness(run_dir)
    _delivered(h, "c1")
    h.sink.refuse.add("c1")
    h.session.upsert_comment(_note("c1", "edited"))
    assert h.by_id()["c1"].send_error is not None
    h.gone("c1")

    state = h.session.retry_deliveries()

    assert h.kinds()[-2:] == ["update", "reconcile"] or h.kinds()[-1] == "reconcile"
    assert state["unsent"] == []
    assert h.by_id()["c1"].delivery == "draft" and h.by_id()["c1"].notice == REMOVED


def test_the_retry_tick_survives_github_being_unreachable(run_dir: paths.RunDir) -> None:
    h = _Harness(run_dir)
    h.session.upsert_comment(_note("c1"))
    h.sink.refuse.add("c1")
    h.session.send_comment("c1")
    h.sink.refuse.update({"reconcile", "c1"})
    state = h.session.retry_deliveries()
    assert [u["id"] for u in state["unsent"]] == ["c1"]


def test_resume_reconciles_what_the_store_already_believes(run_dir: paths.RunDir) -> None:
    h = _Harness(run_dir)
    _delivered(h, "c1")
    h.gone("c1")
    h = _Harness(run_dir)  # a restart of the run
    h.sink.on_github = {"C1": None}

    h.session.resume_pending_review()

    assert h.kinds() == ["resume", "reconcile"]
    assert h.by_id()["c1"].delivery == "draft" and h.by_id()["c1"].notice == REMOVED


# --- Submit after the reconcile -------------------------------------------------


def test_submit_refuses_when_the_review_was_published_from_github(run_dir: paths.RunDir) -> None:
    h = _Harness(run_dir)
    _delivered(h, "c1")
    h.published("c1")

    err = _refused(lambda: h.session.submit({"event": "COMMENT"}))

    assert err.status == 409
    assert err.body() == {"error": "the review was already submitted on GitHub", "submitted_url": PENDING_URL}
    assert not any(call[0] == "submit" for call in h.sink.calls)
    assert h.by_id()["c1"].source == "github"
    assert h.frames.payloads("pending-review")[-1]["submitted_from"] == "github"
    # Having seen that, a second Submit is a fresh review — an LGTM here.
    response = h.session.submit({"event": "APPROVE"})
    assert response["submitted"] == 0 and h.sink.calls[-1] == ("submit", "APPROVE", "")
    assert h.session.pending_review_state()["submitted_from"] == "viewer"


def test_submit_after_a_comment_vanished_publishes_what_is_left(run_dir: paths.RunDir) -> None:
    h = _Harness(run_dir)
    _delivered(h, "c1", "c2")
    h.gone("c1")

    response = h.session.submit({"event": "REQUEST_CHANGES", "body": "one nit"})

    assert response["submitted"] == 1
    by_id = h.by_id()
    assert by_id["c1"].is_draft and by_id["c1"].notice == REMOVED
    assert by_id["c2"].source == "github"


def test_submit_with_nothing_pending_after_a_reconcile_is_still_the_lgtm(run_dir: paths.RunDir) -> None:
    h = _Harness(run_dir)
    _delivered(h, "c1")
    h.gone("c1")
    response = h.session.submit({"event": "APPROVE"})
    assert response["submitted"] == 0
    assert h.kinds()[-2:] == ["reconcile", "submit"]


def test_a_not_found_on_submit_reconciles_and_submits_once_more(run_dir: paths.RunDir) -> None:
    """The pending review was discarded between the reconcile and the
    submit: one more look, one more submit into a fresh review."""
    h = _Harness(run_dir)
    h.sink.not_found.append("submit")
    response = h.session.submit({"event": "APPROVE"})
    assert h.kinds() == ["reconcile", "submit", "reconcile", "submit"]
    assert response["review_url"]


def test_a_not_found_on_submit_that_finds_a_publication_is_refused(run_dir: paths.RunDir) -> None:
    h = _Harness(run_dir)
    _delivered(h, "c1")
    h.sink.not_found.append("submit")
    # The review was published from the web while Submit was underway:
    # the first reconcile still saw it pending, the second does not.
    real_reconcile = h.sink.reconcile

    def flip_then(node_ids: list[str]) -> dict:
        out = real_reconcile(node_ids)
        h.published("c1")
        return out

    h.sink.reconcile = flip_then  # type: ignore[method-assign]
    err = _refused(lambda: h.session.submit({"event": "APPROVE"}))
    assert err.status == 409 and err.body()["submitted_url"] == PENDING_URL
    assert h.kinds() == ["thread", "reconcile", "submit", "reconcile"]


def test_a_submit_github_refuses_for_another_reason_is_relayed_once(run_dir: paths.RunDir) -> None:
    h = _Harness(run_dir)
    h.sink.refuse.add("submit")
    err = _refused(lambda: h.session.submit({"event": "COMMENT"}))
    assert err.status == 502 and h.kinds() == ["reconcile", "submit"]
