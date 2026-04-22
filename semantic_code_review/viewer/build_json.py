"""Transform an AugmentedDiff + run metadata into the viewer JSON schema.

The viewer's JS consumes this structure directly; it does not re-parse
the unified diff. Per-hunk `diff_text` is carried through so diff2html
renders one hunk at a time on expand.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..augment.schemas import (
    SMELL_CATALOGUE, AugmentedDiff, FilePatch, Hunk, Segment,
)


def build_viewer_json(diff: AugmentedDiff, meta: dict[str, Any]) -> dict[str, Any]:
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
        "files": [_file_block(f, i) for i, f in enumerate(diff.files)],
    }


def _pr_block(diff: AugmentedDiff, meta: dict[str, Any]) -> dict[str, Any]:
    ov = diff.overview
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


def _file_block(f: FilePatch, idx: int) -> dict[str, Any]:
    adds = sum(sum(1 for ln in h.body.splitlines() if ln.startswith("+")) for h in f.hunks)
    dels = sum(sum(1 for ln in h.body.splitlines() if ln.startswith("-")) for h in f.hunks)
    return {
        "id": f"F{idx}",
        "path": f.path,
        "old_path": f.old_path,
        "status": f.role.value if f.role else "modified",
        "language": f.lang or _lang_from_path(f.path),
        "adds": adds,
        "dels": dels,
        "summary": f.summary,
        "symbols": f.symbols.model_dump() if f.symbols else {"added": [], "modified": [], "removed": []},
        "hunks": [_hunk_block(h, idx, hi, f) for hi, h in enumerate(f.hunks)],
    }


def _hunk_block(h: Hunk, fi: int, hi: int, f: FilePatch) -> dict[str, Any]:
    hunk_id = f"H{fi}_{hi}"
    # diff2html wants each hunk wrapped in the minimal file header so it can
    # produce a self-contained side-by-side render.
    diff_text = (
        f"diff --git a/{f.path} b/{f.path}\n"
        f"{f.old_file_marker or f'--- a/{f.path}'}\n"
        f"{f.new_file_marker or f'+++ b/{f.path}'}\n"
        f"{h.header}\n"
        f"{h.body.rstrip(chr(10))}\n"
    )
    return {
        "id": hunk_id,
        "header": h.header,
        "old_start": h.old_start, "old_count": h.old_count,
        "new_start": h.new_start, "new_count": h.new_count,
        "intent": h.intent,
        "smells": [s.model_dump() for s in h.smells],
        "confidence": h.confidence,
        "context": h.context,
        "refs": [r.model_dump() for r in h.refs],
        "line_notes": [ln.model_dump() for ln in h.line_notes],
        "segments": [_segment_block(s, hunk_id, si) for si, s in enumerate(h.segments)],
        "diff_text": diff_text,
    }


def _segment_block(s: Segment, parent_id: str, si: int) -> dict[str, Any]:
    return {
        "id": f"{parent_id}_S{si}",
        "new_start": s.new_start,
        "new_count": s.new_count,
        "intent": s.intent,
        "smells": [sm.model_dump() for sm in s.smells],
        "context": s.context,
        "refs": [r.model_dump() for r in s.refs],
    }


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
