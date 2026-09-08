"""`identity.this_build()`: the digest that tells two installs of one
version apart."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import semantic_code_review
from semantic_code_review.review import identity
from semantic_code_review.viewer import static


def test_this_build_names_this_install() -> None:
    this = identity.this_build()
    assert re.fullmatch(r"\d+\.\d+\.\d+.*", this.version)
    assert re.fullmatch(r"[0-9a-f]{12}", this.build)
    assert this.package == str(Path(semantic_code_review.__file__).parent.resolve())
    assert identity.this_build() == this


def test_a_rebuilt_bundle_is_another_build(tmp_path: Path, monkeypatch) -> None:
    """Same checkout, same interpreter, new `viewer.js`: a server started
    before the rebuild serves the old page, so it must read as another
    build."""
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    bundle = build_dir / static.BUNDLE
    bundle.write_bytes(b"// v1\n")
    monkeypatch.setenv("SCR_VIEWER_BUILD_DIR", str(build_dir))
    before = identity.this_build()

    bundle.write_bytes(b"// v2, longer\n")
    after = identity.this_build()

    assert before.version == after.version
    assert before.build != after.build


def test_another_interpreter_is_another_build(monkeypatch) -> None:
    here = identity.this_build()
    monkeypatch.setattr(sys, "executable", "/elsewhere/.venv/bin/python")
    assert identity.this_build().build != here.build


def test_a_missing_bundle_is_a_build_too(tmp_path: Path, monkeypatch) -> None:
    """A checkout that has not run `npm run build` still has an identity;
    building the bundle changes it."""
    monkeypatch.setenv("SCR_VIEWER_BUILD_DIR", str(tmp_path))
    monkeypatch.setattr(static, "ASSETS_DIR", tmp_path / "assets")
    without = identity.this_build()
    (tmp_path / static.BUNDLE).write_bytes(b"// built\n")
    assert identity.this_build().build != without.build
