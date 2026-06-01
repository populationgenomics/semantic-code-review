"""Comments server: routes, atomic writes, shutdown path."""

from __future__ import annotations

import asyncio
import json
import threading
import time
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


# --- /events SSE channel ------------------------------------------------


def test_events_stream_delivers_published_payload(server) -> None:
    """A connected /events client receives the next published frame."""

    # Use a raw HTTPConnection so we can read framed bytes incrementally —
    # urlopen would buffer indefinitely on a never-closing response.
    conn = HTTPConnection("127.0.0.1", int(server.url().rsplit(":", 1)[1]), timeout=5)
    conn.request("GET", "/events")
    r = conn.getresponse()
    assert r.status == 200
    assert r.getheader("Content-Type") == "text/event-stream"

    # Consume the initial `: ok` comment so we're aligned on the next frame.
    primer = r.fp.readline()
    assert primer.startswith(b":")
    blank = r.fp.readline()
    assert blank == b"\n"

    # Give the handler thread a beat to register the subscriber before
    # we publish. Without this, `subscribers` may still be empty when
    # publish() snapshots the list.
    for _ in range(50):
        with server.ctx.subs_lock:
            if server.ctx.subscribers:
                break
        time.sleep(0.01)
    else:
        raise AssertionError("subscriber never registered")

    server.publish("reload", {"reason": "test"})

    # Read until we have an event/data pair.
    event_line = r.fp.readline()
    data_line = r.fp.readline()
    trailing = r.fp.readline()
    assert event_line == b"event: reload\n"
    assert data_line == b'data: {"reason": "test"}\n'
    assert trailing == b"\n"

    conn.close()


def test_update_viewer_json_replaces_data_endpoint(server) -> None:
    """`data.json` returns whatever the latest update_viewer_json set."""
    server.update_viewer_json({"version": "1", "files": [], "marker": "ok"})
    code, body = _request(server.url() + "/data.json")
    assert code == 200
    assert body["marker"] == "ok"


# --- serve_review orchestration -----------------------------------------


_RAW_DIFF_FOR_RUN = """diff --git a/foo.py b/foo.py
index 0123456..89abcde 100644
--- a/foo.py
+++ b/foo.py
@@ -1,2 +1,2 @@
 def foo():
-    return 1
+    return 2
"""


def _populate_minimal_run_dir(run_dir: Path) -> None:
    (run_dir / "raw.diff").write_text(_RAW_DIFF_FOR_RUN, encoding="utf-8")
    (run_dir / "meta.json").write_text(json.dumps({
        "title": "Bump",
        "author": {"login": "tester"},
        "url": "",
        "baseRefOid": "aaa",
        "headRefOid": "bbb",
    }), encoding="utf-8")


def test_serve_review_renders_pending_then_pushes_reload_on_completion(tmp_path: Path) -> None:
    """End-to-end: serve_review starts the server, surfaces a pending
    HTML, then runs the augment closure and fires `reload` over SSE."""
    from semantic_code_review.review.runner import serve_review

    _populate_minimal_run_dir(tmp_path)

    augment_started = threading.Event()
    augment_release = threading.Event()
    augment_finished = threading.Event()

    async def fake_augment(rd: Path) -> None:
        augment_started.set()
        # Block until the test confirms it has observed the pending
        # HTML — otherwise the final render races us and the assertion
        # below sees the post-augment file.
        await asyncio.get_running_loop().run_in_executor(
            None, augment_release.wait, 5.0,
        )
        # Mimic the real pipeline: write augmented.diff with the same
        # content (no annotations) so render_run_dir can parse it.
        (rd / "augmented.diff").write_text(_RAW_DIFF_FOR_RUN, encoding="utf-8")
        augment_finished.set()

    # Drive the orchestration on a worker thread; the main thread polls
    # the server and posts /exit once the reload event has been observed.
    result_box: dict = {}

    def run_serve() -> None:
        result_box["r"] = serve_review(
            tmp_path,
            augment=fake_augment,
            port=0,
            timeout=10,
            open_browser=False,
        )

    serve_thread = threading.Thread(target=run_serve, daemon=True)
    serve_thread.start()

    # Wait for the server to be ready by polling for the rendered HTML.
    deadline = time.time() + 5
    url = None
    while time.time() < deadline:
        # Walk the run_dir for review.html; once it exists the server
        # is up. We don't have a direct handle to the ReviewServer from
        # the test, but stdout/stderr aren't captured here so we sniff
        # the html.
        if (tmp_path / "review.html").exists():
            break
        time.sleep(0.02)
    assert augment_started.wait(timeout=5)
    assert (tmp_path / "review.html").exists()
    html = (tmp_path / "review.html").read_text(encoding="utf-8")
    # Pending data is inlined.
    assert '"pending": true' in html
    # Extract the session endpoint to talk to the server.
    import re as _re
    m = _re.search(r'scr-session-endpoint" content="([^"]+)"', html)
    assert m, "session endpoint not embedded in HTML"
    url = m.group(1)

    # Release the augment closure and wait for it to finish.
    augment_release.set()
    assert augment_finished.wait(timeout=5)
    # Give the runner a beat to re-render + publish.
    deadline = time.time() + 3
    while time.time() < deadline and '"pending": true' in (tmp_path / "review.html").read_text():
        time.sleep(0.02)
    final_html = (tmp_path / "review.html").read_text(encoding="utf-8")
    assert '"pending": true' not in final_html

    # data.json should also reflect the post-augment state.
    code, body = _request(url + "/data.json")
    assert code == 200
    assert "pending" not in body or body.get("pending") is False or body.get("pending") is None

    # Tell the server we're done so wait_until_done unblocks.
    _request(url + "/exit", "POST", {})
    serve_thread.join(timeout=5)
    assert not serve_thread.is_alive()
    assert "r" in result_box


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
