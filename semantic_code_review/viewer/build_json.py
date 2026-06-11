"""Transform an AnnotatedDiff + run metadata into the viewer JSON schema.

The viewer's JS consumes this structure directly; it does not re-parse
the unified diff. Per-hunk `rows` are pre-paired in
``viewer/hunk_layout.py`` (sequential `-`/`+` matching) so the renderer
can emit the side-by-side DOM without running a diff algorithm in the
browser.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .. import structural
from ..augment.schemas import (
    SMELL_CATALOGUE, AnnotatedDiff, AnnotatedFile, FileAnnotations, Overview,
    PRInfo, lift_file,
)
from ..format.parse import parse_raw_diff
from .hunk_layout import build_hunk_viewer_block


#: cap — files with more than this many lines don't bundle head_lines.
_HEAD_LINES_CAP = 5000


def build_viewer_json(
    diff: AnnotatedDiff,
    meta: dict[str, Any],
    head_dir: Path | None = None,
    base_dir: Path | None = None,
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
        "symbols": _symbol_blocks(diff, base_dir, head_dir),
    }


def build_pending_viewer_json(run_dir: Path) -> dict[str, Any]:
    """Build viewer JSON from raw.diff alone, before augmentation runs.

    All annotations are empty; the page renders the file/hunk structure
    with the top-level `pending` flag set so the viewer JS shows
    "analysing…" placeholders in each hunk's intent slot. Replaced by a
    full re-render when the augmentation pass completes.
    """
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    parsed = parse_raw_diff((run_dir / "raw.diff").read_text(encoding="utf-8"))
    pr = PRInfo(
        pr_url=meta.get("url", ""),
        base_sha=meta.get("baseRefOid", ""),
        head_sha=meta.get("headRefOid", ""),
        model="",
    )
    diff = AnnotatedDiff(
        version=parsed.version,
        pr=pr,
        files=[lift_file(pf, ann=FileAnnotations()) for pf in parsed.files],
    )
    head_dir = run_dir / "head"
    base_dir = run_dir / "base"
    data = build_viewer_json(
        diff, meta,
        head_dir=head_dir if head_dir.exists() else None,
        base_dir=base_dir if base_dir.exists() else None,
    )
    data["pending"] = True
    return data


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


def _symbol_blocks(
    diff: AnnotatedDiff,
    base_dir: Path | None,
    head_dir: Path | None,
) -> list[dict[str, Any]]:
    """Deterministic Symbols axis: one block per changed symbol (ADR 0001).

    The tree-sitter base→head set-diff (`structural.diff_file`) gives the
    added / modified / removed symbols per file; each is mapped to the
    viewer hunk ids whose line span its *live*-side range overlaps (head
    for added/modified, base for removed). A changed symbol no hunk
    touches — e.g. one that only shifted because lines moved above it —
    yields no block, so every pill filters to at least one hunk.

    Flat for this slice (the nested class ▸ method render lands in slice
    5); `qualified_name` is carried as the title so the later tree walk
    can group on it. Absent entirely when neither worktree is available
    or the language is unsupported — today's behaviour for those files.
    """
    if base_dir is None and head_dir is None:
        return []
    out: list[dict[str, Any]] = []
    si = 0
    for fi, f in enumerate(diff.files):
        lang = structural.language_for_path(f.path)
        if lang is None:
            continue
        base_src = _read_tree_source(base_dir, f.old_path or f.path)
        head_src = _read_tree_source(head_dir, f.path)
        base_syms = structural.outline_symbols(base_src, lang) if base_src is not None else []
        head_syms = structural.outline_symbols(head_src, lang) if head_src is not None else []
        delta = structural.diff_file(f.path, base_syms, head_syms)
        head_spans = [
            (h.parsed.new_start, h.parsed.new_start + h.parsed.new_count - 1, f"H{fi}_{hi}")
            for hi, h in enumerate(f.hunks)
        ]
        base_spans = [
            (h.parsed.old_start, h.parsed.old_start + h.parsed.old_count - 1, f"H{fi}_{hi}")
            for hi, h in enumerate(f.hunks)
        ]
        # Source order within a file: added, modified, removed. Live side
        # is head for added/modified, base for removed.
        for status, spans, syms in (
            ("added", head_spans, delta.added),
            ("modified", head_spans, delta.modified),
            ("removed", base_spans, delta.removed),
        ):
            for cs in syms:
                hunk_ids = _overlapping_hunks(spans, cs.range)
                if not hunk_ids:
                    continue
                out.append({
                    "id": f"SY{si}",
                    "title": cs.qualified_name,
                    "rationale": f"{status} {cs.kind} in {f.path}",
                    "hunk_ids": hunk_ids,
                })
                si += 1
    return out


def _overlapping_hunks(
    spans: list[tuple[int, int, str]], rng: structural.SymbolRange,
) -> list[str]:
    """Hunk ids whose [start, end] line span overlaps `rng`.

    Empty hunk spans (count 0 — e.g. a pure deletion on the head side)
    have end < start and so never overlap.
    """
    return [
        hid for start, end, hid in spans
        if start <= end and rng.start_line <= end and start <= rng.end_line
    ]


def _read_tree_source(tree_dir: Path | None, rel_path: str) -> str | None:
    """Read `rel_path` from a worktree dir, or None if unavailable."""
    if tree_dir is None:
        return None
    path = tree_dir / rel_path
    if not path.exists() or not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


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
