"""
tests/test_merge.py

Tests for Phase 5 — three-way merge.

Test structure
──────────────
  TestFindMergeBase  — LCA algorithm on commit graphs
  TestMergeLines     — line-level three-way merge
  TestMergeBranch    — end-to-end merge operations (cross-validated with git)

All fixtures use HEAD forced to 'main' (same trick as test_branch.py)
to keep branch names consistent regardless of the system git default.

Run with:  pytest tests/ -v
"""

import subprocess
from pathlib import Path
import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from pygitlib.merge import (
    find_merge_base,
    merge_lines,
    merge_branch,
    MergeResult,
)
from pygitlib.branch import resolve_ref, current_branch
from pygitlib.objects import read_commit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def repo(tmp_path):
    """Fresh git repo with HEAD forced to 'main'."""
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@pygit.dev"],
                   cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "PyGit Test"],
                   cwd=tmp_path, capture_output=True)
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    return tmp_path


def _git_commit(path, msg):
    """Helper: stage everything and commit with real git."""
    subprocess.run(["git", "add", "-A"], cwd=path, capture_output=True)
    subprocess.run(["git", "commit", "-m", msg], cwd=path, capture_output=True)


@pytest.fixture
def linear_repo(repo):
    """
    Linear history:  A ─ B ─ C  (all on main)
    Useful for testing merge-base in simple cases.
    """
    (repo / "a.txt").write_bytes(b"A\n")
    _git_commit(repo, "A")

    (repo / "b.txt").write_bytes(b"B\n")
    _git_commit(repo, "B")

    (repo / "c.txt").write_bytes(b"C\n")
    _git_commit(repo, "C")

    return repo


@pytest.fixture
def diverged_repo(repo):
    """
    Diverged history:
      BASE ─ M1 (main)
           ╲
             F1 (feature)

    main   adds main.txt
    feature adds feature.txt (different file, no conflict)
    """
    (repo / "base.txt").write_bytes(b"base content\n")
    _git_commit(repo, "base")

    # Branch off to feature
    subprocess.run(["git", "branch", "feature"], cwd=repo, capture_output=True)

    # Advance main
    (repo / "main.txt").write_bytes(b"main only\n")
    _git_commit(repo, "main commit")

    # Advance feature
    subprocess.run(["git", "switch", "feature"], cwd=repo, capture_output=True)
    (repo / "feature.txt").write_bytes(b"feature only\n")
    _git_commit(repo, "feature commit")

    # Return to main (clean state)
    subprocess.run(["git", "switch", "main"], cwd=repo, capture_output=True)

    return repo


@pytest.fixture
def conflict_repo(repo):
    """
    Both branches modify the same line of shared.txt → guaranteed conflict.
    """
    (repo / "shared.txt").write_bytes(b"line1\nbase line\nline3\n")
    _git_commit(repo, "base")

    subprocess.run(["git", "branch", "feature"], cwd=repo, capture_output=True)

    # main changes "base line"
    (repo / "shared.txt").write_bytes(b"line1\nours change\nline3\n")
    _git_commit(repo, "main: change shared line")

    # feature also changes "base line" to something different
    subprocess.run(["git", "switch", "feature"], cwd=repo, capture_output=True)
    (repo / "shared.txt").write_bytes(b"line1\ntheirs change\nline3\n")
    _git_commit(repo, "feature: change shared line")

    subprocess.run(["git", "switch", "main"], cwd=repo, capture_output=True)

    return repo


@pytest.fixture
def ff_repo(repo):
    """
    Fast-forward scenario: main has not advanced since feature branched.
      BASE (main, HEAD) ─ F1 ─ F2 (feature)
    Merging feature into main should fast-forward.
    """
    (repo / "base.txt").write_bytes(b"base\n")
    _git_commit(repo, "base")

    subprocess.run(["git", "branch", "feature"], cwd=repo, capture_output=True)
    subprocess.run(["git", "switch", "feature"], cwd=repo, capture_output=True)

    (repo / "added.txt").write_bytes(b"added\n")
    _git_commit(repo, "feature: add file")

    subprocess.run(["git", "switch", "main"], cwd=repo, capture_output=True)

    return repo


# ---------------------------------------------------------------------------
# TestFindMergeBase
# ---------------------------------------------------------------------------

class TestFindMergeBase:

    def test_same_commit(self, linear_repo):
        """Merge base of a commit with itself is that commit."""
        git_dir = linear_repo / ".git"
        sha = resolve_ref(git_dir, "HEAD")
        assert find_merge_base(git_dir, sha, sha) == sha

    def test_ancestor_is_base(self, linear_repo):
        """In a linear history the earlier commit is the merge base."""
        git_dir = linear_repo / ".git"
        tip = resolve_ref(git_dir, "HEAD")
        tip_commit = read_commit(git_dir, tip)
        parent = tip_commit.parents[0]
        # merge-base(parent, tip) == parent
        assert find_merge_base(git_dir, parent, tip) == parent

    def test_diverged_branches(self, diverged_repo):
        """Merge base of two diverged branches is the commit where they split."""
        git_dir = diverged_repo / ".git"
        main_sha    = resolve_ref(git_dir, "main")
        feature_sha = resolve_ref(git_dir, "feature")
        base_sha    = find_merge_base(git_dir, main_sha, feature_sha)

        # The merge base should be the 'base' commit (parent of both tips)
        assert base_sha is not None
        main_commit    = read_commit(git_dir, main_sha)
        feature_commit = read_commit(git_dir, feature_sha)
        assert base_sha == main_commit.parents[0]
        assert base_sha == feature_commit.parents[0]

    def test_matches_git_merge_base(self, diverged_repo):
        """Our merge-base must equal what real git reports."""
        git_dir = diverged_repo / ".git"
        main_sha    = resolve_ref(git_dir, "main")
        feature_sha = resolve_ref(git_dir, "feature")

        ours = find_merge_base(git_dir, main_sha, feature_sha)

        result = subprocess.run(
            ["git", "merge-base", "main", "feature"],
            cwd=diverged_repo, capture_output=True,
        )
        git_base = result.stdout.strip().decode()
        assert ours == git_base


# ---------------------------------------------------------------------------
# TestMergeLines
# ---------------------------------------------------------------------------

class TestMergeLines:

    def test_no_changes(self):
        """All three versions identical → no conflict, output = base."""
        lines = ["a", "b", "c"]
        merged, conflict = merge_lines(lines, lines, lines)
        assert merged == lines
        assert not conflict

    def test_only_ours_changed(self):
        """Only our side changed → take ours, no conflict."""
        base   = ["a", "b", "c"]
        ours   = ["a", "X", "c"]
        theirs = ["a", "b", "c"]
        merged, conflict = merge_lines(base, ours, theirs)
        assert merged == ["a", "X", "c"]
        assert not conflict

    def test_only_theirs_changed(self):
        """Only their side changed → take theirs, no conflict."""
        base   = ["a", "b", "c"]
        ours   = ["a", "b", "c"]
        theirs = ["a", "Y", "c"]
        merged, conflict = merge_lines(base, ours, theirs)
        assert merged == ["a", "Y", "c"]
        assert not conflict

    def test_both_changed_same_way(self):
        """Both sides make the same change → take it, no conflict."""
        base   = ["a", "b", "c"]
        ours   = ["a", "Z", "c"]
        theirs = ["a", "Z", "c"]
        merged, conflict = merge_lines(base, ours, theirs)
        assert merged == ["a", "Z", "c"]
        assert not conflict

    def test_conflict_same_line(self):
        """Both sides change the same line differently → conflict."""
        base   = ["a", "b", "c"]
        ours   = ["a", "X", "c"]
        theirs = ["a", "Y", "c"]
        merged, conflict = merge_lines(base, ours, theirs, "HEAD", "feat")
        assert conflict
        assert "<<<<<<< HEAD" in merged
        assert "=======" in merged
        assert ">>>>>>> feat" in merged
        assert "X" in merged
        assert "Y" in merged
        assert merged[0] == "a"
        assert merged[-1] == "c"

    def test_non_overlapping_changes(self):
        """Each side changes a different line → both applied, no conflict."""
        base   = ["a", "b", "c", "d"]
        ours   = ["a", "X", "c", "d"]   # change b→X
        theirs = ["a", "b", "c", "Y"]   # change d→Y
        merged, conflict = merge_lines(base, ours, theirs)
        assert merged == ["a", "X", "c", "Y"]
        assert not conflict

    def test_insertion_non_overlapping(self):
        """One side inserts a line, the other changes a different line."""
        base   = ["a", "b"]
        ours   = ["a", "NEW", "b"]     # insert NEW before b
        theirs = ["a", "b", "END"]     # append END
        merged, conflict = merge_lines(base, ours, theirs)
        assert not conflict
        assert "NEW" in merged
        assert "END" in merged

    def test_conflict_markers_sandwich_content(self):
        """Context lines before and after conflict must be preserved."""
        base   = ["ctx1", "conflict", "ctx2"]
        ours   = ["ctx1", "ours",     "ctx2"]
        theirs = ["ctx1", "theirs",   "ctx2"]
        merged, conflict = merge_lines(base, ours, theirs)
        assert conflict
        assert merged[0] == "ctx1"
        assert merged[-1] == "ctx2"


# ---------------------------------------------------------------------------
# TestMergeBranch
# ---------------------------------------------------------------------------

class TestMergeBranch:

    # ── already up-to-date ────────────────────────────────────────────────

    def test_already_up_to_date(self, diverged_repo):
        """Merging main into main reports already up to date."""
        git_dir = diverged_repo / ".git"
        result = merge_branch(git_dir, diverged_repo, "main")
        assert result.success
        assert "up to date" in result.message.lower()

    # ── fast-forward ──────────────────────────────────────────────────────

    def test_fast_forward_advances_ref(self, ff_repo):
        """Fast-forward moves our branch ref to the feature tip."""
        git_dir = ff_repo / ".git"
        feature_sha = resolve_ref(git_dir, "feature")
        result = merge_branch(git_dir, ff_repo, "feature")
        assert result.success
        assert result.fast_forward
        assert resolve_ref(git_dir, "main") == feature_sha

    def test_fast_forward_updates_work_tree(self, ff_repo):
        """After fast-forward, the feature's new file is present."""
        git_dir = ff_repo / ".git"
        merge_branch(git_dir, ff_repo, "feature")
        assert (ff_repo / "added.txt").exists()

    def test_fast_forward_matches_git(self, ff_repo):
        """After pygit fast-forward, real git agrees on HEAD."""
        git_dir = ff_repo / ".git"
        feature_sha = resolve_ref(git_dir, "feature")
        merge_branch(git_dir, ff_repo, "feature")

        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ff_repo, capture_output=True,
        )
        assert result.stdout.strip().decode() == feature_sha

    # ── three-way merge (clean) ───────────────────────────────────────────

    def test_clean_merge_both_files_present(self, diverged_repo):
        """After a clean merge, files from both branches exist."""
        git_dir = diverged_repo / ".git"
        result = merge_branch(git_dir, diverged_repo, "feature")
        assert result.success
        assert not result.conflicts
        assert (diverged_repo / "main.txt").exists()
        assert (diverged_repo / "feature.txt").exists()

    def test_clean_merge_creates_commit(self, diverged_repo):
        """Clean merge creates a merge commit with two parents."""
        git_dir = diverged_repo / ".git"
        our_sha    = resolve_ref(git_dir, "main")
        their_sha  = resolve_ref(git_dir, "feature")
        result = merge_branch(git_dir, diverged_repo, "feature")
        assert result.success
        assert result.commit_sha

        merge_commit = read_commit(git_dir, result.commit_sha)
        assert len(merge_commit.parents) == 2
        assert our_sha  in merge_commit.parents
        assert their_sha in merge_commit.parents

    def test_clean_merge_message_contains_branch(self, diverged_repo):
        """Merge commit message must mention the merged branch name."""
        git_dir = diverged_repo / ".git"
        result = merge_branch(git_dir, diverged_repo, "feature")
        merge_commit = read_commit(git_dir, result.commit_sha)
        assert "feature" in merge_commit.message

    def test_clean_merge_head_updated(self, diverged_repo):
        """After merge, our branch ref points to the new merge commit."""
        git_dir = diverged_repo / ".git"
        result = merge_branch(git_dir, diverged_repo, "feature")
        assert resolve_ref(git_dir, "main") == result.commit_sha

    def test_clean_merge_readable_by_real_git(self, diverged_repo):
        """The merge commit we write must be parseable by real git."""
        git_dir = diverged_repo / ".git"
        result = merge_branch(git_dir, diverged_repo, "feature")
        out = subprocess.run(
            ["git", "cat-file", "-t", result.commit_sha],
            cwd=diverged_repo, capture_output=True,
        ).stdout.strip().decode()
        assert out == "commit"

    # ── three-way merge (conflicts) ───────────────────────────────────────

    def test_conflict_reported(self, conflict_repo):
        """Conflicting changes return success=False with the file listed."""
        git_dir = conflict_repo / ".git"
        result = merge_branch(git_dir, conflict_repo, "feature")
        assert not result.success
        assert "shared.txt" in result.conflicts

    def test_conflict_markers_in_file(self, conflict_repo):
        """The conflicted file on disk must contain all three marker types."""
        git_dir = conflict_repo / ".git"
        merge_branch(git_dir, conflict_repo, "feature")
        content = (conflict_repo / "shared.txt").read_text()
        assert "<<<<<<<" in content
        assert "=======" in content
        assert ">>>>>>>" in content

    def test_conflict_contains_both_versions(self, conflict_repo):
        """Both 'ours change' and 'theirs change' appear in the conflict file."""
        git_dir = conflict_repo / ".git"
        merge_branch(git_dir, conflict_repo, "feature")
        content = (conflict_repo / "shared.txt").read_text()
        assert "ours change"   in content
        assert "theirs change" in content

    def test_conflict_writes_merge_head(self, conflict_repo):
        """A conflicted merge must write .git/MERGE_HEAD."""
        git_dir = conflict_repo / ".git"
        their_sha = resolve_ref(git_dir, "feature")
        merge_branch(git_dir, conflict_repo, "feature")
        merge_head = (git_dir / "MERGE_HEAD").read_text().strip()
        assert merge_head == their_sha

    def test_nonexistent_branch_fails(self, diverged_repo):
        """Merging a branch that does not exist returns success=False."""
        git_dir = diverged_repo / ".git"
        result = merge_branch(git_dir, diverged_repo, "ghost")
        assert not result.success
