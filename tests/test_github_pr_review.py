"""Unit tests for the GitHub PR-review helpers in `review/github.py`."""

from __future__ import annotations

import io
import json
import subprocess

import pytest

from semantic_code_review.review import github as gh
from semantic_code_review.review.comments import Comment


# ---------------------------------------------------------------------------
# side mapping
# ---------------------------------------------------------------------------

def test_map_side_old_to_left() -> None:
    assert gh.map_side("old") == "LEFT"


def test_map_side_new_to_right() -> None:
    assert gh.map_side("new") == "RIGHT"


def test_map_side_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        gh.map_side("BOTH")


# ---------------------------------------------------------------------------
# comments_to_github
# ---------------------------------------------------------------------------

def _comment(**kw) -> Comment:
    base = dict(id="c1", file="a.py", side="new", line=10, body="hi")
    base.update(kw)
    return Comment(**base)


def test_comments_to_github_maps_basic_fields() -> None:
    cs = [_comment(side="old", line=5, body="left side"),
          _comment(id="c2", side="new", line=12, body="right side")]
    posted = gh.comments_to_github(cs)
    assert [(p.path, p.line, p.side, p.body) for p in posted] == [
        ("a.py", 5, "LEFT", "left side"),
        ("a.py", 12, "RIGHT", "right side"),
    ]


def test_comments_to_github_drops_malformed_dict() -> None:
    # Dict with no body should be silently dropped, not raise.
    bad = {"file": "a.py", "side": "new", "line": 1, "body": ""}
    good = {"file": "a.py", "side": "new", "line": 2, "body": "ok"}
    out = gh.comments_to_github([bad, good])
    assert len(out) == 1
    assert out[0].body == "ok"


def test_comments_to_github_filters_ingested_comments() -> None:
    """Ingested github comments are already upstream — re-posting would
    duplicate them. They must drop out of the mapper."""
    cs = [
        _comment(id="gh-123", source="github", author="alice", body="upstream"),
        _comment(id="local-1", source="local", body="new local"),
    ]
    out = gh.comments_to_github(cs)
    assert [p.body for p in out] == ["new local"]


def test_comments_to_github_emits_reply_payload_for_local_reply_to_gh_parent() -> None:
    """A local comment with in_reply_to_id pointing at an ingested
    comment becomes a reply payload (in_reply_to + body) rather than
    a new anchored thread."""
    cs = [
        _comment(
            id="local-1", source="local", body="reply text",
            in_reply_to_id="gh-3331909762",
        ),
    ]
    out = gh.comments_to_github(cs)
    assert len(out) == 1
    p = out[0]
    assert p.is_reply
    assert p.in_reply_to == 3331909762
    assert p.body == "reply text"
    # Reply payload doesn't carry anchor fields.
    assert p.path is None and p.line is None and p.side is None
    assert p.to_payload() == {"in_reply_to": 3331909762, "body": "reply text"}


def test_comments_to_github_skips_local_reply_to_local_parent() -> None:
    """A local reply to another local comment can't post in a single
    review (the parent doesn't exist on GitHub yet) — drop with a
    log warning rather than fail the whole batch."""
    cs = [
        _comment(id="local-root", source="local", body="root"),
        _comment(
            id="local-reply", source="local", body="reply",
            in_reply_to_id="local-root",
        ),
    ]
    out = gh.comments_to_github(cs)
    assert len(out) == 1
    assert out[0].body == "root"
    assert out[0].is_reply is False


def test_post_inline_review_serialises_mixed_threads_and_replies(monkeypatch) -> None:
    """Anchored threads and replies coexist in the same review payload —
    the comments[] array contains both shapes."""
    captured: dict = {}

    def fake_run(cmd, *, input=None, capture_output=False, text=False, check=False, **kw):
        captured["cmd"] = cmd
        captured["input"] = input
        return _FakeProc(stdout=json.dumps({"id": 1, "html_url": ""}))

    monkeypatch.setattr(subprocess, "run", fake_run)
    cs = [
        _comment(id="local-1", source="local", body="new thread"),
        _comment(
            id="local-2", source="local", body="reply",
            in_reply_to_id="gh-111",
        ),
        _comment(id="gh-222", source="github", body="should be filtered"),
    ]
    result = gh.post_inline_review("o/r", 1, "head", cs)
    payload = json.loads(captured["input"])
    assert payload["comments"] == [
        {"path": "a.py", "line": 10, "side": "RIGHT", "body": "new thread"},
        {"in_reply_to": 111, "body": "reply"},
    ]
    assert result.posted == 2


def test_post_inline_review_accepts_already_mapped_PostedComments(monkeypatch) -> None:
    """CLI maps once for the prompt count, then passes the mapped list
    to post_inline_review — no double-filter, no surprise."""
    captured: dict = {}

    def fake_run(cmd, *, input=None, **kw):
        captured["input"] = input
        return _FakeProc(stdout=json.dumps({"id": 1, "html_url": ""}))

    monkeypatch.setattr(subprocess, "run", fake_run)
    posted = [
        gh.PostedComment(body="hi", path="x.py", line=2, side="RIGHT"),
        gh.PostedComment(body="ack", in_reply_to=99),
    ]
    result = gh.post_inline_review("o/r", 1, "head", posted)
    payload = json.loads(captured["input"])
    assert payload["comments"] == [
        {"path": "x.py", "line": 2, "side": "RIGHT", "body": "hi"},
        {"in_reply_to": 99, "body": "ack"},
    ]
    assert result.posted == 2


# ---------------------------------------------------------------------------
# post_inline_review argv shape
# ---------------------------------------------------------------------------

class _FakeProc:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_post_inline_review_shells_gh_api(monkeypatch) -> None:
    captured: dict = {}

    def fake_run(cmd, *, input=None, capture_output=False, text=False, check=False, **kw):
        captured["cmd"] = cmd
        captured["input"] = input
        return _FakeProc(stdout=json.dumps({"id": 999, "html_url": "https://github.com/o/r/pull/1#pullrequestreview-999"}))

    monkeypatch.setattr(subprocess, "run", fake_run)

    comments = [_comment(side="new", line=42, body="nit"),
                _comment(id="c2", file="b.py", side="old", line=7, body="why")]
    result = gh.post_inline_review("o/r", 1, "ff2ab91deadbeef", comments)

    cmd = captured["cmd"]
    assert cmd[:5] == ["gh", "api", "-X", "POST", "repos/o/r/pulls/1/reviews"]
    assert "--input" in cmd and cmd[-1] == "-"

    payload = json.loads(captured["input"])
    assert payload["commit_id"] == "ff2ab91deadbeef"
    assert payload["event"] == "COMMENT"
    assert payload["body"] == ""
    assert payload["comments"] == [
        {"path": "a.py", "line": 42, "side": "RIGHT", "body": "nit"},
        {"path": "b.py", "line": 7, "side": "LEFT", "body": "why"},
    ]
    assert result.review_id == 999
    assert result.posted == 2
    assert "pullrequestreview-999" in result.review_url


def test_post_inline_review_raises_when_no_postable(monkeypatch) -> None:
    called = {"n": 0}

    def fake_run(*a, **kw):  # pragma: no cover — should not be called
        called["n"] += 1
        return _FakeProc()

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(gh.GhError):
        gh.post_inline_review("o/r", 1, "abc", [])
    assert called["n"] == 0


def test_post_inline_review_propagates_gh_failure(monkeypatch) -> None:
    def fake_run(*a, **kw):
        return _FakeProc(stderr="HTTP 422: line not in diff hunk", returncode=1)

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(gh.GhError) as exc:
        gh.post_inline_review("o/r", 1, "abc", [_comment()])
    assert "422" in str(exc.value)


def test_post_inline_review_surfaces_github_body_message(monkeypatch) -> None:
    """gh writes the response body to stdout on error; the wrapper
    parses it so the user sees the real cause instead of just the
    gh-side 'Unprocessable Entity' summary."""
    body = json.dumps({
        "message": "Validation Failed",
        "errors": [{"message": "Line 5 must be part of the diff hunk"}],
    })
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _FakeProc(
        stdout=body, stderr="gh: Unprocessable Entity (HTTP 422)", returncode=1,
    ))
    with pytest.raises(gh.GhError) as exc:
        gh.post_inline_review("o/r", 1, "abc", [_comment()])
    text = str(exc.value)
    assert "Validation Failed" in text
    assert "Line 5 must be part of the diff hunk" in text


def test_post_inline_review_hints_at_pending_draft_review(monkeypatch) -> None:
    """The single most common foot-gun: a draft review left on
    github.com blocks the bulk POST. Surface the fix path."""
    body = json.dumps({
        "message": "Unprocessable Entity",
        "errors": ["User can only have one pending review per pull request"],
        "status": "422",
    })
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _FakeProc(
        stdout=body, stderr="gh: Unprocessable Entity (HTTP 422)", returncode=1,
    ))
    with pytest.raises(gh.GhError) as exc:
        gh.post_inline_review("o/r", 1, "abc", [_comment()])
    text = str(exc.value)
    assert "one pending review per pull request" in text
    assert "submit or delete the pending review" in text.lower()


# ---------------------------------------------------------------------------
# list_review_requested_prs
# ---------------------------------------------------------------------------

def test_list_review_requested_prs_parses_json(monkeypatch) -> None:
    payload = [
        {
            "number": 42,
            "title": "Add subgrid",
            "author": {"login": "alice"},
            "headRefName": "feat/subgrid",
            "baseRefName": "main",
            "updatedAt": "2026-04-20T10:00:00Z",
            "url": "https://github.com/o/r/pull/42",
        }
    ]
    captured: dict = {}

    def fake_run(cmd, *, capture_output=False, text=False, check=False, **kw):
        captured["cmd"] = cmd
        return _FakeProc(stdout=json.dumps(payload))

    monkeypatch.setattr(subprocess, "run", fake_run)
    prs = gh.list_review_requested_prs("o/r")

    assert "is:open review-requested:@me" in captured["cmd"]
    assert len(prs) == 1
    assert prs[0].number == 42
    assert prs[0].author == "alice"
    assert prs[0].head_ref == "feat/subgrid"
    assert prs[0].base_ref == "main"


# ---------------------------------------------------------------------------
# pick_pr_interactive
# ---------------------------------------------------------------------------

def _prs() -> list[gh.OpenPR]:
    return [
        gh.OpenPR(number=42, title="A", author="alice", head_ref="x", base_ref="main",
                  updated_at="", url=""),
        gh.OpenPR(number=45, title="B", author="bob", head_ref="y", base_ref="main",
                  updated_at="", url=""),
        gh.OpenPR(number=50, title="C", author="carol", head_ref="z", base_ref="main",
                  updated_at="", url=""),
    ]


def test_picker_selects_by_number() -> None:
    out = io.StringIO()
    in_ = io.StringIO("2\n")
    chosen = gh.pick_pr_interactive("o/r", _prs(), out=out, in_=in_)
    assert chosen == 45


def test_picker_quit_returns_none() -> None:
    out = io.StringIO()
    in_ = io.StringIO("q\n")
    chosen = gh.pick_pr_interactive("o/r", _prs(), out=out, in_=in_)
    assert chosen is None


def test_picker_eof_returns_none() -> None:
    out = io.StringIO()
    in_ = io.StringIO("")
    chosen = gh.pick_pr_interactive("o/r", _prs(), out=out, in_=in_)
    assert chosen is None


def test_picker_out_of_range_returns_none() -> None:
    out = io.StringIO()
    in_ = io.StringIO("9\n")
    chosen = gh.pick_pr_interactive("o/r", _prs(), out=out, in_=in_)
    assert chosen is None
    assert "invalid selection" in out.getvalue()


def test_picker_garbage_returns_none() -> None:
    out = io.StringIO()
    in_ = io.StringIO("not-a-number\n")
    chosen = gh.pick_pr_interactive("o/r", _prs(), out=out, in_=in_)
    assert chosen is None
