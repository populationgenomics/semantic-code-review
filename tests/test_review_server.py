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
        with server.ctx.state_lock:
            if server.ctx.subscribers:
                break
        time.sleep(0.01)
    else:
        raise AssertionError("subscriber never registered")

    server.publish("reload", {"reason": "test"})

    # Frame is id/event/data terminated by a blank line.
    id_line = r.fp.readline()
    event_line = r.fp.readline()
    data_line = r.fp.readline()
    trailing = r.fp.readline()
    assert id_line == b"id: 1\n"
    assert event_line == b"event: reload\n"
    assert data_line == b'data: {"reason": "test"}\n'
    assert trailing == b"\n"

    conn.close()


def _read_sse_frame(fp) -> tuple[int, str, str]:
    """Read one SSE frame (id/event/data, terminated by a blank line)
    from a buffered file-like and return (id, event_type, data_body)."""
    lines: list[str] = []
    while True:
        line = fp.readline().decode("utf-8")
        if line == "\n":
            break
        if not line:
            raise EOFError("connection closed mid-frame")
        lines.append(line.rstrip("\n"))
    parts = {}
    for ln in lines:
        if ":" in ln:
            k, _, v = ln.partition(":")
            parts[k.strip()] = v.lstrip()
    return int(parts.get("id", "0")), parts.get("event", ""), parts.get("data", "")


def test_events_replay_buffered_after_reconnect(server) -> None:
    """Reconnecting with Last-Event-ID replays only the events the
    client hasn't seen yet."""
    server.publish("hunk", {"file_idx": 0, "hunk_idx": 0})
    server.publish("hunk", {"file_idx": 0, "hunk_idx": 1})
    server.publish("done", {})

    conn = HTTPConnection("127.0.0.1", int(server.url().rsplit(":", 1)[1]), timeout=5)
    conn.putrequest("GET", "/events")
    conn.putheader("Last-Event-ID", "1")
    conn.endheaders()
    r = conn.getresponse()
    assert r.status == 200

    # Skip the priming comment frame.
    primer = r.fp.readline()
    assert primer.startswith(b":")
    blank = r.fp.readline()
    assert blank == b"\n"

    # Should replay events with id 2 and 3 (we acked id 1).
    eid, etype, data = _read_sse_frame(r.fp)
    assert eid == 2 and etype == "hunk"
    assert json.loads(data) == {"file_idx": 0, "hunk_idx": 1}

    eid, etype, data = _read_sse_frame(r.fp)
    assert eid == 3 and etype == "done"

    conn.close()


def test_fold_summary_returns_409_when_summariser_not_wired(server) -> None:
    """Before serve_review installs the summariser, POST returns 409."""
    conn = HTTPConnection("127.0.0.1", int(server.url().rsplit(":", 1)[1]), timeout=5)
    conn.request(
        "POST", "/fold-summary",
        body=json.dumps({"hunk_id": "H0_0", "new_start": 1, "new_count": 3}),
        headers={"Content-Type": "application/json"},
    )
    r = conn.getresponse()
    assert r.status == 409
    body = json.loads(r.read())
    assert "augmentation" in body["error"]
    conn.close()


def test_fold_summary_persists_and_publishes(tmp_path: Path) -> None:
    """When the summariser is wired and the sidecar is on disk, the
    route calls the closure, writes the result back to the sidecar +
    /data.json, and fans out a `fold-summary` SSE event."""
    from semantic_code_review.format.parse import parse_augmented_diff
    from semantic_code_review.format.sidecar import dump_sidecar
    from semantic_code_review.review.server import ReviewServer

    # Use the augmented fixture (carries an overview + per-hunk
    # annotations) so the sidecar resolves cleanly.
    fixture = Path(__file__).parent / "fixtures" / "sample.augmented.diff"
    diff = parse_augmented_diff(fixture.read_text(encoding="utf-8"))
    sidecar = tmp_path / "augmented.scr.json"
    dump_sidecar(diff, sidecar)
    (tmp_path / "augmented.diff").write_text(
        fixture.read_text(encoding="utf-8"), encoding="utf-8",
    )
    (tmp_path / "review.html").write_text("<html></html>", encoding="utf-8")
    viewer_json = {
        "version": "1",
        "files": [{
            "id": "F0", "path": diff.files[0].path,
            "hunks": [{
                "id": "H0_0",
                "fold_regions": [{
                    "context": "right", "right_start": 1, "right_end": 3,
                    "left_start": 0, "left_end": 0, "summary": "",
                }],
            }],
        }],
    }
    # head/<path> needs to exist so the summariser closure can read
    # it; we use an empty file because our stub doesn't actually read.
    (tmp_path / "head").mkdir()
    (tmp_path / "head" / diff.files[0].path).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "head" / diff.files[0].path).write_text("noop\n", encoding="utf-8")

    srv = ReviewServer(
        run_dir=tmp_path,
        html_path=tmp_path / "review.html",
        viewer_json=viewer_json,
    )
    srv.start()
    try:
        captured = {}

        async def summariser(
            file_path, file_summary, overview_json,
            context, right_range, left_range,
        ):
            captured["called"] = True
            captured["file_path"] = file_path
            captured["context"] = context
            captured["right_range"] = right_range
            captured["left_range"] = left_range
            return "wraps the body in a try/except to fail-soft on bad input"

        srv.set_fold_summariser(summariser)

        # Subscribe to /events so we can assert the broadcast happened.
        conn = HTTPConnection("127.0.0.1", int(srv.url().rsplit(":", 1)[1]), timeout=5)
        conn.request("GET", "/events")
        events_resp = conn.getresponse()
        assert events_resp.status == 200
        # Consume the priming frame.
        events_resp.fp.readline()
        events_resp.fp.readline()

        # Wait for the subscriber to actually register.
        for _ in range(50):
            with srv.ctx.state_lock:
                if srv.ctx.subscribers:
                    break
            time.sleep(0.01)

        code, body = _request(
            srv.url() + "/fold-summary", "POST",
            {"file_idx": 0, "context": "right", "right_start": 1, "right_end": 3},
        )
        assert code == 200
        assert body["summary"].startswith("wraps the body")
        assert captured["called"] is True
        assert captured["context"] == "right"
        assert captured["right_range"] == (1, 3)
        assert captured["left_range"] is None

        # Sidecar now carries the summary.
        from semantic_code_review.format.sidecar import load_sidecar
        reloaded = load_sidecar(sidecar)
        folds = reloaded.files[0].hunks[0].ann.fold_descriptions
        assert any(
            fd.context == "right" and fd.right_start == 1 and fd.right_end == 3
            and fd.summary.startswith("wraps the body")
            for fd in folds
        )
        # `/data.json` reflects the patched viewer_json.
        code, data = _request(srv.url() + "/data.json")
        assert code == 200
        assert data["files"][0]["hunks"][0]["fold_regions"][0]["summary"].startswith(
            "wraps the body"
        )
        # The SSE channel broadcast the same payload.
        id_line = events_resp.fp.readline()
        event_line = events_resp.fp.readline()
        data_line = events_resp.fp.readline()
        events_resp.fp.readline()  # trailing blank
        assert event_line == b"event: fold-summary\n"
        assert b'"summary":' in data_line
        conn.close()
    finally:
        srv.stop()


def test_fold_summary_for_left_context_resolves_and_persists(tmp_path: Path) -> None:
    """A pure-deletion fold posts {context:'left', left_start, left_end};
    the server routes to the same summariser and persists with
    context='left'."""
    from semantic_code_review.format.parse import parse_augmented_diff
    from semantic_code_review.format.sidecar import dump_sidecar, load_sidecar
    from semantic_code_review.review.server import ReviewServer

    fixture = Path(__file__).parent / "fixtures" / "sample.augmented.diff"
    diff = parse_augmented_diff(fixture.read_text(encoding="utf-8"))
    sidecar = tmp_path / "augmented.scr.json"
    dump_sidecar(diff, sidecar)
    (tmp_path / "augmented.diff").write_text(
        fixture.read_text(encoding="utf-8"), encoding="utf-8",
    )
    (tmp_path / "review.html").write_text("<html></html>", encoding="utf-8")
    viewer_json = {
        "version": "1",
        "files": [{
            "id": "F0", "path": diff.files[0].path,
            "hunks": [{
                "id": "H0_0",
                "fold_regions": [{
                    "context": "left", "right_start": 0, "right_end": 0,
                    "left_start": 12, "left_end": 14, "summary": "",
                }],
            }],
        }],
    }

    srv = ReviewServer(
        run_dir=tmp_path, html_path=tmp_path / "review.html",
        viewer_json=viewer_json,
    )
    srv.start()
    try:
        seen = {}

        async def summariser(
            file_path, file_summary, overview_json,
            context, right_range, left_range,
        ):
            seen["context"] = context
            seen["right_range"] = right_range
            seen["left_range"] = left_range
            return "drops the legacy retry loop"

        srv.set_fold_summariser(summariser)
        code, body = _request(
            srv.url() + "/fold-summary", "POST",
            {"file_idx": 0, "context": "left", "left_start": 12, "left_end": 14},
        )
        assert code == 200
        assert seen == {
            "context": "left", "right_range": None, "left_range": (12, 14),
        }
        assert body["context"] == "left" and body["left_start"] == 12

        reloaded = load_sidecar(sidecar)
        folds = reloaded.files[0].hunks[0].ann.fold_descriptions
        assert any(
            fd.context == "left" and fd.left_start == 12 and fd.left_end == 14
            for fd in folds
        )
    finally:
        srv.stop()


def test_events_replay_from_zero_when_header_absent(server) -> None:
    """A fresh connection with no Last-Event-ID gets the full buffer."""
    server.publish("overview", {"summary": "first"})
    server.publish("hunk", {"file_idx": 0, "hunk_idx": 0})

    conn = HTTPConnection("127.0.0.1", int(server.url().rsplit(":", 1)[1]), timeout=5)
    conn.request("GET", "/events")
    r = conn.getresponse()
    assert r.status == 200

    primer = r.fp.readline()
    assert primer.startswith(b":")
    r.fp.readline()

    eid1, etype1, _ = _read_sse_frame(r.fp)
    eid2, etype2, _ = _read_sse_frame(r.fp)
    assert (eid1, etype1) == (1, "overview")
    assert (eid2, etype2) == (2, "hunk")

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


def test_serve_review_renders_pending_then_streams_and_finalises(tmp_path: Path) -> None:
    """End-to-end: serve_review starts the server, renders a pending
    HTML, then runs the augment closure (which can publish streaming
    events via the supplied publish callable) and fires `done` once
    augmentation finishes."""
    from semantic_code_review.review.runner import serve_review

    _populate_minimal_run_dir(tmp_path)

    augment_started = threading.Event()
    augment_release = threading.Event()
    augment_finished = threading.Event()

    async def fake_augment(rd: Path, publish) -> None:
        augment_started.set()
        # Block until the test confirms it has observed the pending
        # HTML — otherwise the final render races us and the assertion
        # below sees the post-augment file.
        await asyncio.get_running_loop().run_in_executor(
            None, augment_release.wait, 5.0,
        )
        # Mimic a per-hunk completion before the pipeline writes its
        # final on-disk output. The page would react by patching the
        # hunk slot; here we just confirm the callable was wired in.
        publish("hunk", {"file_idx": 0, "hunk_idx": 0, "ok": True, "block": {"id": "H0_0"}})
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
