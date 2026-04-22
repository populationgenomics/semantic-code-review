"""Render a run directory as a self-contained HTML viewer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..format.parse import parse_augmented_diff
from ..format.sidecar import load_sidecar
from .build_json import build_viewer_json


ASSETS_DIR = Path(__file__).parent / "assets"


def render_run_dir(run_dir: Path, out_path: Path, *, offline: bool = False) -> Path:
    meta_path = run_dir / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    sidecar = run_dir / "augmented.scr.json"
    if sidecar.exists():
        diff = load_sidecar(sidecar)
    else:
        diff = parse_augmented_diff((run_dir / "augmented.diff").read_text(encoding="utf-8"))
    data = build_viewer_json(diff, meta)
    html = render_html(data, offline=offline)
    out_path.write_text(html, encoding="utf-8")
    return out_path


def render_html(data: dict[str, Any], *, offline: bool = False) -> str:
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
    viewer_js = (ASSETS_DIR / "viewer.js").read_text(encoding="utf-8")

    ctx: dict[str, Any] = {
        "pr_title": pr_title,
        "pr_meta": pr_meta,
        "viewer_css": viewer_css,
        "viewer_js": viewer_js,
        "data_json": json.dumps(data, ensure_ascii=False).replace("</", "<\\/"),
        "offline": offline,
    }
    if offline:
        vendor = ASSETS_DIR / "vendor"
        ctx["d2h_css"] = (vendor / "diff2html.min.css").read_text(encoding="utf-8") if (vendor / "diff2html.min.css").exists() else ""
        ctx["d2h_js"]  = (vendor / "diff2html.min.js").read_text(encoding="utf-8") if (vendor / "diff2html.min.js").exists() else ""
        ctx["hljs_css"] = (vendor / "github-dark.min.css").read_text(encoding="utf-8") if (vendor / "github-dark.min.css").exists() else ""
        ctx["hljs_js"]  = (vendor / "highlight.min.js").read_text(encoding="utf-8") if (vendor / "highlight.min.js").exists() else ""
    return tmpl.render(**ctx)
