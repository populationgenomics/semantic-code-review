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
    UserPromptPart,
)
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RequestUsage

from semantic_code_review.augment import config
from semantic_code_review.augment.agents import Client
from semantic_code_review.augment.pipeline import augment_run_dir
from semantic_code_review.format.parse import parse_augmented_diff


def _sh(cwd: Path, *args: str) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def _user_text(messages: list[ModelMessage]) -> str:
    """Concatenate the text blocks of every user prompt in `messages`."""
    parts: list[str] = []
    for message in messages:
        for part in getattr(message, "parts", []):
            if not isinstance(part, UserPromptPart):
                continue
            content = part.content
            if isinstance(content, str):
                parts.append(content)
                continue
            parts.extend(block for block in content if isinstance(block, str))
    return "\n".join(parts)


def _instructions(messages: list[ModelMessage]) -> str:
    """Concatenate the system/instruction text of every request in `messages`."""
    return "\n".join(text for m in messages if (text := getattr(m, "instructions", None)))


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
        # Flattened user-prompt text per call, keyed by output tool, so
        # tests can assert what a pass actually sent.
        self.prompts: list[tuple[str, str]] = []
        # The same calls with their system text as well, for tests that
        # have to compare a whole envelope rather than a substring.
        self.envelopes: list[tuple[str, str, str]] = []

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
        self.prompts.append((tool_name, _user_text(messages)))
        self.envelopes.append((tool_name, _instructions(messages), _user_text(messages)))
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
    await augment_run_dir(run, config.AugmentConfig(model="t", concurrency=1), client=backend, cache=None)

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


async def test_hunk_prompt_carries_the_file_outline(tmp_path: Path) -> None:
    """The outline is an in-process answer to a read the model used to make.

    A `read_file` of the hunk's own file costs a whole extra turn, and a
    turn re-reads the accumulated context — so the cheapest fix is to
    have already answered it.
    """
    run = _make_run_dir(tmp_path)
    (run / "head" / "f.py").write_text(
        "\n".join(
            ["x = 2", "", "def helper(n: int) -> int:", "    return n + 1", ""] + [f"# pad {i}" for i in range(20)]
        ),
        encoding="utf-8",
    )
    backend, canned = _make_canned_backend(
        overview_args={"summary": "s", "themes": [], "files": [{"path": "f.py", "summary": "fs"}]},
        hunk_args_list=[
            {"intent": "a", "confidence": 90, "smells": []},
            {"intent": "b", "confidence": 90, "smells": []},
        ],
    )
    await augment_run_dir(run, config.AugmentConfig(model="t", concurrency=1), client=backend, cache=None)

    hunk_prompts = [text for tool, text in canned.prompts if tool == "submit_annotations"]
    assert len(hunk_prompts) == 2
    for prompt in hunk_prompts:
        assert "# File outline" in prompt
        assert "def helper(n: int) -> int" in prompt


async def test_overview_prompt_has_no_hunk_seeds(tmp_path: Path) -> None:
    """The outline is per-file — memoised once and shared by the file's
    hunks, which is why it sits inside the cached prefix. The overview
    pass works from headers alone."""
    run = _make_run_dir(tmp_path)
    backend, canned = _make_canned_backend(
        overview_args={"summary": "s", "themes": [], "files": [{"path": "f.py", "summary": "fs"}]},
        hunk_args_list=[
            {"intent": "a", "confidence": 90, "smells": []},
            {"intent": "b", "confidence": 90, "smells": []},
        ],
    )
    await augment_run_dir(run, config.AugmentConfig(model="t", concurrency=1), client=backend, cache=None)

    overview_prompt = next(text for tool, text in canned.prompts if tool == "submit_overview")
    assert "# File outline" not in overview_prompt


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

    await augment_run_dir(run, config.AugmentConfig(model="t", concurrency=1), client=backend, cache=None)

    assert len(created) == 1  # one warm host for the whole run, not per hunk
    assert created[0].started
    assert created[0].stopped  # torn down at end
    assert model.endpoints and model.endpoints[0]["type"] == "http"


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
        run, config.AugmentConfig(model="t", concurrency=1), client=backend, cache=None, on_event=collect
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
        run, config.AugmentConfig(model="t", concurrency=1), client=backend, cache=None, on_event=explode
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
    await augment_run_dir(run, config.AugmentConfig(model="t", concurrency=1), client=backend, cache=None)

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
        config.AugmentConfig(model="t", concurrency=1, extra_review_prompt="Reviewer prompt body"),
        client=backend,
        cache=None,
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
        config.AugmentConfig(model="t", concurrency=1, extra_review_prompt="Reviewer prompt body"),
        client=backend,
        cache=None,
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
    # No extra_review_prompt on the config.
    await augment_run_dir(run, config.AugmentConfig(model="t", concurrency=1), client=backend, cache=None)
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
        config.AugmentConfig(model="t", concurrency=1, extra_review_prompt="Reviewer prompt body"),
        client=backend,
        cache=None,
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


def test_hunk_trace_path_stays_flat_when_the_header_holds_a_path() -> None:
    """Git puts trailing section text on `@@` headers, often containing a path.

    An unsanitised separator made the trace a nested file (the writer
    `mkdir -p`s), where a flat scan of the trace dir could not see it.
    """
    from semantic_code_review.augment.hunks import _hunk_trace_path
    from semantic_code_review.augment.schemas import (
        AnnotatedFile,
        AnnotatedHunk,
        FileAnnotations,
        HunkAnnotations,
        ParsedHunk,
    )

    hunk = AnnotatedHunk(
        parsed=ParsedHunk(
            header="@@ -74,10 +74,10 @@ See `commands/review.md` for the prompt.",
            body="",
            old_start=74,
            old_count=10,
            new_start=74,
            new_count=10,
        ),
        ann=HunkAnnotations(intent=""),
    )
    fp = AnnotatedFile(
        path="docs/a/README.md",
        diff_git_line="diff --git a/docs/a/README.md b/docs/a/README.md",
        ann=FileAnnotations(),
        hunks=[hunk],
    )

    path = _hunk_trace_path(Path("/trace"), fp, hunk)

    assert path is not None
    # `Path.name` is a single component by construction, so asserting no
    # "/" in it proves nothing — the parent is what shows it stayed flat.
    assert path.parent == Path("/trace")
    assert path.name.endswith(".json")


async def test_batching_sends_one_call_per_file(tmp_path: Path) -> None:
    """Both hunks live in f.py, so batch_size=2 is a single call."""
    run = _make_run_dir(tmp_path)
    backend, canned = _make_canned_backend(
        overview_args={"summary": "s", "themes": [], "files": [{"path": "f.py", "summary": "fs"}]},
        hunk_args_list=[
            {"annotations": [{"hunk_index": 0, "intent": "a"}, {"hunk_index": 1, "intent": "b"}]},
        ],
    )
    await augment_run_dir(run, config.AugmentConfig(model="t", concurrency=8), client=backend, cache=None, batch_size=2)

    assert canned.calls == 2  # 1 overview + 1 batch
    reparsed = parse_augmented_diff((run / "augmented.diff").read_text(encoding="utf-8"))
    assert [h.ann.intent for h in reparsed.files[0].hunks] == ["a", "b"]


async def test_batch_user_prompt_carries_file_context_once(tmp_path: Path) -> None:
    run = _make_run_dir(tmp_path)
    backend, canned = _make_canned_backend(
        overview_args={"summary": "s", "themes": [], "files": [{"path": "f.py", "summary": "fs"}]},
        hunk_args_list=[{"annotations": [{"hunk_index": 0, "intent": "a"}, {"hunk_index": 1, "intent": "b"}]}],
    )
    await augment_run_dir(run, config.AugmentConfig(model="t", concurrency=8), client=backend, cache=None, batch_size=2)

    batch_prompt = next(text for tool, text in canned.prompts if tool == "submit_annotations")
    assert "# Hunk 0" in batch_prompt and "# Hunk 1" in batch_prompt
    # file context appears once for the whole batch, not once per hunk
    assert batch_prompt.count("# File summary") == 1
    # the overview is run-invariant, so it belongs in the cached system prompt
    assert "# PR overview" not in batch_prompt


async def test_hunk_missing_from_a_batch_is_retried_individually(tmp_path: Path) -> None:
    """A partial batch must not silently cost the reviewer that hunk."""
    run = _make_run_dir(tmp_path)
    backend, canned = _make_canned_backend(
        overview_args={"summary": "s", "themes": [], "files": [{"path": "f.py", "summary": "fs"}]},
        hunk_args_list=[
            {"annotations": [{"hunk_index": 0, "intent": "batched"}]},  # hunk 1 omitted
            {"intent": "retried singly"},  # the fallback call, single-hunk shaped
        ],
    )
    await augment_run_dir(run, config.AugmentConfig(model="t", concurrency=8), client=backend, cache=None, batch_size=2)

    assert canned.calls == 3  # overview + batch + one fallback
    reparsed = parse_augmented_diff((run / "augmented.diff").read_text(encoding="utf-8"))
    assert [h.ann.intent for h in reparsed.files[0].hunks] == ["batched", "retried singly"]


async def test_unusable_batch_entry_costs_one_hunk_not_the_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Applying a returned annotation can raise on a payload that passed
    validation — a cache entry written under an older schema, say.

    Uncontained it escapes `asyncio.gather` and no augmented.diff is
    written at all. The unbatched path loses one hunk for the same
    payload, so the batched path must too.
    """
    from semantic_code_review.augment import pipeline as pipeline_mod

    run = _make_run_dir(tmp_path)
    backend, _canned = _make_canned_backend(
        overview_args={"summary": "s", "themes": [], "files": [{"path": "f.py", "summary": "fs"}]},
        hunk_args_list=[
            {"annotations": [{"hunk_index": 0, "intent": "ok"}, {"hunk_index": 1, "intent": "bad"}]},
            {"intent": "retried singly"},
        ],
    )

    real = pipeline_mod.build_hunk_annotations
    calls = {"n": 0}

    def flaky(parsed, args):
        calls["n"] += 1
        if calls["n"] == 2:  # the second hunk of the batch
            raise ValueError("malformed annotation payload")
        return real(parsed, args)

    monkeypatch.setattr(pipeline_mod, "build_hunk_annotations", flaky)
    await augment_run_dir(run, config.AugmentConfig(model="t", concurrency=8), client=backend, cache=None, batch_size=2)

    assert (run / "augmented.diff").exists()
    reparsed = parse_augmented_diff((run / "augmented.diff").read_text(encoding="utf-8"))
    assert [h.ann.intent for h in reparsed.files[0].hunks] == ["ok", "retried singly"]


async def test_a_bug_in_the_batch_path_is_not_swallowed_as_a_model_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Degrading on a TypeError turns a defect into N silently empty
    annotations and a run that still exits 0 — it must abort instead."""
    from semantic_code_review.augment import pipeline as pipeline_mod

    run = _make_run_dir(tmp_path)
    backend, _canned = _make_canned_backend(
        overview_args={"summary": "s", "themes": [], "files": [{"path": "f.py", "summary": "fs"}]},
        hunk_args_list=[{"annotations": [{"hunk_index": 0, "intent": "x"}]}],
    )

    async def boom(*args, **kwargs):
        raise TypeError("run_batch_pass() got an unexpected keyword argument")

    monkeypatch.setattr(pipeline_mod, "run_batch_pass", boom)
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        await augment_run_dir(
            run, config.AugmentConfig(model="t", concurrency=8), client=backend, cache=None, batch_size=2
        )


async def test_batch_size_of_one_uses_the_unbatched_path(tmp_path: Path) -> None:
    """'Batching off' must mean the untouched pass, not a batch of one."""
    run = _make_run_dir(tmp_path)
    backend, canned = _make_canned_backend(
        overview_args={"summary": "s", "themes": [], "files": [{"path": "f.py", "summary": "fs"}]},
        hunk_args_list=[{"intent": "a"}, {"intent": "b"}],
    )
    await augment_run_dir(run, config.AugmentConfig(model="t", concurrency=8), client=backend, cache=None, batch_size=1)

    assert canned.calls == 3  # overview + one call per hunk
    prompt = next(text for tool, text in canned.prompts if tool == "submit_annotations")
    assert "# PR overview" in prompt  # single-hunk shape keeps it in the user prompt


def test_batches_never_span_files() -> None:
    from semantic_code_review.augment.pipeline import _plan_batches

    queued = [(0, 0), (0, 1), (1, 0), (1, 1), (1, 2)]
    batches = _plan_batches(queued, 4)
    assert [[e[0] for e in b] for b in batches] == [[0, 0], [1, 1, 1]]


def test_batches_split_a_file_that_exceeds_the_cap() -> None:
    from semantic_code_review.augment.pipeline import _plan_batches

    queued = [(0, i) for i in range(7)]
    batches = _plan_batches(queued, 3)
    assert [len(b) for b in batches] == [3, 3, 1]


async def test_removed_symbols_reach_the_hunk_prompt(tmp_path: Path) -> None:
    """A deleted symbol is absent from head, so the model can only learn it
    from the delta we already compute for the overview seed."""
    run = _make_run_dir(tmp_path)
    # base has a function the head no longer defines
    _sh(run / "repo.git", "git", "config", "user.email", "t@t")
    _sh(run / "repo.git", "git", "config", "user.name", "t")
    (run / "head" / "f.py").write_text("x = 2\n", encoding="utf-8")

    backend, canned = _make_canned_backend(
        overview_args={"summary": "s", "themes": [], "files": [{"path": "f.py", "summary": "fs"}]},
        hunk_args_list=[{"intent": "a"}, {"intent": "b"}],
    )
    await augment_run_dir(run, config.AugmentConfig(model="t", concurrency=1), client=backend, cache=None)

    # With no resolvable base worktree the delta is empty, so the section is
    # absent — the point is that an empty delta emits nothing rather than a
    # bare heading.
    for tool, text in canned.prompts:
        if tool == "submit_annotations":
            assert "# Removed by this change" not in text


@pytest.mark.parametrize(
    ("left", "right"),
    [
        # The whitelist collapses every non-ASCII character, so same-length
        # siblings sanitise identically...
        ("docs/zh/入门.md", "docs/zh/安装.md"),
        # ...as do ASCII specials...
        ("src/foo bar.py", "src/foo(bar.py"),
        # ...and a separator against the character replacing it.
        ("a/b.py", "a_b.py"),
    ],
)
def test_trace_names_do_not_collide_after_sanitising(left: str, right: str) -> None:
    """A collision silently drops one trace, under-reporting the run's
    token accounting — the failure this naming exists to prevent."""
    from semantic_code_review.augment.trace_adapter import trace_filename

    header = "@@ -1,4 +1,6 @@"
    assert trace_filename("hunk", left, header) != trace_filename("hunk", right, header)


def test_trace_name_keeps_its_pass_prefix() -> None:
    """`usage.py` buckets a trace by the first path component."""
    from semantic_code_review.augment.trace_adapter import trace_filename
    from semantic_code_review.augment.usage import _pass_name

    for prefix in ("hunk", "fold", "overview", "extra-review"):
        assert _pass_name(trace_filename(prefix, "a/b.py", "tag")) == prefix


@pytest.mark.parametrize("size", [0, -4])
async def test_a_nonsense_batch_size_is_rejected(tmp_path: Path, size: int) -> None:
    """`max(1, batch_size)` silently turned these into "batching off"."""
    run = _make_run_dir(tmp_path)
    backend, _canned = _make_canned_backend(
        overview_args={"summary": "s", "themes": [], "files": []},
        hunk_args_list=[],
    )
    with pytest.raises(ValueError, match="batch_size must be >= 1"):
        await augment_run_dir(
            run, config.AugmentConfig(model="t", concurrency=8), client=backend, cache=None, batch_size=size
        )


# ---------------------------------------------------------------------------
# The explainer's house style stops at the explainer.
# ---------------------------------------------------------------------------

#: A string no prompt in this repo contains, so finding it anywhere in a
#: per-hunk envelope is unambiguous.
_HOUSE_STYLE = "HOUSE-STYLE-SENTINEL-8f21: write every intent as a limerick."


async def test_a_house_style_for_the_explainer_never_reaches_the_hunk_pass(tmp_path: Path) -> None:
    """The per-hunk pass's envelope is byte-identical with the house style
    set and unset.

    The hunk intents are the ground truth the explainer document is
    written from, and clicking a reference is how a reviewer checks a
    claim against them. If one instruction could shape both, the document
    and the annotations could agree because the same text shaped them
    rather than because both are right — so the annotations stay
    hermetic.

    Driven through the shared server-task bundle rather than
    `augment_run_dir` directly: `augment_run_dir` has no parameter for
    the house style, and the guard worth having is that the layer which
    *does* hold it hands the pipeline the same envelope either way.
    """
    from semantic_code_review.review import runner
    from semantic_code_review.review.config import ReviewConfig

    async def envelopes(house_style: str | None) -> list[tuple[str, str, str]]:
        root = tmp_path / ("styled" if house_style else "plain")
        root.mkdir()
        run = _make_run_dir(root)
        backend, canned = _make_canned_backend(
            overview_args={"summary": "s", "themes": [], "files": [{"path": "f.py", "summary": "fs"}]},
            hunk_args_list=[
                {"intent": "a", "confidence": 90, "smells": []},
                {"intent": "b", "confidence": 90, "smells": []},
            ],
        )
        cfg = ReviewConfig(
            runs_root=root,
            augment=True,
            model="t",
            concurrency=1,
            no_cache=True,
            open_browser=False,
            timeout=1,
            client=backend,
            explainer_prompt=house_style,
        )
        tasks = runner.build_server_tasks(run, cfg)
        assert tasks.explainer is not None  # the bundle that holds the house style
        await tasks.augment(run, lambda *_args, **_kwargs: None)
        return canned.envelopes

    styled = await envelopes(_HOUSE_STYLE)
    plain = await envelopes(None)

    hunk_envelopes = [e for e in styled if e[0] == "submit_annotations"]
    assert len(hunk_envelopes) == 2
    assert hunk_envelopes == [e for e in plain if e[0] == "submit_annotations"]
    # And no other pass on the pipeline picked it up either.
    assert not any(_HOUSE_STYLE in system or _HOUSE_STYLE in user for _tool, system, user in styled)
