"""Render a run directory as a self-contained HTML viewer.

Every asset — highlight.js, its stylesheets, our own CSS/JS — is
inlined into the output file. No CDN fetches, no relative-path
references. The bytes shipped to the browser are the bytes committed
to this repo, so a supply-chain attack on a third-party origin cannot
reach the viewer. See `assets/vendor/VENDOR.md` for provenance and
SHA-256 hashes of the vendored files.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..format.parse import parse_augmented_diff
from ..format.sidecar import load_sidecar
from .build_json import build_viewer_json


ASSETS_DIR = Path(__file__).parent / "assets"
VENDOR_DIR = ASSETS_DIR / "vendor"


def render_run_dir(
    run_dir: Path,
    out_path: Path,
    *,
    session_endpoint: str | None = None,
) -> Path:
    meta_path = run_dir / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    sidecar = run_dir / "augmented.scr.json"
    if sidecar.exists():
        diff = load_sidecar(sidecar)
    else:
        diff = parse_augmented_diff((run_dir / "augmented.diff").read_text(encoding="utf-8"))
    head_dir = run_dir / "head"
    data = build_viewer_json(diff, meta, head_dir=head_dir if head_dir.exists() else None)
    html = render_html(data, session_endpoint=session_endpoint)
    out_path.write_text(html, encoding="utf-8")
    return out_path


def render_html(
    data: dict[str, Any],
    *,
    session_endpoint: str | None = None,
) -> str:
    env = Environment(
        loader=FileSystemLoader(ASSETS_DIR),
        autoescape=select_autoescape([]),  # we escape in the viewer JS; here we control every tag
        keep_trailing_newline=True,
    )
    tmpl = env.get_template("template.html.j2")

    pr = data.get("pr", {})
    pr_title = pr.get("title") or "(untitled PR)"
    repo = pr.get("repo") or ""
    number = pr.get("number")
    pr_meta_bits = []
    if repo: pr_meta_bits.append(repo)
    if number is not None: pr_meta_bits.append(f"#{number}")
    base = (pr.get("base_sha") or "")[:8]
    head = (pr.get("head_sha") or "")[:8]
    if base and head: pr_meta_bits.append(f"{base}..{head}")
    pr_meta = " · ".join(pr_meta_bits)

    viewer_css = (ASSETS_DIR / "viewer.css").read_text(encoding="utf-8")
    # Concatenate the compiled annotations module before viewer.js.
    # annotations.ts is the source of truth; tsc emits annotations.js
    # (either via `npm run build` or the bin/scr bootstrap). If the
    # compiled artifact is missing, fail loudly with a clear hint
    # rather than silently shipping a viewer whose annotation pipeline
    # is `undefined`.
    annotations_path = ASSETS_DIR / "annotations.js"
    if not annotations_path.exists():
        raise FileNotFoundError(
            f"compiled annotations module missing at {annotations_path}. "
            "Run `bin/scr` (which auto-builds) or `npm run build` to "
            "compile it from annotations.ts."
        )
    viewer_js = (
        annotations_path.read_text(encoding="utf-8")
        + "\n"
        + (ASSETS_DIR / "viewer.js").read_text(encoding="utf-8")
    )

    ctx: dict[str, Any] = {
        "pr_title": pr_title,
        "pr_meta": pr_meta,
        "viewer_css": viewer_css,
        "viewer_js": viewer_js,
        "hljs_js": _read_vendor("highlight.min.js"),
        "hljs_css_light": _read_vendor("github.min.css"),
        "hljs_css_dark": _read_vendor("github-dark.min.css"),
        "data_json": json.dumps(data, ensure_ascii=False).replace("</", "<\\/"),
        "session_endpoint": session_endpoint or "",
    }
    return tmpl.render(**ctx)


def _read_vendor(name: str) -> str:
    """Load a vendored asset or raise a clear error.

    The files are committed to the repo and pinned by hash (see
    VENDOR.md). If one is missing, that's a build-tree problem worth
    surfacing loudly rather than silently shipping an empty asset.
    """
    path = VENDOR_DIR / name
    if not path.exists():
        raise FileNotFoundError(
            f"vendored asset missing: {path}. "
            "Run semantic_code_review/viewer/assets/vendor/refresh.sh to "
            "restore it from the pinned upstream source."
        )
    return path.read_text(encoding="utf-8")
