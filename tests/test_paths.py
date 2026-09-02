"""`paths` helpers — private (0600/0700) writes and the `RunDir` layout."""

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


# --- RunDir ---------------------------------------------------------------


def test_every_accessor_names_a_child_of_the_directory(tmp_path: Path) -> None:
    """The whole contract in one assertion: nothing escapes the run dir."""
    rd = paths.RunDir(tmp_path)
    named = [
        rd.head,
        rd.base,
        rd.repo_git,
        rd.raw_diff,
        rd.files_txt,
        rd.meta,
        rd.spec_md,
        rd.augmented,
        rd.sidecar,
        rd.trace,
        rd.usage,
        rd.comments,
        rd.explainer,
    ]
    assert all(p.parent == tmp_path for p in named)
    # Distinct names, so no two artefacts share a file.
    assert len({p.name for p in named}) == len(named)


def test_the_on_disk_names_are_the_ones_every_run_dir_has_always_had(tmp_path: Path) -> None:
    """Pinning the layout: renaming one of these orphans existing runs."""
    rd = paths.RunDir(tmp_path)
    assert rd.head.name == "head"
    assert rd.base.name == "base"
    assert rd.repo_git.name == "repo.git"
    assert rd.raw_diff.name == "raw.diff"
    assert rd.files_txt.name == "files.txt"
    assert rd.meta.name == "meta.json"
    assert rd.spec_md.name == "spec.md"
    assert rd.augmented.name == "augmented.diff"
    assert rd.sidecar.name == "augmented.scr.json"
    assert rd.trace.name == "trace"
    assert rd.usage.name == "usage.json"
    assert rd.comments.name == "comments.json"
    assert rd.explainer.name == "explainer.json"


def test_the_slug_is_the_directorys_own_name(tmp_path: Path) -> None:
    assert paths.RunDir(tmp_path / "a-b-pr7-deadbeef").slug == "a-b-pr7-deadbeef"


def test_create_makes_the_directory_and_hands_back_the_same_handle(tmp_path: Path) -> None:
    rd = paths.RunDir(tmp_path / "runs" / "slug")
    assert rd.create() is rd
    assert rd.path.is_dir()
    # Idempotent — a re-materialise re-runs it against an existing run.
    assert rd.create().path.is_dir()


def test_accessors_name_paths_that_need_not_exist(tmp_path: Path) -> None:
    """A run is filled in over its lifetime, so every consumer still
    checks; the accessor itself never touches the filesystem."""
    rd = paths.RunDir(tmp_path / "absent")
    assert not rd.sidecar.exists()
    assert not rd.path.exists()


def test_two_handles_on_one_directory_are_equal(tmp_path: Path) -> None:
    """Frozen and value-typed, so a test can compare what a producer
    returned against the path it expected."""
    assert paths.RunDir(tmp_path) == paths.RunDir(tmp_path)
    assert paths.RunDir(tmp_path) != paths.RunDir(tmp_path / "other")
