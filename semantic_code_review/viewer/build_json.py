"""Transform an AnnotatedDiff + run metadata into the viewer JSON schema.

The viewer's JS consumes this structure directly; it does not re-parse
the unified diff. Per-hunk `rows` are pre-paired in
``viewer/hunk_layout.py`` (sequential `-`/`+` matching) so the renderer
can emit the side-by-side DOM without running a diff algorithm in the
browser.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..augment.schemas import (
    SMELL_CATALOGUE, AnnotatedDiff, AnnotatedFile, Overview,
)
from .hunk_layout import build_hunk_viewer_block


#: cap — files with more than this many lines don't bundle head_lines.
_HEAD_LINES_CAP = 5000


def build_viewer_json(
    diff: AnnotatedDiff,
    meta: dict[str, Any],
    head_dir: Path | None = None,
) -> dict[str, Any]:
    return {
        "version": "1",
        "pr": _pr_block(diff, meta),
        "smells_catalogue": {
            tag: {
                "label": d.label,
                "severity": d.severity.value,
                "color": d.color,
            }
            for tag, d in SMELL_CATALOGUE.items()
        },
        "files": [_file_block(f, i, head_dir) for i, f in enumerate(diff.files)],
        "groups": _group_blocks(diff),
    }


def _group_blocks(diff: AnnotatedDiff) -> list[dict[str, Any]]:
    """Translate Overview.groups into viewer-friendly blocks.

    Each member's (path, hunk_index) becomes a stable viewer hunk id
    of the form "H{fileIdx}_{hunkIdx}", matching the per-hunk block.
    Members whose path isn't in the diff (e.g. the file got filtered
    after the overview ran) are silently dropped.
    """
    ov = diff.overview
    if not isinstance(ov, Overview) or not ov.groups:
        return []
    path_to_file_idx = {fp.path: i for i, fp in enumerate(diff.files)}
    out: list[dict[str, Any]] = []
    for gi, g in enumerate(ov.groups):
        hunk_ids: list[str] = []
        for m in g.members:
            fi = path_to_file_idx.get(m.path)
            if fi is None:
                continue
            hunk_ids.append(f"H{fi}_{m.hunk_index}")
        if not hunk_ids:
            continue
        out.append({
            "id": f"G{gi}",
            "title": g.title,
            "rationale": g.rationale,
            "hunk_ids": hunk_ids,
        })
    return out


def _pr_block(diff: AnnotatedDiff, meta: dict[str, Any]) -> dict[str, Any]:
    ov = diff.overview if isinstance(diff.overview, Overview) else None
    return {
        "title": meta.get("title", ""),
        "number": _try_int(meta.get("number") or _number_from_url(meta.get("url", ""))),
        "repo": _repo_from_url(meta.get("url", "")),
        "base_sha": diff.pr.base_sha,
        "head_sha": diff.pr.head_sha,
        "author": (meta.get("author") or {}).get("login", ""),
        "url": meta.get("url", ""),
        "summary": ov.summary if ov else "",
        "themes": ov.themes if ov else [],
        "symbols_added": [s.model_dump() for s in (ov.symbols_added if ov else [])],
        "symbols_modified": [s.model_dump() for s in (ov.symbols_modified if ov else [])],
        "symbols_removed": [s.model_dump() for s in (ov.symbols_removed if ov else [])],
        "callgraph_edges": [e.model_dump(by_alias=True) for e in (ov.callgraph_edges if ov else [])],
    }


def _file_block(f: AnnotatedFile, idx: int, head_dir: Path | None = None) -> dict[str, Any]:
    hunks = [build_hunk_viewer_block(h, idx, hi) for hi, h in enumerate(f.hunks)]
    adds = sum(h["adds"] for h in hunks)
    dels = sum(h["dels"] for h in hunks)
    head_lines = _load_head_lines(f, head_dir)
    ann = f.ann
    return {
        "id": f"F{idx}",
        "path": f.path,
        "old_path": f.old_path,
        "status": ann.role.value if ann.role else "modified",
        "language": ann.lang or _lang_from_path(f.path),
        "adds": adds,
        "dels": dels,
        "summary": ann.summary,
        "symbols": ann.symbols.model_dump() if ann.symbols else {"added": [], "modified": [], "removed": []},
        "head_lines": head_lines,
        "hunks": hunks,
    }


def _load_head_lines(f: AnnotatedFile, head_dir: Path | None) -> list[str] | None:
    """Return the full head-file content split into lines, or None if we skip.

    Skipped when: no head_dir available, file is GENERATED/BINARY, head file
    doesn't exist (e.g. deleted file), or the file is over the size cap.
    """
    if head_dir is None:
        return None
    role = f.ann.role
    if role is not None and role.value in ("generated", "binary", "deleted"):
        return None
    path = head_dir / f.path
    if not path.exists() or not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = text.splitlines()
    if len(lines) > _HEAD_LINES_CAP:
        return None
    return lines


_LANG_BY_EXT = {
    ".py": "python", ".js": "javascript", ".ts": "typescript", ".tsx": "typescript",
    ".jsx": "javascript", ".go": "go", ".rs": "rust", ".rb": "ruby",
    ".java": "java", ".kt": "kotlin", ".c": "c", ".h": "c", ".cc": "cpp",
    ".cpp": "cpp", ".hpp": "cpp", ".cs": "csharp", ".sh": "bash", ".yaml": "yaml",
    ".yml": "yaml", ".json": "json", ".toml": "toml", ".md": "markdown",
    ".html": "xml", ".xml": "xml", ".sql": "sql",
}


def _lang_from_path(path: str) -> str:
    p = Path(path)
    return _LANG_BY_EXT.get(p.suffix.lower(), "")


def _number_from_url(url: str) -> str:
    if "/pull/" in url:
        return url.rsplit("/pull/", 1)[1].split("/")[0]
    return ""


def _repo_from_url(url: str) -> str:
    # https://github.com/owner/repo/pull/N -> owner/repo
    parts = url.split("/")
    if len(parts) >= 5 and "github.com" in parts[2]:
        return f"{parts[3]}/{parts[4]}"
    return ""


def _try_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
