"""Wrappers around the `gh` CLI for PR metadata and diff retrieval."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass


_PR_URL_RE = re.compile(r"https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)/?")


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
    """Any non-zero exit from a `gh` subprocess used during PR fetch."""


# baseRefOid and headRefOid landed in gh 2.21 (Jan 2023). Older
# releases reject them at flag-parse time with "Unknown JSON field".
_MIN_GH_VERSION = "2.21"


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
