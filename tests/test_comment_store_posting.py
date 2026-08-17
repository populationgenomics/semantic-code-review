"""Posting a comment must take it out of the local set.

A comment that reached GitHub *is* an upstream comment. Left as
`source="local"` it is re-sent by the next post, which duplicates it
into a second review — the pending-review dedupe cannot see it, because
it lives in a review that has already been submitted.
"""

from __future__ import annotations

from pathlib import Path

from semantic_code_review.review.comments import CommentStore


def _store(tmp_path: Path) -> CommentStore:
    store = CommentStore(tmp_path / "comments.json")
    store.upsert({"id": "c1", "file": "a.py", "side": "new", "line": 3, "body": "one"})
    store.upsert({"id": "c2", "file": "a.py", "side": "new", "line": 9, "body": "two"})
    return store


def test_marking_posted_makes_a_comment_ingested(tmp_path: Path) -> None:
    store = _store(tmp_path)

    assert store.mark_posted({"c1": "PRRT_abc"}) == 1

    by_id = {c.id: c for c in store.all()}
    assert by_id["c1"].source == "github"
    assert by_id["c1"].node_id == "PRRT_abc"
    assert by_id["c2"].source == "local"  # untouched


def test_a_posted_comment_is_not_offered_for_posting_again(tmp_path: Path) -> None:
    """`comments_to_github` drops non-local comments, so the transition
    is what stops the re-post."""
    from semantic_code_review.review.github import comments_to_github

    store = _store(tmp_path)
    store.mark_posted({"c1": "PRRT_abc"})

    mapped = comments_to_github(store.all())

    assert [m.body for m in mapped] == ["two"]


def test_marking_survives_a_reload(tmp_path: Path) -> None:
    """The write-back has to reach disk — the next `scr pr` builds a new
    store over the same file."""
    store = _store(tmp_path)
    store.mark_posted({"c1": "PRRT_abc"})

    reloaded = {c.id: c for c in CommentStore(tmp_path / "comments.json").all()}

    assert reloaded["c1"].source == "github"
    assert reloaded["c1"].node_id == "PRRT_abc"


def test_unknown_and_already_posted_ids_are_ignored(tmp_path: Path) -> None:
    """A comment deleted between posting and write-back is not an error."""
    store = _store(tmp_path)
    store.mark_posted({"c1": "PRRT_abc"})

    assert store.mark_posted({"gone": "PRRT_x", "c1": "PRRT_second"}) == 0
    assert {c.id: c.node_id for c in store.all()}["c1"] == "PRRT_abc"


def test_nothing_posted_is_a_no_op(tmp_path: Path) -> None:
    assert _store(tmp_path).mark_posted({}) == 0


def test_the_post_callback_marks_what_it_posted(tmp_path: Path) -> None:
    """The store transition is only useful if the post path performs it —
    the store tests above pass whether or not it is wired up."""
    from unittest.mock import patch

    from semantic_code_review.review import pr_flow
    from semantic_code_review.review.github import PostResult

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "raw.diff").write_text("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,3 +1,3 @@\n+x\n")
    _store(run_dir)  # seeds c1 + c2

    def fake_post(repo, number, mapped, **kw):
        return PostResult(review_id=1, review_url="u", posted=2, posted_node_ids={"c1": "TH_1", "c2": "TH_2"})

    callback = pr_flow._build_post_callback("o/r", 1, run_dir)
    with patch.object(pr_flow, "post_review_via_graphql", fake_post):
        callback(["c1", "c2"])

    reloaded = {c.id: c for c in CommentStore(run_dir / "comments.json").all()}
    assert reloaded["c1"].source == "github"
    assert reloaded["c2"].source == "github"
