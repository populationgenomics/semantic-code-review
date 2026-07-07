"""End-to-end augment pipeline on a synthetic run directory with canned LLM."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    ToolCallPart,
)
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RequestUsage

from semantic_code_review.augment.agents import Client
from semantic_code_review.augment.pipeline import augment_run_dir
from semantic_code_review.format.parse import parse_augmented_diff


def _sh(cwd: Path, *args: str) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


class _CannedModel(Model):
    """Pydantic-ai Model that returns pre-baked tool calls per pass.

    The discriminator is the output tool name pydantic-ai puts in
    `model_request_parameters.output_tools[0].name`:

    - `submit_overview`        — the PR-level overview pass
    - `submit_annotations`     — the per-hunk comprehension pass
    - `submit_extra_notes`     — the optional extra-review pass

    Hunk/extra payloads are popped off in order so tests can assert
    call ordering. Mirrors the v0.10 CannedClient.
    """

    _provider = None  # type: ignore[assignment]

    def __init__(
        self,
        overview_args: dict,
        hunk_args_list: list[dict],
        extra_args_list: list[dict] | None = None,
    ) -> None:
        super().__init__()
        self._overview = overview_args
        self._hunks = list(hunk_args_list)
        self._extras = list(extra_args_list or [])
        self.calls = 0

    @property
    def model_name(self) -> str:
        return "canned"

    @property
    def system(self) -> str:
        return "canned"

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        self.calls += 1
        if not model_request_parameters.output_tools:
            raise AssertionError("_CannedModel expects a ToolOutput-driven Agent — no output_tools present")
        tool_name = model_request_parameters.output_tools[0].name
        if tool_name == "submit_overview":
            args = self._overview
        elif tool_name == "submit_annotations":
            if not self._hunks:
                raise AssertionError("_CannedModel ran out of hunk payloads")
            args = self._hunks.pop(0)
        elif tool_name == "submit_extra_notes":
            if not self._extras:
                raise AssertionError("_CannedModel ran out of extra-note payloads")
            args = self._extras.pop(0)
        else:
            raise AssertionError(f"unexpected output tool: {tool_name!r}")
        return ModelResponse(
            parts=[ToolCallPart(tool_name=tool_name, args=args, tool_call_id="c1")],
            usage=RequestUsage(input_tokens=1, output_tokens=1),
            model_name="canned",
            finish_reason="tool_call",
        )


def _make_canned_backend(
    overview_args: dict,
    hunk_args_list: list[dict],
    extra_args_list: list[dict] | None = None,
) -> tuple[Client, _CannedModel]:
    model = _CannedModel(overview_args, hunk_args_list, extra_args_list)
    return Client(model=model), model


def _make_run_dir(tmp_path: Path) -> Path:
    run = tmp_path / "run"
    run.mkdir()
    # Minimal raw.diff with two hunks in one file.
    (run / "raw.diff").write_text(
        "diff --git a/f.py b/f.py\n"
        "--- a/f.py\n"
        "+++ b/f.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-x = 1\n"
        "+x = 2\n"
        "@@ -10,1 +10,1 @@\n"
        "-y = 1\n"
        "+y = 2\n",
        encoding="utf-8",
    )
    # meta.json
    (run / "meta.json").write_text(
        json.dumps(
            {
                "title": "Bump constants",
                "body": "x and y",
                "author": {"login": "t"},
                "url": "https://github.com/a/b/pull/1",
                "baseRefOid": "b" * 40,
                "headRefOid": "a" * 40,
                "files": [{"path": "f.py"}],
            }
        ),
        encoding="utf-8",
    )
    # Head worktree (so RepoTools can instantiate even if not called)
    head = run / "head"
    head.mkdir()
    (head / "f.py").write_text("x = 2\n", encoding="utf-8")
    # Bare-ish repo.git
    repo_git = run / "repo.git"
    repo_git.mkdir()
    _sh(repo_git, "git", "init", "-q")
    return run


async def test_augment_produces_parseable_output(tmp_path: Path) -> None:
    run = _make_run_dir(tmp_path)
    backend, canned = _make_canned_backend(
        overview_args={
            "summary": "Bumps two constants.",
            "themes": ["constants"],
            "files": [
                {
                    "path": "f.py",
                    "summary": "x and y bumped",
                    "symbols": {"added": [], "modified": ["x", "y"], "removed": []},
                },
            ],
        },
        hunk_args_list=[
            {"intent": "Bump x from 1 to 2", "confidence": 90, "smells": []},
            {"intent": "Bump y from 1 to 2", "confidence": 90, "smells": []},
        ],
    )
    await augment_run_dir(run, model="t", concurrency=1, client=backend, cache=None)

    augmented_path = run / "augmented.diff"
    sidecar_path = run / "augmented.scr.json"
    assert augmented_path.exists()
    assert sidecar_path.exists()

    from semantic_code_review.augment.schemas import Overview

    text = augmented_path.read_text(encoding="utf-8")
    reparsed = parse_augmented_diff(text)
    assert isinstance(reparsed.overview, Overview)
    assert reparsed.overview.summary == "Bumps two constants."
    assert reparsed.files[0].path == "f.py"
    assert reparsed.files[0].ann.summary == "x and y bumped"
    assert len(reparsed.files[0].hunks) == 2
    assert reparsed.files[0].hunks[0].ann.intent.startswith("Bump x")
    assert reparsed.files[0].hunks[1].ann.intent.startswith("Bump y")
    assert canned.calls == 3  # 1 overview + 2 hunks


class _RecordingSubprocModel(_CannedModel):
    """Canned model that records the MCP endpoint the driver would be given."""

    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self.endpoints: list = []

    def set_mcp_endpoint(self, config) -> None:  # type: ignore[no-untyped-def]
        self.endpoints.append(config)


async def test_augment_subprocess_backend_hosts_one_mcp_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A subprocess backend starts one HTTP MCP host for the run, points the
    driver at it, and tears it down afterward (ADR 0003 Slice 3)."""
    from semantic_code_review.augment import pipeline as pipeline_mod

    run = _make_run_dir(tmp_path)
    model = _RecordingSubprocModel(
        overview_args={"summary": "s", "themes": [], "files": []},
        hunk_args_list=[
            {"intent": "a", "confidence": 90, "smells": []},
            {"intent": "b", "confidence": 90, "smells": []},
        ],
    )
    backend = Client(model=model, is_subprocess_backend=True)

    created: list = []

    class _FakeHost:
        def __init__(self, repo_tools, *, on_tool=None, name="scr") -> None:  # type: ignore[no-untyped-def]
            self.repo_tools = repo_tools
            self.started = False
            self.stopped = False
            created.append(self)

        def start(self) -> None:
            self.started = True

        def stop(self) -> None:
            self.stopped = True

        def mcp_config(self) -> dict:
            return {"type": "http", "url": "http://127.0.0.1:0/mcp", "headers": {"Authorization": "Bearer t"}}

    monkeypatch.setattr(pipeline_mod.mcp_http_host, "McpHttpHost", _FakeHost)

    await augment_run_dir(run, model="t", concurrency=1, client=backend, cache=None)

    assert len(created) == 1  # one warm host for the whole run, not per hunk
    assert created[0].started
    assert created[0].stopped  # torn down at end
    assert model.endpoints and model.endpoints[0]["type"] == "http"


async def test_augment_only_files_filters(tmp_path: Path) -> None:
    run = _make_run_dir(tmp_path)
    backend, _ = _make_canned_backend(
        overview_args={"summary": "", "files": []},
        hunk_args_list=[{"intent": "ok"}, {"intent": "ok"}],
    )
    await augment_run_dir(
        run,
        model="t",
        concurrency=1,
        client=backend,
        cache=None,
        only_files=["does-not-exist.py"],
    )
    text = (run / "augmented.diff").read_text(encoding="utf-8")
    reparsed = parse_augmented_diff(text)
    assert reparsed.files == []


async def test_augment_max_hunks_caps_calls(tmp_path: Path) -> None:
    run = _make_run_dir(tmp_path)
    backend, canned = _make_canned_backend(
        overview_args={"summary": "", "files": []},
        hunk_args_list=[{"intent": "first"}],
    )
    await augment_run_dir(
        run,
        model="t",
        concurrency=1,
        client=backend,
        cache=None,
        max_hunks=1,
    )
    assert canned.calls == 2  # overview + 1 hunk


async def test_augment_publishes_overview_and_per_hunk_events(tmp_path: Path) -> None:
    """The on_event hook fires once for overview and once per hunk
    completion, carrying enough payload for the viewer to patch."""
    run = _make_run_dir(tmp_path)
    backend, _ = _make_canned_backend(
        overview_args={
            "summary": "Bumps two constants.",
            "themes": ["constants"],
            "files": [{"path": "f.py", "summary": "x and y bumped"}],
        },
        hunk_args_list=[
            {"intent": "Bump x from 1 to 2", "confidence": 90, "smells": []},
            {"intent": "Bump y from 1 to 2", "confidence": 90, "smells": []},
        ],
    )

    events: list[tuple[str, dict]] = []

    def collect(event_type: str, payload: dict) -> None:
        events.append((event_type, payload))

    await augment_run_dir(
        run,
        model="t",
        concurrency=1,
        client=backend,
        cache=None,
        on_event=collect,
    )

    types = [t for t, _ in events]
    assert types.count("overview-start") == 1
    assert types.count("overview") == 1
    # overview-start precedes the completion event.
    assert types.index("overview-start") < types.index("overview")
    # Two start events + two completion events for the two hunks; each
    # start always precedes its matching completion (same indices).
    assert types.count("hunk-start") == 2
    hunk_events = [p for t, p in events if t == "hunk"]
    assert len(hunk_events) == 2
    start_events = [p for t, p in events if t == "hunk-start"]
    assert {(p["file_idx"], p["hunk_idx"]) for p in start_events} == {(0, 0), (0, 1)}
    # Identity + payload shape — sufficient for the viewer to patch.
    indices = {(p["file_idx"], p["hunk_idx"]) for p in hunk_events}
    assert indices == {(0, 0), (0, 1)}
    for p in hunk_events:
        assert p["ok"] is True
        assert p["block"]["id"] == f"H{p['file_idx']}_{p['hunk_idx']}"
        assert p["block"]["intent"].startswith("Bump ")

    overview_payload = next(p for t, p in events if t == "overview")
    assert overview_payload["pr"]["summary"] == "Bumps two constants."
    assert overview_payload["pr"]["themes"] == ["constants"]
    assert overview_payload["files"][0]["summary"] == "x and y bumped"


async def test_augment_event_consumer_failure_does_not_break_pipeline(
    tmp_path: Path,
) -> None:
    """A consumer that throws on every event must not abort the run —
    the on_event hook is a best-effort progress channel."""
    run = _make_run_dir(tmp_path)
    backend, _ = _make_canned_backend(
        overview_args={"summary": "ok", "files": []},
        hunk_args_list=[{"intent": "ok"}, {"intent": "ok"}],
    )

    def explode(_event_type: str, _payload: dict) -> None:
        raise RuntimeError("consumer is on fire")

    await augment_run_dir(
        run,
        model="t",
        concurrency=1,
        client=backend,
        cache=None,
        on_event=explode,
    )
    # Run still produced parseable output.
    assert (run / "augmented.diff").exists()


class _BlowsUpModel(_CannedModel):
    """Canned model that returns the overview normally and raises on
    the first hunk request — simulating UsageLimitExceeded / any other
    mid-run agent failure for trace-on-failure testing."""

    async def request(  # type: ignore[override]
        self,
        messages,
        model_settings,
        model_request_parameters,
    ):
        self.calls += 1
        tool_name = model_request_parameters.output_tools[0].name if model_request_parameters.output_tools else ""
        if tool_name == "submit_annotations":
            raise RuntimeError("simulated request_limit of 50 exceeded")
        return await super().request(messages, model_settings, model_request_parameters)


async def test_per_hunk_trace_written_on_agent_failure(tmp_path: Path) -> None:
    """When the per-hunk agent raises mid-run, the trace file must
    still appear, carry the prompt that was sent, and record the
    failure type+message so we can diagnose the next outlier."""
    import json as _json

    run = _make_run_dir(tmp_path)
    blowup = _BlowsUpModel(
        overview_args={"summary": "ok", "files": []},
        hunk_args_list=[{"intent": "n/a"}, {"intent": "n/a"}],
    )
    backend = Client(model=blowup)

    # Pipeline-level: the failing hunks are caught and accounted as
    # `failed`; the run still completes.
    await augment_run_dir(run, model="t", concurrency=1, client=backend, cache=None)

    trace_dir = run / "trace"
    hunk_traces = list(trace_dir.glob("hunk-*.json"))
    assert hunk_traces, "no hunk traces were written"
    # At least one hunk trace carries the error block we just wired in.
    failures = []
    for p in hunk_traces:
        data = _json.loads(p.read_text(encoding="utf-8"))
        if "error" in data:
            failures.append((p, data))
    assert failures, "expected at least one failed hunk trace with error metadata"
    _, sample = failures[0]
    assert sample["error"]["type"] == "RuntimeError"
    assert "request_limit" in sample["error"]["message"]
    # The user prompt that was sent is preserved (so reviewers can see
    # what the model was working from when it ran out of budget).
    sent = sample["iterations"][0]["messages_sent"]
    assert sent and sent[0]["role"] == "user"


async def test_augment_extra_review_buckets_notes_into_matching_hunks(tmp_path: Path) -> None:
    """`extra_review_prompt` triggers a single PR-level extra pass; its
    flat (file, line, body) notes get bucketed back into the matching
    hunk's line_notes on top of whatever the main pass produced."""
    run = _make_run_dir(tmp_path)
    backend, canned = _make_canned_backend(
        overview_args={
            "summary": "Bumps two constants.",
            "files": [{"path": "f.py", "summary": "x and y bumped"}],
        },
        hunk_args_list=[
            {"intent": "Bump x", "line_notes": [{"line": 1, "body": "main note"}]},
            {"intent": "Bump y", "line_notes": []},
        ],
        extra_args_list=[
            # One whole-PR call: notes that span both hunks.
            {
                "notes": [
                    {"file": "f.py", "line": 1, "body": "extra: be careful"},
                    {"file": "f.py", "line": 10, "body": "extra: same here"},
                ]
            },
        ],
    )
    await augment_run_dir(
        run,
        model="t",
        concurrency=1,
        client=backend,
        cache=None,
        extra_review_prompt="Reviewer prompt body",
    )
    reparsed = parse_augmented_diff((run / "augmented.diff").read_text())
    h0_notes = [(n.line, n.body) for n in reparsed.files[0].hunks[0].ann.line_notes]
    h1_notes = [(n.line, n.body) for n in reparsed.files[0].hunks[1].ann.line_notes]
    # Hunk 0 (line 1): main pass produced one note, extras added one.
    assert h0_notes == [(1, "main note"), (1, "extra: be careful")]
    # Hunk 1 (line 10): main produced none, extras produced one.
    assert h1_notes == [(10, "extra: same here")]
    # Calls: 1 overview + 2 main hunks + 1 PR-level extra = 4
    # (was 5 under the old per-hunk model).
    assert canned.calls == 4


async def test_augment_extra_review_drops_notes_outside_any_hunk(tmp_path: Path) -> None:
    """Extra-pass notes whose `(file, line)` doesn't fall inside any
    hunk's post-image range, or that point at a file the diff didn't
    touch, get filtered with a warning. Empty bodies also drop."""
    run = _make_run_dir(tmp_path)
    backend, _ = _make_canned_backend(
        overview_args={"summary": "", "files": [{"path": "f.py", "summary": ""}]},
        hunk_args_list=[
            {"intent": "Bump x", "line_notes": []},
            {"intent": "Bump y", "line_notes": []},
        ],
        extra_args_list=[
            {
                "notes": [
                    # Hunk 0 covers line 1; hunk 1 covers line 10.
                    {"file": "f.py", "line": 1, "body": "kept"},
                    {"file": "f.py", "line": 99, "body": "dropped — outside any hunk"},
                    {"file": "other.py", "line": 1, "body": "dropped — unknown file"},
                    {"file": "f.py", "line": 10, "body": "   "},  # empty after strip
                ]
            },
        ],
    )
    await augment_run_dir(
        run,
        model="t",
        concurrency=1,
        client=backend,
        cache=None,
        extra_review_prompt="Reviewer prompt body",
    )
    reparsed = parse_augmented_diff((run / "augmented.diff").read_text())
    h0_notes = [(n.line, n.body) for n in reparsed.files[0].hunks[0].ann.line_notes]
    h1_notes = [(n.line, n.body) for n in reparsed.files[0].hunks[1].ann.line_notes]
    assert h0_notes == [(1, "kept")]
    assert h1_notes == []


async def test_augment_no_extra_review_when_prompt_unset(tmp_path: Path) -> None:
    """Without `extra_review_prompt`, the extra pass is skipped
    entirely — no submit_extra_notes calls fire."""
    run = _make_run_dir(tmp_path)
    backend, canned = _make_canned_backend(
        overview_args={"summary": "", "files": [{"path": "f.py", "summary": ""}]},
        hunk_args_list=[{"intent": "x"}, {"intent": "y"}],
        extra_args_list=[],  # no payloads — assertion fires if asked
    )
    await augment_run_dir(
        run,
        model="t",
        concurrency=1,
        client=backend,
        cache=None,
        # no extra_review_prompt
    )
    assert canned.calls == 3  # 1 overview + 2 main hunks; no extras.


async def test_augment_extra_review_re_emits_sse_for_touched_hunks(tmp_path: Path) -> None:
    """When the PR-level extras land notes into a hunk, an additional
    `hunk` SSE event fires for that hunk so live viewers re-render
    the block with the new notes (and the promote-to-comment
    affordance lights up on them)."""
    run = _make_run_dir(tmp_path)
    backend, _ = _make_canned_backend(
        overview_args={"summary": "", "files": [{"path": "f.py", "summary": ""}]},
        hunk_args_list=[
            {"intent": "Bump x", "line_notes": []},
            {"intent": "Bump y", "line_notes": []},
        ],
        extra_args_list=[
            # Notes land in hunk 0 only; hunk 1 should NOT re-emit.
            {"notes": [{"file": "f.py", "line": 1, "body": "look here"}]},
        ],
    )
    events: list[tuple[str, dict]] = []

    def _capture(kind: str, payload: dict) -> None:
        events.append((kind, payload))

    await augment_run_dir(
        run,
        model="t",
        concurrency=1,
        client=backend,
        cache=None,
        extra_review_prompt="Reviewer prompt body",
        on_event=_capture,
    )
    # Count `hunk` events targeting (file_idx=0, hunk_idx=*).
    h0_events = [p for k, p in events if k == "hunk" and p["hunk_idx"] == 0]
    h1_events = [p for k, p in events if k == "hunk" and p["hunk_idx"] == 1]
    # Hunk 0: initial completion + extras-driven re-emit = 2.
    assert len(h0_events) == 2
    # Hunk 1: just the initial completion; extras didn't touch it.
    assert len(h1_events) == 1
    # The re-emitted block carries the new line_note in its body.
    assert h0_events[-1]["block"]["line_notes"] == [{"line": 1, "body": "look here"}]


def test_should_skip_defaults_and_extra_globs() -> None:
    """The denylist covers common generated/lock/binary files, and config
    `skip_globs` extends it (both full-path and basename are matched)."""
    from semantic_code_review.augment import skip

    # Broadened defaults.
    for p in ("go.sum", "app.js.map", "x/__snapshots__/y.snap", "uv.lock", "a/b.min.js", "logo.png"):
        assert skip.should_skip(p), f"{p} should be skipped by default"
    # Not skipped without a matching pattern.
    assert not skip.should_skip("src/app.py")
    assert not skip.should_skip("gen/schema.py")
    # Config-supplied extra globs extend the denylist (path or basename).
    assert skip.should_skip("gen/schema.py", ("gen/**",))
    assert skip.should_skip("build/out.js", ("*.js",))
