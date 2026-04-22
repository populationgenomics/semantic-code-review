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
]


def fetch_pr_meta(ref: PRRef) -> dict:
    result = subprocess.run(
        ["gh", "pr", "view", str(ref.number), "--repo", ref.slug, "--json", ",".join(_PR_FIELDS)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh pr view failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def fetch_pr_diff(ref: PRRef) -> str:
    result = subprocess.run(
        ["gh", "pr", "diff", str(ref.number), "--repo", ref.slug],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh pr diff failed: {result.stderr.strip()}")
    return result.stdout


def fetch_pr_files(ref: PRRef) -> list[str]:
    """Return the list of changed file paths (post-image)."""
    # gh pr view ... --json files returns {path, additions, deletions}
    result = subprocess.run(
        ["gh", "pr", "view", str(ref.number), "--repo", ref.slug, "--json", "files"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh pr view --json files failed: {result.stderr.strip()}")
    data = json.loads(result.stdout)
    return [f["path"] for f in data.get("files", [])]
