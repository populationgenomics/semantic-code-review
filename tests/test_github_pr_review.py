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

    monkeypatch.setattr(gh, "require_gh", lambda: "/usr/bin/gh")
    monkeypatch.setattr(subprocess, "run", fake_run)

    comments = [_comment(side="new", line=42, body="nit"),
                _comment(id="c2", file="b.py", side="old", line=7, body="why")]
    result = gh.post_inline_review("o/r", 1, "ff2ab91deadbeef", comments)

    cmd = captured["cmd"]
    assert cmd[:5] == ["/usr/bin/gh", "api", "-X", "POST", "repos/o/r/pulls/1/reviews"]
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
    monkeypatch.setattr(gh, "require_gh", lambda: "/usr/bin/gh")
    called = {"n": 0}

    def fake_run(*a, **kw):  # pragma: no cover — should not be called
        called["n"] += 1
        return _FakeProc()

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(gh.GhError):
        gh.post_inline_review("o/r", 1, "abc", [])
    assert called["n"] == 0


def test_post_inline_review_propagates_gh_failure(monkeypatch) -> None:
    monkeypatch.setattr(gh, "require_gh", lambda: "/usr/bin/gh")

    def fake_run(*a, **kw):
        return _FakeProc(stderr="HTTP 422: line not in diff hunk", returncode=1)

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(gh.GhError) as exc:
        gh.post_inline_review("o/r", 1, "abc", [_comment()])
    assert "422" in str(exc.value)


# ---------------------------------------------------------------------------
# list_review_requested_prs
# ---------------------------------------------------------------------------

def test_list_review_requested_prs_parses_json(monkeypatch) -> None:
    monkeypatch.setattr(gh, "require_gh", lambda: "/usr/bin/gh")
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
