"""Session-scoped fixtures for the pytest suite.

Ensures the viewer's TypeScript bundle has been built before any test
that exercises the viewer runs. In normal `scr` use the `bin/scr`
bootstrap handles this; during `pytest` we don't go through the
bootstrap, so we have to arrange the build ourselves.

The bundled output lives out-of-tree (alongside how `bin/scr` builds
it) and is exposed via SCR_VIEWER_BUILD_DIR so the review server picks
it up via `_resolve_asset`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


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
