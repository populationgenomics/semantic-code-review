"""Reference lookup: what a text search gets wrong.

73% of the model's observed tool calls were reference questions ("is
this still used", "confirm it is gone"), and it answered all of them
with grep. These are the cases where that is the wrong instrument.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from semantic_code_review.augment.tools import RepoTools, mcp_dispatch
from semantic_code_review.structural import references as refs


def test_an_import_is_used_through_attribute_access() -> None:
    """`np.array(x)` uses numpy, but the text `numpy` appears only in
    the import. The tree-sitter tags query captures this as `array` and
    loses the root, which is why Python needs `ast`."""
    src = "import numpy as np\nimport io\n\ndef f(x):\n    return np.array(x)\n"

    assert [r.line for r in refs.references(src, "m.py", "np")] == [5]
    assert refs.references(src, "m.py", "io") == []
    assert refs.python_bindings(src) == {"np": "numpy", "io": "io"}


def test_comments_and_strings_are_not_uses() -> None:
    """grep's central failure on this question."""
    src = '# helper is deprecated\nMSG = "call helper()"\n\ndef g():\n    return 1\n'

    assert refs.references(src, "m.py", "helper") == []


def test_a_substring_of_a_longer_name_is_not_a_use() -> None:
    src = "def f():\n    return helper_two()\n"

    assert refs.references(src, "m.py", "helper") == []
    assert len(refs.references(src, "m.py", "helper_two")) == 1


def test_the_definition_itself_is_not_a_use() -> None:
    """Otherwise "is it still referenced" always answers yes."""
    src = "def helper():\n    return 1\n"

    assert refs.references(src, "m.py", "helper") == []


def test_unparseable_source_raises_rather_than_returning_nothing() -> None:
    """Both sides come from git, so this should not happen — and an
    empty result would read as 'no uses', which is a wrong answer
    rather than a missing one."""
    with pytest.raises(refs.ParseFailed):
        refs.references("def broken(:\n", "m.py", "broken")


def test_typescript_falls_back_to_the_tags_query() -> None:
    src = "class Thing {}\nconst t = new Thing();\n"

    assert any(r.line == 2 for r in refs.references(src, "m.ts", "Thing"))


# --- the tool ---------------------------------------------------------------


def _sh(cwd: Path, *args: str) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> RepoTools:
    root = tmp_path / "wt"
    root.mkdir()
    (root / "a.py").write_text("import numpy as np\n\ndef go(x):\n    return np.array(x)\n")
    (root / "b.py").write_text("# np is not used here, just mentioned\nX = 1\n")
    _sh(root, "git", "init", "-q", "-b", "main")
    _sh(root, "git", "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    _sh(root, "git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "i")
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    return RepoTools(head_worktree=root, repo_git=root, base_sha=sha, head_sha=sha)


def test_the_tool_counts_real_uses_and_skips_the_comment(repo: RepoTools) -> None:
    out = repo.references("np")

    assert "1 use site(s)" in out
    assert "a.py:4" in out
    assert "b.py" not in out  # mentioned in a comment only


def test_the_tool_reports_absence_plainly(repo: RepoTools) -> None:
    """For a removed symbol, zero is the answer, not a failure."""
    assert repo.references("nonexistent_symbol").startswith("0 use site(s)")


def test_an_unparseable_file_degrades_to_text_and_says_so(repo: RepoTools) -> None:
    (repo.head_worktree / "broken.py").write_text("def oops(:\n    np\n")

    out = repo.references("np")

    assert "did not parse" in out
    assert "broken.py" in out


def test_references_is_on_both_tool_surfaces(repo: RepoTools) -> None:
    from semantic_code_review.augment.tools import mcp_tool_schemas

    assert "references" in {s["name"] for s in mcp_tool_schemas()}
    assert "use site(s)" in mcp_dispatch(repo, "references", {"purpose": "check np", "name": "np"})


def test_a_qualified_call_is_a_use_of_the_attribute() -> None:
    """`docs/style/python.md` mandates `import module` + `module.symbol`,
    so crediting only the root made most cross-module references
    invisible — while the prompt tells the model a zero here is reliable.
    """
    src = "import anchors\nfrom pkg import mod\n\ndef f():\n    return anchors.postable_ranges(1) + mod.helper()\n"

    assert len(refs.references(src, "x.py", "postable_ranges")) == 1
    assert len(refs.references(src, "x.py", "helper")) == 1
    assert len(refs.references(src, "x.py", "anchors")) == 1


def test_an_import_alias_resolves_to_the_module_it_binds() -> None:
    """The example both hunk prompts cite: `np.array(x)` uses `numpy`
    even though the word `numpy` appears only in the import."""
    src = "import numpy as np\n\ndef f(x):\n    return np.array(x)\n"

    assert len(refs.references(src, "x.py", "numpy")) == 1
    assert len(refs.references(src, "x.py", "np")) == 1


def test_a_qualified_call_on_an_unrelated_module_is_not_a_use() -> None:
    src = "import other\n\ndef f():\n    return other.something()\n"

    assert refs.references(src, "x.py", "helper") == []
    assert refs.references(src, "x.py", "numpy") == []


def test_the_total_counts_every_matching_file(tmp_path: Path) -> None:
    """Narrowing used a line-oriented grep whose output is capped at
    20 KB, so on a common identifier most matching files were never
    parsed — while the header still read as a definite total."""
    root = tmp_path / "wt"
    root.mkdir()
    for i in range(60):
        (root / f"m{i:02d}.py").write_text("def go():\n" + "    helper()\n" * 12)
    _sh(root, "git", "init", "-q", "-b", "main")
    _sh(root, "git", "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    _sh(root, "git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "i")
    rt = RepoTools(head_worktree=root, repo_git=root, base_sha="HEAD", head_sha="HEAD")

    assert rt.references("helper").startswith("720 use site(s)")


def test_a_truncated_candidate_set_is_reported_as_a_lower_bound(tmp_path: Path) -> None:
    """A cap is fine; a cap presented as an exact count is not."""
    root = tmp_path / "wt"
    root.mkdir()
    for i in range(12):
        (root / f"m{i:02d}.py").write_text("def go():\n    helper()\n")
    _sh(root, "git", "init", "-q", "-b", "main")
    _sh(root, "git", "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    _sh(root, "git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "i")
    rt = RepoTools(head_worktree=root, repo_git=root, base_sha="HEAD", head_sha="HEAD")
    rt.REFERENCE_FILE_CAP = 4

    out = rt.references("helper")

    assert out.startswith("4+ use site(s)")
    assert "lower bound" in out
