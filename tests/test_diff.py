"""
tests/test_diff.py

Tests for Phase 4 — Myers diff algorithm and unified diff formatting.

Validation strategy:
  - Core algorithm: verify that applying the edit script to *a* always
    reconstructs *b* exactly.
  - Unified diff: verify structure (headers, hunk markers, prefix chars).
  - Integration: cross-validate pygit diff output against real git diff
    by comparing the hunk bodies (file paths and line content).

Run with:  pytest tests/ -v
"""

import subprocess
from pathlib import Path
import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from pygitlib.diff import (
    myers_diff,
    format_unified_diff,
    diff_unstaged,
    diff_staged,
)
from pygitlib.index import add


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def repo(tmp_path):
    """Git repo with HEAD forced to 'main'."""
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@pygit.dev"],
                   cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "PyGit Test"],
                   cwd=tmp_path, capture_output=True)
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    return tmp_path


@pytest.fixture
def committed_repo(repo):
    """Repo with one real git commit on main containing hello.py."""
    (repo / "hello.py").write_bytes(b"def hello():\n    print('hello')\n")
    subprocess.run(["git", "add", "hello.py"], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"],
                   cwd=repo, capture_output=True)
    return repo


# ---------------------------------------------------------------------------
# Helper: reconstruct b by applying an edit script to a
# ---------------------------------------------------------------------------

def _apply(edits):
    """Apply an edit script; returns the resulting sequence (the 'b' side)."""
    return [elem for op, elem in edits if op in ("=", "+")]


# ---------------------------------------------------------------------------
# TestMyersDiff — core algorithm
# ---------------------------------------------------------------------------

class TestMyersDiff:

    def test_identical_sequences(self):
        """Identical input → all '=' operations, no edits."""
        a = ["line1", "line2", "line3"]
        result = myers_diff(a, a)
        assert all(op == "=" for op, _ in result)
        assert [e for _, e in result] == a

    def test_empty_to_lines(self):
        """Empty old → all insertions."""
        b = ["a", "b", "c"]
        result = myers_diff([], b)
        assert result == [("+", "a"), ("+", "b"), ("+", "c")]

    def test_lines_to_empty(self):
        """Empty new → all deletions."""
        a = ["x", "y"]
        result = myers_diff(a, [])
        assert result == [("-", "x"), ("-", "y")]

    def test_both_empty(self):
        assert myers_diff([], []) == []

    def test_reconstructs_b(self):
        """Applying the edit script to a must always reconstruct b exactly."""
        cases = [
            (["a", "b", "c"], ["a", "x", "c"]),
            (["a", "b", "c", "d"], ["b", "d", "e"]),
            (list("abcabba"), list("cbabac")),        # Myers paper example
            (["only"], []),
            ([], ["only"]),
            (["same", "same"], ["same", "same"]),
        ]
        for a, b in cases:
            edits = myers_diff(a, b)
            assert _apply(edits) == b, f"Failed for a={a!r}, b={b!r}"

    def test_minimum_edit_count(self):
        """
        Myers computes the *minimum* number of edits.
        For the classic example from the paper:
          a = 'abcabba'   b = 'cbabac'
        the minimum is 5 edits.
        """
        a = list("abcabba")
        b = list("cbabac")
        edits = myers_diff(a, b)
        num_edits = sum(1 for op, _ in edits if op != "=")
        assert num_edits == 5

    def test_single_insertion(self):
        a = ["line1", "line2"]
        b = ["line1", "NEW", "line2"]
        edits = myers_diff(a, b)
        assert _apply(edits) == b
        assert any(op == "+" and e == "NEW" for op, e in edits)
        assert all(op != "-" for op, _ in edits)

    def test_single_deletion(self):
        a = ["line1", "GONE", "line2"]
        b = ["line1", "line2"]
        edits = myers_diff(a, b)
        assert _apply(edits) == b
        assert any(op == "-" and e == "GONE" for op, e in edits)
        assert all(op != "+" for op, _ in edits)


# ---------------------------------------------------------------------------
# TestUnifiedDiff — formatting
# ---------------------------------------------------------------------------

class TestUnifiedDiff:

    def test_no_changes_returns_empty(self):
        lines = ["a", "b", "c"]
        assert format_unified_diff(lines, lines, "a/f", "b/f") == ""

    def test_headers_present(self):
        old = ["line1", "line2"]
        new = ["line1", "changed"]
        out = format_unified_diff(old, new, "a/f.txt", "b/f.txt")
        assert out.startswith("--- a/f.txt\n+++ b/f.txt\n")

    def test_hunk_header_format(self):
        """@@ header must use 1-indexed positions and correct counts."""
        old = ["a", "b", "c"]
        new = ["a", "X", "c"]
        out = format_unified_diff(old, new, "a/f", "b/f")
        assert "@@" in out
        # Single-line change surrounded by 1 context on each side → 3+3 max
        assert "@@ -1,3 +1,3 @@" in out

    def test_prefix_characters(self):
        """Changed lines must be prefixed with '+'/'-'; context with space."""
        old = ["same", "old", "same"]
        new = ["same", "new", "same"]
        out = format_unified_diff(old, new, "a/f", "b/f")
        lines = out.splitlines()
        prefixes = {l[0] for l in lines if not l.startswith("---") and not l.startswith("+++") and not l.startswith("@@")}
        assert prefixes <= {" ", "-", "+"}
        assert any(l.startswith("-old") for l in lines)
        assert any(l.startswith("+new") for l in lines)
        assert any(l.startswith(" same") for l in lines)

    def test_new_file(self):
        """Diffing empty → lines shows @@ -0,0 +1,n @@ header."""
        new = ["alpha", "beta"]
        out = format_unified_diff([], new, "/dev/null", "b/new.txt")
        assert "@@ -0,0 +1,2 @@" in out
        assert "+alpha" in out
        assert "+beta" in out

    def test_deleted_file(self):
        """Diffing lines → empty shows @@ -1,n +0,0 @@ header."""
        old = ["alpha", "beta"]
        out = format_unified_diff(old, [], "a/old.txt", "/dev/null")
        assert "@@ -1,2 +0,0 @@" in out
        assert "-alpha" in out
        assert "-beta" in out

    def test_context_lines(self):
        """Unchanged lines near a change must appear as context (space prefix)."""
        old = ["c1", "c2", "c3", "CHANGED", "c4", "c5", "c6"]
        new = ["c1", "c2", "c3", "NEW",     "c4", "c5", "c6"]
        out = format_unified_diff(old, new, "a/f", "b/f", context=2)
        lines = out.splitlines()
        # Should see 2 context lines before and after the change
        assert any(l == " c2" for l in lines)
        assert any(l == " c3" for l in lines)
        assert any(l == " c4" for l in lines)
        assert any(l == " c5" for l in lines)
        # c1 and c6 are >2 lines from the change, so NOT in the hunk
        assert not any(l == " c1" for l in lines)
        assert not any(l == " c6" for l in lines)

    def test_multiple_hunks(self):
        """Two distant changes must produce two separate @@ sections."""
        # Make a 20-line file, change line 2 and line 18 (far apart)
        old = [f"line{i}" for i in range(1, 21)]
        new = old[:]
        new[1]  = "changed_2"
        new[17] = "changed_18"
        out = format_unified_diff(old, new, "a/f", "b/f")
        assert out.count("@@") >= 2


# ---------------------------------------------------------------------------
# TestDiffCommands — integration tests (cross-validated with real git)
# ---------------------------------------------------------------------------

class TestDiffCommands:

    def test_diff_unstaged_shows_nothing_when_clean(self, committed_repo):
        """No output when working tree matches index."""
        git_dir = committed_repo / ".git"
        results = diff_unstaged(git_dir, committed_repo)
        assert results == []

    def test_diff_unstaged_detects_modification(self, committed_repo):
        """Modifying a tracked file shows it in diff_unstaged."""
        git_dir = committed_repo / ".git"
        (committed_repo / "hello.py").write_bytes(
            b"def hello():\n    print('hello world')\n"
        )
        results = diff_unstaged(git_dir, committed_repo)
        assert len(results) == 1
        path, diff_text = results[0]
        assert path == "hello.py"
        assert "-    print('hello')" in diff_text
        assert "+    print('hello world')" in diff_text

    def test_diff_unstaged_matches_git_hunk(self, committed_repo):
        """
        The hunk content produced by pygit must match what real git diff shows.
        We strip the 'diff --git' and 'index' lines because the SHA format
        may differ slightly; the --- / +++ / @@ / +/- lines must be identical.
        """
        git_dir = committed_repo / ".git"
        (committed_repo / "hello.py").write_bytes(
            b"def hello():\n    print('hi')\n    return True\n"
        )

        # Our diff
        results = diff_unstaged(git_dir, committed_repo)
        assert results, "Expected at least one diff entry"
        _, our_text = results[0]
        our_hunks = "\n".join(
            l for l in our_text.splitlines()
            if not l.startswith("diff --git") and not l.startswith("index")
        )

        # Real git diff
        git_out = subprocess.run(
            ["git", "diff", "hello.py"],
            cwd=committed_repo, capture_output=True
        ).stdout.decode()
        git_hunks = "\n".join(
            l for l in git_out.splitlines()
            if not l.startswith("diff --git") and not l.startswith("index")
        )

        assert our_hunks == git_hunks

    def test_diff_staged_shows_nothing_when_clean(self, committed_repo):
        """No staged changes right after a commit."""
        git_dir = committed_repo / ".git"
        results = diff_staged(git_dir)
        assert results == []

    def test_diff_staged_detects_new_file(self, committed_repo):
        """Staging a new file shows it in diff_staged as a new file."""
        git_dir = committed_repo / ".git"
        (committed_repo / "new.py").write_bytes(b"x = 1\n")
        add(git_dir, committed_repo, ["new.py"])

        results = diff_staged(git_dir)
        assert any(path == "new.py" for path, _ in results)
        _, diff_text = next((p, d) for p, d in results if p == "new.py")
        assert "new file mode" in diff_text
        assert "+x = 1" in diff_text

    def test_diff_staged_detects_modification(self, committed_repo):
        """Staging a modified file shows it in diff_staged."""
        git_dir = committed_repo / ".git"
        (committed_repo / "hello.py").write_bytes(
            b"def hello():\n    print('staged change')\n"
        )
        add(git_dir, committed_repo, ["hello.py"])

        results = diff_staged(git_dir)
        assert any(path == "hello.py" for path, _ in results)
        _, diff_text = next((p, d) for p, d in results if p == "hello.py")
        assert "-    print('hello')" in diff_text
        assert "+    print('staged change')" in diff_text

    def test_diff_staged_matches_git_hunk(self, committed_repo):
        """Staged diff hunk must match real git diff --staged output."""
        git_dir = committed_repo / ".git"
        new_content = b"def hello():\n    print('hi')\n    return 42\n"
        (committed_repo / "hello.py").write_bytes(new_content)

        # Stage with real git so both indices are in sync
        subprocess.run(["git", "add", "hello.py"],
                       cwd=committed_repo, capture_output=True)

        results = diff_staged(git_dir)
        assert results, "Expected staged diff entries"
        _, our_text = next((p, d) for p, d in results if p == "hello.py")
        our_hunks = "\n".join(
            l for l in our_text.splitlines()
            if not l.startswith("diff --git") and not l.startswith("index")
        )

        git_out = subprocess.run(
            ["git", "diff", "--staged", "hello.py"],
            cwd=committed_repo, capture_output=True
        ).stdout.decode()
        git_hunks = "\n".join(
            l for l in git_out.splitlines()
            if not l.startswith("diff --git") and not l.startswith("index")
        )

        assert our_hunks == git_hunks
