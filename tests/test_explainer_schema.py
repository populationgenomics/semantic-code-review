"""Change-explainer document: persistence, invalidation, reference validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from semantic_code_review.augment import explainer_schema
from semantic_code_review.augment.schemas import lift_diff
from semantic_code_review.format.parse import parse_raw_diff
from semantic_code_review.viewer import build_json


def _doc(**overrides) -> explainer_schema.ExplainerDocument:
    payload = {
        "base_sha": "aaaa1111",
        "head_sha": "bbbb2222",
        "verdict": "narrate",
        "sections": [
            explainer_schema.Section(
                id="map",
                kind="map",
                title="Map",
                state="ready",
                map_rows=[
                    explainer_schema.MapRow(
                        ref=explainer_schema.Reference(kind="file", id="F0"),
                        why="the contract every other file is derived from",
                    )
                ],
            )
        ],
    }
    payload.update(overrides)
    return explainer_schema.ExplainerDocument(**payload)


def test_save_then_load_round_trips(tmp_path: Path) -> None:
    explainer_schema.save_explainer(tmp_path, _doc())
    assert explainer_schema.explainer_path(tmp_path) == tmp_path / "explainer.json"
    loaded = explainer_schema.load_explainer(tmp_path, base_sha="aaaa1111", head_sha="bbbb2222")
    assert loaded is not None
    assert loaded.sections[0].map_rows[0].ref.id == "F0"


def test_load_absent_document_is_none(tmp_path: Path) -> None:
    assert explainer_schema.load_explainer(tmp_path, base_sha="a", head_sha="b") is None


@pytest.mark.parametrize(
    ("base_sha", "head_sha"),
    [("aaaa1111", "cccc3333"), ("dddd4444", "bbbb2222"), ("dddd4444", "cccc3333")],
)
def test_load_discards_a_document_from_other_shas(tmp_path: Path, base_sha: str, head_sha: str) -> None:
    """A moving diff invalidates the document wholesale — no migration.

    Re-anchoring prose that describes vanished code yields a correct
    pointer to a wrong sentence (ADR 0007).
    """
    explainer_schema.save_explainer(tmp_path, _doc())
    assert explainer_schema.load_explainer(tmp_path, base_sha=base_sha, head_sha=head_sha) is None


def test_load_discards_an_older_document_version(tmp_path: Path) -> None:
    path = tmp_path / "explainer.json"
    payload = _doc().model_dump()
    payload["version"] = explainer_schema.DOCUMENT_VERSION - 1
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert explainer_schema.load_explainer(tmp_path, base_sha="aaaa1111", head_sha="bbbb2222") is None


@pytest.mark.parametrize("body", ["{not json", '{"version": 1}'])
def test_load_raises_on_a_corrupt_document(tmp_path: Path, body: str) -> None:
    """Corruption is loud; a stale document is not. The two are different
    outcomes and the caller has to be able to tell them apart."""
    (tmp_path / "explainer.json").write_text(body, encoding="utf-8")
    with pytest.raises(explainer_schema.ExplainerCorrupt):
        explainer_schema.load_explainer(tmp_path, base_sha="aaaa1111", head_sha="bbbb2222")


def test_save_is_atomic_and_leaves_no_temp_file(tmp_path: Path) -> None:
    explainer_schema.save_explainer(tmp_path, _doc())
    assert sorted(p.name for p in tmp_path.iterdir()) == ["explainer.json"]


# --- Reference validation ------------------------------------------------

_FILE_IDS = frozenset({"F0", "F1"})
_HUNK_IDS = frozenset({"H0_0", "H0_1", "H1_0"})


def test_valid_references_survive_in_order() -> None:
    refs = [
        explainer_schema.Reference(kind="hunk", id="H1_0"),
        explainer_schema.Reference(kind="file", id="F0"),
    ]
    kept, dropped = explainer_schema.validate_references(refs, file_ids=_FILE_IDS, hunk_ids=_HUNK_IDS)
    assert [r.id for r in kept] == ["H1_0", "F0"]
    assert dropped == 0


@pytest.mark.parametrize(
    ("kind", "ref_id"),
    [
        ("file", "F9"),  # index past the file list
        ("hunk", "H0_7"),  # index past the file's hunk list
        ("hunk", "H4_0"),  # file index that isn't in the diff
        ("file", "H0_0"),  # right id, wrong id space
        ("hunk", "F0"),  # ditto, the other way
        ("file", "src/x.py"),  # a path, not a viewer id
    ],
)
def test_invalid_references_are_dropped_and_counted(kind: str, ref_id: str) -> None:
    refs = [
        explainer_schema.Reference(kind="file", id="F1"),
        explainer_schema.Reference(kind=kind, id=ref_id),  # type: ignore[arg-type]
    ]
    kept, dropped = explainer_schema.validate_references(refs, file_ids=_FILE_IDS, hunk_ids=_HUNK_IDS)
    assert [r.id for r in kept] == ["F1"]
    assert dropped == 1


def test_id_index_enumerates_what_the_viewer_renders() -> None:
    """The index the explainer validates against is the id space
    build_json mints, so a valid reference is always clickable."""
    raw = (
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-x\n"
        "+y\n"
        "@@ -10,1 +10,1 @@\n"
        "-p\n"
        "+q\n"
        "diff --git a/b.py b/b.py\n"
        "--- a/b.py\n"
        "+++ b/b.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-m\n"
        "+n\n"
    )
    diff = lift_diff(parse_raw_diff(raw))
    index = build_json.viewer_id_index(diff)
    assert index.files == frozenset({"F0", "F1"})
    assert index.hunks == frozenset({"H0_0", "H0_1", "H1_0"})

    data = build_json.build_viewer_json(diff, {})
    assert frozenset(f["id"] for f in data["files"]) == index.files
    assert frozenset(h["id"] for f in data["files"] for h in f["hunks"]) == index.hunks
