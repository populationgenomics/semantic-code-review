"""Resolve a reviewer-supplied git input into a unified diff + SHAs.

Accepted inputs (all run against the current repository's cwd):

* ``ref1..ref2`` or ``ref1...ref2`` — committed-only diff. Must NOT be
  combined with ``--no-staged`` / ``--no-unstaged``; the range is taken
  at face value.
* ``<ref>`` — diff from ``<ref>`` to the current working state (HEAD +
  staged + unstaged). ``--no-staged`` drops staged changes from the
  overlay; ``--no-unstaged`` drops unstaged changes. With both, the
  result is equivalent to ``<ref>..HEAD``.

Public entry point is :func:`build_local_diff`.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from .. import git_ops


_RANGE_RE = re.compile(r"^(?P<a>[^.]+)\.\.\.?(?P<b>.+)$")


@dataclass(frozen=True)
class LocalDiff:
    raw_diff: str
    base_sha: str
    head_sha: str            # real SHA for committed modes; synthetic for dirty.
    head_is_working: bool    # if True, head_worktree is the actual cwd checkout.
    head_worktree: Path      # path pointing at the head tree (checkout or new worktree needed).
    repo_git: Path           # .git dir of the repo.
    mode: str                # "range" | "branch-range" | "working" | "working-no-staged" | ...
    slug: str                # stable-ish slug for run-dir naming.
    files: list[str]


class LocalDiffError(ValueError):
    """Raised when the requested diff input is malformed or empty."""


class EmptyDiff(LocalDiffError):
    """The spec resolved cleanly but produced no changes.

    Distinct from the malformed-input cases so the CLI can exit 0
    with a friendly message instead of treating "nothing to review"
    as an error.
    """


def build_local_diff(
    spec: str,
    *,
    repo_root: Path | None = None,
    no_staged: bool = False,
    no_unstaged: bool = False,
) -> LocalDiff:
    """Resolve ``spec`` into a :class:`LocalDiff`. Run from cwd or *repo_root*."""
    cwd = (repo_root or _find_repo_root(Path.cwd())).resolve()
    git_dir = cwd / ".git"
    if not git_dir.exists():
        raise LocalDiffError(f"not a git repo: {cwd}")

    spec = spec.strip()
    if not spec:
        raise LocalDiffError("empty ref/range")

    m = _RANGE_RE.match(spec)
    if m:
        if no_staged or no_unstaged:
            raise LocalDiffError(
                "--no-staged / --no-unstaged only apply when a single ref is given"
            )
        base_ref, head_ref = m.group("a"), m.group("b")
        sep = "..." if "..." in spec else ".."
        raw, base_sha, head_sha = _diff_committed_range(cwd, base_ref, head_ref, sep)
        mode = "range"
        slug_source = f"{base_ref}{sep}{head_ref}"
        head_is_working = False
    else:
        raw, base_sha, head_sha, mode, head_is_working = _diff_single_ref(
            cwd, spec, no_staged=no_staged, no_unstaged=no_unstaged
        )
        slug_source = spec

    if not raw.strip():
        raise EmptyDiff(f"no changes to review for {spec!r}")

    files = _changed_files(raw)
    slug = _slug(slug_source, head_sha, head_is_working)
    return LocalDiff(
        raw_diff=raw,
        base_sha=base_sha,
        head_sha=head_sha,
        head_is_working=head_is_working,
        head_worktree=cwd,  # working mode uses cwd directly; committed mode will re-point
        repo_git=git_dir,
        mode=mode,
        slug=slug,
        files=files,
    )


# --- internals --------------------------------------------------------------


def _find_repo_root(start: Path) -> Path:
    """Walk up looking for a ``.git`` entry."""
    p = start.resolve()
    while True:
        if (p / ".git").exists():
            return p
        if p.parent == p:
            raise LocalDiffError(f"no git repo found at or above {start}")
        p = p.parent


def _diff_committed_range(
    cwd: Path, base_ref: str, head_ref: str, sep: str
) -> tuple[str, str, str]:
    base_sha = _safe_rev_parse(cwd, base_ref)
    head_sha = _safe_rev_parse(cwd, head_ref)
    # Use the resolved SHAs in the diff command so rename/symbol churn in the
    # interim (another checkout, etc.) can't change what we show.
    if sep == "...":
        merge_base_sha = _safe_merge_base(cwd, base_sha, head_sha)
        raw = _safe_diff(cwd, f"{merge_base_sha}..{head_sha}")
        base_sha = merge_base_sha
    else:
        raw = _safe_diff(cwd, f"{base_sha}..{head_sha}")
    return raw, base_sha, head_sha


def _diff_single_ref(
    cwd: Path,
    ref: str,
    *,
    no_staged: bool,
    no_unstaged: bool,
) -> tuple[str, str, str, str, bool]:
    """Return (raw_diff, base_sha, head_sha, mode, head_is_working)."""
    base_sha = _safe_rev_parse(cwd, ref)

    if no_staged and no_unstaged:
        # <ref>..HEAD (pure committed diff)
        head_sha = _safe_rev_parse(cwd, "HEAD")
        raw = _safe_diff(cwd, f"{base_sha}..{head_sha}")
        return raw, base_sha, head_sha, "ref..HEAD", False

    if no_staged:
        # <ref> vs working tree, minus index — i.e. HEAD-committed + unstaged.
        # `git diff <ref>` gives ref vs working; subtracting staged would
        # require two-pass plumbing. Simpler: combine `ref..HEAD` with
        # unstaged-only (HEAD vs working tree, excluding staged).
        head_committed = _safe_rev_parse(cwd, "HEAD")
        committed_part = _safe_diff(cwd, f"{base_sha}..{head_committed}")
        unstaged_part = _safe_diff(cwd)  # HEAD vs working (approx); see note
        # Concatenation of two independent diffs can touch the same file twice;
        # that's fine for our viewer (rare enough to ignore) but warn:
        raw = _concat_diffs(committed_part, unstaged_part)
        head_sha = _synthesise_head_sha(head_committed, raw)
        return raw, base_sha, head_sha, "ref-working-no-staged", True

    if no_unstaged:
        # <ref> vs (HEAD + staged). That's `git diff --cached <ref>` inverted:
        # actually `git diff <ref> --cached` gives ref vs index.
        raw = _safe_diff(cwd, "--cached", base_sha)
        head_committed = _safe_rev_parse(cwd, "HEAD")
        head_sha = _synthesise_head_sha(head_committed, raw, tag="staged")
        return raw, base_sha, head_sha, "ref-working-no-unstaged", True

    # Default: <ref> vs current working state (includes staged + unstaged).
    raw = _safe_diff(cwd, base_sha)
    head_committed = _safe_rev_parse(cwd, "HEAD")
    is_dirty = bool(_safe_status(cwd).strip())
    head_sha = _synthesise_head_sha(head_committed, raw) if is_dirty else head_committed
    mode = "ref-working" if is_dirty else "ref..HEAD"
    return raw, base_sha, head_sha, mode, True


# Thin shims that re-raise GitError as LocalDiffError so callers of
# build_local_diff catch a single domain exception. Keeping them as
# small named helpers (rather than inlined try/except blocks) keeps
# the call-site flow readable.

def _safe_rev_parse(cwd: Path, ref: str) -> str:
    try:
        return git_ops.rev_parse(cwd, ref)
    except git_ops.GitError as e:
        raise LocalDiffError(str(e)) from e


def _safe_merge_base(cwd: Path, a: str, b: str) -> str:
    try:
        return git_ops.merge_base(cwd, a, b)
    except git_ops.GitError as e:
        raise LocalDiffError(str(e)) from e


def _safe_diff(cwd: Path, *args: str) -> str:
    try:
        return git_ops.diff(cwd, *args)
    except git_ops.GitError as e:
        raise LocalDiffError(str(e)) from e


def _safe_status(cwd: Path) -> str:
    try:
        return git_ops.status_porcelain(cwd)
    except git_ops.GitError as e:
        raise LocalDiffError(str(e)) from e


def _concat_diffs(*parts: str) -> str:
    return "".join(p if p.endswith("\n") or not p else p + "\n" for p in parts if p)


def _changed_files(raw: str) -> list[str]:
    paths: list[str] = []
    for line in raw.splitlines():
        if line.startswith("diff --git "):
            # diff --git a/<old> b/<new>
            try:
                _, _, rest = line.partition("diff --git ")
                _a, b = rest.split(" ", 1)
                paths.append(b.removeprefix("b/"))
            except ValueError:
                continue
    # Dedup preserving order (concatenated diffs can repeat).
    seen: set[str] = set()
    out: list[str] = []
    for p in paths:
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out


def _synthesise_head_sha(head_sha: str, dirty_diff: str, tag: str = "dirty") -> str:
    """Produce a cache-stable SHA for a working-state head.

    We want cache-hits to NOT apply across different working states, but to
    apply cleanly when the working state is unchanged. A hash of (head_sha +
    diff text) satisfies both.
    """
    h = hashlib.sha256()
    h.update(head_sha.encode())
    h.update(b"\x1f")
    h.update(dirty_diff.encode())
    return f"{head_sha}-{tag}-{h.hexdigest()[:12]}"


def _slug(source: str, head_sha: str, head_is_working: bool) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", source).strip("-") or "local"
    short = head_sha[:8]
    if head_is_working and "-" in head_sha:
        # synthetic SHA already carries a -dirty-<hash> suffix; keep it
        short = head_sha.split("-", 1)[1][:12]
    return f"local-{s}-{short}"
