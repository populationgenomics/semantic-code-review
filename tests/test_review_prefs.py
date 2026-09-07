"""The viewer preference store: the file, its merge, and what it refuses.

The routes in front of it are covered in tests/test_review_server.py.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib

import pytest

from semantic_code_review import paths
from semantic_code_review.review import prefs


@pytest.fixture
def store(tmp_path: pathlib.Path) -> prefs.PrefsStore:
    return prefs.PrefsStore(tmp_path / "config" / "scr" / "viewer-prefs.json")


def test_absent_file_reads_empty(store: prefs.PrefsStore) -> None:
    assert store.read() == {}
    assert not store.path.parent.exists()


def test_update_round_trips_through_the_file(store: prefs.PrefsStore) -> None:
    merged = store.update({"scr-gutter-fold": "collapsed", "scr-sidebar-width": 300, "dark": True, "zoom": 1.5})
    assert merged == {"scr-gutter-fold": "collapsed", "scr-sidebar-width": 300, "dark": True, "zoom": 1.5}
    assert prefs.PrefsStore(store.path).read() == merged
    assert json.loads(store.path.read_text(encoding="utf-8")) == merged


def test_update_merges_and_the_last_value_wins(store: prefs.PrefsStore) -> None:
    store.update({"a": 1, "b": "x"})
    assert store.update({"b": "y", "c": False}) == {"a": 1, "b": "y", "c": False}


def test_update_with_null_removes_the_key(store: prefs.PrefsStore) -> None:
    store.update({"a": 1, "b": 2})
    assert store.update({"a": None, "nonesuch": None}) == {"b": 2}


def test_write_creates_the_config_dir_owner_only(store: prefs.PrefsStore) -> None:
    store.update({"a": 1})
    assert store.path.parent.is_dir()
    assert (store.path.parent.stat().st_mode & 0o777) == 0o700


def test_write_is_atomic_and_leaves_no_temp_file(store: prefs.PrefsStore, monkeypatch: pytest.MonkeyPatch) -> None:
    store.update({"a": 1})
    before = store.path.read_text(encoding="utf-8")
    real_replace = os.replace
    seen: list[str] = []

    def replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        # The temp file is complete before it takes the target's place.
        seen.append(pathlib.Path(src).read_text(encoding="utf-8"))
        real_replace(src, dst)

    monkeypatch.setattr(prefs.os, "replace", replace)
    store.update({"b": 2})
    assert seen == [json.dumps({"a": 1, "b": 2}, indent=2, sort_keys=True) + "\n"]
    assert seen[0] != before
    assert [p.name for p in store.path.parent.iterdir()] == ["viewer-prefs.json"]


def test_failed_write_removes_its_temp_file(store: prefs.PrefsStore, monkeypatch: pytest.MonkeyPatch) -> None:
    def replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(prefs.os, "replace", replace)
    with pytest.raises(OSError, match="disk full"):
        store.update({"a": 1})
    assert list(store.path.parent.iterdir()) == []


@pytest.mark.parametrize(
    "text",
    [
        "{not json",
        "[1, 2]",
        '{"a": [1]}',
        '{"a": {"b": 1}}',
        '{"a": null}',
    ],
)
def test_malformed_file_logs_the_path_and_reads_empty(
    store: prefs.PrefsStore, text: str, caplog: pytest.LogCaptureFixture
) -> None:
    store.path.parent.mkdir(parents=True)
    store.path.write_text(text, encoding="utf-8")
    with caplog.at_level(logging.ERROR, logger="semantic_code_review.review.prefs"):
        assert store.read() == {}
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.ERROR
    assert str(store.path) in caplog.records[0].getMessage()


def test_next_write_replaces_a_malformed_file(store: prefs.PrefsStore, caplog: pytest.LogCaptureFixture) -> None:
    store.path.parent.mkdir(parents=True)
    store.path.write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.ERROR, logger="semantic_code_review.review.prefs"):
        assert store.update({"a": 1}) == {"a": 1}
    assert store.read() == {"a": 1}


@pytest.mark.parametrize(
    ("patch", "match"),
    [
        (["a", 1], "must be a JSON object"),
        ("a", "must be a JSON object"),
        ({"": 1}, "non-empty string"),
        ({"a": [1]}, "value for 'a'"),
        ({"a": {"b": 1}}, "value for 'a'"),
    ],
)
def test_update_rejects_non_flat_scalars_with_400(store: prefs.PrefsStore, patch: object, match: str) -> None:
    with pytest.raises(prefs.PrefsError, match=match) as info:
        store.update(patch)
    assert info.value.status == 400
    assert not store.path.exists()


def test_default_path_is_beside_config_toml(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "conf"))
    assert paths.default_viewer_prefs_path() == tmp_path / "conf" / "scr" / "viewer-prefs.json"
    assert paths.default_viewer_prefs_path().parent == paths.default_config_path().parent
