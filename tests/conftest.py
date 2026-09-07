"""Shared fixtures for the pytest suite.

`run_dir` hands a test an empty [[run-directory]] to fill in.

`gh` fakes `gh api graphql`: a test queues one response per operation
and reads back the calls made, with their variables.

`_build_viewer_js` ensures the viewer's TypeScript bundle has been
built before any test that exercises the viewer runs. In normal `scr`
use the `bin/scr` bootstrap handles this; during `pytest` we don't go
through the bootstrap, so we have to arrange the build ourselves.

The bundled output lives out-of-tree (alongside how `bin/scr` builds
it) and is exposed via SCR_VIEWER_BUILD_DIR so the review server picks
it up via `_resolve_asset`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from semantic_code_review import paths

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def run_dir(tmp_path: Path) -> paths.RunDir:
    """An empty run directory. Which artefacts it holds is the test's to
    decide — `run_dir.raw_diff.write_text(...)`, `run_dir.head.mkdir()`.
    """
    return paths.RunDir(tmp_path).create()


#: The GraphQL operations the fake recognises, by the name in the query.
#: Longer names first: `addPullRequestReview` is a prefix of two others.
_GRAPHQL_OPS = (
    "addPullRequestReviewThread",
    "addPullRequestReviewComment",
    "addPullRequestReview",
    "updatePullRequestReviewComment",
    "deletePullRequestReviewComment",
    "submitPullRequestReview",
    "unresolveReviewThread",
    "resolveReviewThread",
)


class GhSequence:
    """A fake `gh api graphql`, dispatching by GraphQL operation.

    `expect(op, response)` queues the JSON body the next call of that
    operation answers with — a dict for a successful `gh`, or an
    `Exception`-shaped `GhFailure` for a non-zero exit. `calls` records
    `(op, variables)` in order. Queries are `"query"`, except the pending
    review's comments listing, `"pendingReviewComments"`.
    """

    def __init__(self) -> None:
        self.responses: dict[str, list[dict | GhFailure]] = {}
        self.calls: list[tuple[str, dict[str, str]]] = []

    def expect(self, op: str, response: dict | GhFailure) -> None:
        self.responses.setdefault(op, []).append(response)

    def variables(self, op: str) -> list[dict[str, str]]:
        """The variables of every call of `op`, in order."""
        return [v for o, v in self.calls if o == op]

    def ops(self) -> list[str]:
        return [o for o, _ in self.calls]

    def __call__(self, argv: list[str], *args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        query = ""
        variables: dict[str, str] = {}
        for i, a in enumerate(argv):
            if a in ("-f", "-F") and i + 1 < len(argv):
                key, _, value = argv[i + 1].partition("=")
                if key == "query":
                    query = value
                else:
                    variables[key] = value
        op = "query"
        if "PullRequestReview" in query and "comments(first: 100)" in query:
            op = "pendingReviewComments"
        for name in _GRAPHQL_OPS:
            if name in query and "mutation" in query:
                op = name
                break
        self.calls.append((op, variables))
        bucket = self.responses.get(op, [])
        if not bucket:
            raise AssertionError(f"unexpected gh graphql call: {op} (no response queued)")
        response = bucket.pop(0)
        if isinstance(response, GhFailure):
            return subprocess.CompletedProcess(args=["gh"], returncode=1, stdout="", stderr=response.stderr)
        return subprocess.CompletedProcess(args=["gh"], returncode=0, stdout=json.dumps(response), stderr="")


class GhFailure:
    """A `gh` exit 1 with this stderr, for `GhSequence.expect`."""

    def __init__(self, stderr: str = "HTTP 502: bad gateway") -> None:
        self.stderr = stderr


@pytest.fixture
def gh() -> Iterator[GhSequence]:
    """`gh api graphql` faked for the test's duration."""
    seq = GhSequence()
    with patch("semantic_code_review.git_ops.subprocess.run", side_effect=seq):
        yield seq


@pytest.fixture(scope="session", autouse=True)
def _build_viewer_js() -> None:
    """Bundle boot.ts → viewer.js once per test session.

    Output lands in a dedicated build dir under build/ (not the source
    tree) and is exposed via SCR_VIEWER_BUILD_DIR so the review
    server's `_resolve_asset` picks it up. Skips silently if Node
    isn't available — tests that actually require the bundle will fail
    with a clear FileNotFoundError from the server.
    """
    if not shutil.which("node") or not shutil.which("npm"):
        return
    build_dir = REPO_ROOT / "build" / "viewer-js"
    build_dir.mkdir(parents=True, exist_ok=True)
    viewer_js = build_dir / "viewer.js"
    sources_dir = REPO_ROOT / "semantic_code_review" / "viewer" / "assets"
    if viewer_js.exists():
        latest_src = max(
            (p.stat().st_mtime for p in sources_dir.glob("*.ts")),
            default=0,
        )
        if viewer_js.stat().st_mtime >= latest_src:
            os.environ["SCR_VIEWER_BUILD_DIR"] = str(build_dir)
            return

    if not (REPO_ROOT / "node_modules" / ".bin" / "esbuild").exists():
        subprocess.run(
            ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
            cwd=REPO_ROOT,
            check=True,
        )
    subprocess.run(
        [
            str(REPO_ROOT / "node_modules" / ".bin" / "esbuild"),
            str(sources_dir / "boot.ts"),
            "--bundle",
            "--format=iife",
            "--target=es2020",
            f"--outfile={viewer_js}",
        ],
        cwd=REPO_ROOT,
        check=True,
    )
    os.environ["SCR_VIEWER_BUILD_DIR"] = str(build_dir)
