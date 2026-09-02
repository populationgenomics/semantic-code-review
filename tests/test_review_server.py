"""The review server as transport: framing, shutdown, and the wire.

What a route *does* lives on `ReviewSession` and is tested in
tests/test_review_session.py without binding a port. What is left here
needs a real socket: the SSE stream (framing, replay, `Last-Event-ID`), a
client that hangs up mid-request, the idle-timeout definition, the
comment routes' status mapping, and `serve_review` end to end.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import socket
import struct
import threading
import time
import urllib.error
import urllib.request
from http.client import HTTPConnection
from pathlib import Path

import pytest

from semantic_code_review import paths
from semantic_code_review.review.comments import Comment, format_markdown
from semantic_code_review.review.server import ReviewServer
from semantic_code_review.review.session import ServerTasks


@pytest.fixture
def server(run_dir: paths.RunDir):
    srv = ReviewServer(
        run_dir=run_dir,
        viewer_json={"version": "1", "files": []},
    )
    srv.start()
    yield srv
    srv.stop()


def _request(url: str, method: str = "GET", body: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(
        url,
        method=method,
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


def test_get_static_vendor_mermaid(server) -> None:
    """The console lazy-loads the vendored mermaid bundle by `<script>`
    injection; it must be served and must expose the `globalThis.mermaid`
    global the loader reads."""
    req = urllib.request.Request(server.url() + "/static/vendor/mermaid.min.js")
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 200
        assert r.headers.get("Content-Type", "").startswith("application/javascript")
        body = r.read()
        assert b'globalThis["mermaid"]' in body


def test_get_static_vendor_katex(server) -> None:
    """Rendered-markdown mode lazy-loads the vendored KaTeX bundle +
    stylesheet by injection; both must be served, and the CSS must pull
    its fonts from the served `vendor/fonts/` path."""
    for rel, prefix in [
        ("vendor/katex.min.js", "application/javascript"),
        ("vendor/katex.min.css", "text/css"),
    ]:
        req = urllib.request.Request(server.url() + "/static/" + rel)
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.status == 200
            assert r.headers.get("Content-Type", "").startswith(prefix)
            assert r.read()  # non-empty


def test_get_static_vendor_katex_font(server) -> None:
    """A KaTeX woff2 font is served (matched by the font-name pattern, not
    an exact whitelist entry) so the stylesheet's `url(fonts/…)` resolves."""
    req = urllib.request.Request(server.url() + "/static/vendor/fonts/KaTeX_Main-Regular.woff2")
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 200
        assert r.headers.get("Content-Type", "").startswith("font/woff2")
        assert r.read()


def test_get_static_vendor_font_traversal_404(server) -> None:
    """The font pattern doesn't become a generic reader: a non-KaTeX font
    name under vendor/fonts/ is refused."""
    try:
        urllib.request.urlopen(server.url() + "/static/vendor/fonts/evil.woff2", timeout=5)
    except urllib.error.HTTPError as e:
        assert e.code == 404
    else:
        raise AssertionError("expected 404 for unlisted font")


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
    # Debug off by default: the viewer won't mount the drawer.
    assert body["debug"] is False


def test_post_comment_upserts_and_persists(server, run_dir: paths.RunDir) -> None:
    code, body = _request(
        server.url() + "/comments",
        "POST",
        {
            "id": "c1",
            "file": "a.py",
            "side": "new",
            "line": 5,
            "body": "hmm",
        },
    )
    assert code == 200
    assert body["id"] == "c1"
    # File on disk
    comments_path = run_dir.comments
    assert comments_path.exists()
    data = json.loads(comments_path.read_text())
    assert data["comments"][0]["body"] == "hmm"

    # Update the same id
    code, body = _request(
        server.url() + "/comments",
        "POST",
        {
            "id": "c1",
            "file": "a.py",
            "side": "new",
            "line": 5,
            "body": "clearer",
        },
    )
    assert code == 200
    data = json.loads(comments_path.read_text())
    assert data["comments"][0]["body"] == "clearer"


def test_post_invalid_comment_400(server) -> None:
    req = urllib.request.Request(
        server.url() + "/comments",
        method="POST",
        data=json.dumps({"id": "x"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=5)
    except urllib.error.HTTPError as e:
        assert e.code == 400


def test_post_cannot_overwrite_ingested_comment(server, run_dir: paths.RunDir) -> None:
    """Ingested PR comments (source != local) are read-only — the server
    rejects an upsert that targets one with 403 rather than letting the
    body get rewritten in place."""
    # Seed comments.json with an ingested comment, then re-create the
    # server so it loads from the file we just wrote.
    run_dir.comments.write_text(
        json.dumps(
            {
                "comments": [
                    {
                        "id": "gh-1",
                        "file": "a.py",
                        "side": "new",
                        "line": 1,
                        "body": "upstream",
                        "source": "github",
                        "author": "alice",
                        "created_at": 1.0,
                        "updated_at": 1.0,
                    }
                ],
            }
        )
    )
    server.stop()
    srv2 = ReviewServer(run_dir=run_dir, viewer_json={"version": "1", "files": []})
    srv2.start()
    try:
        try:
            _request(
                srv2.url() + "/comments",
                "POST",
                {
                    "id": "gh-1",
                    "file": "a.py",
                    "side": "new",
                    "line": 1,
                    "body": "overwritten",
                },
            )
        except urllib.error.HTTPError as e:
            assert e.code == 403
        else:
            raise AssertionError("expected 403 for overwrite of ingested comment")
        # On disk the body is unchanged.
        data = json.loads(run_dir.comments.read_text())
        assert data["comments"][0]["body"] == "upstream"
    finally:
        srv2.stop()


def test_delete_cannot_remove_ingested_comment(server, run_dir: paths.RunDir) -> None:
    run_dir.comments.write_text(
        json.dumps(
            {
                "comments": [
                    {
                        "id": "gh-1",
                        "file": "a.py",
                        "side": "new",
                        "line": 1,
                        "body": "upstream",
                        "source": "github",
                        "author": "alice",
                        "created_at": 1.0,
                        "updated_at": 1.0,
                    }
                ],
            }
        )
    )
    server.stop()
    srv2 = ReviewServer(run_dir=run_dir, viewer_json={"version": "1", "files": []})
    srv2.start()
    try:
        conn = HTTPConnection("127.0.0.1", int(srv2.url().rsplit(":", 1)[1]), timeout=5)
        conn.request("DELETE", "/comments/gh-1")
        r = conn.getresponse()
        assert r.status == 403
        # Still on disk.
        data = json.loads(run_dir.comments.read_text())
        assert len(data["comments"]) == 1
    finally:
        srv2.stop()


def test_post_resets_source_to_local_on_new_comment(server, run_dir: paths.RunDir) -> None:
    """A new comment claiming source=github on the wire is still stored
    as local — provenance can only be set by the ingest path, not by a
    client POST."""
    code, body = _request(
        server.url() + "/comments",
        "POST",
        {
            "id": "c1",
            "file": "a.py",
            "side": "new",
            "line": 1,
            "body": "x",
            "source": "github",
            "author": "evil",
        },
    )
    assert code == 200
    assert body["source"] == "local"


def test_delete_comment(server, run_dir: paths.RunDir) -> None:
    _request(
        server.url() + "/comments",
        "POST",
        {
            "id": "c1",
            "file": "a.py",
            "side": "new",
            "line": 1,
            "body": "x",
        },
    )
    # Delete via stdlib (no helper method for DELETE with body)
    conn = HTTPConnection("127.0.0.1", int(server.url().rsplit(":", 1)[1]), timeout=5)
    conn.request("DELETE", "/comments/c1")
    r = conn.getresponse()
    assert r.status == 200
    assert json.load(r)["ok"] is True
    data = json.loads(run_dir.comments.read_text())
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


def _spawn_waiter(server, *, timeout: float, idle_poll: float) -> tuple[threading.Thread, dict]:
    """Run wait_until_done off-thread; the dict carries its verdict."""
    result: dict = {"done": None}

    def wait():
        result["done"] = server.wait_until_done(timeout=timeout, idle_poll=idle_poll)

    t = threading.Thread(target=wait, daemon=True)
    t.start()
    return t, result


def test_an_open_viewer_is_not_idle(server) -> None:
    """A registered SSE subscriber holds the server open indefinitely;
    the countdown only starts once the last one goes away."""
    q: queue.Queue = queue.Queue()
    with server.ctx.state_lock:
        server.ctx.subscribers.append(q)

    t, result = _spawn_waiter(server, timeout=0.2, idle_poll=0.02)
    t.join(timeout=1.0)
    assert t.is_alive()
    assert result["done"] is None

    with server.ctx.state_lock:
        server.ctx.subscribers.remove(q)
    t.join(timeout=2.0)
    assert result["done"] is False


def test_a_request_resets_the_idle_countdown(server) -> None:
    """Every route touches `last_activity`, and the countdown restarts
    from it — so a reviewer poking the server outlives the window."""
    t, result = _spawn_waiter(server, timeout=0.3, idle_poll=0.02)
    for _ in range(6):
        _request(server.url() + "/data.json")
        time.sleep(0.1)
    assert result["done"] is None

    t.join(timeout=3.0)
    assert result["done"] is False


def test_exit_beats_the_idle_countdown(server) -> None:
    """/exit returns True promptly even with the idle window wide open."""
    t, result = _spawn_waiter(server, timeout=60.0, idle_poll=0.02)
    _request(server.url() + "/exit", "POST", {})
    t.join(timeout=5.0)
    assert result["done"] is True


# --- client hangups -----------------------------------------------------


def test_a_dropped_connection_is_logged_not_printed(server, capfd, caplog) -> None:
    """A client that vanishes mid-request costs a debug line, not a
    traceback on the reviewer's terminal, and the server keeps serving."""
    caplog.set_level(logging.DEBUG, logger="semantic_code_review.review.server")
    port = int(server.url().rsplit(":", 1)[1])
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    # Headers left unterminated, so the handler is parked in readline when
    # the reset lands; SO_LINGER with a zero timeout sends RST rather than
    # FIN, which is what a frozen tab's teardown looks like to the server.
    s.sendall(b"GET /data.json HTTP/1.1\r\nHost: localhost\r\n")
    s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    s.close()

    for _ in range(100):
        if any("disconnected mid-request" in r.getMessage() for r in caplog.records):
            break
        time.sleep(0.02)
    else:
        raise AssertionError("hangup never reached handle_error")

    code, body = _request(server.url() + "/data.json")
    assert code == 200
    assert body["version"] == "1"
    assert "Traceback" not in capfd.readouterr().err


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


# --- session outcomes on the wire ---------------------------------------
# The session's own coverage is in tests/test_review_session.py; these
# two pin the one mapping only the transport can perform.


def _post(server, path: str, payload: dict) -> tuple[int, dict]:
    """POST without urlopen's raise-on-non-2xx, so the body is readable."""
    conn = HTTPConnection("127.0.0.1", int(server.url().rsplit(":", 1)[1]), timeout=5)
    conn.request("POST", path, body=json.dumps(payload), headers={"Content-Type": "application/json"})
    r = conn.getresponse()
    body = json.loads(r.read())
    conn.close()
    return r.status, body


def test_a_refusal_answers_with_the_status_it_carries(server) -> None:
    """Nothing is attached yet, so /fold-summary refuses — and the status
    comes off the error rather than out of the handler."""
    code, body = _post(server, "/fold-summary", {"file_idx": 0, "context": "right"})
    assert code == 409
    assert "augmentation" in body["error"]


def test_an_unexpected_failure_answers_500_naming_its_type(server) -> None:
    """A bug is not an outcome: the client still gets a response, and it
    names what went wrong rather than reporting a refusal."""

    async def boom(*_a, **_k):
        raise ZeroDivisionError("nope")

    server.attach(ServerTasks(fold_summary=boom), {"version": "1", "files": []})
    code, body = _post(server, "/fold-summary", {"file_idx": 0, "context": "right", "right_start": 1, "right_end": 3})
    assert code == 500
    assert body["error"] == "ZeroDivisionError: nope"


# --- The route table ------------------------------------------------------

#: Every route, and the status it answers before any task is attached —
#: the state a viewer meets while augmentation is still running. What each
#: one *does* is tested on the session; this holds the wire the viewer
#: addresses it by. The TS reads these paths and discriminates on these
#: codes, and it is not built from this table, so a rename here is a
#: broken viewer rather than a failing type check.
_ROUTES = [
    ("GET", "/data.json", 200),
    ("GET", "/comments", 200),
    ("GET", "/post-config", 200),
    ("GET", "/post-preview", 409),
    ("GET", "/explainer", 409),
    ("GET", "/file-text?file_idx=0", 404),
    ("GET", "/file-text?file_idx=abc", 400),
    ("GET", "/nope", 404),
    ("POST", "/fold-summary", 409),
    ("POST", "/console/ask", 409),
    ("POST", "/console/cancel", 200),
    ("POST", "/console/reset", 200),
    ("POST", "/explainer/skeleton", 409),
    ("POST", "/explainer/section/background", 409),
    ("POST", "/post-review", 409),
    ("POST", "/nope", 404),
    ("DELETE", "/comments/nope", 404),
    ("DELETE", "/nope", 404),
]


def _status(server, method: str, path: str) -> int:
    """The status alone, for the routes whose refusal is the point."""
    req = urllib.request.Request(
        server.url() + path,
        method=method,
        data=b"{}" if method == "POST" else None,
        headers={"Content-Type": "application/json"} if method == "POST" else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


@pytest.mark.parametrize(("method", "path", "expected"), _ROUTES, ids=str)
def test_a_route_answers_its_own_status(server, method: str, path: str, expected: int) -> None:
    assert _status(server, method, path) == expected


def test_an_out_of_range_file_is_not_a_malformed_one(server) -> None:
    """404 and 400 are different answers: the reviewer asked for a file
    that isn't there, or the viewer sent something that was never a file
    index. Only the second is a bug in the caller."""
    assert _status(server, "GET", "/file-text?file_idx=99") == 404
    assert _status(server, "GET", "/file-text?file_idx=abc") == 400


def test_cancel_and_reset_answer_without_a_console(server) -> None:
    """Both are idempotent: a console that was never started has nothing
    in flight and no history, which is the state they ask for."""
    for path in ("/console/cancel", "/console/reset"):
        code, body = _post(server, path, {})
        assert (code, body) == (200, {"ok": True})


def test_a_console_turn_is_accepted_not_awaited(server) -> None:
    """202, not 200: the answer arrives over SSE, and a viewer that read
    this as the turn's result would render an empty one."""
    started = threading.Event()

    def asker(*_a, **_k) -> None:
        started.set()

    server.attach(ServerTasks(console=asker), {"version": "1", "files": []})
    code, body = _post(server, "/console/ask", {"question": "why?"})
    assert code == 202
    assert "console_id" in body
    assert started.wait(timeout=5)


def test_the_house_style_reaches_both_explainer_generators(run_dir: paths.RunDir, monkeypatch) -> None:
    """The house style reaches the document and nothing else, so its only
    guard is that the layer holding it hands both generators the same
    value. Dropping it changes no shape and fails no other test."""
    from semantic_code_review.review import runner

    seen: dict[str, str | None] = {}

    def _spy(name: str):
        def build(*, house_style: str | None, **_kw):
            seen[name] = house_style
            return lambda *_a, **_k: None

        return build

    monkeypatch.setattr(runner, "_build_explainer_task", _spy("skeleton"))
    monkeypatch.setattr(runner, "_build_explainer_section_task", _spy("section"))
    runner.build_server_tasks(run_dir, _task_config(augment=True, explainer_prompt="name the dataset"))
    assert seen == {"skeleton": "name the dataset", "section": "name the dataset"}


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


def _populate_minimal_run_dir(run_dir: paths.RunDir) -> None:
    run_dir.raw_diff.write_text(_RAW_DIFF_FOR_RUN, encoding="utf-8")
    run_dir.meta.write_text(
        json.dumps(
            {
                "title": "Bump",
                "author": {"login": "tester"},
                "url": "",
                "baseRefOid": "aaa",
                "headRefOid": "bbb",
            }
        ),
        encoding="utf-8",
    )


def test_serve_review_serves_pending_then_streams_and_finalises(run_dir: paths.RunDir) -> None:
    """End-to-end: serve_review starts the server with pending
    viewer JSON, runs the augment closure (which can publish streaming
    events via the supplied publish callable), then swaps /data.json
    to the post-augment state and fires `done` once augmentation
    finishes."""
    from semantic_code_review.review.config import ReviewConfig
    from semantic_code_review.review.runner import serve_review

    _populate_minimal_run_dir(run_dir)

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
            None,
            augment_release.wait,
            5.0,
        )
        # Mimic a per-hunk completion before the pipeline writes its
        # final on-disk output. The page would react by patching the
        # hunk slot; here we just confirm the callable was wired in.
        publish("hunk", {"file_idx": 0, "hunk_idx": 0, "ok": True, "block": {"id": "H0_0"}})
        rd.augmented.write_text(_RAW_DIFF_FOR_RUN, encoding="utf-8")
        augment_finished.set()

    result_box: dict = {}

    def run_serve() -> None:
        def _on_ready(url: str) -> None:
            ready_url["url"] = url
            url_ready.set()

        result_box["r"] = serve_review(
            run_dir,
            ReviewConfig(port=0, timeout=10, open_browser=False),
            ServerTasks(augment=fake_augment),
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


def test_serve_review_reports_the_idle_shutdown(run_dir: paths.RunDir, capsys) -> None:
    """Both CLI entry points come through serve_review, so the idle
    shutdown names itself here — once, before either prints comments."""
    from semantic_code_review.review.config import ReviewConfig
    from semantic_code_review.review.runner import serve_review

    _populate_minimal_run_dir(run_dir)
    run_dir.augmented.write_text(_RAW_DIFF_FOR_RUN, encoding="utf-8")

    result = serve_review(
        run_dir,
        ReviewConfig(port=0, timeout=1, open_browser=False),
        ServerTasks(),
    )
    assert result.clean is False
    assert "idle timeout — 1s with no request and no open viewer" in capsys.readouterr().err


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


# --- server-task bundle -------------------------------------------------
# One builder serves both entry points, so these cover `scr review` and
# `scr pr` at once: a generator added to the bundle reaches both or
# neither.


def _task_config(*, augment: bool, debug: bool = False, explainer_prompt: str | None = None):
    from semantic_code_review.augment.agents import Client
    from semantic_code_review.review.config import ReviewConfig

    return ReviewConfig(
        augment=augment,
        model="claude-opus-4-7",
        concurrency=4,
        no_cache=True,
        open_browser=False,
        timeout=1,
        client=Client(model="anthropic:claude-opus-4-7"),
        debug=debug,
        explainer_prompt=explainer_prompt,
    )


def test_build_server_tasks_wires_console_when_augmenting(run_dir: paths.RunDir) -> None:
    from semantic_code_review.review.runner import build_server_tasks

    tasks = build_server_tasks(run_dir, _task_config(augment=True))
    assert tasks.augment is not None
    assert tasks.fold_summary is not None
    assert tasks.console is not None  # the console callback the server installs
    assert tasks.explainer is not None  # opt-out, so on unless config says otherwise
    # Debug off by default → no sink binder.
    assert tasks.bind_debug_sink is None


def test_build_server_tasks_binds_debug_sink_when_debug(run_dir: paths.RunDir) -> None:
    from semantic_code_review.review.runner import build_server_tasks

    tasks = build_server_tasks(run_dir, _task_config(augment=True, debug=True))
    assert tasks.bind_debug_sink is not None


def test_build_server_tasks_omits_the_explainer_when_it_is_disabled(run_dir: paths.RunDir) -> None:
    import dataclasses

    from semantic_code_review.review.runner import build_server_tasks

    cfg = dataclasses.replace(_task_config(augment=True), explainer=False)
    tasks = build_server_tasks(run_dir, cfg)
    # Everything else still wires; only the explainer drops out, so the
    # server reports the feature disabled rather than "not ready yet".
    assert tasks.console is not None
    assert tasks.explainer is None
    assert tasks.explainer_section is None


def test_build_server_tasks_wires_both_explainer_generators(run_dir: paths.RunDir) -> None:
    """The skeleton and the per-section pass go together.

    `scr pr` shipped with only the skeleton wired: the Map rendered, and
    every prose section then 409'd with "augmentation still in progress"
    long after augmentation had finished, because that is the message an
    unbound generator produces.
    """
    from semantic_code_review.review.runner import build_server_tasks

    tasks = build_server_tasks(run_dir, _task_config(augment=True))
    assert tasks.explainer is not None
    assert tasks.explainer_section is not None


def test_a_disabled_explainer_gets_no_house_style_either(run_dir: paths.RunDir) -> None:
    """`explainer = false` means no document, so nothing to style. Both
    generators go unwired together, as they already do."""
    import dataclasses

    from semantic_code_review.review.runner import build_server_tasks

    cfg = dataclasses.replace(
        _task_config(augment=True, explainer_prompt="name the dataset"),
        explainer=False,
    )
    tasks = build_server_tasks(run_dir, cfg)
    assert tasks.explainer is None
    assert tasks.explainer_section is None


def test_build_server_tasks_returns_an_empty_bundle_without_augment(run_dir: paths.RunDir) -> None:
    from semantic_code_review.review.runner import build_server_tasks

    assert build_server_tasks(run_dir, _task_config(augment=False)) == ServerTasks()
