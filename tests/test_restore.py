"""
tests/test_restore.py

Tests for pygit restore — unstage files and discard working-tree changes.

Test structure
──────────────
  TestRestoreStaged     — restore_staged() (git restore --staged)
  TestRestoreWorktree   — restore_worktree() (git restore)
  TestRestoreIntegration — end-to-end workflow through status()

Run with:  pytest tests/ -v
"""

import subprocess
from pathlib import Path
import pytest
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from pygitlib.restore import restore_staged, restore_worktree
from pygitlib.index import add, read_index, write_index, status


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.dev"],
                   cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"],
                   cwd=tmp_path, capture_output=True)
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    return tmp_path


def _commit(repo, files: dict[str, str], message: str = "commit") -> str:
    """Create files, stage them, and make a pygit commit. Returns the SHA."""
    from pygitlib.commit import commit as make_commit
    git_dir = repo / ".git"
    for fname, content in files.items():
        p = repo / fname
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    add(git_dir, repo, list(files.keys()))
    return make_commit(git_dir, message)


def _index_sha(repo, path: str) -> str | None:
    """Return the blob SHA for *path* in the index, or None if absent."""
    git_dir = repo / ".git"
    for e in read_index(git_dir):
        if e.path == path:
            return e.sha
    return None


# ---------------------------------------------------------------------------
# restore_staged  (pygit restore --staged)
# ---------------------------------------------------------------------------

class TestRestoreStaged:

    # --- new file (never committed) ---

    def test_unstage_new_file_removes_from_index(self, repo):
        git_dir = repo / ".git"
        (repo / "hello.txt").write_text("hello")
        add(git_dir, repo, ["hello.txt"])
        assert _index_sha(repo, "hello.txt") is not None

        errors = restore_staged(git_dir, ["hello.txt"])

        assert errors == []
        assert _index_sha(repo, "hello.txt") is None

    def test_unstage_new_file_leaves_disk_untouched(self, repo):
        git_dir = repo / ".git"
        (repo / "hello.txt").write_text("hello")
        add(git_dir, repo, ["hello.txt"])
        restore_staged(git_dir, ["hello.txt"])
        assert (repo / "hello.txt").read_text() == "hello"

    # --- staged modification ---

    def test_unstage_modification_restores_head_sha(self, repo):
        git_dir = repo / ".git"
        _commit(repo, {"file.txt": "original"})
        original_sha = _index_sha(repo, "file.txt")

        (repo / "file.txt").write_text("modified")
        add(git_dir, repo, ["file.txt"])
        assert _index_sha(repo, "file.txt") != original_sha

        errors = restore_staged(git_dir, ["file.txt"])

        assert errors == []
        assert _index_sha(repo, "file.txt") == original_sha

    def test_unstage_modification_leaves_disk_untouched(self, repo):
        git_dir = repo / ".git"
        _commit(repo, {"file.txt": "original"})
        (repo / "file.txt").write_text("modified")
        add(git_dir, repo, ["file.txt"])

        restore_staged(git_dir, ["file.txt"])

        assert (repo / "file.txt").read_text() == "modified"

    # --- staged deletion (file removed from index but exists in HEAD) ---

    def test_unstage_staged_deletion_restores_index_entry(self, repo):
        git_dir = repo / ".git"
        _commit(repo, {"file.txt": "content"})
        head_sha = _index_sha(repo, "file.txt")

        # Simulate a staged deletion: remove entry from index
        entries = [e for e in read_index(git_dir) if e.path != "file.txt"]
        write_index(git_dir, entries)
        assert _index_sha(repo, "file.txt") is None

        errors = restore_staged(git_dir, ["file.txt"])

        assert errors == []
        assert _index_sha(repo, "file.txt") == head_sha

    # --- errors ---

    def test_error_on_completely_unknown_path(self, repo):
        git_dir = repo / ".git"
        errors = restore_staged(git_dir, ["ghost.txt"])
        assert "ghost.txt" in errors

    def test_valid_paths_processed_despite_error(self, repo):
        """One bad path must not block the good paths."""
        git_dir = repo / ".git"
        (repo / "good.txt").write_text("g")
        add(git_dir, repo, ["good.txt"])

        errors = restore_staged(git_dir, ["good.txt", "bad.txt"])

        assert "bad.txt" in errors
        assert "good.txt" not in errors
        assert _index_sha(repo, "good.txt") is None   # was unstaged

    # --- multiple files ---

    def test_multiple_files_unstaged_at_once(self, repo):
        git_dir = repo / ".git"
        (repo / "a.txt").write_text("a")
        (repo / "b.txt").write_text("b")
        add(git_dir, repo, ["a.txt", "b.txt"])

        errors = restore_staged(git_dir, ["a.txt", "b.txt"])

        assert errors == []
        assert _index_sha(repo, "a.txt") is None
        assert _index_sha(repo, "b.txt") is None


# ---------------------------------------------------------------------------
# restore_worktree  (pygit restore)
# ---------------------------------------------------------------------------

class TestRestoreWorktree:

    def test_discard_modification(self, repo):
        git_dir = repo / ".git"
        _commit(repo, {"file.txt": "original"})

        (repo / "file.txt").write_text("modified")
        errors = restore_worktree(git_dir, repo, ["file.txt"])

        assert errors == []
        assert (repo / "file.txt").read_text() == "original"

    def test_restore_deleted_file(self, repo):
        git_dir = repo / ".git"
        _commit(repo, {"file.txt": "content"})
        (repo / "file.txt").unlink()
        assert not (repo / "file.txt").exists()

        errors = restore_worktree(git_dir, repo, ["file.txt"])

        assert errors == []
        assert (repo / "file.txt").read_text() == "content"

    def test_index_is_not_modified(self, repo):
        git_dir = repo / ".git"
        _commit(repo, {"file.txt": "original"})
        sha_before = _index_sha(repo, "file.txt")

        (repo / "file.txt").write_text("modified")
        restore_worktree(git_dir, repo, ["file.txt"])

        assert _index_sha(repo, "file.txt") == sha_before

    def test_restore_nested_file(self, repo):
        git_dir = repo / ".git"
        _commit(repo, {"src/main.py": "print('hello')"})
        (repo / "src" / "main.py").write_text("print('oops')")

        errors = restore_worktree(git_dir, repo, ["src/main.py"])

        assert errors == []
        assert (repo / "src" / "main.py").read_text() == "print('hello')"

    def test_error_if_not_in_index(self, repo):
        git_dir = repo / ".git"
        (repo / "untracked.txt").write_text("whatever")
        errors = restore_worktree(git_dir, repo, ["untracked.txt"])
        assert "untracked.txt" in errors

    def test_multiple_files(self, repo):
        git_dir = repo / ".git"
        _commit(repo, {"a.txt": "aaa", "b.txt": "bbb"})
        (repo / "a.txt").write_text("AAA")
        (repo / "b.txt").write_text("BBB")

        errors = restore_worktree(git_dir, repo, ["a.txt", "b.txt"])

        assert errors == []
        assert (repo / "a.txt").read_text() == "aaa"
        assert (repo / "b.txt").read_text() == "bbb"


# ---------------------------------------------------------------------------
# Integration: restore interacts correctly with status()
# ---------------------------------------------------------------------------

class TestRestoreIntegration:

    def test_unstage_new_file_moves_to_untracked(self, repo):
        git_dir = repo / ".git"
        (repo / "new.txt").write_text("new")
        add(git_dir, repo, ["new.txt"])

        assert "new.txt" in status(git_dir, repo)["staged"]["new_file"]

        restore_staged(git_dir, ["new.txt"])

        st = status(git_dir, repo)
        assert "new.txt" not in st["staged"]["new_file"]
        assert "new.txt" in st["untracked"]

    def test_unstage_modification_appears_as_unstaged(self, repo):
        git_dir = repo / ".git"
        _commit(repo, {"file.txt": "v1"})
        (repo / "file.txt").write_text("v2")
        add(git_dir, repo, ["file.txt"])

        assert "file.txt" in status(git_dir, repo)["staged"]["modified"]

        restore_staged(git_dir, ["file.txt"])

        st = status(git_dir, repo)
        assert "file.txt" not in st["staged"]["modified"]
        assert "file.txt" in st["unstaged"]["modified"]   # disk still has v2

    def test_restore_worktree_clears_unstaged(self, repo):
        git_dir = repo / ".git"
        _commit(repo, {"file.txt": "original"})
        (repo / "file.txt").write_text("modified")

        assert "file.txt" in status(git_dir, repo)["unstaged"]["modified"]

        restore_worktree(git_dir, repo, ["file.txt"])

        st = status(git_dir, repo)
        assert not any(st["unstaged"][k] for k in st["unstaged"])

    def test_full_add_restore_cycle(self, repo):
        """add → restore --staged → restore → clean status."""
        git_dir = repo / ".git"
        _commit(repo, {"file.txt": "original"})

        # Modify and stage
        (repo / "file.txt").write_text("modified")
        add(git_dir, repo, ["file.txt"])
        assert "file.txt" in status(git_dir, repo)["staged"]["modified"]

        # Unstage
        restore_staged(git_dir, ["file.txt"])
        assert "file.txt" in status(git_dir, repo)["unstaged"]["modified"]

        # Discard working-tree change
        restore_worktree(git_dir, repo, ["file.txt"])
        st = status(git_dir, repo)
        assert not any(st["staged"][k]   for k in st["staged"])
        assert not any(st["unstaged"][k] for k in st["unstaged"])
