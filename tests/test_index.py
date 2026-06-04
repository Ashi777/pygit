"""
tests/test_index.py

Tests for the Git index (staging area).

Every critical assertion is cross-validated against real git so that
our binary format is byte-for-byte compatible.

Run with:  pytest tests/ -v
"""

import subprocess
from pathlib import Path
import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from pygitlib.objects import hash_object, write_tree, TreeEntry
from pygitlib.index import (
    IndexEntry, read_index, write_index, add, status,
)


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def repo(tmp_path):
    """Temp dir with both a real git repo and pygit available."""
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@pygit.dev"],
                   cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "PyGit Test"],
                   cwd=tmp_path, capture_output=True)
    return tmp_path


# ---------------------------------------------------------------------------
# Index read / write
# ---------------------------------------------------------------------------

class TestIndexReadWrite:

    def test_no_index_returns_empty(self, repo):
        """read_index returns [] when .git/index does not exist."""
        git_dir = repo / ".git"
        (git_dir / "index").unlink(missing_ok=True)
        assert read_index(git_dir) == []

    def test_empty_index_round_trip(self, repo):
        """Writing an empty index and reading it back yields no entries."""
        git_dir = repo / ".git"
        write_index(git_dir, [])
        assert read_index(git_dir) == []

    def test_single_entry_round_trip(self, repo):
        """Write one entry, read it back — all fields should match."""
        git_dir = repo / ".git"
        blob_sha = hash_object(git_dir, b"hello\n", write=True)

        original = IndexEntry(
            ctime_s=1700000000, ctime_ns=0,
            mtime_s=1700000000, mtime_ns=0,
            dev=1, ino=2, mode=0o100644,
            uid=0, gid=0, size=6,
            sha=blob_sha, flags=0, path="hello.txt",
        )
        write_index(git_dir, [original])
        entries = read_index(git_dir)

        assert len(entries) == 1
        e = entries[0]
        assert e.path == "hello.txt"
        assert e.sha == blob_sha
        assert e.mode == 0o100644
        assert e.size == 6

    def test_entries_are_returned_sorted(self, repo):
        """Entries are stored sorted by path and returned in that order."""
        git_dir = repo / ".git"
        sha = hash_object(git_dir, b"x\n", write=True)

        entries = [
            IndexEntry(0, 0, 0, 0, 0, 0, 0o100644, 0, 0, 2, sha, 0, "z.txt"),
            IndexEntry(0, 0, 0, 0, 0, 0, 0o100644, 0, 0, 2, sha, 0, "a.txt"),
            IndexEntry(0, 0, 0, 0, 0, 0, 0o100644, 0, 0, 2, sha, 0, "m.txt"),
        ]
        write_index(git_dir, entries)
        result = read_index(git_dir)

        assert [e.path for e in result] == ["a.txt", "m.txt", "z.txt"]

    def test_various_name_lengths_round_trip(self, repo):
        """Entry binary padding is correct across different name lengths."""
        git_dir = repo / ".git"
        sha = hash_object(git_dir, b"x\n", write=True)

        # Cover name lengths that hit different 8-byte padding buckets
        names = [
            "a.txt",          # 5 chars
            "ab.txt",         # 6 chars
            "hello.txt",      # 9 chars
            "hello_world.py", # 14 chars
            "a_very_long_filename_that_crosses_padding_boundary.txt",  # 54 chars
        ]
        entries = [
            IndexEntry(0, 0, 0, 0, 0, 0, 0o100644, 0, 0, 2, sha, 0, n)
            for n in names
        ]
        write_index(git_dir, entries)
        result = read_index(git_dir)

        assert {e.path for e in result} == set(names)


# ---------------------------------------------------------------------------
# git add
# ---------------------------------------------------------------------------

class TestAdd:

    def test_add_single_file(self, repo):
        """add() stages a file and creates .git/index."""
        git_dir = repo / ".git"
        (repo / "hello.txt").write_bytes(b"Hello, World!\n")

        add(git_dir, repo, ["hello.txt"])

        entries = read_index(git_dir)
        assert len(entries) == 1
        assert entries[0].path == "hello.txt"
        assert entries[0].mode == 0o100644

    def test_add_index_readable_by_real_git(self, repo):
        """An index we write must be accepted by real git ls-files."""
        git_dir = repo / ".git"
        (repo / "readme.txt").write_bytes(b"# My Project\n")

        add(git_dir, repo, ["readme.txt"])

        result = subprocess.run(
            ["git", "ls-files"], capture_output=True, cwd=repo
        )
        assert result.returncode == 0
        assert "readme.txt" in result.stdout.decode()

    def test_add_produces_correct_blob_sha(self, repo):
        """The blob SHA stored in the index must match git hash-object."""
        git_dir = repo / ".git"
        content = b"print('hello')\n"
        (repo / "main.py").write_bytes(content)

        add(git_dir, repo, ["main.py"])

        git_sha = subprocess.run(
            ["git", "hash-object", "main.py"],
            capture_output=True, cwd=repo
        ).stdout.strip().decode()

        assert read_index(git_dir)[0].sha == git_sha

    def test_add_produces_correct_tree_sha(self, repo):
        """
        The tree SHA from git write-tree must equal our write_tree output
        when built from the same index entries.
        """
        git_dir = repo / ".git"
        (repo / "main.py").write_bytes(b"print('hello')\n")
        add(git_dir, repo, ["main.py"])

        git_tree_sha = subprocess.run(
            ["git", "write-tree"], capture_output=True, cwd=repo
        ).stdout.strip().decode()

        entries = read_index(git_dir)
        our_tree_sha = write_tree(
            git_dir,
            [TreeEntry(mode=f"{e.mode:o}", name=e.path, sha=e.sha)
             for e in entries]
        )

        assert our_tree_sha == git_tree_sha, (
            f"Tree SHA mismatch: ours={our_tree_sha}, git={git_tree_sha}"
        )

    def test_add_multiple_files(self, repo):
        """add() stages multiple files in a single call."""
        git_dir = repo / ".git"
        (repo / "a.txt").write_bytes(b"aaa\n")
        (repo / "b.txt").write_bytes(b"bbb\n")
        (repo / "c.txt").write_bytes(b"ccc\n")

        add(git_dir, repo, ["a.txt", "b.txt", "c.txt"])

        paths = {e.path for e in read_index(git_dir)}
        assert paths == {"a.txt", "b.txt", "c.txt"}

    def test_add_updates_existing_entry(self, repo):
        """Re-adding a modified file replaces the index entry."""
        git_dir = repo / ".git"
        f = repo / "file.txt"
        f.write_bytes(b"original\n")
        add(git_dir, repo, ["file.txt"])
        original_sha = read_index(git_dir)[0].sha

        f.write_bytes(b"modified content\n")
        add(git_dir, repo, ["file.txt"])

        entries = read_index(git_dir)
        assert len(entries) == 1
        assert entries[0].sha != original_sha

    def test_add_nonexistent_file_raises(self, repo):
        """add() raises FileNotFoundError for a missing path."""
        git_dir = repo / ".git"
        with pytest.raises(FileNotFoundError):
            add(git_dir, repo, ["ghost.txt"])

    def test_add_preserves_other_entries(self, repo):
        """Adding a new file does not remove previously staged files."""
        git_dir = repo / ".git"
        (repo / "first.txt").write_bytes(b"first\n")
        (repo / "second.txt").write_bytes(b"second\n")

        add(git_dir, repo, ["first.txt"])
        add(git_dir, repo, ["second.txt"])  # second call, not a combined call

        paths = {e.path for e in read_index(git_dir)}
        assert "first.txt" in paths
        assert "second.txt" in paths


# ---------------------------------------------------------------------------
# git status
# ---------------------------------------------------------------------------

class TestStatus:

    def test_untracked_file(self, repo):
        """A new file not yet added appears in 'untracked'."""
        git_dir = repo / ".git"
        (repo / "new.txt").write_bytes(b"new\n")

        result = status(git_dir, repo)
        assert "new.txt" in result["untracked"]

    def test_staged_new_file(self, repo):
        """After add(), file appears in staged['new_file'], not untracked."""
        git_dir = repo / ".git"
        (repo / "staged.txt").write_bytes(b"staged\n")
        add(git_dir, repo, ["staged.txt"])

        result = status(git_dir, repo)
        assert "staged.txt" in result["staged"]["new_file"]
        assert "staged.txt" not in result["untracked"]

    def test_unstaged_modified(self, repo):
        """Editing a staged file without re-adding shows it as unstaged modified."""
        git_dir = repo / ".git"
        f = repo / "track.txt"
        f.write_bytes(b"original\n")
        add(git_dir, repo, ["track.txt"])

        f.write_bytes(b"changed content\n")  # modify without staging

        result = status(git_dir, repo)
        assert "track.txt" in result["staged"]["new_file"]   # still staged (original)
        assert "track.txt" in result["unstaged"]["modified"]  # but disk differs

    def test_staged_file_not_modified_when_unchanged(self, repo):
        """Staging a file then reading immediately shows it as clean (no unstaged)."""
        git_dir = repo / ".git"
        (repo / "clean.txt").write_bytes(b"content\n")
        add(git_dir, repo, ["clean.txt"])

        result = status(git_dir, repo)
        assert "clean.txt" not in result["unstaged"]["modified"]
        assert "clean.txt" not in result["untracked"]

    def test_unstaged_deleted(self, repo):
        """Deleting a staged file shows it in unstaged['deleted']."""
        git_dir = repo / ".git"
        f = repo / "will_delete.txt"
        f.write_bytes(b"exists\n")
        add(git_dir, repo, ["will_delete.txt"])
        f.unlink()

        result = status(git_dir, repo)
        assert "will_delete.txt" in result["unstaged"]["deleted"]

    def test_git_dir_not_in_untracked(self, repo):
        """The .git directory must never appear in untracked files."""
        git_dir = repo / ".git"
        result = status(git_dir, repo)
        for path in result["untracked"]:
            assert not path.startswith(".git"), (
                f".git file appeared in untracked: {path}"
            )
