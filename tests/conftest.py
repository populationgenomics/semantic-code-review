"""Session-scoped fixtures for the pytest suite.

Ensures the viewer's TypeScript module has been compiled before any
test that renders the viewer runs. In normal `scr` use the `bin/scr`
bootstrap handles this; during `pytest` we don't go through the
bootstrap, so we have to arrange the build ourselves.

The compiled output lives out-of-tree (alongside how `bin/scr` builds
it), and we point `render_html.py` at it via SCR_VIEWER_BUILD_DIR.
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
    """Compile annotations.ts → annotations.js once per test session.

    Output lands in a dedicated build dir under /tmp (not the source
    tree) and is exposed via SCR_VIEWER_BUILD_DIR so render_html.py
    picks it up. Skips silently if Node isn't available — tests that
    actually require the compiled JS will fail with a clear
    FileNotFoundError from render_html.py.
    """
    if not shutil.which("node") or not shutil.which("npm"):
        return
    build_dir = REPO_ROOT / "build" / "viewer-js"
    build_dir.mkdir(parents=True, exist_ok=True)
    annotations_js = build_dir / "annotations.js"
    # Skip if already built and nothing newer in sources.
    if annotations_js.exists():
        latest_src = max(
            (p.stat().st_mtime for p in (REPO_ROOT / "semantic_code_review" / "viewer" / "assets").glob("*.ts")),
            default=0,
        )
        if annotations_js.stat().st_mtime >= latest_src:
            os.environ["SCR_VIEWER_BUILD_DIR"] = str(build_dir)
            return

    # First-time install (or lock changed) — run npm ci.
    if not (REPO_ROOT / "node_modules" / ".bin" / "tsc").exists():
        subprocess.run(
            ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
            cwd=REPO_ROOT,
            check=True,
        )
    subprocess.run(
        [str(REPO_ROOT / "node_modules" / ".bin" / "tsc"), "--outDir", str(build_dir)],
        cwd=REPO_ROOT,
        check=True,
    )
    os.environ["SCR_VIEWER_BUILD_DIR"] = str(build_dir)
