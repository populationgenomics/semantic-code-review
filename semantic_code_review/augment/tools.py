"""Repo tools the LLM can call during a per-hunk pass.

Every tool is read-only, operates on the fetched worktrees, and returns
text. Large results are truncated and flagged so the model can narrow
its query.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


TOOL_RESULT_CAP_BYTES = 20 * 1024


# Resolved once at import; tests that want to force a path can monkeypatch.
_HAS_RIPGREP = shutil.which("rg") is not None


@dataclass
class RepoTools:
    head_worktree: Path
    repo_git: Path
    base_sha: str
    head_sha: str

    # --- file reads -------------------------------------------------------

    def read_file(self, path: str, start_line: int | None = None, end_line: int | None = None) -> str:
        """Read a file from the head worktree, optionally a line range."""
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

    def read_file_at(self, sha: str, path: str, start_line: int | None = None, end_line: int | None = None) -> str:
        """Read a file at an arbitrary SHA via `git show`."""
        result = subprocess.run(
            ["git", "show", f"{sha}:{path}"],
            cwd=self.repo_git, capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            return f"error: git show {sha}:{path} failed: {result.stderr.strip()}"
        return _slice_and_cap(result.stdout, start_line, end_line)

    # --- search -----------------------------------------------------------

    def grep(self, pattern: str, path_glob: str | None = None, max_hits: int = 50) -> str:
        """Search the head worktree. Prefers ripgrep; falls back to `git grep`.

        Output is ``path:line:text`` with worktree prefix stripped.
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
        hard requirement. Respects .gitignore; only searches tracked files."""
        args = ["git", "grep", "-n", "-I", "--max-count", str(max_hits), "-e", pattern]
        if path_glob:
            # git grep wants pathspecs after ``--``.
            args += ["--", path_glob]
        result = subprocess.run(
            args, cwd=self.head_worktree, capture_output=True, text=True, check=False,
        )
        # git grep exits 1 on no matches, like rg; 128+ on error.
        if result.returncode not in (0, 1):
            return f"error: git grep failed: {result.stderr.strip()}"
        return _cap(result.stdout)

    # --- listing ----------------------------------------------------------

    def list_dir(self, path: str = "") -> str:
        """Shallow listing of a directory in the head worktree."""
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

    def git_log(self, path: str, limit: int = 5) -> str:
        """Short git log for a path (relative to repo root)."""
        result = subprocess.run(
            ["git", "log", f"-n{limit}", "--oneline", "--", path],
            cwd=self.repo_git, capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            return f"error: git log failed: {result.stderr.strip()}"
        return _cap(result.stdout)


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


# --- Anthropic tool schemas -------------------------------------------------

ANTHROPIC_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "read_file",
        "description": "Read a file from the head worktree. Returns up to 20 KB of text.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to repo root."},
                "start_line": {"type": "integer", "description": "1-indexed start line (optional)."},
                "end_line": {"type": "integer", "description": "1-indexed end line inclusive (optional)."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "read_file_at",
        "description": "Read a file at a specific commit SHA (e.g. the PR base). Use for pre-change content.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sha": {"type": "string", "description": "Commit SHA."},
                "path": {"type": "string"},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
            },
            "required": ["sha", "path"],
        },
    },
    {
        "name": "grep",
        "description": "Search the head worktree with ripgrep. Returns path:line:text matches, capped at 50 by default.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path_glob": {"type": "string", "description": "Restrict to matching files (e.g. 'src/**/*.py')."},
                "max_hits": {"type": "integer"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "list_dir",
        "description": "List a directory in the head worktree (shallow, hidden files skipped).",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to repo root (empty for root)."},
            },
        },
    },
    {
        "name": "git_log",
        "description": "Recent commits touching a path (short form).",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["path"],
        },
    },
]


def dispatch(tools: RepoTools, name: str, input_args: dict[str, Any]) -> str:
    if name == "read_file":
        return tools.read_file(**input_args)
    if name == "read_file_at":
        return tools.read_file_at(**input_args)
    if name == "grep":
        return tools.grep(**input_args)
    if name == "list_dir":
        return tools.list_dir(**input_args)
    if name == "git_log":
        return tools.git_log(**input_args)
    return f"error: unknown tool {name!r}"
