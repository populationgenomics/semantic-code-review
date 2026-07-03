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
    assert len(h.ann.segments) == 2
    assert h.ann.segments[0].new_start == 1 and h.ann.segments[0].new_count == 3
    assert h.ann.segments[0].smells[0].tag == "string-sql"
    assert h.ann.segments[1].new_start == 5 and h.ann.segments[1].new_count == 3
    assert len(h.ann.line_notes) == 1 and h.ann.line_notes[0].line == 5
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
        FileAnnotations,
        FileRole,
        FileSymbols,
        HunkAnnotations,
        LineNote,
        Overview,
        OverviewSymbol,
        ParsedHunk,
        PRInfo,
        Ref,
        Segment,
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
            symbols_added=[OverviewSymbol(path="m.py", kind="function", name="f")],
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
                            old_start=1, old_count=1,
                            new_start=1, new_count=3,
                            body="-pass\n+def f():\n+    return 1\n+\n",
                        ),
                        ann=HunkAnnotations(
                            intent="Introduce f.",
                            confidence=80,
                            smells=[Smell(tag="missing-test", note="no test yet")],
                            context="No callers yet.",
                            refs=[Ref(path="m.py", line=2, reason="defines f")],
                            line_notes=[LineNote(line=2, body="entry point")],
                            segments=[Segment(new_start=1, new_count=2, intent="def + body")],
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
    text = FIXTURE.read_text(encoding="utf-8").replace(
        "string-sql", "made-up-smell"
    )
    result = lint_text(text)
    # Parse still succeeds (tags are free strings), but lint rejects.
    assert not result.ok
    assert any("made-up-smell" in e for e in result.errors)
