"""`paths` helpers — private (0600/0700) config + secret writes."""

from __future__ import annotations

import stat
from pathlib import Path

from semantic_code_review import paths


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def test_write_private_file_creates_0600(tmp_path: Path) -> None:
    f = tmp_path / "sub" / "secret.env"
    paths.write_private_file(f, "KEY=value\n")
    assert f.read_text() == "KEY=value\n"
    assert _mode(f) == 0o600


def test_write_private_file_tightens_existing(tmp_path: Path) -> None:
    """A pre-existing world-readable file is tightened, not left open."""
    f = tmp_path / "config.toml"
    f.write_text("old\n")
    f.chmod(0o644)
    paths.write_private_file(f, "new\n")
    assert f.read_text() == "new\n"
    assert _mode(f) == 0o600


def test_ensure_private_dir_is_0700(tmp_path: Path) -> None:
    d = paths.ensure_private_dir(tmp_path / "scr")
    assert d.is_dir()
    assert _mode(d) == 0o700
