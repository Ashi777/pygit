"""
tests/test_branch.py

Tests for Phase 3 — branching and checkout.

Key guarantee: every operation that touches .git/refs or HEAD must produce
output that real git accepts without complaint.

Run with:  pytest tests/ -v
"""

import subprocess
from pathlib import Path
import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from pygitlib.branch import (
    resolve_ref, current_branch, list_branches,
    create_branch, delete_branch, set_head_to_branch,
)
from pygitlib.checkout import switch_branch


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def repo(tmp_path):
    """Bare git repo (no commits), with HEAD forced to 'main'."""
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@pygit.dev"],
                   cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "PyGit Test"],
                   cwd=tmp_path, capture_output=True)
    # Force the default branch to 'main' regardless of the system git config.
    # Writing HEAD directly works on any git version without needing 'git init -b'.
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    return tmp_path


@pytest.fixture
def committed_repo(repo):
    """
    Repo with one commit on main (created with real git).
    Working tree contains readme.txt.
    """
    (repo / "readme.txt").write_bytes(b"# readme\n")
    subprocess.run(["git", "add", "readme.txt"], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial commit"],
                   cwd=repo, capture_output=True)
    return repo


# ---------------------------------------------------------------------------
# Branch management tests
# ---------------------------------------------------------------------------

class TestBranch:

    def test_current_branch_after_init(self, repo):
        """After init HEAD points to 'main'."""
        assert current_branch(repo / ".git") == "main"

    def test_current_branch_after_commit(self, committed_repo):
        """current_branch returns 'main' after the first commit."""
        assert current_branch(committed_repo / ".git") == "main"

    def test_list_branches_includes_main(self, committed_repo):
        """list_branches returns at least 'main' after the first commit."""
        branches = list_branches(committed_repo / ".git")
        assert "main" in branches

    def test_create_branch(self, committed_repo):
        """create_branch adds a new entry under refs/heads/."""
        git_dir = committed_repo / ".git"
        create_branch(git_dir, "feature")
        assert "feature" in list_branches(git_dir)

    def test_created_branch_points_to_head(self, committed_repo):
        """A new branch must point to the same commit as HEAD."""
        git_dir = committed_repo / ".git"
        head_sha = resolve_ref(git_dir, "HEAD")
        create_branch(git_dir, "feature")
        assert resolve_ref(git_dir, "feature") == head_sha

    def test_list_matches_real_git(self, committed_repo):
        """Our list_branches must match what real git reports."""
        git_dir = committed_repo / ".git"
        subprocess.run(["git", "branch", "alpha"], cwd=committed_repo, capture_output=True)
        subprocess.run(["git", "branch", "beta"],  cwd=committed_repo, capture_output=True)

        git_out = subprocess.run(
            ["git", "branch"], cwd=committed_repo, capture_output=True
        ).stdout.decode()
        git_branches = {b.strip().lstrip("* ") for b in git_out.splitlines() if b.strip()}

        assert set(list_branches(git_dir)) == git_branches

    def test_create_duplicate_branch_raises(self, committed_repo):
        """Creating a branch that already exists raises ValueError."""
        git_dir = committed_repo / ".git"
        create_branch(git_dir, "dup")
        with pytest.raises(ValueError, match="already exists"):
            create_branch(git_dir, "dup")

    def test_delete_branch(self, committed_repo):
        """Deleting a branch removes it from list_branches."""
        git_dir = committed_repo / ".git"
        create_branch(git_dir, "temp")
        delete_branch(git_dir, "temp")
        assert "temp" not in list_branches(git_dir)

    def test_delete_current_branch_raises(self, committed_repo):
        """Cannot delete the currently checked-out branch."""
        git_dir = committed_repo / ".git"
        with pytest.raises(ValueError, match="checked out"):
            delete_branch(git_dir, "main")

    def test_delete_nonexistent_branch_raises(self, committed_repo):
        """Deleting a branch that does not exist raises ValueError."""
        git_dir = committed_repo / ".git"
        with pytest.raises(ValueError, match="not found"):
            delete_branch(git_dir, "ghost")

    def test_create_branch_is_readable_by_real_git(self, committed_repo):
        """A branch we create must appear in real git's branch list."""
        git_dir = committed_repo / ".git"
        create_branch(git_dir, "my-feature")

        result = subprocess.run(
            ["git", "branch"], cwd=committed_repo, capture_output=True
        )
        assert "my-feature" in result.stdout.decode()

    def test_resolve_ref_head(self, committed_repo):
        """resolve_ref('HEAD') returns the current commit SHA."""
        git_dir = committed_repo / ".git"
        sha = resolve_ref(git_dir, "HEAD")
        assert sha is not None and len(sha) == 40

    def test_resolve_ref_branch_name(self, committed_repo):
        """resolve_ref('<branch>') resolves to the same SHA as HEAD."""
        git_dir = committed_repo / ".git"
        assert resolve_ref(git_dir, "main") == resolve_ref(git_dir, "HEAD")


# ---------------------------------------------------------------------------
# Switch tests
# ---------------------------------------------------------------------------

class TestSwitch:

    def test_switch_updates_head(self, committed_repo):
        """After switch, current_branch returns the new branch name."""
        git_dir = committed_repo / ".git"
        create_branch(git_dir, "feature")
        switch_branch(git_dir, committed_repo, "feature")
        assert current_branch(git_dir) == "feature"

    def test_switch_head_readable_by_real_git(self, committed_repo):
        """After pygit switch, real git agrees on which branch is active."""
        git_dir = committed_repo / ".git"
        create_branch(git_dir, "feature")
        switch_branch(git_dir, committed_repo, "feature")

        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=committed_repo, capture_output=True
        )
        assert result.stdout.strip().decode() == "feature"

    def test_switch_already_on_branch(self, committed_repo, capsys):
        """Switching to the branch you are already on prints a message."""
        git_dir = committed_repo / ".git"
        switch_branch(git_dir, committed_repo, "main")
        out = capsys.readouterr().out
        assert "Already on" in out

    def test_switch_nonexistent_branch_raises(self, committed_repo):
        """Switching to a branch that does not exist raises ValueError."""
        git_dir = committed_repo / ".git"
        with pytest.raises(ValueError):
            switch_branch(git_dir, committed_repo, "ghost")

    def test_switch_create_flag(self, committed_repo):
        """switch -c creates the branch and updates HEAD."""
        git_dir = committed_repo / ".git"
        switch_branch(git_dir, committed_repo, "new-feature", create=True)
        assert current_branch(git_dir) == "new-feature"
        assert "new-feature" in list_branches(git_dir)

    def test_switch_create_preserves_files(self, committed_repo):
        """switch -c must not change the working tree (same commit)."""
        git_dir = committed_repo / ".git"
        switch_branch(git_dir, committed_repo, "same-tree", create=True)
        assert (committed_repo / "readme.txt").exists()
        assert (committed_repo / "readme.txt").read_bytes() == b"# readme\n"

    def test_switch_create_existing_branch_raises(self, committed_repo):
        """switch -c on an already-existing branch raises ValueError."""
        git_dir = committed_repo / ".git"
        create_branch(git_dir, "exists")
        with pytest.raises(ValueError, match="already exists"):
            switch_branch(git_dir, committed_repo, "exists", create=True)

    def test_switch_changes_working_tree(self, committed_repo):
        """
        The core integration test.

        Branch layout (created with real git so objects are genuine):
          main    → readme.txt only
          feature → readme.txt + feature.txt

        Verify:
          pygit switch feature  → feature.txt appears
          pygit switch main     → feature.txt disappears
        """
        git_dir = committed_repo / ".git"

        # --- Build the 'feature' branch with real git ---
        subprocess.run(["git", "branch", "feature"],
                       cwd=committed_repo, capture_output=True)
        subprocess.run(["git", "switch", "feature"],
                       cwd=committed_repo, capture_output=True)
        (committed_repo / "feature.txt").write_bytes(b"feature content\n")
        subprocess.run(["git", "add", "feature.txt"],
                       cwd=committed_repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add feature file"],
                       cwd=committed_repo, capture_output=True)

        # Return to main with real git (so we start in a clean state)
        subprocess.run(["git", "switch", "main"],
                       cwd=committed_repo, capture_output=True)
        assert not (committed_repo / "feature.txt").exists()

        # --- Use pygit to switch to feature ---
        switch_branch(git_dir, committed_repo, "feature")
        assert (committed_repo / "feature.txt").exists()
        assert (committed_repo / "readme.txt").exists()
        assert current_branch(git_dir) == "feature"

        # --- Use pygit to switch back to main ---
        switch_branch(git_dir, committed_repo, "main")
        assert not (committed_repo / "feature.txt").exists()
        assert (committed_repo / "readme.txt").exists()
        assert current_branch(git_dir) == "main"

    def test_switch_refuses_dirty_staged(self, committed_repo):
        """Switch refuses when there are staged (but uncommitted) changes."""
        git_dir = committed_repo / ".git"
        create_branch(git_dir, "feature")

        # Stage a new file using pygit (creates a dirty index vs HEAD)
        (committed_repo / "new.txt").write_bytes(b"new\n")
        from pygitlib.index import add
        add(git_dir, committed_repo, ["new.txt"])

        with pytest.raises(ValueError, match="local changes"):
            switch_branch(git_dir, committed_repo, "feature")

    def test_switch_refuses_dirty_unstaged(self, committed_repo):
        """Switch refuses when a tracked file is modified on disk."""
        git_dir = committed_repo / ".git"
        create_branch(git_dir, "feature")

        # Modify a tracked file without staging
        (committed_repo / "readme.txt").write_bytes(b"modified\n")

        with pytest.raises(ValueError, match="local changes"):
            switch_branch(git_dir, committed_repo, "feature")
