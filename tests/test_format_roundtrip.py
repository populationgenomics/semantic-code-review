"""Round-trip: canonical fixture parses and re-emits byte-identically."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from semantic_code_review.format.emit import emit_augmented_diff
from semantic_code_review.format.lint import lint_text
from semantic_code_review.format.parse import parse_augmented_diff
from semantic_code_review.format.sidecar import dump_sidecar, load_sidecar
from semantic_code_review.format.strip import strip_annotations

FIXTURE = Path(__file__).parent / "fixtures" / "sample.augmented.diff"


def test_fixture_round_trips() -> None:
    text = FIXTURE.read_text(encoding="utf-8")
    diff = parse_augmented_diff(text)
    emitted = emit_augmented_diff(diff)
    assert emitted == text, "canonical fixture is not idempotent under parse/emit"


def test_fixture_lint_passes() -> None:
    text = FIXTURE.read_text(encoding="utf-8")
    result = lint_text(text)
    assert result.ok, result.errors


def test_preamble_tolerates_retired_symbol_inventories() -> None:
    """A run directory written before the LLM symbol echo was retired
    still carries `symbols_added` / `symbols_modified` / `symbols_removed`
    in `scr-overview`. `_build_overview` reads named keys, so the retired
    ones are skipped rather than rejected; re-emitting drops them."""
    from semantic_code_review.augment.schemas import Overview

    text = FIXTURE.read_text(encoding="utf-8")
    stale = text.replace(
        '#scr>   "summary": "Introduces pagination on /users; callers updated.",',
        '#scr>   "summary": "Introduces pagination on /users; callers updated.",\n'
        '#scr>   "symbols_added": [{"path": "src/users.py", "kind": "function", "name": "paginate"}],\n'
        '#scr>   "symbols_modified": [],\n'
        '#scr>   "symbols_removed": [],',
    )
    diff = parse_augmented_diff(stale)
    assert isinstance(diff.overview, Overview)
    assert diff.overview.summary.startswith("Introduces pagination")
    assert emit_augmented_diff(diff) == text


def test_fixture_has_expected_structure() -> None:
    from semantic_code_review.augment.schemas import Overview

    diff = parse_augmented_diff(FIXTURE.read_text(encoding="utf-8"))
    assert diff.pr.base_sha == "7c3a2b1"
    assert isinstance(diff.overview, Overview)
    assert diff.overview.summary.startswith("Introduces pagination")
    assert len(diff.files) == 1

    f = diff.files[0]
    assert f.path == "src/users.py"
    assert f.ann.lang == "python"
    assert len(f.hunks) == 1

    h = f.hunks[0]
    assert h.parsed.old_start == 1 and h.parsed.old_count == 2
    assert h.parsed.new_start == 1 and h.parsed.new_count == 7
    assert h.ann.confidence == 85
    # Two multi-line spans and one single-line callout nested in the second.
    assert [(s.start, s.end) for s in h.ann.spans] == [(1, 3), (5, 7), (5, 5)]
    assert h.ann.spans[0].smells[0].tag == "string-sql"
    assert h.ann.spans[2].intent.startswith("size=50")
    assert len(h.ann.refs) == 2


def test_strip_produces_clean_patch(tmp_path: Path) -> None:
    """The stripped augmented diff must apply cleanly against the base image."""
    worktree = tmp_path / "worktree"
    (worktree / "src").mkdir(parents=True)
    (worktree / "src" / "users.py").write_text(
        'def list_users(db):\n    return db.query("SELECT * FROM users")\n',
        encoding="utf-8",
    )

    stripped = strip_annotations(FIXTURE.read_text(encoding="utf-8"))
    patch_file = tmp_path / "stripped.diff"
    patch_file.write_text(stripped, encoding="utf-8")

    result = subprocess.run(
        ["patch", "-p1", "--dry-run", "-i", str(patch_file)],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"patch rejected: {result.stdout}\n{result.stderr}"


def test_sidecar_round_trip(tmp_path: Path) -> None:
    text = FIXTURE.read_text(encoding="utf-8")
    diff = parse_augmented_diff(text)
    path = tmp_path / "sidecar.scr.json"
    dump_sidecar(diff, path)
    reloaded = load_sidecar(path)
    assert reloaded.model_dump() == diff.model_dump()


def test_older_sidecar_with_segments_and_line_notes_loads_as_spans(tmp_path: Path) -> None:
    """A sidecar written before slice 4 carries `segments` and `line_notes`
    on each hunk. It loads with them as spans, matches the text form's
    parse of the same run, and dumps in the new shape."""
    diff = parse_augmented_diff(FIXTURE.read_text(encoding="utf-8"))
    data = json.loads(diff.model_dump_json())
    ann = data["files"][0]["hunks"][0]["ann"]
    ann.pop("spans")
    ann["segments"] = [
        {
            "new_start": 1,
            "new_count": 3,
            "intent": "New paginate() helper that builds the LIMIT/OFFSET suffix.",
            "smells": [{"tag": "string-sql", "note": "SQL is still interpolated, not parameterized."}],
            "context": "",
            "refs": [],
        },
        {
            "new_start": 5,
            "new_count": 3,
            "intent": "list_users gains keyword args with safe defaults; existing callers continue to work.",
            "smells": [],
            "context": "",
            "refs": [],
        },
    ]
    ann["line_notes"] = [{"line": 5, "body": "size=50 is an implicit API contract; worth documenting."}]
    path = tmp_path / "old.scr.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    loaded = load_sidecar(path)
    assert loaded.model_dump() == diff.model_dump()
    assert "segments" not in loaded.model_dump_json()


def test_lint_reports_sidecar_mismatch(tmp_path: Path) -> None:
    text = FIXTURE.read_text(encoding="utf-8")
    diff = parse_augmented_diff(text)
    path = tmp_path / "sidecar.scr.json"
    # Corrupt the sidecar by changing an unrelated field.
    data = json.loads(diff.model_dump_json())
    data["pr"]["base_sha"] = "DEADBEEF"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    result = lint_text(text, sidecar_path=path)
    assert not result.ok
    assert any("sidecar" in e for e in result.errors)


def test_handwritten_annotated_diff_round_trips() -> None:
    """Construct an AnnotatedDiff in code, emit it, parse it back, and
    assert the model_dump matches. Locks in `parse(emit(x)) == x` for
    the typed form, complementing the canonical-fixture round-trip."""
    from semantic_code_review.augment.schemas import (
        AnnotatedDiff,
        AnnotatedFile,
        AnnotatedHunk,
        AnnotationSpan,
        FileAnnotations,
        FileRole,
        FileSymbols,
        HunkAnnotations,
        Overview,
        ParsedHunk,
        PRInfo,
        Ref,
        Smell,
    )

    diff = AnnotatedDiff(
        pr=PRInfo(
            pr_url="https://example.test/pr/1",
            base_sha="b" * 7,
            head_sha="h" * 7,
            model="claude-x",
        ),
        overview=Overview(
            summary="Round-trip fixture.",
            themes=["round-trip"],
        ),
        files=[
            AnnotatedFile(
                path="m.py",
                diff_git_line="diff --git a/m.py b/m.py",
                old_file_marker="--- a/m.py",
                new_file_marker="+++ b/m.py",
                ann=FileAnnotations(
                    role=FileRole.MODIFIED,
                    summary="Adds f().",
                    lang="python",
                    symbols=FileSymbols(added=["f"]),
                ),
                hunks=[
                    AnnotatedHunk(
                        parsed=ParsedHunk(
                            header="@@ -1,1 +1,3 @@",
                            old_start=1,
                            old_count=1,
                            new_start=1,
                            new_count=3,
                            body="-pass\n+def f():\n+    return 1\n+\n",
                        ),
                        ann=HunkAnnotations(
                            intent="Introduce f.",
                            confidence=80,
                            smells=[Smell(tag="missing-test", note="no test yet")],
                            context="No callers yet.",
                            refs=[Ref(path="m.py", line=2, reason="defines f")],
                            spans=[
                                AnnotationSpan(start=1, end=2, intent="def + body"),
                                AnnotationSpan(start=2, end=2, intent="entry point"),
                            ],
                        ),
                    ),
                ],
            ),
        ],
    )
    text = emit_augmented_diff(diff)
    reparsed = parse_augmented_diff(text)
    assert reparsed.model_dump() == diff.model_dump()
    # And the text round-trips byte-for-byte too.
    assert emit_augmented_diff(reparsed) == text


def test_lint_rejects_unknown_smell_tag() -> None:
    text = FIXTURE.read_text(encoding="utf-8").replace("string-sql", "made-up-smell")
    result = lint_text(text)
    # Parse still succeeds (tags are free strings), but lint rejects.
    assert not result.ok
    assert any("made-up-smell" in e for e in result.errors)
