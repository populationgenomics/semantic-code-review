"""Repo tools the LLM can call during a per-hunk pass.

Every tool is read-only, operates on the fetched worktrees, and returns
text. Large results are truncated and flagged so the model can narrow
its query.

`RepoTools` is the single source of truth for the tool surface. Methods
decorated with `@_tool` are exported to two consumers:

  * pydantic-ai SDK Agents — via `TOOL_FUNCTIONS`, a list of `RunContext`-
    wrapping callables produced from the decorated methods.
  * The hosted HTTP MCP server (`mcp_http_host.py`) — via
    `mcp_tool_schemas()` for the `tools/list` payload and `mcp_dispatch()`
    for `tools/call`.

Both surfaces are derived by introspection: rename a `RepoTools` method
and both update with no other edits.
"""

from __future__ import annotations

import inspect
import logging
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, cast

from pydantic import Field
from pydantic_ai import RunContext
from pydantic_ai.tools import Tool

from .. import git_ops, structural
from ..structural import references as structural_references
from . import source_cache

log = logging.getLogger(__name__)

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
    # Optional `(sha, path)` read/parse memo, owned by the run and shared
    # across every `RepoTools` it builds (ADR 0003 Slice 1). None ⇒ each
    # read/parse recomputes; behaviour is otherwise identical.
    cache: source_cache.SourceCache | None = None

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

        Use for pre-change content. The base SHA is in the `# PR
        overview` block as `base_sha` — pass that. Relative revisions
        (`HEAD~1`, `HEAD^`) do not resolve here: the repository is a
        shallow fetch of base and head only, with no parents.

        Args:
            sha: Commit SHA.
            path: Path relative to repo root.
            start_line: 1-indexed start line (optional).
            end_line: 1-indexed end line inclusive (optional).
        """
        rc, stdout, stderr = git_ops.git_capture(
            self.repo_git,
            "show",
            f"{sha}:{path}",
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
        symbols = self._outline_symbols(path, sha, source, lang)
        return _cap(structural.symbols_to_json(symbols))

    def _read_source(self, path: str, sha: str | None) -> str | None:
        """Raw file text from the head worktree (``sha is None``) or at a
        revision via ``git show``. ``None`` if it can't be read.

        Memoised through `self.cache` when one is bound; the content of a
        `(sha, path)` is immutable for the run, so the read runs once.
        """
        if self.cache is None:
            return self._read_source_uncached(path, sha)
        return self.cache.source(sha, path, lambda: self._read_source_uncached(path, sha))

    def _read_source_uncached(self, path: str, sha: str | None) -> str | None:
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

    def _outline_symbols(self, path: str, sha: str | None, source: str, lang: str) -> list[structural.Symbol]:
        """Tree-sitter outline of `source`, memoised by `(sha, path)`.

        `source` and `lang` are redundant with `(sha, path)` under the
        immutability invariant; they are the inputs the compute actually
        needs, passed so the cache stays agnostic to how a parse is done.
        """
        if self.cache is None:
            return structural.outline_symbols(source, lang)
        return self.cache.outline(sha, path, lambda: structural.outline_symbols(source, lang))

    # --- prompt seeds -----------------------------------------------------
    #
    # Not part of the LLM tool surface (no `@_tool`): runs in-process to
    # put in the prompt what the model would otherwise spend a turn
    # fetching. Returns "" when there is no answer — an unsupported
    # language has no outline — so the caller omits the section rather
    # than asserting an empty one.

    def outline_seed(self, path: str) -> str:
        """Flat text outline of a head-worktree file, for the hunk prompt.

        Text rather than the `outline` tool's JSON: this rides in every
        hunk prompt for the file, where the JSON's punctuation is pure
        overhead. Signatures are kept — they are what makes the outline
        answer "what does this call take" without a read.
        """
        lang = structural.language_for_path(path)
        if lang is None:
            return ""
        source = self._read_source(path, None)
        if source is None:
            return ""
        symbols = self._outline_symbols(path, None, source, lang)
        lines: list[str] = []
        _render_outline(symbols, depth=0, out=lines)
        return _cap("\n".join(lines))

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
        symbols = self._outline_symbols(path, sha, source, lang)
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
            base_syms = self._outline_symbols(path, self.base_sha, base_src, lang) if base_src is not None else []
            head_syms = self._outline_symbols(path, None, head_src, lang) if head_src is not None else []
            deltas.append(structural.diff_file(path, base_syms, head_syms))
        return structural.merge(deltas)

    @_tool
    def changed_symbols(self, path: str | None = None) -> str:
        """Deterministic structural delta of the diff, as JSON.

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

        This is the whole-PR symbol inventory the hunk prompt no longer
        carries inline: on a large diff it ran to tens of thousands of
        characters and was re-sent with every hunk, while any one hunk
        needed a handful of entries.

        Args:
            path: Restrict to symbols in this file. Omit for the whole
                diff, which is what you want when chasing a symbol whose
                definition moved or was deleted from another file.
        """
        try:
            delta = self.compute_symbol_delta()
        except git_ops.GitError as e:
            return f"error: {e}"
        if path is not None:
            delta = structural.SymbolDelta(
                added=[s for s in delta.added if s.path == path],
                removed=[s for s in delta.removed if s.path == path],
                modified=[s for s in delta.modified if s.path == path],
            )
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
        out = "\n".join(line.removeprefix(prefix) for line in result.stdout.splitlines())
        return _cap(out)

    def _grep_git(self, pattern: str, path_glob: str | None, max_hits: int) -> str:
        """Fallback search via ``git grep`` — always available since git is a
        hard requirement. Respects .gitignore; only searches tracked files.
        """
        try:
            return _cap(git_ops.grep(self.head_worktree, pattern, path_glob, max_hits))
        except git_ops.GitError as e:
            return f"error: {e}"

    @_tool
    def grep_at(self, pattern: str, sha: str, path_glob: str | None = None, max_hits: int = 50) -> str:
        """Search the repository at a commit — use this for removed code.

        `grep` searches the head worktree, i.e. the code as it is AFTER
        the change, so anything the change deleted is not there and the
        search comes back empty. That empty result is indistinguishable
        from a bad pattern, which is what sends a search into a loop.
        Pass the base SHA here to search the pre-change tree instead.

        Same `path:line:text` output as `grep`.

        Args:
            pattern: Pattern to search for.
            sha: Commit to search at. Use `base_sha` from the `# PR
                overview` block; relative revisions (`HEAD~1`, `HEAD^`)
                do not resolve, as the repository is a shallow fetch of
                base and head with no parents.
            path_glob: Restrict to matching files (e.g. 'src/**/*.py').
            max_hits: Maximum matches to return.
        """
        try:
            return _cap(git_ops.grep_at(self.repo_git, pattern, sha, path_glob, max_hits))
        except git_ops.GitError as e:
            return f"error: {e}"

    @_tool
    def references(self, name: str, path_glob: str | None = None, max_hits: int = 50) -> str:
        """Where `name` is actually *used* in the head worktree.

        Use this for "is X still referenced", "who calls X", "did that
        removal leave anything behind" — the questions a text search
        answers badly. Unlike `grep` this reads the code structurally,
        so comments, strings, substrings of longer names and the
        definition itself are all excluded, and `numpy` is reported as
        used by `np.array(x)` even though the text `numpy` appears only
        in the import.

        Name-based, not scope-resolved: two distinct `helper`s in one
        file are indistinguishable. A file that will not parse falls
        back to a text search for that file and is flagged inline —
        both sides come from git, so a parse failure is itself worth
        knowing about.

        Returns `path:line: kind: text`, plus a total. Empty means no
        use sites, which for a removed symbol is a real answer.

        Args:
            name: Bare symbol name — `helper`, `RepoTools`, `np`.
            path_glob: Restrict to matching files (e.g. 'src/**/*.py').
            max_hits: Maximum use sites to return.
        """
        candidates, truncated = self._reference_candidates(name, path_glob)
        lines: list[str] = []
        degraded: list[str] = []
        total = 0
        for rel in candidates:
            full = self.head_worktree / rel
            try:
                source = full.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            try:
                refs = structural_references.references(source, rel, name)
            except structural_references.ParseFailed:
                # Fall back to a text search of just this file, and say
                # so — an unparseable file in a diff is a signal, not
                # something to quietly drop.
                degraded.append(rel)
                try:
                    hits = git_ops.grep(self.head_worktree, name, rel, max_hits)
                except git_ops.GitError:
                    hits = ""
                for hit in hits.splitlines():
                    total += 1
                    if len(lines) < max_hits:
                        lines.append(f"{hit}  (text match; {rel} did not parse)")
                continue
            for r in refs:
                total += 1
                if len(lines) < max_hits:
                    lines.append(f"{rel}:{r.line}: {r.kind}: {r.text}")
        head = f"{total} use site(s) of {name!r}" + (f", showing {len(lines)}" if total > len(lines) else "")
        if truncated:
            head = (
                f"{total}+ use site(s) of {name!r} across the first {len(candidates)} matching files"
                f" — more files matched than were parsed, so this is a lower bound"
            )
        body = "\n".join(lines)
        if degraded:
            body += ("\n" if body else "") + (
                f"note: {len(degraded)} file(s) did not parse; their hits above are text matches: "
                + ", ".join(sorted(degraded)[:5])
            )
        return _cap(f"{head}\n{body}" if body else head)

    #: Candidate files parsed for one `references` call. A cap so a
    #: pathological identifier cannot parse an entire monorepo; when it
    #: bites, the tool says so rather than reporting a confident total.
    REFERENCE_FILE_CAP = 400

    def _reference_candidates(self, name: str, path_glob: str | None) -> tuple[list[str], bool]:
        """Files worth parsing, and whether the list was truncated.

        A file-name search rather than a line search: the line-oriented
        form is capped at 20 KB of output, which on a common identifier
        silently dropped most matching files while the caller went on to
        report a definite total.
        """
        limit = self.REFERENCE_FILE_CAP + 1
        if _HAS_RIPGREP:
            # Mirrors `grep`'s backend choice: ripgrep sees the whole
            # worktree, `git grep` only tracked files.
            args = ["rg", "--files-with-matches", "-e", name]
            if path_glob:
                args += ["--glob", path_glob]
            args.append(str(self.head_worktree))
            proc = subprocess.run(args, capture_output=True, text=True, check=False)
            if proc.returncode not in (0, 1):
                return [], False
            prefix = str(self.head_worktree) + os.sep
            found = [ln.removeprefix(prefix) for ln in proc.stdout.splitlines() if ln.strip()][:limit]
        else:
            try:
                found = git_ops.grep_files(self.head_worktree, name, path_glob, limit)
            except git_ops.GitError:
                return [], False
        if len(found) > self.REFERENCE_FILE_CAP:
            return found[: self.REFERENCE_FILE_CAP], True
        return found, False

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
            return _cap(
                git_ops.git(
                    self.repo_git,
                    "log",
                    f"-n{limit}",
                    "--oneline",
                    "--",
                    path,
                )
            )
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


def _render_outline(symbols: list[structural.Symbol], *, depth: int, out: list[str]) -> None:
    """Append `symbols` to `out` as indented `kind name  (start-end)` lines."""
    for sym in symbols:
        # A declared signature already names its own kind syntactically
        # (`def f(...)`, `class C`), so `kind` is only worth spending a
        # token on when there is no signature to show.
        label = sym.signature or f"{sym.kind} {sym.name}"
        out.append(f"{'  ' * depth}{label}  ({sym.range.start_line}-{sym.range.end_line})")
        _render_outline(sym.children, depth=depth + 1, out=out)


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
# Both the pydantic-ai Agent (`tools=TOOL_FUNCTIONS`) and the hosted HTTP
# MCP server (`mcp_tool_schemas`, `mcp_dispatch`) read from the same set of
# `@_tool`-marked methods. Adding/renaming a tool means editing one
# method — the wire surface follows.


def _exported_methods() -> list[tuple[str, Callable]]:
    """Return `(name, func)` for each `@_tool`-marked method, in source order."""
    out: list[tuple[str, Callable]] = []
    for name, attr in vars(RepoTools).items():
        if callable(attr) and getattr(attr, _TOOL_EXPORT_ATTR, False):
            out.append((name, attr))
    return out


PURPOSE_PARAM = "purpose"

PURPOSE_DESC = (
    "What you are trying to establish with this call, in one short clause "
    "(e.g. 'find where RepoTools is imported'). Recorded for diagnostics, "
    "not used to answer the query."
)

#: The `purpose` parameter's annotation. The description rides on a
#: `Field` rather than in the docstring's `Args:` section: pydantic-ai
#: derives parameter descriptions with griffe, which honours only the
#: last `Args:` section and is sensitive to its exact indentation — and
#: Python 3.13 dedents docstrings at compile time (gh-81283), so the
#: right indent is version-dependent. Annotating leaves every method's
#: docstring untouched, so real parameters keep their descriptions.
PurposeParam = Annotated[str, Field(description=PURPOSE_DESC)]


def _make_tool_fn(method_name: str, method: Callable) -> Callable:
    """Wrap a `RepoTools` method as a pydantic-ai-compatible tool function.

    The returned callable takes `RunContext[RepoTools]` followed by a
    `purpose` string and the method's parameters (minus `self`), and
    forwards to the matching method on `ctx.deps`. Name, docstring,
    signature, and annotations are copied so pydantic-ai's introspection
    produces the same schema a hand-written wrapper would.

    `purpose` is injected here rather than added to each `RepoTools`
    method because the tool surface is derived by introspection: one
    insertion covers every tool on both the SDK and MCP paths. It is
    stripped before the call — the method never sees it. It exists so a
    trace shows the *reason* for each call, which is what distinguishes a
    productive investigation from a loop that rephrases the same question
    (observed: 50 calls, zero exact repeats, 4 distinct paths).
    """
    sig = inspect.signature(method)
    method_params = list(sig.parameters.values())[1:]  # drop self
    ctx_param = inspect.Parameter(
        "ctx",
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        annotation=RunContext[RepoTools],
    )
    purpose_param = inspect.Parameter(
        PURPOSE_PARAM,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        annotation=PurposeParam,
    )
    new_sig = sig.replace(
        parameters=[ctx_param, purpose_param, *method_params],
        return_annotation=str,
    )

    async def fn(ctx: RunContext[RepoTools], **kwargs: Any) -> str:
        purpose = kwargs.pop(PURPOSE_PARAM, "")
        log.info("tool %s(%s) — %s", method_name, _arg_summary(kwargs), purpose or "(no purpose given)")
        return getattr(ctx.deps, method_name)(**kwargs)

    fn.__name__ = method_name
    fn.__qualname__ = method_name
    fn.__doc__ = method.__doc__
    fn.__signature__ = new_sig  # type: ignore[attr-defined]
    annotations: dict[str, Any] = {
        p.name: p.annotation
        for p in (ctx_param, purpose_param, *method_params)
        if p.annotation is not inspect.Parameter.empty
    }
    annotations["return"] = str
    fn.__annotations__ = annotations
    return fn


def _arg_summary(kwargs: dict[str, Any]) -> str:
    """Compact one-line rendering of tool args for the log."""
    return ", ".join(f"{k}={v!r}" for k, v in kwargs.items() if v is not None)


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
    # `purpose` is advertised on the schema for diagnostics and is not a
    # parameter of the underlying method; strip it before dispatch.
    call_args = {k: v for k, v in args.items() if k != PURPOSE_PARAM}
    log.info("tool %s(%s) — %s", name, _arg_summary(call_args), args.get(PURPOSE_PARAM) or "(no purpose given)")
    try:
        # Exported tool methods return str; getattr erases that to object.
        return cast("str", method(**call_args))
    except TypeError as e:
        return f"error: bad args for {name}: {e}"
