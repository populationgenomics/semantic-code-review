"""`PendingReview`: the GraphQL primitives composed into what the session
asks of GitHub in PR mode — resume, deliver one comment by its state,
submit, resolve. Against the fake `gh` fixture."""

from __future__ import annotations

import datetime
import json

import pytest

from semantic_code_review import paths
from semantic_code_review.review import github_graphql as gql
from semantic_code_review.review import pending_review
from semantic_code_review.review.comments import Comment
from tests.conftest import GhFailure, GhSequence
from tests.test_github_graphql import _pending_comment, _pending_comments, _state, _thread

Review = pending_review.PendingReview

DIFF = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,3 +1,4 @@\n a\n-b\n+B\n+c\n"


@pytest.fixture
def review(run_dir: paths.RunDir) -> pending_review.PendingReview:
    run_dir.raw_diff.write_text(DIFF, encoding="utf-8")
    return pending_review.PendingReview("o/r", 7, run_dir, head_sha="head1234")


def _local(cid: str, **kw: object) -> Comment:
    base: dict = {"id": cid, "file": "a.py", "side": "new", "line": 3, "body": f"note {cid}", "delivery": "sent"}
    base.update(kw)
    return Comment(**base)


_DELETED = {"data": {"deletePullRequestReviewComment": {"pullRequestReview": None}}}


def _created(gh: GhSequence, review_id: str = "PRR_new") -> None:
    gh.expect("query", _state())
    gh.expect("addPullRequestReview", {"data": {"addPullRequestReview": {"pullRequestReview": {"id": review_id}}}})


# --- resume -----------------------------------------------------------------


def test_resume_without_a_pending_review_is_none(gh: GhSequence, review: Review) -> None:
    gh.expect("query", _state())
    assert review.resume({}) is None
    assert review.review_id is None
    assert gh.ops() == ["query"]


def test_resume_adopts_the_pending_comments_as_delivered_local_ones(gh: GhSequence, review: Review) -> None:
    gh.expect("query", _state(pending=[("PRR_p", "alice")]))
    gh.expect(
        "pendingReviewComments",
        _pending_comments(
            [
                _pending_comment("C1", 11, diffHunk="@@\n-b"),
                _pending_comment("C2", 12, line=4, replyTo={"id": "C1"}),
                _pending_comment("C3", 13, line=2, replyTo={"id": "PRRC_ingested"}),
                _pending_comment("C4", 14, line=None, originalLine=None),
            ]
        ),
    )

    resumed = review.resume({"PRRC_ingested": "gh-1"})

    assert resumed is not None and review.review_id == "PRR_p"
    assert resumed.unanchored == 1
    by_id = {c.id: c for c in resumed.comments}
    assert list(by_id) == ["gh-11", "gh-12", "gh-13"]
    c1 = by_id["gh-11"]
    assert (c1.source, c1.delivery, c1.deliveries, c1.node_id) == ("local", "delivered", 1, "C1")
    assert (c1.file, c1.side, c1.line, c1.body, c1.commit_id) == ("a.py", "old", 3, "body of C1", "c0ffee")
    assert c1.created_at == datetime.datetime(2026, 9, 1, 10, 0, tzinfo=datetime.UTC).timestamp()
    assert c1.updated_at == c1.created_at + 300
    assert c1.body_html is None and c1.author is None
    # A pending reply threads under the store's copy of its parent —
    # another pending comment, or an ingested one by its node id.
    assert by_id["gh-12"].in_reply_to_id == "gh-11"
    assert by_id["gh-13"].in_reply_to_id == "gh-1"


def test_resume_skips_what_the_store_already_holds(gh: GhSequence, review: Review) -> None:
    gh.expect("query", _state(pending=[("PRR_p", "alice")]))
    gh.expect("pendingReviewComments", _pending_comments([_pending_comment("C1", 11), _pending_comment("C2", 12)]))
    resumed = review.resume({"C1": "c-known"})
    assert resumed is not None
    assert [c.id for c in resumed.comments] == ["gh-12"]
    assert resumed.reclaimed == ["c-known"]


# --- reconcile --------------------------------------------------------------


def test_reconcile_reports_each_standing_and_takes_githubs_pending_review(gh: GhSequence, review: Review) -> None:
    gh.expect("query", _state(pending=[("PRR_p", "alice")]))
    gh.expect("addPullRequestReviewThread", _thread("TH1", "C1"))
    c = _local("c1")
    review.deliver(c, {"c1": c})
    assert review.review_id == "PRR_p"

    response = _state()  # the pending review was discarded on the web
    response["data"]["nodes"] = [
        None,
        {"id": "C2", "body": "b", "pullRequestReview": {"id": "PRR_x", "state": "APPROVED", "url": "u"}},
    ]
    response["errors"] = [{"type": "NOT_FOUND", "message": "Could not resolve to a node"}]
    gh.expect("query", response)

    standing = review.reconcile(["C1", "C2"])

    assert standing["C1"] is None
    assert standing["C2"] == gql.CommentStanding(review_id="PRR_x", review_state="APPROVED", review_url="u", body="b")
    assert review.review_id is None, "the cached id is dropped; the next delivery creates afresh"


# --- deliver ----------------------------------------------------------------


def test_the_first_delivery_creates_the_review_and_opens_a_thread(gh: GhSequence, review: Review) -> None:
    _created(gh)
    gh.expect("addPullRequestReviewThread", _thread("TH1", "C1"))
    c = _local("c1")

    delivered = review.deliver(c, {"c1": c})

    assert delivered == pending_review.Delivered(node_id="C1", thread_id="TH1")
    assert gh.ops() == ["query", "addPullRequestReview", "addPullRequestReviewThread"]
    assert gh.variables("addPullRequestReviewThread") == [
        {"rid": "PRR_new", "path": "a.py", "line": "3", "side": "RIGHT", "body": "note c1"}
    ]
    assert review.review_id == "PRR_new"

    # The second delivery reuses the review: no lookup, no create.
    gh.expect("addPullRequestReviewThread", _thread("TH2", "C2"))
    c2 = _local("c2", line=4, side="old")
    review.deliver(c2, {"c1": c, "c2": c2})
    assert gh.ops()[3:] == ["addPullRequestReviewThread"]
    assert gh.variables("addPullRequestReviewThread")[1]["side"] == "LEFT"


def test_an_existing_pending_review_is_reused_not_recreated(gh: GhSequence, review: Review) -> None:
    gh.expect("query", _state(pending=[("PRR_p", "alice")]))
    gh.expect("addPullRequestReviewThread", _thread("TH1", "C1"))
    c = _local("c1")
    review.deliver(c, {"c1": c})
    assert "addPullRequestReview" not in gh.ops()
    assert gh.variables("addPullRequestReviewThread")[0]["rid"] == "PRR_p"


def test_a_line_outside_the_diff_is_moved_into_it_and_the_body_says_so(gh: GhSequence, review: Review) -> None:
    _created(gh)
    gh.expect("addPullRequestReviewThread", _thread("TH1", "C1"))
    c = _local("c1", line=9)
    review.deliver(c, {"c1": c})
    (vars_,) = gh.variables("addPullRequestReviewThread")
    assert vars_["line"] == "4"
    assert "moved to 4" in vars_["body"]


def test_a_reply_is_added_under_its_parents_node_and_inherits_the_thread(gh: GhSequence, review: Review) -> None:
    gh.expect("query", _state(pending=[("PRR_p", "alice")]))
    gh.expect("addPullRequestReviewComment", {"data": {"addPullRequestReviewComment": {"comment": {"id": "C9"}}}})
    parent = Comment(
        id="gh-1", file="a.py", side="new", line=3, body="up", source="github", node_id="PRRC_p", thread_id="PRRT_p"
    )
    reply = _local("r1", in_reply_to_id="gh-1")

    delivered = review.deliver(reply, {"gh-1": parent, "r1": reply})

    assert delivered == pending_review.Delivered(node_id="C9", thread_id="PRRT_p")
    assert gh.variables("addPullRequestReviewComment") == [{"rid": "PRR_p", "reply_to": "PRRC_p", "body": "note r1"}]


def test_a_reply_to_a_parent_github_lacks_is_refused_before_any_call(gh: GhSequence, review: Review) -> None:
    parent = _local("c1", delivery="draft")
    reply = _local("r1", in_reply_to_id="c1")
    with pytest.raises(gql.GitHubRefused, match="has not reached GitHub"):
        review.deliver(reply, {"c1": parent, "r1": reply})
    assert gh.calls == []


def test_an_edit_updates_the_pending_comment_in_place(gh: GhSequence, review: Review) -> None:
    """A comment GitHub holds is addressed by its node id; no review lookup."""
    gh.expect(
        "updatePullRequestReviewComment",
        {"data": {"updatePullRequestReviewComment": {"pullRequestReviewComment": {"id": "C1"}}}},
    )
    c = _local("c1", body="edited", node_id="C1", thread_id="TH1", deliveries=1)
    assert review.deliver(c, {"c1": c}) == pending_review.Delivered(node_id="C1", thread_id="TH1")
    assert gh.ops() == ["updatePullRequestReviewComment"]
    assert gh.variables("updatePullRequestReviewComment") == [{"cid": "C1", "body": "edited"}]


def test_an_edit_keeps_the_relocation_note(gh: GhSequence, review: Review) -> None:
    gh.expect(
        "updatePullRequestReviewComment",
        {"data": {"updatePullRequestReviewComment": {"pullRequestReviewComment": {"id": "C1"}}}},
    )
    c = _local("c1", line=9, body="edited", node_id="C1", deliveries=1)
    review.deliver(c, {"c1": c})
    body = gh.variables("updatePullRequestReviewComment")[0]["body"]
    assert body.startswith("edited\n\n_(originally anchored at line 9")


def test_a_withdrawal_deletes_the_pending_comment(gh: GhSequence, review: Review) -> None:
    gh.expect("deletePullRequestReviewComment", _DELETED)
    c = _local("c1", node_id="C1", withdrawn=True, deliveries=1)
    assert review.deliver(c, {"c1": c}) == pending_review.Delivered(node_id=None, thread_id=None)
    assert gh.variables("deletePullRequestReviewComment") == [{"cid": "C1"}]


def test_a_withdrawal_with_nothing_upstream_is_a_store_bug(gh: GhSequence, review: Review) -> None:
    c = _local("c1", withdrawn=True, deliveries=1)
    with pytest.raises(ValueError, match="no node_id"):
        review.deliver(c, {"c1": c})
    assert gh.calls == []


def test_a_refusal_propagates_untouched(gh: GhSequence, review: Review) -> None:
    _created(gh)
    gh.expect("addPullRequestReviewThread", GhFailure("HTTP 502"))
    c = _local("c1")
    with pytest.raises(gql.GitHubRefused, match="HTTP 502"):
        review.deliver(c, {"c1": c})
    # The review was created and is kept: the retry adds into it.
    assert review.review_id == "PRR_new"


# --- submit -----------------------------------------------------------------


def _submitted(gh: GhSequence, url: str = "https://gh/o/r/pull/7#pullrequestreview-5") -> None:
    gh.expect(
        "submitPullRequestReview",
        {"data": {"submitPullRequestReview": {"pullRequestReview": {"id": "PRR_x", "databaseId": 5, "url": url}}}},
    )


def test_submit_publishes_the_pending_review_and_forgets_it(gh: GhSequence, review: Review) -> None:
    gh.expect("query", _state(pending=[("PRR_p", "alice")]))
    _submitted(gh)
    result = review.submit("REQUEST_CHANGES", "please")
    assert result.url.endswith("pullrequestreview-5")
    assert gh.variables("submitPullRequestReview") == [{"rid": "PRR_p", "event": "REQUEST_CHANGES", "body": "please"}]
    assert review.review_id is None


def test_approve_with_nothing_pending_creates_an_empty_review_to_submit(gh: GhSequence, review: Review) -> None:
    """The LGTM: no comments, no pending review, one APPROVE review."""
    _created(gh)
    _submitted(gh)
    review.submit("APPROVE", "")
    assert gh.ops() == ["query", "addPullRequestReview", "submitPullRequestReview"]
    assert gh.variables("submitPullRequestReview")[0]["rid"] == "PRR_new"


def test_submit_refuses_an_unknown_event_without_calling_github(gh: GhSequence, review: Review) -> None:
    gh.expect("query", _state(pending=[("PRR_p", "alice")]))
    with pytest.raises(ValueError, match="event must be one of"):
        review.submit("SHIP_IT", "")
    assert gh.ops() == ["query"]


# --- threads ----------------------------------------------------------------


def test_set_thread_resolved_flips_the_thread_on_github(gh: GhSequence, review: Review) -> None:
    gh.expect("unresolveReviewThread", {"data": {"unresolveReviewThread": {"thread": {"isResolved": False}}}})
    review.set_thread_resolved("T", False)
    assert gh.variables("unresolveReviewThread") == [{"tid": "T"}]


# --- for_run ----------------------------------------------------------------


def test_for_run_reads_the_pr_off_meta_json(run_dir: paths.RunDir) -> None:
    run_dir.meta.write_text(
        json.dumps({"url": "https://github.com/o/r/pull/7", "headRefOid": "abc123", "number": 7}), encoding="utf-8"
    )
    review = pending_review.for_run(run_dir)
    assert (review.repo, review.number, review.head_sha, review.run_dir) == ("o/r", 7, "abc123", run_dir)


@pytest.mark.parametrize(
    ("meta", "message"),
    [({"headRefOid": "abc"}, "no url"), ({"url": "https://github.com/o/r/pull/7"}, "no headRefOid")],
)
def test_for_run_refuses_a_run_that_is_not_a_pr(run_dir: paths.RunDir, meta: dict, message: str) -> None:
    run_dir.meta.write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        pending_review.for_run(run_dir)
