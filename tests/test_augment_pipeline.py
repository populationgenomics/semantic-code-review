"""End-to-end augment pipeline on a synthetic run directory with canned LLM."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

from semantic_code_review.augment.pipeline import augment_run_dir
from semantic_code_review.format.parse import parse_augmented_diff


def _sh(cwd: Path, *args: str) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


class CannedClient:
    """Returns overview then hunk-annotation payloads in order."""

    def __init__(self, overview_args: dict, hunk_args_list: list[dict]) -> None:
        self._overview = overview_args
        self._hunks = list(hunk_args_list)
        self.calls = 0

    async def create_message(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        tools = kwargs.get("tools") or []
        names = {t["name"] for t in tools}
        if "submit_overview" in names:
            return _wrap("submit_overview", self._overview)
        # per-hunk call
        if not self._hunks:
            raise AssertionError("CannedClient ran out of hunk payloads")
        return _wrap("submit_annotations", self._hunks.pop(0))


def _wrap(tool_name: str, args: dict) -> dict[str, Any]:
    return {
        "id": "m", "model": "t", "role": "assistant", "stop_reason": "tool_use",
        "usage": {"input_tokens": 1, "output_tokens": 1},
        "content": [{"type": "tool_use", "id": "u1", "name": tool_name, "input": args}],
    }


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
    (run / "meta.json").write_text(json.dumps({
        "title": "Bump constants",
        "body": "x and y",
        "author": {"login": "t"},
        "url": "https://github.com/a/b/pull/1",
        "baseRefOid": "b" * 40,
        "headRefOid": "a" * 40,
        "files": [{"path": "f.py"}],
    }), encoding="utf-8")
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
    client = CannedClient(
        overview_args={
            "summary": "Bumps two constants.",
            "themes": ["constants"],
            "files": [
                {"path": "f.py", "summary": "x and y bumped",
                 "symbols": {"added": [], "modified": ["x", "y"], "removed": []}},
            ],
        },
        hunk_args_list=[
            {"intent": "Bump x from 1 to 2", "confidence": 90, "smells": []},
            {"intent": "Bump y from 1 to 2", "confidence": 90, "smells": []},
        ],
    )
    await augment_run_dir(run, model="t", concurrency=1, client=client, cache=None)

    augmented_path = run / "augmented.diff"
    sidecar_path = run / "augmented.scr.json"
    assert augmented_path.exists()
    assert sidecar_path.exists()

    text = augmented_path.read_text(encoding="utf-8")
    reparsed = parse_augmented_diff(text)
    assert reparsed.overview is not None
    assert reparsed.overview.summary == "Bumps two constants."
    assert reparsed.files[0].path == "f.py"
    assert reparsed.files[0].summary == "x and y bumped"
    assert len(reparsed.files[0].hunks) == 2
    assert reparsed.files[0].hunks[0].intent.startswith("Bump x")
    assert reparsed.files[0].hunks[1].intent.startswith("Bump y")
    assert client.calls == 3  # 1 overview + 2 hunks


async def test_augment_only_files_filters(tmp_path: Path) -> None:
    run = _make_run_dir(tmp_path)
    client = CannedClient(
        overview_args={"summary": "", "files": []},
        hunk_args_list=[{"intent": "ok"}, {"intent": "ok"}],
    )
    await augment_run_dir(
        run, model="t", concurrency=1, client=client, cache=None,
        only_files=["does-not-exist.py"],
    )
    text = (run / "augmented.diff").read_text(encoding="utf-8")
    reparsed = parse_augmented_diff(text)
    assert reparsed.files == []


async def test_augment_max_hunks_caps_calls(tmp_path: Path) -> None:
    run = _make_run_dir(tmp_path)
    client = CannedClient(
        overview_args={"summary": "", "files": []},
        hunk_args_list=[{"intent": "first"}],
    )
    await augment_run_dir(
        run, model="t", concurrency=1, client=client, cache=None, max_hunks=1,
    )
    assert client.calls == 2  # overview + 1 hunk
