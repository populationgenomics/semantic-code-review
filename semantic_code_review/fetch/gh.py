"""Wrappers around the `gh` CLI for PR metadata and diff retrieval."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass


_PR_URL_RE = re.compile(r"https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)/?")
_GH_VERSION_RE = re.compile(r"gh version (\d+)\.(\d+)\.(\d+)")
_MIN_GH_VERSION_TUPLE = (2, 21, 0)


@dataclass(frozen=True)
class PRRef:
    owner: str
    repo: str
    number: int

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}/pull/{self.number}"

    @property
    def clone_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}.git"


def parse_pr_url(url: str) -> PRRef:
    m = _PR_URL_RE.match(url.strip())
    if not m:
        raise ValueError(f"not a GitHub PR URL: {url!r}")
    return PRRef(owner=m.group(1), repo=m.group(2), number=int(m.group(3)))


_PR_FIELDS = [
    "title",
    "body",
    "author",
    "baseRefName",
    "baseRefOid",
    "headRefName",
    "headRefOid",
    "labels",
    "url",
    "additions",
    "deletions",
    "changedFiles",
    "files",
    "number",
]


class GhFetchError(RuntimeError):
    """Any non-zero exit from a `gh` subprocess used during PR fetch.

    Also covers preflight failures (missing gh, too-old gh) so a
    single CLI catch handler covers all gh-related environment errors.
    """


# baseRefOid and headRefOid landed in gh 2.21 (Jan 2023). Older
# releases reject them at flag-parse time with "Unknown JSON field".
_MIN_GH_VERSION = "2.21"


def preflight_gh() -> str:
    """Verify gh is installed and recent enough; return the binary path.

    Run once at the top of any command that calls a gh subprocess so
    a missing-gh / too-old-gh diagnosis surfaces before we spend time
    fetching, parsing, or contacting any backend. Raises GhFetchError
    on either failure mode; the message is actionable on its own.
    """
    path = shutil.which("gh")
    if not path:
        raise GhFetchError(
            "`gh` (GitHub CLI) not found on PATH. Install it from "
            "https://cli.github.com/ or via your package manager."
        )
    result = subprocess.run(
        [path, "--version"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        # gh is on PATH but `gh --version` fails — surface the actual
        # error rather than swallow it; something's seriously wrong.
        msg = (result.stderr or result.stdout or "").strip()
        raise GhFetchError(f"`gh --version` failed: {msg}")
    m = _GH_VERSION_RE.search(result.stdout)
    if not m:
        # Unknown output format — don't block. If a real call later
        # fails on Unknown JSON field, the runtime translation will
        # still produce a clear error.
        return path
    ver = tuple(int(x) for x in m.groups())
    if ver < _MIN_GH_VERSION_TUPLE:
        version_str = ".".join(str(x) for x in ver)
        raise GhFetchError(
            f"gh {version_str} is too old for scr (need >= "
            f"{_MIN_GH_VERSION}, released Jan 2023). Upgrade gh — "
            f"`brew upgrade gh` on macOS, or see "
            f"https://cli.github.com/ for other platforms."
        )
    return path


def fetch_pr_meta(ref: PRRef) -> dict:
    result = subprocess.run(
        ["gh", "pr", "view", str(ref.number), "--repo", ref.slug, "--json", ",".join(_PR_FIELDS)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "Unknown JSON field" in stderr:
            raise GhFetchError(
                f"gh is too old to expose baseRefOid/headRefOid (need >= "
                f"{_MIN_GH_VERSION}, released Jan 2023). Upgrade gh — "
                f"`brew upgrade gh` on macOS, or see "
                f"https://cli.github.com/ for other platforms."
            )
        raise GhFetchError(f"gh pr view failed: {stderr}")
    return json.loads(result.stdout)


def fetch_pr_diff(ref: PRRef) -> str:
    result = subprocess.run(
        ["gh", "pr", "diff", str(ref.number), "--repo", ref.slug],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise GhFetchError(f"gh pr diff failed: {result.stderr.strip()}")
    return result.stdout


def fetch_pr_files(ref: PRRef) -> list[str]:
    """Return the list of changed file paths (post-image)."""
    # gh pr view ... --json files returns {path, additions, deletions}
    result = subprocess.run(
        ["gh", "pr", "view", str(ref.number), "--repo", ref.slug, "--json", "files"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise GhFetchError(f"gh pr view --json files failed: {result.stderr.strip()}")
    data = json.loads(result.stdout)
    return [f["path"] for f in data.get("files", [])]
