"""Transform an AugmentedDiff + run metadata into the viewer JSON schema.

The viewer's JS consumes this structure directly; it does not re-parse
the unified diff. Per-hunk `rows` are pre-paired here (sequential `-`/`+`
matching) so the renderer can emit the side-by-side DOM without running
a diff algorithm in the browser.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..augment.schemas import (
    SMELL_CATALOGUE, AugmentedDiff, FilePatch, Hunk, Segment,
)
from .rows import build_rows, compute_fold_regions


#: cap — files with more than this many lines don't bundle head_lines.
_HEAD_LINES_CAP = 5000


def build_viewer_json(
    diff: AugmentedDiff,
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


def _file_block(f: FilePatch, idx: int, head_dir: Path | None = None) -> dict[str, Any]:
    adds = sum(sum(1 for ln in h.body.splitlines() if ln.startswith("+")) for h in f.hunks)
    dels = sum(sum(1 for ln in h.body.splitlines() if ln.startswith("-")) for h in f.hunks)
    head_lines = _load_head_lines(f, head_dir)
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
        "head_lines": head_lines,
        "hunks": [_hunk_block(h, idx, hi, f) for hi, h in enumerate(f.hunks)],
    }


def _load_head_lines(f: FilePatch, head_dir: Path | None) -> list[str] | None:
    """Return the full head-file content split into lines, or None if we skip.

    Skipped when: no head_dir available, file is GENERATED/BINARY, head file
    doesn't exist (e.g. deleted file), or the file is over the size cap.
    """
    if head_dir is None:
        return None
    if f.role is not None and f.role.value in ("generated", "binary", "deleted"):
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


def _hunk_block(h: Hunk, fi: int, hi: int, f: FilePatch) -> dict[str, Any]:
    hunk_id = f"H{fi}_{hi}"
    rows = build_rows(h)
    regions = compute_fold_regions(rows)
    # Match LLM-generated summaries to regions by (new_start, new_count).
    summary_by_range = {(fd.new_start, fd.new_count): fd.summary for fd in h.fold_descriptions}
    fold_region_blocks: list[dict[str, Any]] = []
    for reg in regions:
        summary = ""
        if reg.new_start is not None and reg.new_end is not None:
            count = reg.new_end - reg.new_start + 1
            summary = summary_by_range.get((reg.new_start, count), "")
        fold_region_blocks.append({
            "header_idx": reg.header_idx,
            "body_start_idx": reg.body_start_idx,
            "body_end_idx": reg.body_end_idx,
            "new_start": reg.new_start,
            "new_end": reg.new_end,
            "has_changes": reg.has_changes,
            "summary": summary,
        })
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
        "rows": [r.to_dict() for r in rows],
        "fold_regions": fold_region_blocks,
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
