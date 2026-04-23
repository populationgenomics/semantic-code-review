"""Comments server: routes, atomic writes, shutdown path."""

from __future__ import annotations

import json
import threading
import urllib.request
from http.client import HTTPConnection
from pathlib import Path

import pytest

from semantic_code_review.review.comments import Comment, format_markdown
from semantic_code_review.review.server import ReviewServer


@pytest.fixture
def server(tmp_path: Path):
    (tmp_path / "review.html").write_text("<html><body>hi</body></html>")
    srv = ReviewServer(
        run_dir=tmp_path,
        html_path=tmp_path / "review.html",
        viewer_json={"version": "1", "files": []},
    )
    srv.start()
    yield srv
    srv.stop()


def _request(url: str, method: str = "GET", body: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        text = r.read().decode("utf-8")
        try:
            return r.status, json.loads(text)
        except ValueError:
            return r.status, {"_text": text}


def test_get_index_returns_html(server) -> None:
    req = urllib.request.Request(server.url() + "/")
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 200
        assert "hi" in r.read().decode()


def test_get_data_json(server) -> None:
    code, body = _request(server.url() + "/data.json")
    assert code == 200
    assert body["version"] == "1"


def test_post_comment_upserts_and_persists(server, tmp_path: Path) -> None:
    code, body = _request(server.url() + "/comments", "POST", {
        "id": "c1", "file": "a.py", "side": "new", "line": 5, "body": "hmm",
    })
    assert code == 200
    assert body["id"] == "c1"
    # File on disk
    comments_path = tmp_path / "comments.json"
    assert comments_path.exists()
    data = json.loads(comments_path.read_text())
    assert data["comments"][0]["body"] == "hmm"

    # Update the same id
    code, body = _request(server.url() + "/comments", "POST", {
        "id": "c1", "file": "a.py", "side": "new", "line": 5, "body": "clearer",
    })
    assert code == 200
    data = json.loads(comments_path.read_text())
    assert data["comments"][0]["body"] == "clearer"


def test_post_invalid_comment_400(server) -> None:
    req = urllib.request.Request(
        server.url() + "/comments", method="POST",
        data=json.dumps({"id": "x"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=5)
    except urllib.error.HTTPError as e:
        assert e.code == 400


def test_delete_comment(server, tmp_path: Path) -> None:
    _request(server.url() + "/comments", "POST", {
        "id": "c1", "file": "a.py", "side": "new", "line": 1, "body": "x",
    })
    # Delete via stdlib (no helper method for DELETE with body)
    conn = HTTPConnection("127.0.0.1", int(server.url().rsplit(":", 1)[1]), timeout=5)
    conn.request("DELETE", "/comments/c1")
    r = conn.getresponse()
    assert r.status == 200
    assert json.load(r)["ok"] is True
    data = json.loads((tmp_path / "comments.json").read_text())
    assert data["comments"] == []


def test_exit_triggers_done_event(server) -> None:
    code, body = _request(server.url() + "/exit", "POST", {})
    assert code == 200
    assert body["ok"] is True
    assert server.wait_until_done(timeout=2.0)


def test_wait_until_done_times_out_cleanly(server) -> None:
    # Fire a waiter with a very short timeout; ensure we don't block forever.
    result = {"done": None}

    def wait():
        result["done"] = server.wait_until_done(timeout=0.2)

    t = threading.Thread(target=wait)
    t.start()
    t.join(timeout=2.0)
    assert result["done"] is False


# --- format_markdown ---------------------------------------------------


def test_format_markdown_empty() -> None:
    md = format_markdown([], run_slug="local-foo-abc")
    assert "No comments left" in md
    assert "local-foo-abc" in md


def test_format_markdown_nonempty() -> None:
    cs = [
        Comment(id="c1", file="a.py", side="new", line=10, body="line one\nline two"),
        Comment(id="c2", file="b.py", side="old", line=3, body="one-liner"),
    ]
    md = format_markdown(cs, run_slug="local-r")
    assert "a.py:10 (new)" in md
    assert "b.py:3 (old)" in md
    assert "> line one" in md
    assert "> line two" in md
    assert "2 comments total" in md
