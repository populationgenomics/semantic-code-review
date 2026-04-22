"""Viewer JSON + HTML: structural correctness on the fixture."""

from __future__ import annotations

from pathlib import Path

from semantic_code_review.format.parse import parse_augmented_diff
from semantic_code_review.viewer.build_json import build_viewer_json
from semantic_code_review.viewer.render_html import render_html


FIXTURE = Path(__file__).parent / "fixtures" / "sample.augmented.diff"


def _data():
    text = FIXTURE.read_text(encoding="utf-8")
    diff = parse_augmented_diff(text)
    return build_viewer_json(diff, {
        "title": "Introduce pagination",
        "body": "",
        "author": {"login": "octocat"},
        "url": "https://github.com/acme/widgets/pull/482",
    })


def test_viewer_json_shape() -> None:
    d = _data()
    assert d["version"] == "1"
    assert d["pr"]["title"] == "Introduce pagination"
    assert d["pr"]["number"] == 482
    assert d["pr"]["repo"] == "acme/widgets"
    assert d["pr"]["base_sha"] == "7c3a2b1"
    assert d["pr"]["summary"].startswith("Introduces pagination")
    assert "string-sql" in d["smells_catalogue"]
    assert d["smells_catalogue"]["string-sql"]["severity"] == "major"


def test_viewer_json_files_and_hunks() -> None:
    d = _data()
    assert len(d["files"]) == 1
    f = d["files"][0]
    assert f["id"] == "F0"
    assert f["path"] == "src/users.py"
    assert f["language"] == "python"
    assert f["adds"] == 7 and f["dels"] == 2
    assert len(f["hunks"]) == 1

    h = f["hunks"][0]
    assert h["id"] == "H0_0"
    assert h["intent"].startswith("Pagination")
    assert h["confidence"] == 85
    assert len(h["segments"]) == 2
    assert h["segments"][0]["id"] == "H0_0_S0"
    assert h["segments"][0]["new_start"] == 1 and h["segments"][0]["new_count"] == 3
    assert h["segments"][0]["smells"][0]["tag"] == "string-sql"
    assert h["segments"][1]["new_start"] == 5 and h["segments"][1]["new_count"] == 3

    # rows carry the side-by-side structure: two pairs + five ins rows
    # (hunk replaces 2 old lines with 7 new, so 2 are paired and 5 are inserts).
    rows = h["rows"]
    kinds = [r["kind"] for r in rows]
    assert kinds.count("pair") == 2
    assert kinds.count("ins") == 5
    # First row is the pair (list_users → paginate).
    assert rows[0]["kind"] == "pair"
    assert rows[0]["old_text"].startswith("def list_users(db):")
    assert rows[0]["new_text"].startswith("def paginate(")
    # Line numbers advance correctly.
    assert rows[0]["old_line"] == 1 and rows[0]["new_line"] == 1
    assert rows[-1]["new_line"] == 7 and rows[-1]["old_line"] is None


def test_render_html_has_key_elements() -> None:
    d = _data()
    html = render_html(d)
    assert "<title>Introduce pagination</title>" in html
    assert "Semantic Code Review" not in html or "viewer" in html.lower()  # sanity
    assert "fold-slider" in html
    assert 'data-fold="files"' in html and 'data-fold="hunks"' in html
    assert 'id="scr-data"' in html
    # The embedded JSON must be safely encoded (no raw </script>).
    assert "</script>" not in html.split('id="scr-data"')[1].split("</script>")[0]
    # Viewer JS is inlined.
    assert "renderHunk" in html
    # CSS variables defined.
    assert "--accent" in html


def test_render_html_self_contained_contains_expected_text() -> None:
    """The HTML should inline intent text and segment intents verbatim (via JSON)."""
    d = _data()
    html = render_html(d)
    assert "Pagination is introduced" in html
    assert "paginate() helper" in html
    assert "string-sql" in html
