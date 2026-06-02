"""Local-diff source: a git ref/range against the cwd repo → run directory.

Accepted inputs (all run against the current repository's cwd):

* ``ref1..ref2`` or ``ref1...ref2`` — committed-only diff. Must NOT be
  combined with ``--no-staged`` / ``--no-unstaged``; the range is taken
  at face value.
* ``<ref>`` — diff from ``<ref>`` to the current working state (HEAD +
  staged + unstaged). ``--no-staged`` drops staged changes from the
  overlay; ``--no-unstaged`` drops unstaged changes. With both, the
  result is equivalent to ``<ref>..HEAD``.

Pipeline mirrors the GitHub-PR source:

1. `resolve_local_diff(spec, ...)` — resolve refs against the cwd
   repo, run `git diff`, synthesise a PR-shaped meta dict, package
   everything into a `LocalResolved` wrapping a `RunSpec`.
2. `materialize_run_metadata(resolved.spec, runs_root)` — shared
   on-disk write.
3. `setup_local_worktrees(run_dir, resolved)` — symlink head/ for
   working-state mode, else `worktree add --detach`. Base is always
   a real worktree against the cwd repo.

`materialize_local_diff_run` ties the three together.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from .. import git_ops
from .run_source import RunSpec, materialize_run_metadata


_RANGE_RE = re.compile(r"^(?P<a>[^.]+)\.\.\.?(?P<b>.+)$")


class LocalDiffError(ValueError):
    """Raised when the requested diff input is malformed or empty."""


class EmptyDiff(LocalDiffError):
    """The spec resolved cleanly but produced no changes.

    Distinct from the malformed-input cases so the CLI can exit 0
    with a friendly message instead of treating "nothing to review"
    as an error.
    """


@dataclass(frozen=True)
class LocalResolved:
    """`RunSpec` + local-source extras carried between resolve and the
    per-source worktree setup.

    Direct attributes (`mode`, `head_is_working`) duplicate fields the
    resolve step also writes into `spec.meta`; carried here so tests
    and worktree setup can probe them structurally without parsing
    meta.json back out.
    """

    spec: RunSpec
    repo_git: Path           # .git dir of the cwd repo
    head_is_working: bool    # if True, head/ is a symlink to head_worktree
    head_worktree: Path      # the cwd checkout (used only when head_is_working)
    mode: str                # "range" | "ref-working" | "ref..HEAD" | ...

    # Convenience aliases so tests don't need to dig through `.spec.*`.
    @property
    def raw_diff(self) -> str:
        return self.spec.raw_diff

    @property
    def base_sha(self) -> str:
        return self.spec.base_sha

    @property
    def head_sha(self) -> str:
        return self.spec.head_sha

    @property
    def files(self) -> list[str]:
        return self.spec.files

    @property
    def slug(self) -> str:
        return self.spec.slug


def resolve_local_diff(
    spec: str,
    *,
    repo_root: Path | None = None,
    no_staged: bool = False,
    no_unstaged: bool = False,
    spec_md_path: Path | None = None,
) -> LocalResolved:
    """Resolve ``spec`` against the cwd repo into a `LocalResolved`.

    Side-effect-free above the git subprocess calls — does not touch
    `runs_root`. Caller-owned: deciding where to materialise.
    """
    cwd = (repo_root or _find_repo_root(Path.cwd())).resolve()
    git_dir = cwd / ".git"
    if not git_dir.exists():
        raise LocalDiffError(f"not a git repo: {cwd}")

    spec_str = spec.strip()
    if not spec_str:
        raise LocalDiffError("empty ref/range")

    m = _RANGE_RE.match(spec_str)
    if m:
        if no_staged or no_unstaged:
            raise LocalDiffError(
                "--no-staged / --no-unstaged only apply when a single ref is given"
            )
        base_ref, head_ref = m.group("a"), m.group("b")
        sep = "..." if "..." in spec_str else ".."
        raw, base_sha, head_sha = _diff_committed_range(cwd, base_ref, head_ref, sep)
        mode = "range"
        slug_source = f"{base_ref}{sep}{head_ref}"
        head_is_working = False
    else:
        raw, base_sha, head_sha, mode, head_is_working = _diff_single_ref(
            cwd, spec_str, no_staged=no_staged, no_unstaged=no_unstaged
        )
        slug_source = spec_str

    if not raw.strip():
        raise EmptyDiff(f"no changes to review for {spec!r}")

    files = _changed_files(raw)
    slug = _slug(slug_source, head_sha, head_is_working)

    spec_md_text: str | None = None
    if spec_md_path is not None:
        spec_md_text = spec_md_path.read_text(encoding="utf-8")

    run_spec = RunSpec(
        slug=slug,
        raw_diff=raw,
        base_sha=base_sha,
        head_sha=head_sha,
        files=files,
        meta=_synthesise_meta(
            slug=slug, base_sha=base_sha, head_sha=head_sha,
            files=files, mode=mode, head_is_working=head_is_working,
            spec_md_text=spec_md_text,
        ),
        spec_md_text=spec_md_text,
    )
    return LocalResolved(
        spec=run_spec,
        repo_git=git_dir,
        head_is_working=head_is_working,
        head_worktree=cwd,
        mode=mode,
    )


def setup_local_worktrees(run_dir: Path, resolved: LocalResolved) -> None:
    """Set up `run_dir/{repo.git,base,head}` from a `LocalResolved`.

    `repo.git` is a symlink to the cwd repo's `.git` so RepoTools can
    `git grep` / `git log` through it.

    `head/` is a symlink to the cwd checkout when `head_is_working`
    (so RepoTools sees the live working tree the reviewer is editing);
    otherwise a detached worktree at the committed head SHA.

    `base/` is always a detached worktree at the resolved base SHA —
    the LLM should always read pre-change code from a stable tree.
    """
    repo_git_link = run_dir / "repo.git"
    if not repo_git_link.exists():
        _symlink(repo_git_link, resolved.repo_git)

    head_link = run_dir / "head"
    base_dir = run_dir / "base"

    if resolved.head_is_working:
        if not head_link.exists():
            _symlink(head_link, resolved.head_worktree)
    else:
        if not head_link.exists():
            git_ops.worktree_add(
                resolved.repo_git.parent, head_link.resolve(), resolved.head_sha,
            )

    if not base_dir.exists():
        git_ops.worktree_add(
            resolved.repo_git.parent, base_dir.resolve(), resolved.base_sha,
        )


def materialize_local_diff_run(
    spec: str,
    runs_root: Path,
    *,
    repo_root: Path | None = None,
    no_staged: bool = False,
    no_unstaged: bool = False,
    spec_md_path: Path | None = None,
) -> Path:
    """High-level: resolve → materialise metadata → set up worktrees.

    Returns the run-directory path.
    """
    resolved = resolve_local_diff(
        spec,
        repo_root=repo_root,
        no_staged=no_staged,
        no_unstaged=no_unstaged,
        spec_md_path=spec_md_path,
    )
    run_dir = materialize_run_metadata(resolved.spec, runs_root)
    setup_local_worktrees(run_dir, resolved)
    return run_dir


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


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
# resolve_local_diff catch a single domain exception.

def _safe_rev_parse(cwd: Path, ref: str) -> str:
    try:
        return git_ops.rev_parse(cwd, ref)
    except git_ops.GitError as e:
        raise LocalDiffError(str(e)) from e


def _safe_merge_base(cwd: Path, a: str, b: str) -> str:
    try:
        return git_ops.git(cwd, "merge-base", a, b).strip()
    except git_ops.GitError as e:
        raise LocalDiffError(str(e)) from e


def _safe_diff(cwd: Path, *args: str) -> str:
    try:
        return git_ops.git(cwd, "diff", *args)
    except git_ops.GitError as e:
        raise LocalDiffError(str(e)) from e


def _safe_status(cwd: Path) -> str:
    try:
        return git_ops.git(cwd, "status", "--porcelain")
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


def _synthesise_meta(
    *,
    slug: str,
    base_sha: str,
    head_sha: str,
    files: list[str],
    mode: str,
    head_is_working: bool,
    spec_md_text: str | None,
) -> dict:
    """Build the PR-shaped meta dict the viewer + serve_review expect.

    Same wire shape as a real GitHub PR meta from `gh pr view`, with
    `local: True` plus diagnostic `mode`/`head_is_working` so a
    reopened run dir can be inspected post-hoc.
    """
    title = "Local review: " + slug.removeprefix("local-")
    body = ""
    if spec_md_text:
        body = "# Spec (ground truth)\n\n" + spec_md_text.strip() + "\n"
    return {
        "title": title,
        "body": body,
        "author": {"login": ""},
        "url": "",
        "baseRefOid": base_sha,
        "headRefOid": head_sha,
        "files": [{"path": p} for p in files],
        "number": None,
        "labels": [],
        "additions": 0,
        "deletions": 0,
        "changedFiles": len(files),
        "local": True,
        "mode": mode,
        "head_is_working": head_is_working,
    }


def _symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target.resolve())
    except OSError:
        # Filesystems without symlink support (rare on macOS/Linux): create a
        # marker file with the target path. Tools that rely on the path will
        # fail more loudly and the user can switch modes.
        link.write_text(str(target.resolve()) + "\n", encoding="utf-8")


__all__ = [
    "EmptyDiff", "LocalDiffError",
    "LocalResolved",
    "materialize_local_diff_run",
    "resolve_local_diff", "setup_local_worktrees",
]
