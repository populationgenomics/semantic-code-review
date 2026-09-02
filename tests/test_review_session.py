"""The review session: what a reviewer can ask of a run, minus the socket.

Everything here used to be reachable only by binding a port and speaking
HTTP. It is domain logic — the generated-file fold policy, the busy
guards, the explainer flows, the post path — so it is tested against
`ReviewSession` directly. `tests/test_review_server.py` keeps what is
genuinely transport: SSE framing and replay, the client-hangup path, the
idle timeout.

A session publishes through the SSE fan-out rather than owning it, so
`_Frames` stands in for it here and the assertions on broadcasts read
off a list instead of a socket.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from semantic_code_review import errors
from semantic_code_review.augment import explainer_schema
from semantic_code_review.review.comments import CommentStore
from semantic_code_review.review.session import ReviewSession, ServerTasks


class _Frames:
    """The SSE fan-out, recorded rather than written to a socket."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        # How far `wait_for` has read. Successive waits pick up where the
        # last one stopped, so a second turn doesn't match the first
        # turn's terminal frame.
        self._cursor = 0
        self.frames: list[tuple[str, dict, bool]] = []

    def publish(self, event_type: str, payload: dict, *, buffer: bool = True) -> None:
        with self._cond:
            self.frames.append((event_type, payload, buffer))
            self._cond.notify_all()

    def types(self) -> list[str]:
        with self._cond:
            return [t for t, _, _ in self.frames]

    def payloads(self, event_type: str) -> list[dict]:
        with self._cond:
            return [p for t, p, _ in self.frames if t == event_type]

    def wait_for(self, *event_types: str, timeout: float = 5.0) -> dict:
        """Block until a frame of one of these types lands; return it."""
        deadline = time.time() + timeout
        with self._cond:
            while True:
                while self._cursor < len(self.frames):
                    etype, payload, _ = self.frames[self._cursor]
                    self._cursor += 1
                    if etype in event_types:
                        return payload
                remaining = deadline - time.time()
                if remaining <= 0 or not self._cond.wait(remaining):
                    raise AssertionError(f"no {event_types} frame within {timeout}s (got {self.types()})")


class _Harness:
    """A session plus the frames it published — a review server's state
    without its socket. `attach` delivers the augmented diff, which is
    the one event that unlocks the LLM-backed operations.
    """

    def __init__(self, run_dir: Path, *, viewer_json: dict | None = None, **kwargs: Any) -> None:
        self.viewer_json = {"version": "1", "files": []} if viewer_json is None else viewer_json
        self.frames = _Frames()
        self.session = ReviewSession(
            run_dir=run_dir,
            viewer_json=self.viewer_json,
            store=CommentStore(run_dir / "comments.json"),
            publish=self.frames.publish,
            **kwargs,
        )

    def attach(self, **tasks: Any) -> None:
        self.session.attach(ServerTasks(**tasks), self.viewer_json)


def _refused(op) -> errors.ScrError:
    """Run an operation expected to refuse; hand back the refusal.

    Every refusal carries the status and body the route answers with, so
    the assertions here are the same contract the wire has.
    """
    with pytest.raises(errors.ScrError) as excinfo:
        op()
    return excinfo.value


def _wait_until(predicate, *, what: str, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


# --- /data.json ---------------------------------------------------------


def test_data_json_carries_the_runtime_flags(tmp_path: Path) -> None:
    """`debug` mounts the raw-log drawer and `explainer` the overview-mode
    button; both ride the payload because the viewer decides on its first
    fetch, long before any pass has run."""
    plain = _Harness(tmp_path).session.data_json()
    assert plain["version"] == "1"
    assert plain["debug"] is False
    assert plain["explainer"] is False

    flagged = _Harness(tmp_path, debug=True, explainer_enabled=True).session.data_json()
    assert flagged["debug"] is True
    assert flagged["explainer"] is True


def test_set_viewer_json_replaces_the_payload(tmp_path: Path) -> None:
    h = _Harness(tmp_path)
    h.session.set_viewer_json({"version": "1", "files": [], "marker": "ok"})
    assert h.session.data_json()["marker"] == "ok"


def test_attach_swaps_the_diff_and_unlocks_the_tasks(tmp_path: Path) -> None:
    """One event, one call: the sidecar landed, so the augmented view and
    the tasks that read it arrive together."""
    h = _Harness(tmp_path)
    assert _refused(lambda: h.session.fold_summary({})).status == 409

    async def task(*_a: Any) -> dict:
        return {"summary": "s"}

    h.session.attach(ServerTasks(fold_summary=task), {"version": "1", "files": [], "marker": "augmented"})
    assert h.session.data_json()["marker"] == "augmented"
    assert h.session.fold_summary({"file_idx": 0, "context": "right", "right_start": 1, "right_end": 3})


# --- fold summaries -----------------------------------------------------


def _fold_viewer_json(*, status: str | None = None) -> dict:
    file: dict[str, Any] = {
        "id": "F0",
        "path": "src/x.py",
        "hunks": [
            {
                "id": "H0_0",
                "fold_regions": [
                    {
                        "context": "right",
                        "right_start": 1,
                        "right_end": 3,
                        "left_start": 0,
                        "left_end": 0,
                        "qualified_name": "Foo.bar",
                        "kind": "function",
                        "summary": "",
                    }
                ],
            }
        ],
    }
    if status is not None:
        file["status"] = status
    return {"version": "1", "files": [file]}


def test_fold_summary_refuses_before_the_summariser_is_attached(tmp_path: Path) -> None:
    err = _refused(lambda: _Harness(tmp_path).session.fold_summary({"file_idx": 0, "context": "right"}))
    assert err.status == 409
    assert "augmentation" in err.body()["error"]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"file_idx": 0}, "context must be"),
        ({"file_idx": 0, "context": "sideways"}, "context must be"),
        ({"context": "right", "right_start": 1, "right_end": 3}, "file_idx must be an integer"),
        ({"file_idx": 0, "context": "right"}, "right_start/right_end required"),
        ({"file_idx": 0, "context": "left"}, "left_start/left_end required"),
        ({"file_idx": 0, "context": "both", "right_start": 1, "right_end": 3}, "left_start/left_end required"),
    ],
)
def test_fold_summary_rejects_a_malformed_address(tmp_path: Path, payload: dict, message: str) -> None:
    h = _Harness(tmp_path)

    async def unreachable(*_a: Any) -> dict:
        raise AssertionError("a malformed address must not reach the summariser")

    h.attach(fold_summary=unreachable)
    err = _refused(lambda: h.session.fold_summary(payload))
    assert err.status == 400
    assert message in err.body()["error"]


def test_fold_summary_seeds_the_symbol_patches_and_broadcasts(tmp_path: Path) -> None:
    """The address resolves to a server-computed region, whose symbol
    seeds the prompt; the result is patched into the viewer JSON and
    fanned out as well as returned."""
    h = _Harness(tmp_path, viewer_json=_fold_viewer_json())
    captured: dict[str, Any] = {}

    async def fake_task(file_idx, context, right_range, left_range, qualified_name=None, kind=None) -> dict:
        captured.update(
            file_idx=file_idx,
            context=context,
            right_range=right_range,
            left_range=left_range,
            qualified_name=qualified_name,
            kind=kind,
        )
        return {
            "file_idx": file_idx,
            "context": context,
            "right_start": 1,
            "right_end": 3,
            "left_start": 0,
            "left_end": 0,
            "summary": "wraps the body in a try/except",
        }

    h.attach(fold_summary=fake_task)
    result = h.session.fold_summary({"file_idx": 0, "context": "right", "right_start": 1, "right_end": 3})

    assert result["summary"].startswith("wraps the body")
    assert captured == {
        "file_idx": 0,
        "context": "right",
        "right_range": (1, 3),
        "left_range": None,
        "qualified_name": "Foo.bar",
        "kind": "function",
    }
    served = h.session.data_json()["files"][0]["hunks"][0]["fold_regions"][0]
    assert served["summary"].startswith("wraps the body")
    assert h.frames.payloads("fold-summary") == [result]


def test_fold_summary_leaves_an_unknown_region_unseeded(tmp_path: Path) -> None:
    """A client-only region over expanded context matches nothing the
    server computed, so the prompt goes without a symbol rather than
    with the wrong one."""
    h = _Harness(tmp_path, viewer_json=_fold_viewer_json())
    seen: dict[str, Any] = {}

    async def fake_task(file_idx, context, right_range, left_range, qualified_name=None, kind=None) -> dict:
        seen.update(qualified_name=qualified_name, kind=kind)
        return {"summary": "s"}

    h.attach(fold_summary=fake_task)
    h.session.fold_summary({"file_idx": 0, "context": "right", "right_start": 40, "right_end": 48})
    assert seen == {"qualified_name": None, "kind": None}


@pytest.mark.parametrize("status", ["generated", "binary"])
def test_fold_summary_skips_an_unreviewed_file(tmp_path: Path, status: str) -> None:
    """A generated / lock / binary file gets a canned summary and the
    summariser is never invoked — expanding a fold inside one must not be
    the way a lock file reaches the model."""
    h = _Harness(tmp_path, viewer_json=_fold_viewer_json(status=status))

    async def boom(*_a: Any) -> dict:
        raise AssertionError("fold summariser must not run for a generated file")

    h.attach(fold_summary=boom)
    result = h.session.fold_summary({"file_idx": 0, "context": "right", "right_start": 1, "right_end": 3})

    assert "not summarised" in result["summary"]
    # The canned note is patched in and broadcast like a real one.
    served = h.session.data_json()["files"][0]["hunks"][0]["fold_regions"][0]
    assert served["summary"] == result["summary"]
    assert h.frames.payloads("fold-summary") == [result]


def test_fold_summary_for_left_context_passes_the_ranges_through(tmp_path: Path) -> None:
    """A pure-deletion fold posts {context:'left', left_start, left_end};
    the same task is called with right_range=None."""
    h = _Harness(tmp_path)
    seen: dict[str, Any] = {}

    async def fake_task(file_idx, context, right_range, left_range, qualified_name=None, kind=None) -> dict:
        seen.update(context=context, right_range=right_range, left_range=left_range)
        return {"context": context, "left_start": 12, "left_end": 14, "summary": "drops the legacy retry loop"}

    h.attach(fold_summary=fake_task)
    result = h.session.fold_summary({"file_idx": 0, "context": "left", "left_start": 12, "left_end": 14})

    assert seen == {"context": "left", "right_range": None, "left_range": (12, 14)}
    assert result["context"] == "left" and result["left_start"] == 12


def test_fold_summary_typed_errors_carry_their_statuses(tmp_path: Path) -> None:
    """`FoldSummaryNotReady` → 409; `FoldSummaryFileIndexError` → 404."""
    from semantic_code_review.augment.fold_summary import (
        FoldSummaryFileIndexError,
        FoldSummaryNotReady,
    )

    h = _Harness(tmp_path)
    address = {"file_idx": 0, "context": "right", "right_start": 1, "right_end": 3}

    async def not_ready(*_a: Any) -> dict:
        raise FoldSummaryNotReady("sidecar gone walkabout")

    h.attach(fold_summary=not_ready)
    err = _refused(lambda: h.session.fold_summary(address))
    assert err.status == 409
    assert "walkabout" in err.body()["error"]

    async def out_of_range(*_a: Any) -> dict:
        raise FoldSummaryFileIndexError("file_idx 999 not in diff")

    h.attach(fold_summary=out_of_range)
    err = _refused(lambda: h.session.fold_summary({**address, "file_idx": 999}))
    assert err.status == 404
    assert "999" in err.body()["error"]


def test_a_failed_fold_summary_broadcasts_nothing(tmp_path: Path) -> None:
    """An unexpected failure is a bug, not an outcome: it propagates for
    the route to answer 500 with, and no tab is told a summary landed."""
    h = _Harness(tmp_path)

    async def boom(*_a: Any) -> dict:
        raise RuntimeError("model said no")

    h.attach(fold_summary=boom)
    with pytest.raises(RuntimeError):
        h.session.fold_summary({"file_idx": 0, "context": "right", "right_start": 1, "right_end": 3})
    assert h.frames.types() == []


# --- file text (rendered markdown mode) ---------------------------------


def _file_text_harness(tmp_path: Path, files: list[dict]) -> _Harness:
    return _Harness(tmp_path, viewer_json={"version": "1", "files": files})


def test_file_text_serves_base_and_head(tmp_path: Path) -> None:
    (tmp_path / "base").mkdir()
    (tmp_path / "head").mkdir()
    (tmp_path / "base" / "doc.md").write_text("# Old\n\nbase body\n")
    (tmp_path / "head" / "doc.md").write_text("# New\n\nhead body\n")
    h = _file_text_harness(tmp_path, [{"id": "F0", "path": "doc.md", "old_path": None, "status": "modified"}])

    payload = h.session.file_text(0)

    assert payload["path"] == "doc.md"
    assert payload["base"] == "# Old\n\nbase body\n"
    assert payload["head"] == "# New\n\nhead body\n"


def test_file_text_added_file_has_null_base(tmp_path: Path) -> None:
    """An added file has no pre-image: base is None, head is present."""
    (tmp_path / "head").mkdir()
    (tmp_path / "head" / "new.md").write_text("# Added\n")
    h = _file_text_harness(tmp_path, [{"id": "F0", "path": "new.md", "old_path": None, "status": "added"}])

    payload = h.session.file_text(0)

    assert payload["base"] is None
    assert payload["head"] == "# Added\n"


def test_file_text_renamed_reads_base_from_old_path(tmp_path: Path) -> None:
    (tmp_path / "base").mkdir()
    (tmp_path / "head").mkdir()
    (tmp_path / "base" / "old.md").write_text("was here\n")
    (tmp_path / "head" / "new.md").write_text("now here\n")
    h = _file_text_harness(tmp_path, [{"id": "F0", "path": "new.md", "old_path": "old.md", "status": "renamed"}])

    payload = h.session.file_text(0)

    assert payload["base"] == "was here\n"
    assert payload["head"] == "now here\n"


def test_file_text_out_of_range(tmp_path: Path) -> None:
    err = _refused(lambda: _file_text_harness(tmp_path, []).session.file_text(5))
    assert err.status == 404
    assert "5" in err.body()["error"]


def test_file_text_path_traversal_refused(tmp_path: Path) -> None:
    """A traversing old_path yields None rather than escaping the worktree."""
    (tmp_path / "secret.md").write_text("top secret\n")
    (tmp_path / "head").mkdir()
    (tmp_path / "head" / "doc.md").write_text("ok\n")
    h = _file_text_harness(tmp_path, [{"id": "F0", "path": "doc.md", "old_path": "../secret.md", "status": "renamed"}])

    assert h.session.file_text(0)["base"] is None


# --- console (free-form Q&A) --------------------------------------------


def _console_end(frames: _Frames) -> dict:
    return frames.wait_for("console-done", "console-error")


def test_console_refuses_before_the_asker_is_attached(tmp_path: Path) -> None:
    """A --no-augment review, or one still augmenting, has nothing to
    ground a conversation against."""
    err = _refused(lambda: _Harness(tmp_path).session.console_turn({"question": "what changed?"}))
    assert err.status == 409
    assert "console" in err.body()["error"]


def test_console_refuses_an_empty_question(tmp_path: Path) -> None:
    h = _Harness(tmp_path)

    async def asker(*_a: Any) -> tuple[str, list]:
        raise AssertionError("an empty question must not start a turn")

    h.attach(console=asker)
    err = _refused(lambda: h.session.console_turn({"question": "   "}))
    assert err.status == 400


def test_console_streams_and_threads_history(tmp_path: Path) -> None:
    """A turn streams delta/tool frames tagged with the console id,
    finishes with console-done carrying the answer, and threads the
    returned history into the next turn."""
    h = _Harness(tmp_path)
    seen: list = []

    async def asker(question, history, on_delta, on_tool, cancel, selection=None) -> tuple[str, list]:
        seen.append((question, history, selection))
        on_tool("grep RepoTools")
        on_delta("answer ")
        on_delta(f"to {question!r}")
        return f"answer to {question!r}", [*(history or []), question]

    h.attach(console=asker)
    assert h.session.console_turn({"question": "why pagination?", "console_id": "tab-1"}) == "tab-1"

    done = _console_end(h.frames)
    assert h.frames.types() == ["console-tool", "console-delta", "console-delta", "console-done"]
    # Every frame is tagged with the requesting tab, and none is buffered:
    # a reload starts the conversation fresh rather than replaying it.
    assert all(p["console_id"] == "tab-1" for _, p, _ in h.frames.frames)
    assert not any(buffered for _, _, buffered in h.frames.frames)
    assert h.frames.payloads("console-tool")[0]["label"] == "grep RepoTools"
    assert "".join(p["text"] for p in h.frames.payloads("console-delta")) == "answer to 'why pagination?'"
    assert done["answer"] == "answer to 'why pagination?'"
    assert seen[0] == ("why pagination?", None, None)

    _wait_until(lambda: not h.session.console_busy, what="the turn to release the slot")
    h.session.console_turn({"question": "follow-up", "console_id": "tab-1"})
    _console_end(h.frames)
    assert seen[1] == ("follow-up", ["why pagination?"], None)


def test_console_passes_a_pinned_selection_through(tmp_path: Path) -> None:
    """The reviewer's pinned selection rides the turn opaquely; anything
    that isn't an object is dropped rather than handed on."""
    h = _Harness(tmp_path)
    seen: list = []

    async def asker(question, history, on_delta, on_tool, cancel, selection=None) -> tuple[str, list]:
        seen.append(selection)
        return "ok", []

    h.attach(console=asker)
    h.session.console_turn({"question": "q", "selection": {"file": "a.py"}})
    _console_end(h.frames)
    _wait_until(lambda: not h.session.console_busy, what="the turn to release the slot")
    h.session.console_turn({"question": "q", "selection": "not an object"})
    _console_end(h.frames)

    assert seen == [{"file": "a.py"}, None]


def test_console_cancel_discards_the_turn(tmp_path: Path) -> None:
    """Cancelling ends the turn with a cancelled console-done and leaves
    the conversation history untouched."""
    from semantic_code_review.augment.console import ConsoleCancelled

    h = _Harness(tmp_path)

    async def asker(question, history, on_delta, on_tool, cancel, selection=None) -> tuple[str, list]:
        on_delta("partial")
        while not cancel.is_set():
            await asyncio.sleep(0.01)
        raise ConsoleCancelled("console turn cancelled")

    h.attach(console=asker)
    h.session.console_turn({"question": "why?", "console_id": "tab-1"})
    assert h.frames.wait_for("console-delta")["text"] == "partial"

    h.session.console_cancel()

    assert _console_end(h.frames)["cancelled"] is True
    _wait_until(lambda: not h.session.console_busy, what="the turn to release the slot")
    assert h.session.console_history is None


def test_console_allows_one_turn_at_a_time(tmp_path: Path) -> None:
    from semantic_code_review.augment.console import ConsoleCancelled

    h = _Harness(tmp_path)

    async def asker(question, history, on_delta, on_tool, cancel, selection=None) -> tuple[str, list]:
        while not cancel.is_set():
            await asyncio.sleep(0.01)
        raise ConsoleCancelled("console turn cancelled")

    h.attach(console=asker)
    h.session.console_turn({"question": "q1"})
    _wait_until(lambda: h.session.console_busy, what="the first turn to take the slot")

    err = _refused(lambda: h.session.console_turn({"question": "q2"}))
    assert err.status == 409
    assert "in flight" in err.body()["error"]

    h.session.console_cancel()
    _console_end(h.frames)
    _wait_until(lambda: not h.session.console_busy, what="the turn to release the slot")


def test_a_failing_turn_becomes_a_console_error_frame(tmp_path: Path) -> None:
    """The worker must not die with the turn: the failure streams as a
    console-error and the slot is released for a retry."""
    h = _Harness(tmp_path)

    async def asker(*_a: Any) -> tuple[str, list]:
        raise RuntimeError("boom")

    h.attach(console=asker)
    h.session.console_turn({"question": "x", "console_id": "tab-1"})

    assert "boom" in _console_end(h.frames)["error"]
    _wait_until(lambda: not h.session.console_busy, what="the turn to release the slot")


def test_a_refusal_mid_turn_states_itself(tmp_path: Path) -> None:
    """`ConsoleNotReady` is an outcome, so its message stands alone —
    only a bug gets its exception type named in the frame."""
    from semantic_code_review.augment.console import ConsoleNotReady

    h = _Harness(tmp_path)

    async def asker(*_a: Any) -> tuple[str, list]:
        raise ConsoleNotReady("augmented.scr.json missing")

    h.attach(console=asker)
    h.session.console_turn({"question": "x", "console_id": "tab-1"})

    assert _console_end(h.frames)["error"] == "augmented.scr.json missing"


def test_console_reset_clears_the_conversation(tmp_path: Path) -> None:
    h = _Harness(tmp_path)
    seen: list = []

    async def asker(question, history, on_delta, on_tool, cancel, selection=None) -> tuple[str, list]:
        seen.append(history)
        return "a", ["turn"]

    h.attach(console=asker)
    h.session.console_turn({"question": "q1"})
    _console_end(h.frames)
    _wait_until(lambda: not h.session.console_busy, what="the turn to release the slot")
    assert h.session.console_history == ["turn"]

    h.session.console_reset()

    assert h.session.console_history is None
    h.session.console_turn({"question": "q2"})
    _console_end(h.frames)
    assert seen == [None, None]


# --- change explainer (ADR 0007) ----------------------------------------


def _explainer_doc(base_sha: str = "base1234", head_sha: str = "head5678") -> dict:
    return {
        "version": explainer_schema.DOCUMENT_VERSION,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "verdict": "narrate",
        "verdict_note": "a cursor threaded from the proto to the client.",
        "figure_family": "boxes are services",
        "cast": ["ListRequest"],
        "toy_data": False,
        "turns_used": 0,
        "sections": [
            {
                "id": "background",
                "kind": "background",
                "pass_id": "background",
                "title": "Background",
                "state": "pending",
            },
            {
                "id": "map",
                "kind": "map",
                "pass_id": "skeleton",
                "title": "Map",
                "state": "ready",
                "refs": [{"kind": "file", "id": "F0"}],
                "map_rows": [{"ref": {"kind": "file", "id": "F0"}, "why": "the contract"}],
            },
        ],
        "dropped_refs": 0,
    }


def _explainer_harness(tmp_path: Path, *, enabled: bool = True) -> _Harness:
    return _Harness(
        tmp_path,
        viewer_json={"version": "1", "pr": {"base_sha": "base1234", "head_sha": "head5678"}, "files": []},
        explainer_enabled=enabled,
    )


def test_explainer_refuses_before_the_generator_is_attached(tmp_path: Path) -> None:
    """Still augmenting: the refusal is transient, so it says `retry` and
    the caller re-queues rather than latching the section to `failed`."""
    h = _explainer_harness(tmp_path)
    for op in (h.session.explainer_skeleton, lambda: h.session.explainer_section("code")):
        err = _refused(op)
        assert err.status == 409
        assert "augmentation" in err.body()["error"]
        assert err.body()["retry"] is True


def test_explainer_says_disabled_rather_than_not_ready(tmp_path: Path) -> None:
    """Disabled is a different answer from not-ready, and the message says
    so — otherwise the viewer tells the reviewer to wait forever."""
    h = _explainer_harness(tmp_path, enabled=False)
    for op in (h.session.explainer_skeleton, h.session.get_explainer, lambda: h.session.explainer_section("code")):
        err = _refused(op)
        assert err.status == 409
        assert "disabled" in err.body()["error"]
        assert "retry" not in err.body()


def test_get_explainer_404s_until_one_is_generated(tmp_path: Path) -> None:
    """404 is the press-the-button state, not an error: generation is
    reviewer-initiated and an ungenerated document costs nothing."""
    assert _refused(_explainer_harness(tmp_path).session.get_explainer).status == 404


def test_explainer_skeleton_persists_broadcasts_and_serves(tmp_path: Path) -> None:
    h = _explainer_harness(tmp_path)
    calls: list[int] = []

    async def generator() -> dict:
        calls.append(1)
        doc = _explainer_doc()
        (tmp_path / "explainer.json").write_text(json.dumps(doc), encoding="utf-8")
        return doc

    h.attach(explainer=generator)
    payload = h.session.explainer_skeleton()

    assert payload["sections"][-1]["map_rows"][0]["why"] == "the contract"
    assert h.frames.payloads("explainer") == [payload]
    # GET serves what was persisted, and a second press reuses it rather
    # than paying for the call twice.
    assert h.session.get_explainer()["verdict_note"].startswith("a cursor")
    h.session.explainer_skeleton()
    assert len(calls) == 1


def test_explainer_discards_a_document_from_another_diff(tmp_path: Path) -> None:
    """The run moved on; the prose describes code that may be gone. The
    document is dropped wholesale rather than re-anchored."""
    (tmp_path / "explainer.json").write_text(json.dumps(_explainer_doc(head_sha="stale999")), encoding="utf-8")
    assert _refused(_explainer_harness(tmp_path).session.get_explainer).status == 404


def test_explainer_skeleton_regenerates_over_a_corrupt_document(tmp_path: Path) -> None:
    """A torn write must not wedge the button — regenerating is the
    repair. GET still reports the corruption loudly."""
    (tmp_path / "explainer.json").write_text("{ not json", encoding="utf-8")
    h = _explainer_harness(tmp_path)
    assert _refused(h.session.get_explainer).status == 500

    async def generator() -> dict:
        return _explainer_doc()

    h.attach(explainer=generator)
    assert h.session.explainer_skeleton()["verdict"] == "narrate"


def test_explainer_skeleton_not_ready_is_a_409(tmp_path: Path) -> None:
    from semantic_code_review.augment.explainer import ExplainerNotReady

    h = _explainer_harness(tmp_path)

    async def generator() -> dict:
        raise ExplainerNotReady("augmented.scr.json missing — augment not complete")

    h.attach(explainer=generator)
    err = _refused(h.session.explainer_skeleton)
    assert err.status == 409
    assert "augment not complete" in err.body()["error"]


def test_a_failed_skeleton_releases_the_slot(tmp_path: Path) -> None:
    h = _explainer_harness(tmp_path)

    async def generator() -> dict:
        raise RuntimeError("model said no")

    h.attach(explainer=generator)
    with pytest.raises(RuntimeError, match="model said no"):
        h.session.explainer_skeleton()
    assert h.session.explainer_busy is False
    assert h.frames.types() == []


def test_explainer_runs_one_pass_at_a_time(tmp_path: Path) -> None:
    """One generation per session, skeleton and per-section alike: a
    second POST while one is in flight gets `retry` rather than a
    duplicate spend, and that is also what keeps two passes from
    interleaving their read-modify-write of explainer.json."""
    h = _explainer_harness(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    async def slow_generator() -> dict:
        entered.set()
        await asyncio.get_running_loop().run_in_executor(None, release.wait, 5.0)
        return _explainer_doc()

    async def section_generator(section_id: str) -> dict:
        raise AssertionError("the slot was held; this must not run")

    h.attach(explainer=slow_generator, explainer_section=section_generator)
    worker = threading.Thread(target=h.session.explainer_skeleton, daemon=True)
    worker.start()
    assert entered.wait(timeout=5)

    err = _refused(lambda: h.session.explainer_section("background"))
    assert err.status == 409
    assert err.body()["retry"] is True

    release.set()
    worker.join(timeout=5)
    assert h.session.explainer_busy is False


def test_explainer_section_writes_and_broadcasts_the_document(tmp_path: Path) -> None:
    """The id addresses a section; what comes back is the whole document,
    because a prose call may write more than one."""
    h = _explainer_harness(tmp_path)
    asked: list[str] = []

    async def generator(section_id: str) -> dict:
        asked.append(section_id)
        doc = _explainer_doc()
        doc["sections"][0].update({"state": "ready", "body": "the system before."})
        return doc

    h.attach(explainer_section=generator)
    payload = h.session.explainer_section("background")

    assert asked == ["background"]
    assert payload["sections"][0]["body"] == "the system before."
    assert h.frames.payloads("explainer") == [payload]


def test_explainer_section_reports_what_it_is_waiting_for(tmp_path: Path) -> None:
    """Prose over half the intents reads as fluently as prose over all of
    them, so the reviewer is told the counts rather than handed it."""
    from semantic_code_review.augment.explainer_section import SectionNotReady

    h = _explainer_harness(tmp_path)

    async def generator(section_id: str) -> dict:
        raise SectionNotReady(12, 31)

    h.attach(explainer_section=generator)
    err = _refused(lambda: h.session.explainer_section("code"))
    assert err.status == 409
    body = err.body()
    assert (body["annotated"], body["total"]) == (12, 31)
    assert "12 of 31" in body["error"]


def test_explainer_section_404s_on_a_section_that_is_not_there(tmp_path: Path) -> None:
    from semantic_code_review.augment.explainer_section import SectionNotFound

    h = _explainer_harness(tmp_path)

    async def generator(section_id: str) -> dict:
        raise SectionNotFound(section_id)

    h.attach(explainer_section=generator)
    err = _refused(lambda: h.session.explainer_section("nonesuch"))
    assert err.status == 404
    assert err.body()["error"] == "no section 'nonesuch' in this document"


def test_a_failed_section_is_broadcast_so_every_tab_sees_it_retryable(tmp_path: Path) -> None:
    """The pass raised, but the section is persisted `failed` — the other
    tabs must not sit on `pending` forever."""
    from semantic_code_review.augment.explainer_section import SectionFailed

    h = _explainer_harness(tmp_path)
    failed = _explainer_doc()
    failed["sections"][0]["state"] = "failed"

    async def generator(section_id: str) -> dict:
        raise SectionFailed("RuntimeError: model said no", failed)

    h.attach(explainer_section=generator)
    err = _refused(lambda: h.session.explainer_section("background"))

    assert err.status == 500
    assert "model said no" in err.body()["error"]
    assert h.frames.payloads("explainer")[0]["sections"][0]["state"] == "failed"
    # The slot is released, so a retry is possible.
    assert h.session.explainer_busy is False


# --- post (confirm-and-post modal) --------------------------------------


class _FakePostResult:
    """A `PostResult`-shaped outcome, minus the GitHub round trip."""

    def __init__(self, *, review_id: int = 42, review_url: str = "https://gh/r/1", posted: int = 1) -> None:
        self.review_id = review_id
        self.review_url = review_url
        self.posted = posted


def _posting_harness(tmp_path: Path, callback) -> _Harness:
    return _Harness(
        tmp_path,
        post_callback=callback,
        post_meta={"repo": "o/r", "number": 7, "head_sha": "deadbeef"},
    )


def test_post_config_reports_a_review_that_is_not_posting(tmp_path: Path) -> None:
    """No callback, no modal: Done exits the way it always has."""
    assert _Harness(tmp_path).session.post_config() == {"posting": False}


def test_post_config_labels_the_modal(tmp_path: Path) -> None:
    h = _posting_harness(tmp_path, lambda _ids: _FakePostResult())
    assert h.session.post_config() == {
        "posting": True,
        "repo": "o/r",
        "number": 7,
        "head_sha": "deadbeef",
    }


def test_post_routes_refuse_when_the_review_is_not_posting(tmp_path: Path) -> None:
    h = _Harness(tmp_path)
    for op in (h.session.post_preview, lambda: h.session.post({"comment_ids": []})):
        err = _refused(op)
        assert err.status == 409
        assert "posting" in err.body()["error"]


def test_post_preview_lists_the_comments_that_would_be_posted(tmp_path: Path) -> None:
    """Computed on demand: the store mutates all session, so a preview
    taken at startup would be stale by Done."""
    h = _posting_harness(tmp_path, lambda _ids: _FakePostResult())
    h.session.store.upsert({"id": "c1", "file": "a.py", "side": "new", "line": 3, "body": "one"})

    assert h.session.post_preview() == [
        {"id": "c1", "file": "a.py", "side": "new", "line": 3, "body": "one", "is_reply": False}
    ]

    h.session.store.upsert({"id": "c2", "file": "b.py", "side": "old", "line": 9, "body": "two"})
    assert [row["id"] for row in h.session.post_preview()] == ["c1", "c2"]


def test_post_preview_drops_a_comment_already_upstream(tmp_path: Path) -> None:
    """An ingested comment is already on GitHub; re-posting duplicates it."""
    h = _posting_harness(tmp_path, lambda _ids: _FakePostResult())
    h.session.store.upsert({"id": "c1", "file": "a.py", "side": "new", "line": 3, "body": "one"})
    h.session.store.upsert({"id": "c2", "file": "a.py", "side": "new", "line": 9, "body": "two"})
    h.session.store.mark_posted({"c1": "PRRT_abc"})

    assert [row["id"] for row in h.session.post_preview()] == ["c2"]


def test_post_rejects_a_malformed_selection(tmp_path: Path) -> None:
    h = _posting_harness(tmp_path, lambda _ids: _FakePostResult())
    for payload in ({}, {"comment_ids": "c1"}, {"comment_ids": ["c1", 2]}):
        err = _refused(lambda p=payload: h.session.post(p))
        assert err.status == 400
        assert "comment_ids" in err.body()["error"]


def test_post_keeps_the_result_and_broadcasts_it(tmp_path: Path) -> None:
    """The CLI reads the outcome off the session after `wait_until_done`
    returns, and other open tabs learn about it over the bus."""
    selected: list[list[str]] = []

    def callback(ids: list[str]) -> _FakePostResult:
        selected.append(ids)
        return _FakePostResult(review_id=99, review_url="https://gh/pull/7#r99", posted=2)

    h = _posting_harness(tmp_path, callback)
    response = h.session.post({"comment_ids": ["c1", "c2"]})

    assert selected == [["c1", "c2"]]
    assert response == {"posted": 2, "review_url": "https://gh/pull/7#r99", "review_id": 99}
    assert h.frames.payloads("posted") == [response]
    result = h.session.posted_result
    assert result is not None
    assert result.review_id == 99


def test_a_failed_post_keeps_no_result(tmp_path: Path) -> None:
    """The route answers 500 and the modal offers a retry; nothing must
    look like it landed."""

    def callback(ids: list[str]) -> _FakePostResult:
        raise RuntimeError("gh: 403")

    h = _posting_harness(tmp_path, callback)
    with pytest.raises(RuntimeError, match="403"):
        h.session.post({"comment_ids": ["c1"]})

    assert h.session.posted_result is None
    assert h.frames.types() == []


def test_posting_takes_the_selected_comments_out_of_the_local_set(tmp_path: Path) -> None:
    """The money path end to end, minus the network: the session drives
    the real `scr pr` callback, which maps the selection, posts it, and
    marks what landed — so the next post can't send it twice.
    """
    from unittest.mock import patch

    from semantic_code_review.review import pr_flow
    from semantic_code_review.review.github import PostResult

    (tmp_path / "raw.diff").write_text(
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,3 +1,3 @@\n+x\n",
        encoding="utf-8",
    )
    store = CommentStore(tmp_path / "comments.json")
    store.upsert({"id": "c1", "file": "a.py", "side": "new", "line": 3, "body": "one"})
    store.upsert({"id": "c2", "file": "a.py", "side": "new", "line": 9, "body": "two"})

    posted_bodies: list[list[str]] = []

    def fake_post(repo, number, mapped, **_kw) -> PostResult:
        posted_bodies.append([m.body for m in mapped])
        return PostResult(review_id=5, review_url="https://gh/r/5", posted=1, posted_node_ids={"c1": "TH_1"})

    h = _posting_harness(tmp_path, pr_flow._build_post_callback("o/r", 7, tmp_path))
    with patch.object(pr_flow, "post_review_via_graphql", fake_post):
        response = h.session.post({"comment_ids": ["c1"]})

    # Only the selected comment was mapped and sent.
    assert posted_bodies == [["one"]]
    assert response == {"posted": 1, "review_url": "https://gh/r/5", "review_id": 5}
    reloaded = {c.id: c for c in CommentStore(tmp_path / "comments.json").all()}
    assert reloaded["c1"].source == "github"
    assert reloaded["c1"].node_id == "TH_1"
    assert reloaded["c2"].source == "local"
