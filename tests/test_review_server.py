"""Comments server: routes, atomic writes, shutdown path."""

from __future__ import annotations

import asyncio
import json
import threading
import time
import urllib.error
import urllib.request
from http.client import HTTPConnection
from pathlib import Path

import pytest

from semantic_code_review.review.comments import Comment, format_markdown
from semantic_code_review.review.server import ReviewServer


@pytest.fixture
def server(tmp_path: Path):
    srv = ReviewServer(
        run_dir=tmp_path,
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
    """GET / returns the static viewer shell."""
    req = urllib.request.Request(server.url() + "/")
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 200
        assert r.headers.get("Content-Type", "").startswith("text/html")
        body = r.read().decode()
        # The static shell references the bundled JS and includes the
        # session-endpoint meta tag the viewer uses to flip into
        # server-mediated mode.
        assert "/static/viewer.js" in body
        assert 'name="scr-session-endpoint"' in body


def test_get_static_viewer_js(server) -> None:
    """/static/viewer.js serves the bundled output."""
    req = urllib.request.Request(server.url() + "/static/viewer.js")
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 200
        assert r.headers.get("Content-Type", "").startswith("application/javascript")
        body = r.read()
        assert body  # non-empty


def test_get_static_unknown_404(server) -> None:
    """Unlisted static paths 404 even if the file would exist on disk."""
    try:
        urllib.request.urlopen(server.url() + "/static/../cli.py", timeout=5)
    except urllib.error.HTTPError as e:
        assert e.code == 404
    else:
        raise AssertionError("expected 404 for path-traversal asset")


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


def test_post_cannot_overwrite_ingested_comment(server, tmp_path: Path) -> None:
    """Ingested PR comments (source != local) are read-only — the server
    rejects an upsert that targets one with 403 rather than letting the
    body get rewritten in place."""
    # Seed comments.json with an ingested comment, then re-create the
    # server so it loads from the file we just wrote.
    (tmp_path / "comments.json").write_text(json.dumps({
        "comments": [{
            "id": "gh-1", "file": "a.py", "side": "new", "line": 1,
            "body": "upstream", "source": "github", "author": "alice",
            "created_at": 1.0, "updated_at": 1.0,
        }],
    }))
    server.stop()
    srv2 = ReviewServer(run_dir=tmp_path, viewer_json={"version": "1", "files": []})
    srv2.start()
    try:
        try:
            _request(srv2.url() + "/comments", "POST", {
                "id": "gh-1", "file": "a.py", "side": "new", "line": 1,
                "body": "overwritten",
            })
        except urllib.error.HTTPError as e:
            assert e.code == 403
        else:
            raise AssertionError("expected 403 for overwrite of ingested comment")
        # On disk the body is unchanged.
        data = json.loads((tmp_path / "comments.json").read_text())
        assert data["comments"][0]["body"] == "upstream"
    finally:
        srv2.stop()


def test_delete_cannot_remove_ingested_comment(server, tmp_path: Path) -> None:
    (tmp_path / "comments.json").write_text(json.dumps({
        "comments": [{
            "id": "gh-1", "file": "a.py", "side": "new", "line": 1,
            "body": "upstream", "source": "github", "author": "alice",
            "created_at": 1.0, "updated_at": 1.0,
        }],
    }))
    server.stop()
    srv2 = ReviewServer(run_dir=tmp_path, viewer_json={"version": "1", "files": []})
    srv2.start()
    try:
        conn = HTTPConnection("127.0.0.1", int(srv2.url().rsplit(":", 1)[1]), timeout=5)
        conn.request("DELETE", "/comments/gh-1")
        r = conn.getresponse()
        assert r.status == 403
        # Still on disk.
        data = json.loads((tmp_path / "comments.json").read_text())
        assert len(data["comments"]) == 1
    finally:
        srv2.stop()


def test_post_resets_source_to_local_on_new_comment(server, tmp_path: Path) -> None:
    """A new comment claiming source=github on the wire is still stored
    as local — provenance can only be set by the ingest path, not by a
    client POST."""
    code, body = _request(server.url() + "/comments", "POST", {
        "id": "c1", "file": "a.py", "side": "new", "line": 1, "body": "x",
        "source": "github", "author": "evil",
    })
    assert code == 200
    assert body["source"] == "local"


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


def test_fold_summary_broadcasts_and_patches_viewer_json(tmp_path: Path) -> None:
    """Transport-only: the route dispatches to the wired-in task, then
    patches the in-memory `viewer_json` and fans out an SSE event.

    Sidecar mutation lives in :func:`apply_fold_summary_to_run`; its
    coverage is in tests/test_fold_summary_apply.py.
    """
    from semantic_code_review.review.server import ReviewServer

    viewer_json = {
        "version": "1",
        "files": [{
            "id": "F0", "path": "src/x.py",
            "hunks": [{
                "id": "H0_0",
                "fold_regions": [{
                    "context": "right", "right_start": 1, "right_end": 3,
                    "left_start": 0, "left_end": 0,
                    "qualified_name": "Foo.bar", "kind": "function",
                    "summary": "",
                }],
            }],
        }],
    }

    srv = ReviewServer(run_dir=tmp_path, viewer_json=viewer_json)
    srv.start()
    try:
        captured = {}

        async def fake_task(
            file_idx, context, right_range, left_range,
            qualified_name=None, kind=None,
        ):
            captured["file_idx"] = file_idx
            captured["context"] = context
            captured["right_range"] = right_range
            captured["left_range"] = left_range
            captured["qualified_name"] = qualified_name
            captured["kind"] = kind
            return {
                "file_idx": file_idx, "context": context,
                "right_start": (right_range or (0, 0))[0],
                "right_end": (right_range or (0, 0))[1],
                "left_start": (left_range or (0, 0))[0],
                "left_end": (left_range or (0, 0))[1],
                "summary": "wraps the body in a try/except",
            }

        srv.set_fold_summariser(fake_task)

        # Subscribe to /events so we can assert the broadcast happened.
        conn = HTTPConnection("127.0.0.1", int(srv.url().rsplit(":", 1)[1]), timeout=5)
        conn.request("GET", "/events")
        events_resp = conn.getresponse()
        assert events_resp.status == 200
        events_resp.fp.readline()
        events_resp.fp.readline()
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
        # Task saw the parsed-out request, not raw payload.
        assert captured["file_idx"] == 0
        assert captured["context"] == "right"
        assert captured["right_range"] == (1, 3)
        assert captured["left_range"] is None
        # The symbol the region snapped to is resolved from viewer_json
        # and threaded through to the summariser.
        assert captured["qualified_name"] == "Foo.bar"
        assert captured["kind"] == "function"

        # `/data.json` reflects the patched viewer_json.
        code, data = _request(srv.url() + "/data.json")
        assert code == 200
        assert data["files"][0]["hunks"][0]["fold_regions"][0]["summary"].startswith(
            "wraps the body"
        )
        # The SSE channel broadcast the same payload.
        events_resp.fp.readline()  # id
        event_line = events_resp.fp.readline()
        data_line = events_resp.fp.readline()
        events_resp.fp.readline()  # trailing blank
        assert event_line == b"event: fold-summary\n"
        assert b'"summary":' in data_line
        conn.close()
    finally:
        srv.stop()


def test_fold_summary_for_left_context_passes_ranges_through(tmp_path: Path) -> None:
    """A pure-deletion fold posts {context:'left', left_start, left_end};
    the server routes to the same task with right_range=None and the
    left tuple populated."""
    from semantic_code_review.review.server import ReviewServer

    viewer_json = {"version": "1", "files": []}
    srv = ReviewServer(run_dir=tmp_path, viewer_json=viewer_json)
    srv.start()
    try:
        seen = {}

        async def fake_task(
            file_idx, context, right_range, left_range,
            qualified_name=None, kind=None,
        ):
            seen["context"] = context
            seen["right_range"] = right_range
            seen["left_range"] = left_range
            return {
                "file_idx": file_idx, "context": context,
                "right_start": 0, "right_end": 0,
                "left_start": (left_range or (0, 0))[0],
                "left_end": (left_range or (0, 0))[1],
                "summary": "drops the legacy retry loop",
            }

        srv.set_fold_summariser(fake_task)
        code, body = _request(
            srv.url() + "/fold-summary", "POST",
            {"file_idx": 0, "context": "left", "left_start": 12, "left_end": 14},
        )
        assert code == 200
        assert seen == {
            "context": "left", "right_range": None, "left_range": (12, 14),
        }
        assert body["context"] == "left" and body["left_start"] == 12
    finally:
        srv.stop()


def test_fold_summary_typed_errors_map_to_http_codes(tmp_path: Path) -> None:
    """`FoldSummaryNotReady` → 409; `FoldSummaryFileIndexError` → 404."""
    from semantic_code_review.augment.fold_summary import (
        FoldSummaryFileIndexError, FoldSummaryNotReady,
    )
    from semantic_code_review.review.server import ReviewServer

    srv = ReviewServer(run_dir=tmp_path, viewer_json={"files": []})
    srv.start()
    try:
        host = "127.0.0.1"
        port = int(srv.url().rsplit(":", 1)[1])

        def _post(payload: dict) -> tuple[int, dict]:
            # urlopen raises on non-2xx; HTTPConnection lets us read the body.
            conn = HTTPConnection(host, port, timeout=5)
            conn.request(
                "POST", "/fold-summary",
                body=json.dumps(payload),
                headers={"Content-Type": "application/json"},
            )
            r = conn.getresponse()
            body = json.loads(r.read())
            conn.close()
            return r.status, body

        async def raises_not_ready(
            file_idx, context, right_range, left_range,
            qualified_name=None, kind=None,
        ):
            raise FoldSummaryNotReady("sidecar gone walkabout")

        srv.set_fold_summariser(raises_not_ready)
        code, body = _post(
            {"file_idx": 0, "context": "right", "right_start": 1, "right_end": 3},
        )
        assert code == 409
        assert "walkabout" in body["error"]

        async def raises_oob(
            file_idx, context, right_range, left_range,
            qualified_name=None, kind=None,
        ):
            raise FoldSummaryFileIndexError("file_idx 999 not in diff")

        srv.set_fold_summariser(raises_oob)
        code, body = _post(
            {"file_idx": 999, "context": "right", "right_start": 1, "right_end": 3},
        )
        assert code == 404
        assert "999" in body["error"]
    finally:
        srv.stop()


def test_console_ask_returns_409_when_asker_not_wired(server) -> None:
    """Before serve_review installs the asker (pre-augment / non-SDK
    backend), POST /console/ask returns 409."""
    conn = HTTPConnection("127.0.0.1", int(server.url().rsplit(":", 1)[1]), timeout=5)
    conn.request(
        "POST", "/console/ask",
        body=json.dumps({"question": "what changed?"}),
        headers={"Content-Type": "application/json"},
    )
    r = conn.getresponse()
    assert r.status == 409
    body = json.loads(r.read())
    assert "console" in body["error"]
    conn.close()


def test_console_ask_empty_question_400(server) -> None:
    async def asker(question, history):
        return "unused", []

    server.ctx.console_asker = asker
    conn = HTTPConnection("127.0.0.1", int(server.url().rsplit(":", 1)[1]), timeout=5)
    conn.request(
        "POST", "/console/ask",
        body=json.dumps({"question": "   "}),
        headers={"Content-Type": "application/json"},
    )
    r = conn.getresponse()
    assert r.status == 400
    conn.close()


def test_console_ask_runs_turn_and_threads_history(server) -> None:
    """The route dispatches to the wired asker, returns the answer text,
    and threads the returned history back in on the next turn."""
    seen: list = []

    async def asker(question, history):
        seen.append((question, history))
        new_history = (history or []) + [question]
        return f"answer to {question!r}", new_history

    server.set_console_asker(asker)

    code, body = _request(
        server.url() + "/console/ask", "POST", {"question": "why pagination?"},
    )
    assert code == 200
    assert body["answer"] == "answer to 'why pagination?'"
    # First turn saw no prior history.
    assert seen[0] == ("why pagination?", None)

    code, body = _request(
        server.url() + "/console/ask", "POST", {"question": "follow-up"},
    )
    assert code == 200
    # Second turn received the history the first turn returned.
    assert seen[1] == ("follow-up", ["why pagination?"])


def test_console_reset_clears_history(server) -> None:
    async def asker(question, history):
        return "ok", (history or []) + [question]

    server.set_console_asker(asker)
    _request(server.url() + "/console/ask", "POST", {"question": "q1"})
    assert server.ctx.console_history == ["q1"]

    code, body = _request(server.url() + "/console/reset", "POST", {})
    assert code == 200 and body["ok"] is True
    assert server.ctx.console_history is None


def test_console_ask_not_ready_maps_to_409(server) -> None:
    """`ConsoleNotReady` from the turn driver surfaces as a 409."""
    from semantic_code_review.augment.console import ConsoleNotReady

    async def asker(question, history):
        raise ConsoleNotReady("augmented.scr.json missing")

    server.set_console_asker(asker)
    conn = HTTPConnection("127.0.0.1", int(server.url().rsplit(":", 1)[1]), timeout=5)
    conn.request(
        "POST", "/console/ask",
        body=json.dumps({"question": "x"}),
        headers={"Content-Type": "application/json"},
    )
    r = conn.getresponse()
    assert r.status == 409
    assert "missing" in json.loads(r.read())["error"]
    conn.close()


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


def test_serve_review_serves_pending_then_streams_and_finalises(tmp_path: Path) -> None:
    """End-to-end: serve_review starts the server with pending
    viewer JSON, runs the augment closure (which can publish streaming
    events via the supplied publish callable), then swaps /data.json
    to the post-augment state and fires `done` once augmentation
    finishes."""
    from semantic_code_review.review.runner import serve_review

    _populate_minimal_run_dir(tmp_path)

    augment_started = threading.Event()
    augment_release = threading.Event()
    augment_finished = threading.Event()
    ready_url: dict[str, str] = {}
    url_ready = threading.Event()

    async def fake_augment(rd: Path, publish) -> None:
        augment_started.set()
        # Block until the test confirms it has observed pending /data.json.
        # Otherwise the post-augment swap races us and the assertion
        # below sees the final state.
        await asyncio.get_running_loop().run_in_executor(
            None, augment_release.wait, 5.0,
        )
        # Mimic a per-hunk completion before the pipeline writes its
        # final on-disk output. The page would react by patching the
        # hunk slot; here we just confirm the callable was wired in.
        publish("hunk", {"file_idx": 0, "hunk_idx": 0, "ok": True, "block": {"id": "H0_0"}})
        (rd / "augmented.diff").write_text(_RAW_DIFF_FOR_RUN, encoding="utf-8")
        augment_finished.set()

    result_box: dict = {}

    def run_serve() -> None:
        def _on_ready(url: str) -> None:
            ready_url["url"] = url
            url_ready.set()
        result_box["r"] = serve_review(
            tmp_path,
            augment=fake_augment,
            port=0,
            timeout=10,
            open_browser=False,
            on_ready=_on_ready,
        )

    serve_thread = threading.Thread(target=run_serve, daemon=True)
    serve_thread.start()

    assert url_ready.wait(timeout=5)
    url = ready_url["url"]
    assert augment_started.wait(timeout=5)

    # /data.json reflects the pending skeleton before augment publishes.
    code, body = _request(url + "/data.json")
    assert code == 200
    assert body.get("pending") is True
    # Skeleton structure is present.
    assert body["files"] and body["files"][0]["path"] == "foo.py"

    augment_release.set()
    assert augment_finished.wait(timeout=5)

    # Give the runner a beat to swap /data.json + publish `done`.
    deadline = time.time() + 3
    while time.time() < deadline:
        code, body = _request(url + "/data.json")
        if body.get("pending") is not True:
            break
        time.sleep(0.02)
    assert body.get("pending") is not True

    # /static/viewer.js stays served throughout.
    code2, _ = _request(url + "/static/viewer.js", "GET")
    assert code2 == 200

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
