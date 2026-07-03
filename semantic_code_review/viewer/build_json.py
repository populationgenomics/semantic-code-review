"""Transform an AnnotatedDiff + run metadata into the viewer JSON schema.

The viewer's JS consumes this structure directly; it does not re-parse
the unified diff. Per-hunk `rows` are pre-paired in
``viewer/hunk_layout.py`` (sequential `-`/`+` matching) so the renderer
can emit the side-by-side DOM without running a diff algorithm in the
browser.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import structural
from ..augment.schemas import (
    SMELL_CATALOGUE,
    AnnotatedDiff,
    AnnotatedFile,
    FileAnnotations,
    Overview,
    PRInfo,
    lift_file,
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
    # Parse the head/base Symbol trees once per file and share them: the
    # Symbols axis reads the base→head delta, each FileBlock ships the
    # flattened per-side fold spans.
    file_syms = [_file_symbols(f, base_dir, head_dir) for f in diff.files]
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
        "files": [_file_block(f, i, head_dir, file_syms[i]) for i, f in enumerate(diff.files)],
        "groups": _group_blocks(diff),
        "symbols": _symbol_blocks(diff, file_syms),
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
        diff,
        meta,
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
        out.append(
            {
                "id": f"G{gi}",
                "title": g.title,
                "rationale": g.rationale,
                "hunk_ids": hunk_ids,
            }
        )
    return out


@dataclass
class _SymNode:
    """A node in the per-file changed-symbol tree (slice 5 nesting).

    Directly-changed symbols carry a `status`; ancestors synthesized only
    to give a changed descendant its context have `status=None`. `name`
    is the short segment (the title shown in the tree); `hunk_ids` is the
    distinct subtree union, filled bottom-up by `_rollup`.
    """

    qn: str
    parent: str | None
    name: str = ""
    kind: str = ""
    status: str | None = None
    start_line: int = 0
    own_hunks: list[str] = field(default_factory=list)
    children: list[str] = field(default_factory=list)
    hunk_ids: list[str] = field(default_factory=list)


@dataclass
class _FileSymbols:
    """The parsed base/head `Symbol` forests for one file.

    Both lists are empty for an unsupported language or an unavailable
    worktree — the same graceful-degradation guard the changed-symbol
    delta uses, so a file with no parse simply carries no spans.
    """

    base: list[structural.Symbol] = field(default_factory=list)
    head: list[structural.Symbol] = field(default_factory=list)


def _file_symbols(
    f: AnnotatedFile,
    base_dir: Path | None,
    head_dir: Path | None,
) -> _FileSymbols:
    """Parse one file's base/head `Symbol` forests, or empty on degrade."""
    lang = structural.language_for_path(f.path)
    if lang is None:
        return _FileSymbols()
    base_src = _read_tree_source(base_dir, f.old_path or f.path)
    head_src = _read_tree_source(head_dir, f.path)
    return _FileSymbols(
        base=structural.outline_symbols(base_src, lang) if base_src is not None else [],
        head=structural.outline_symbols(head_src, lang) if head_src is not None else [],
    )


def _fold_spans(symbols: list[structural.Symbol], depth: int = 0) -> list[dict[str, Any]]:
    """Flatten a `Symbol` forest to per-definition line spans, depth-first.

    Each entry is `{start_line, end_line, kind, qualified_name, depth}` —
    the minimal currency the fold detectors need to snap regions to
    definition boundaries (symbol-aware-folds slice 1). Source order is
    preserved; `depth` is the nesting level (0 for top-level defs).
    """
    out: list[dict[str, Any]] = []
    for s in symbols:
        out.append(
            {
                "start_line": s.range.start_line,
                "end_line": s.range.end_line,
                "kind": s.kind,
                "qualified_name": s.qualified_name,
                "depth": depth,
            }
        )
        out.extend(_fold_spans(s.children, depth + 1))
    return out


def file_fold_spans(
    f: AnnotatedFile,
    base_dir: Path | None,
    head_dir: Path | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Flattened per-side definition spans for one file, as `(head, base)`.

    The currency `build_hunk_viewer_block` needs to snap folds to symbol
    boundaries. Empty lists degrade an unsupported language / unavailable
    worktree, same as `fold_symbols`.
    """
    syms = _file_symbols(f, base_dir, head_dir)
    return _fold_spans(syms.head), _fold_spans(syms.base)


def _symbol_blocks(
    diff: AnnotatedDiff,
    file_syms: list[_FileSymbols],
) -> list[dict[str, Any]]:
    """Deterministic Symbols axis: a nested changed-symbol tree (ADR 0001).

    The tree-sitter base→head set-diff (`structural.diff_file`) gives the
    added / modified / removed symbols per file; each is mapped to the
    viewer hunk ids whose line span its *live*-side range overlaps (head
    for added/modified, base for removed). Changed symbols are then nested
    by `qualified_name` (class ▸ method): a changed method renders under
    its enclosing class even when the class itself is unchanged — those
    ancestors are synthesized as context nodes from the live forest.

    A parent's `hunk_ids` is the union of its subtree's hunks (clicking it
    filters to every changed descendant); a leaf carries only its own.
    Any node whose whole subtree touches no hunk — e.g. a symbol that only
    shifted because lines moved above it, with no changed children —
    yields no block, so every pill filters to at least one hunk. Absent
    entirely when neither worktree is available or the language is
    unsupported — those files carry empty `file_syms` and are skipped.
    """
    out: list[dict[str, Any]] = []
    counter = [0]
    for fi, (f, syms) in enumerate(zip(diff.files, file_syms, strict=False)):
        base_syms, head_syms = syms.base, syms.head
        if not base_syms and not head_syms:
            continue
        delta = structural.diff_file(f.path, base_syms, head_syms)
        head_spans = [
            (h.parsed.new_start, h.parsed.new_start + h.parsed.new_count - 1, f"H{fi}_{hi}")
            for hi, h in enumerate(f.hunks)
        ]
        base_spans = [
            (h.parsed.old_start, h.parsed.old_start + h.parsed.old_count - 1, f"H{fi}_{hi}")
            for hi, h in enumerate(f.hunks)
        ]
        out.extend(_symbol_tree_blocks(f.path, delta, head_spans, base_spans, head_syms, base_syms, counter))
    return out


def _symbol_tree_blocks(
    path: str,
    delta: structural.SymbolDelta,
    head_spans: list[tuple[int, int, str]],
    base_spans: list[tuple[int, int, str]],
    head_syms: list[structural.Symbol],
    base_syms: list[structural.Symbol],
    counter: list[int],
) -> list[dict[str, Any]]:
    """Build one file's nested changed-symbol blocks (see `_symbol_blocks`)."""
    # Live side is head for added/modified, base for removed.
    changed: dict[str, tuple[str, structural.ChangedSymbol, list[str]]] = {}
    for status, spans, syms in (
        ("added", head_spans, delta.added),
        ("modified", head_spans, delta.modified),
        ("removed", base_spans, delta.removed),
    ):
        for cs in syms:
            changed[cs.qualified_name] = (status, cs, _overlapping_hunks(spans, cs.range))
    if not changed:
        return []

    nodes: dict[str, _SymNode] = {}

    def ensure(qn: str) -> _SymNode:
        node = nodes.get(qn)
        if node is not None:
            return node
        parent_qn = qn.rsplit(".", 1)[0] if "." in qn else None
        node = _SymNode(qn=qn, parent=parent_qn)
        nodes[qn] = node
        if parent_qn is not None:
            parent = ensure(parent_qn)
            if qn not in parent.children:
                parent.children.append(qn)
        return node

    for qn, (status, cs, hids) in changed.items():
        node = ensure(qn)
        node.status, node.kind, node.name = status, cs.kind, cs.name
        node.start_line, node.own_hunks = cs.range.start_line, hids

    # Fill metadata for synthesized ancestors from the live forests so the
    # context node shows the class's real name/kind, not just a name guess.
    head_flat = structural.flatten(head_syms)
    base_flat = structural.flatten(base_syms)
    for qn, node in nodes.items():
        if node.status is None:
            sym = head_flat.get(qn) or base_flat.get(qn)
            node.name = sym.name if sym else qn.rsplit(".", 1)[-1]
            node.kind = sym.kind if sym else "container"
            node.start_line = sym.range.start_line if sym else 0

    def rollup(qn: str) -> list[str]:
        node = nodes[qn]
        node.children.sort(key=lambda c: (nodes[c].start_line, c))
        ids = list(node.own_hunks)
        for c in node.children:
            for h in rollup(c):
                if h not in ids:
                    ids.append(h)
        node.hunk_ids = sorted(ids, key=_hunk_sort_key)
        return node.hunk_ids

    roots = sorted(
        (qn for qn, n in nodes.items() if n.parent not in nodes),
        key=lambda q: (nodes[q].start_line, q),
    )
    for r in roots:
        rollup(r)

    def emit(qn: str) -> dict[str, Any] | None:
        node = nodes[qn]
        if not node.hunk_ids:  # nothing in this subtree touches a hunk
            return None
        rationale = (
            f"{node.status} {node.kind} in {path}" if node.status is not None else f"{node.kind} (unchanged) in {path}"
        )
        block: dict[str, Any] = {
            "id": f"SY{counter[0]}",
            "title": node.name,
            "rationale": rationale,
            "hunk_ids": node.hunk_ids,
        }
        counter[0] += 1
        children = [b for c in node.children if (b := emit(c)) is not None]
        if children:
            block["children"] = children
        return block

    return [b for r in roots if (b := emit(r)) is not None]


def _hunk_sort_key(hid: str) -> tuple[int, int]:
    """Sort key for "H{file}_{hunk}" ids → (file_idx, hunk_idx)."""
    try:
        fi, hi = hid[1:].split("_", 1)
        return int(fi), int(hi)
    except (ValueError, IndexError):
        return 0, 0


def _overlapping_hunks(
    spans: list[tuple[int, int, str]],
    rng: structural.SymbolRange,
) -> list[str]:
    """Hunk ids whose [start, end] line span overlaps `rng`.

    Empty hunk spans (count 0 — e.g. a pure deletion on the head side)
    have end < start and so never overlap.
    """
    return [hid for start, end, hid in spans if start <= end and rng.start_line <= end and start <= rng.end_line]


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


def _file_block(
    f: AnnotatedFile,
    idx: int,
    head_dir: Path | None,
    syms: _FileSymbols,
) -> dict[str, Any]:
    head_spans = _fold_spans(syms.head)
    base_spans = _fold_spans(syms.base)
    hunks = [build_hunk_viewer_block(h, idx, hi, head_spans, base_spans) for hi, h in enumerate(f.hunks)]
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
        "fold_symbols": {"head": _fold_spans(syms.head), "base": _fold_spans(syms.base)},
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


# Maps a file extension to a highlight.js language *registered in the
# vendored build* (see vendor/highlight.min.js; the set is asserted in
# tests/test_viewer_json.py). An unmapped extension yields "", which the
# viewer renders as plain text. Values must be canonical hljs names, not
# aliases, so the test guard stays simple.
_LANG_BY_EXT = {
    # Python / JS / TS (incl. the module variants the structural layer parses)
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    # Systems / compiled
    ".go": "go",
    ".rs": "rust",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".cs": "csharp",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".swift": "swift",
    ".vb": "vbnet",
    # Scripting
    ".rb": "ruby",
    ".php": "php",
    ".lua": "lua",
    ".pl": "perl",
    ".pm": "perl",
    ".r": "r",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    # Web / styling
    ".css": "css",
    ".scss": "scss",
    ".sass": "scss",
    ".less": "less",
    ".html": "xml",
    ".xml": "xml",
    ".svg": "xml",
    # Data / config / docs
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".toml": "ini",
    ".ini": "ini",
    ".cfg": "ini",
    ".sql": "sql",
    ".graphql": "graphql",
    ".gql": "graphql",
    ".md": "markdown",
    ".markdown": "markdown",
    ".diff": "diff",
    ".patch": "diff",
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
