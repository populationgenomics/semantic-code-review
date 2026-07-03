"""Repo tools the LLM can call during a per-hunk pass.

Every tool is read-only, operates on the fetched worktrees, and returns
text. Large results are truncated and flagged so the model can narrow
its query.

`RepoTools` is the single source of truth for the tool surface. Methods
decorated with `@_tool` are exported to two consumers:

  * pydantic-ai SDK Agents — via `TOOL_FUNCTIONS`, a list of `RunContext`-
    wrapping callables produced from the decorated methods.
  * The MCP stdio server (`mcp_server.py`) — via `mcp_tool_schemas()` for
    the `tools/list` payload and `mcp_dispatch()` for `tools/call`.

Both surfaces are derived by introspection: rename a `RepoTools` method
and both update with no other edits.
"""

from __future__ import annotations

import inspect
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.tools import Tool

from .. import git_ops, structural

TOOL_RESULT_CAP_BYTES = 20 * 1024


# Resolved once at import; tests that want to force a path can monkeypatch.
_HAS_RIPGREP = shutil.which("rg") is not None


_TOOL_EXPORT_ATTR = "_repo_tool_export"


def _tool(method: Callable) -> Callable:
    """Mark a `RepoTools` method as part of the LLM-facing tool surface."""
    setattr(method, _TOOL_EXPORT_ATTR, True)
    return method


@dataclass
class RepoTools:
    head_worktree: Path
    repo_git: Path
    base_sha: str
    head_sha: str
    # Optional augmented diff (an `AnnotatedDiff`), bound only by the
    # review console so its `hunk(id)` accessor can resolve a viewer
    # hunk id to that hunk's diff text. Kept loosely typed (`Any`) so
    # the augment-side schemas stay off this module's import path;
    # left None on every augment/MCP path, where no sidecar exists yet.
    diff: Any = None

    # --- file reads -------------------------------------------------------

    @_tool
    def read_file(self, path: str, start_line: int | None = None, end_line: int | None = None) -> str:
        """Read a file from the head worktree. Returns up to 20 KB of text.

        Args:
            path: Path relative to repo root.
            start_line: 1-indexed start line (optional).
            end_line: 1-indexed end line inclusive (optional).
        """
        full = (self.head_worktree / path).resolve()
        if not _is_inside(full, self.head_worktree):
            return f"error: path outside worktree: {path}"
        if not full.exists():
            return f"error: not found: {path}"
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return f"error: could not read {path}: {e}"
        return _slice_and_cap(text, start_line, end_line)

    @_tool
    def read_file_at(self, sha: str, path: str, start_line: int | None = None, end_line: int | None = None) -> str:
        """Read a file at a specific commit SHA (e.g. the PR base).

        Use for pre-change content.

        Args:
            sha: Commit SHA.
            path: Path relative to repo root.
            start_line: 1-indexed start line (optional).
            end_line: 1-indexed end line inclusive (optional).
        """
        rc, stdout, stderr = git_ops.git_capture(
            self.repo_git, "show", f"{sha}:{path}",
        )
        if rc != 0:
            return f"error: git show {sha}:{path} failed: {stderr.strip()}"
        return _slice_and_cap(stdout, start_line, end_line)

    # --- structure --------------------------------------------------------

    @_tool
    def outline(self, path: str, sha: str | None = None) -> str:
        """Structural symbol outline of a file, as a JSON array.

        Deterministic tree-sitter parse — no LLM, no hallucination. Each
        entry is a definition (class / function / constant) with its
        `name`, `qualified_name`, 1-indexed line `range`, declared
        `signature` (or null), and nested `children` (class ▸ method).
        Unsupported language or parse failure ⇒ `[]`.

        Args:
            path: Path relative to repo root.
            sha: Commit SHA to read the file at (defaults to head worktree).
        """
        lang = structural.language_for_path(path)
        if lang is None:
            return "[]"
        source = self._read_source(path, sha)
        if source is None:
            return "[]"
        symbols = structural.outline_symbols(source, lang)
        return _cap(structural.symbols_to_json(symbols))

    def _read_source(self, path: str, sha: str | None) -> str | None:
        """Raw file text from the head worktree (``sha is None``) or at a
        revision via ``git show``. ``None`` if it can't be read.
        """
        if sha is None:
            full = (self.head_worktree / path).resolve()
            if not _is_inside(full, self.head_worktree) or not full.is_file():
                return None
            try:
                return full.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return None
        rc, stdout, _stderr = git_ops.git_capture(self.repo_git, "show", f"{sha}:{path}")
        return stdout if rc == 0 else None

    @_tool
    def symbol_at(self, path: str, line: int, sha: str | None = None) -> str:
        """Innermost symbol enclosing a line, as a JSON object (or `null`).

        Deterministic tree-sitter parse — no LLM. Returns the most
        specific definition (the method, not its class) whose 1-indexed
        line `range` covers `line`, with its `name`, `qualified_name`,
        `signature`, and nested `children`. `null` if no symbol covers
        the line, or the language is unsupported / file unreadable.

        Args:
            path: Path relative to repo root.
            line: 1-indexed line number.
            sha: Commit SHA to read the file at (defaults to head worktree).
        """
        lang = structural.language_for_path(path)
        if lang is None:
            return "null"
        source = self._read_source(path, sha)
        if source is None:
            return "null"
        symbols = structural.outline_symbols(source, lang)
        return structural.symbol_to_json(structural.enclosing_symbol(symbols, line))

    def compute_symbol_delta(self) -> structural.SymbolDelta:
        """Deterministic base→head structural delta over the whole diff.

        Compares the base commit against the head worktree for every
        changed file in a supported language and merges the per-file
        `qualified_name` set-diffs into one diff-wide `SymbolDelta`.
        Changed files in unsupported languages are silently absent.

        Underlies both the `changed_symbols` tool (JSON for the LLM) and
        the overview seed (the `SymbolDelta` object, consumed in-process).
        Raises `git_ops.GitError` if the diff can't be enumerated.
        """
        paths = git_ops.diff_name_only(self.repo_git, self.base_sha, self.head_sha)
        deltas = []
        for path in paths:
            lang = structural.language_for_path(path)
            if lang is None:
                continue
            base_src = self._read_source(path, self.base_sha)
            head_src = self._read_source(path, None)
            base_syms = structural.outline_symbols(base_src, lang) if base_src is not None else []
            head_syms = structural.outline_symbols(head_src, lang) if head_src is not None else []
            deltas.append(structural.diff_file(path, base_syms, head_syms))
        return structural.merge(deltas)

    @_tool
    def changed_symbols(self) -> str:
        """Deterministic structural delta of the whole diff, as JSON.

        Compares the base commit against the head worktree for every
        changed file in a supported language, returning
        `{added, removed, modified}` lists of symbols by `qualified_name`
        set-diff — no LLM, no hallucination. `modified` means the same
        qualified name on both sides with a differing line range; a
        same-span body edit is not flagged. Each entry carries its
        `path`, `kind`, `name`, `qualified_name`, declared `signature`,
        and the line `range` on its live side (head for added/modified,
        base for removed). Changed files in unsupported languages are
        silently absent.
        """
        try:
            delta = self.compute_symbol_delta()
        except git_ops.GitError as e:
            return f"error: {e}"
        return _cap(delta.model_dump_json())

    # --- search -----------------------------------------------------------

    @_tool
    def grep(self, pattern: str, path_glob: str | None = None, max_hits: int = 50) -> str:
        """Search the head worktree with ripgrep.

        Returns path:line:text matches, capped at 50 by default. Falls back
        to `git grep` when ripgrep is unavailable. Output is ``path:line:text``
        with worktree prefix stripped.

        Args:
            pattern: Pattern to search for.
            path_glob: Restrict to matching files (e.g. 'src/**/*.py').
            max_hits: Maximum matches to return.
        """
        if _HAS_RIPGREP:
            return self._grep_rg(pattern, path_glob, max_hits)
        return self._grep_git(pattern, path_glob, max_hits)

    def _grep_rg(self, pattern: str, path_glob: str | None, max_hits: int) -> str:
        args = ["rg", "--no-heading", "-n", "--max-count", str(max_hits), "-e", pattern]
        if path_glob:
            args += ["--glob", path_glob]
        args.append(str(self.head_worktree))
        result = subprocess.run(args, capture_output=True, text=True, check=False)
        if result.returncode not in (0, 1):  # 1 = no matches
            return f"error: rg failed: {result.stderr.strip()}"
        prefix = str(self.head_worktree) + os.sep
        out = "\n".join(
            line.removeprefix(prefix) for line in result.stdout.splitlines()
        )
        return _cap(out)

    def _grep_git(self, pattern: str, path_glob: str | None, max_hits: int) -> str:
        """Fallback search via ``git grep`` — always available since git is a
        hard requirement. Respects .gitignore; only searches tracked files.
        """
        try:
            return _cap(git_ops.grep(self.head_worktree, pattern, path_glob, max_hits))
        except git_ops.GitError as e:
            return f"error: {e}"

    # --- listing ----------------------------------------------------------

    @_tool
    def list_dir(self, path: str = "") -> str:
        """List a directory in the head worktree (shallow, hidden files skipped).

        Args:
            path: Path relative to repo root (empty for root).
        """
        full = (self.head_worktree / path).resolve() if path else self.head_worktree
        if not _is_inside(full, self.head_worktree):
            return f"error: path outside worktree: {path}"
        if not full.is_dir():
            return f"error: not a directory: {path}"
        entries: list[str] = []
        for p in sorted(full.iterdir()):
            if p.name.startswith("."):
                continue
            marker = "/" if p.is_dir() else ""
            entries.append(f"{p.name}{marker}")
        return _cap("\n".join(entries))

    # --- git --------------------------------------------------------------

    @_tool
    def git_log(self, path: str, limit: int = 5) -> str:
        """Recent commits touching a path (short form).

        Args:
            path: Path relative to repo root.
            limit: Maximum commits to return.
        """
        try:
            return _cap(git_ops.git(
                self.repo_git, "log", f"-n{limit}", "--oneline", "--", path,
            ))
        except git_ops.GitError as e:
            return f"error: {e}"

    # --- diff (review console only) ---------------------------------------

    def hunk(self, hunk_id: str) -> str:
        """Read one hunk's diff text, addressed by its viewer id.

        Returns the hunk's `@@` header followed by its body (the `+`/`-`/
        context lines as they appear in the change under review). Use this
        to pull the exact diff for a hunk the reviewer is asking about
        rather than re-reading whole files.

        Args:
            hunk_id: Viewer hunk id of the form 'H<file_idx>_<hunk_idx>'.
        """
        if self.diff is None:
            return "error: no diff bound — hunk() is only available in the review console"
        try:
            fi, hi = _parse_hunk_id(hunk_id)
        except ValueError as e:
            return f"error: {e}"
        files = getattr(self.diff, "files", [])
        if not (0 <= fi < len(files)):
            return f"error: file index {fi} not in diff (hunk_id {hunk_id!r})"
        hunks = files[fi].hunks
        if not (0 <= hi < len(hunks)):
            return f"error: hunk index {hi} not in file {files[fi].path!r} (hunk_id {hunk_id!r})"
        parsed = hunks[hi].parsed
        body = parsed.body or ""
        text = parsed.header if not body else f"{parsed.header}\n{body}"
        return _cap(f"# {files[fi].path}\n{text}")


def _parse_hunk_id(hunk_id: str) -> tuple[int, int]:
    """`"H{fi}_{hi}"` -> (fi, hi). Raises ValueError on malformed input.

    Mirrors `review/server.py`'s parser; duplicated rather than imported
    to keep this module free of the stdlib-only server module.
    """
    if not hunk_id.startswith("H") or "_" not in hunk_id:
        raise ValueError(f"malformed hunk_id {hunk_id!r}")
    try:
        fi_str, hi_str = hunk_id[1:].split("_", 1)
        return int(fi_str), int(hi_str)
    except ValueError as e:
        raise ValueError(f"malformed hunk_id {hunk_id!r}") from e


def _is_inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _cap(text: str) -> str:
    data = text.encode("utf-8")
    if len(data) <= TOOL_RESULT_CAP_BYTES:
        return text
    cut = data[:TOOL_RESULT_CAP_BYTES].decode("utf-8", errors="replace")
    return cut + "\n\n... [truncated — narrow your query] ..."


def _slice_and_cap(text: str, start_line: int | None, end_line: int | None) -> str:
    if start_line is None and end_line is None:
        return _cap(text)
    lines = text.splitlines(keepends=True)
    s = (start_line - 1) if start_line else 0
    e = end_line if end_line else len(lines)
    s = max(0, s)
    e = min(len(lines), e)
    return _cap("".join(lines[s:e]))


# ---------------------------------------------------------------------------
# Tool surface — derived from `RepoTools`
# ---------------------------------------------------------------------------
#
# Both the pydantic-ai Agent (`tools=TOOL_FUNCTIONS`) and the MCP stdio
# server (`mcp_tool_schemas`, `mcp_dispatch`) read from the same set of
# `@_tool`-marked methods. Adding/renaming a tool means editing one
# method — the wire surface follows.


def _exported_methods() -> list[tuple[str, Callable]]:
    """Return `(name, func)` for each `@_tool`-marked method, in source order."""
    out: list[tuple[str, Callable]] = []
    for name, attr in vars(RepoTools).items():
        if callable(attr) and getattr(attr, _TOOL_EXPORT_ATTR, False):
            out.append((name, attr))
    return out


def _make_tool_fn(method_name: str, method: Callable) -> Callable:
    """Wrap a `RepoTools` method as a pydantic-ai-compatible tool function.

    The returned callable takes `RunContext[RepoTools]` followed by the
    method's parameters (minus `self`), and forwards to the matching
    method on `ctx.deps`. Name, docstring, signature, and annotations
    are copied so pydantic-ai's introspection produces the same schema
    a hand-written wrapper would.
    """
    sig = inspect.signature(method)
    method_params = list(sig.parameters.values())[1:]  # drop self
    ctx_param = inspect.Parameter(
        "ctx",
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        annotation=RunContext[RepoTools],
    )
    new_sig = sig.replace(
        parameters=[ctx_param, *method_params],
        return_annotation=str,
    )

    async def fn(ctx: RunContext[RepoTools], **kwargs: Any) -> str:
        return getattr(ctx.deps, method_name)(**kwargs)

    fn.__name__ = method_name
    fn.__qualname__ = method_name
    fn.__doc__ = method.__doc__
    fn.__signature__ = new_sig  # type: ignore[attr-defined]
    annotations: dict[str, Any] = {
        p.name: p.annotation
        for p in (ctx_param, *method_params)
        if p.annotation is not inspect.Parameter.empty
    }
    annotations["return"] = str
    fn.__annotations__ = annotations
    return fn


TOOL_FUNCTIONS: list = [_make_tool_fn(n, m) for n, m in _exported_methods()]


def console_tool_functions() -> list:
    """Tool surface for the review console: the shared `@_tool` surface
    plus the console-only `hunk(id)` diff accessor.

    `hunk` is deliberately *not* `@_tool`-marked — it needs a bound diff
    that only exists once augmentation has produced the sidecar, so it
    has no place on the augment-time per-hunk pass or the MCP server.
    The console binds `RepoTools.diff` and wires this extended list as
    its `tools=`.
    """
    return [*TOOL_FUNCTIONS, _make_tool_fn("hunk", RepoTools.hunk)]


def mcp_tool_schemas() -> list[dict[str, Any]]:
    """MCP `tools/list` payload, derived from `TOOL_FUNCTIONS`."""
    out: list[dict[str, Any]] = []
    for fn in TOOL_FUNCTIONS:
        tool = Tool(fn)
        out.append(
            {
                "name": tool.name,
                "description": tool.description or "",
                "inputSchema": tool.function_schema.json_schema,
            }
        )
    return out


def mcp_dispatch(repo_tools: RepoTools, name: str, args: dict[str, Any]) -> str:
    """Run a tool by name against `repo_tools` for the MCP `tools/call` path.

    Only methods marked with `@_tool` on `RepoTools` are reachable —
    private helpers and dunder attrs are rejected.
    """
    method = getattr(repo_tools, name, None)
    if not callable(method) or not getattr(method, _TOOL_EXPORT_ATTR, False):
        return f"error: unknown tool {name!r}"
    try:
        return method(**args)
    except TypeError as e:
        return f"error: bad args for {name}: {e}"
