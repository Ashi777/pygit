"""
tests/test_objects.py

Cross-validates pygit against real Git.
Every SHA-1 we produce must exactly match what real git produces.

Run with:  pytest tests/ -v
"""

import subprocess
import tempfile
import os
from pathlib import Path
import pytest

# Add project root to path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from pygitlib.repository import init
from pygitlib.objects import (
    hash_object, cat_file, get_git_dir,
    write_tree, read_tree, write_commit, read_commit,
    TreeEntry, Commit, hash_object,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def repo(tmp_path):
    """Create a temporary directory with both a real git repo and pygit."""
    # Initialize with real git
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@pygit.dev"],
        cwd=tmp_path, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "PyGit Test"],
        cwd=tmp_path, capture_output=True
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Blob tests
# ---------------------------------------------------------------------------

class TestBlobObjects:

    def test_empty_blob_matches_git(self, repo):
        """The hash of an empty file must match real git."""
        git_dir = repo / ".git"

        # Write empty blob with pygit
        our_sha = hash_object(git_dir, b"", write=True)

        # Ask real git what the hash should be
        result = subprocess.run(
            ["git", "hash-object", "--stdin"],
            input=b"", capture_output=True, cwd=repo
        )
        git_sha = result.stdout.strip().decode()

        assert our_sha == git_sha, (
            f"Empty blob: pygit={our_sha}, git={git_sha}"
        )

    def test_simple_content_matches_git(self, repo):
        """Hash of 'hello world' must match git."""
        git_dir = repo / ".git"
        content = b"hello world\n"

        our_sha = hash_object(git_dir, content, write=True)

        result = subprocess.run(
            ["git", "hash-object", "--stdin"],
            input=content, capture_output=True, cwd=repo
        )
        git_sha = result.stdout.strip().decode()

        assert our_sha == git_sha

    def test_binary_content_matches_git(self, repo):
        """Binary content (null bytes, high bytes) must hash correctly."""
        git_dir = repo / ".git"
        content = bytes(range(256))  # All 256 byte values

        our_sha = hash_object(git_dir, content, write=True)

        result = subprocess.run(
            ["git", "hash-object", "--stdin"],
            input=content, capture_output=True, cwd=repo
        )
        git_sha = result.stdout.strip().decode()

        assert our_sha == git_sha

    def test_file_hash_matches_git(self, repo):
        """Hash a real file on disk and match git hash-object <file>."""
        git_dir = repo / ".git"
        test_file = repo / "hello.py"
        test_file.write_bytes(b"def hello():\n    print('Hello, world!')\n")

        our_sha = hash_object(git_dir, test_file.read_bytes(), write=True)

        result = subprocess.run(
            ["git", "hash-object", str(test_file)],
            capture_output=True, cwd=repo
        )
        git_sha = result.stdout.strip().decode()

        assert our_sha == git_sha

    def test_written_object_is_readable(self, repo):
        """Objects we write must be readable by both pygit and real git."""
        git_dir = repo / ".git"
        content = b"The quick brown fox\n"

        sha = hash_object(git_dir, content, write=True)

        # Read back with pygit
        obj_type, data = cat_file(git_dir, sha)
        assert obj_type == "blob"
        assert data == content

        # Read back with real git
        result = subprocess.run(
            ["git", "cat-file", "-p", sha],
            capture_output=True, cwd=repo
        )
        assert result.stdout == content

    def test_object_file_exists_on_disk(self, repo):
        """Verify the object file is created at the correct path."""
        git_dir = repo / ".git"
        sha = hash_object(git_dir, b"test content\n", write=True)

        expected_path = git_dir / "objects" / sha[:2] / sha[2:]
        assert expected_path.exists(), f"Object file not found at {expected_path}"

    def test_hash_without_write(self, repo):
        """hash_object with write=False should not create any files."""
        git_dir = repo / ".git"
        content = b"ephemeral content"

        sha = hash_object(git_dir, content, write=False)

        # Hash should still be correct
        result = subprocess.run(
            ["git", "hash-object", "--stdin"],
            input=content, capture_output=True, cwd=repo
        )
        git_sha = result.stdout.strip().decode()
        assert sha == git_sha

        # But no file should exist
        path = git_dir / "objects" / sha[:2] / sha[2:]
        assert not path.exists()

    def test_known_sha_values(self, repo):
        """
        Test against hardcoded SHA-1s from the Git spec.
        These are universal — every correct Git implementation must produce these.
        """
        git_dir = repo / ".git"

        # "blob 11\x00hello world" sha1 = 95d09f2b10159347eece71399a7e2e907ea3df4f
        sha = hash_object(git_dir, b"hello world", write=True)
        assert sha == "95d09f2b10159347eece71399a7e2e907ea3df4f"

        # Empty string blob
        sha_empty = hash_object(git_dir, b"", write=True)
        assert sha_empty == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"


# ---------------------------------------------------------------------------
# Tree tests
# ---------------------------------------------------------------------------

class TestTreeObjects:

    def test_tree_round_trip(self, repo):
        """Write a tree, read it back, entries should match."""
        git_dir = repo / ".git"

        # First create blobs to reference
        sha1 = hash_object(git_dir, b"file one content\n", write=True)
        sha2 = hash_object(git_dir, b"file two content\n", write=True)

        entries = [
            TreeEntry(mode="100644", name="file_two.txt", sha=sha2),
            TreeEntry(mode="100644", name="file_one.txt", sha=sha1),
        ]
        tree_sha = write_tree(git_dir, entries)

        # Read back
        read_entries = read_tree(git_dir, tree_sha)

        # Should be sorted alphabetically
        assert read_entries[0].name == "file_one.txt"
        assert read_entries[1].name == "file_two.txt"
        assert read_entries[0].sha == sha1

    def test_tree_matches_git(self, repo):
        """Create a tree with real git and verify our SHA matches."""
        git_dir = repo / ".git"

        # Create a real file and add it to git's index
        test_file = repo / "readme.txt"
        test_file.write_text("# My Project\n")

        subprocess.run(["git", "add", "readme.txt"], cwd=repo, capture_output=True)

        # Get what git thinks the tree SHA is
        result = subprocess.run(
            ["git", "write-tree"], capture_output=True, cwd=repo
        )
        git_tree_sha = result.stdout.strip().decode()

        # Get the blob SHA from git
        blob_result = subprocess.run(
            ["git", "hash-object", "readme.txt"],
            capture_output=True, cwd=repo
        )
        blob_sha = blob_result.stdout.strip().decode()

        # Build the same tree with pygit
        entries = [TreeEntry(mode="100644", name="readme.txt", sha=blob_sha)]
        our_tree_sha = write_tree(git_dir, entries)

        assert our_tree_sha == git_tree_sha, (
            f"Tree SHA mismatch: pygit={our_tree_sha}, git={git_tree_sha}"
        )


# ---------------------------------------------------------------------------
# Commit tests
# ---------------------------------------------------------------------------

class TestCommitObjects:

    def test_commit_round_trip(self, repo):
        """Write and read back a commit."""
        git_dir = repo / ".git"

        blob_sha = hash_object(git_dir, b"main.py content\n", write=True)
        entries = [TreeEntry(mode="100644", name="main.py", sha=blob_sha)]
        tree_sha = write_tree(git_dir, entries)

        commit = Commit(
            tree=tree_sha,
            author="Dev <dev@example.com> 1700000000 +0000",
            committer="Dev <dev@example.com> 1700000000 +0000",
            message="Initial commit",
            parents=[],
        )
        commit_sha = write_commit(git_dir, commit)

        # Read back
        read_back = read_commit(git_dir, commit_sha)
        assert read_back.tree == tree_sha
        assert read_back.message == "Initial commit"
        assert read_back.parents == []

    def test_commit_readable_by_real_git(self, repo):
        """A commit we write should be readable by real git cat-file."""
        git_dir = repo / ".git"

        blob_sha = hash_object(git_dir, b"# Hello\n", write=True)
        tree_sha = write_tree(
            git_dir,
            [TreeEntry(mode="100644", name="readme.md", sha=blob_sha)]
        )

        commit = Commit(
            tree=tree_sha,
            author="Test User <test@test.com> 1700000000 +0000",
            committer="Test User <test@test.com> 1700000000 +0000",
            message="First commit",
        )
        commit_sha = write_commit(git_dir, commit)

        # Real git should be able to read it
        result = subprocess.run(
            ["git", "cat-file", "-t", commit_sha],
            capture_output=True, cwd=repo
        )
        assert result.stdout.strip().decode() == "commit"

        result = subprocess.run(
            ["git", "cat-file", "-p", commit_sha],
            capture_output=True, cwd=repo
        )
        assert f"tree {tree_sha}" in result.stdout.decode()
        assert "First commit" in result.stdout.decode()


# ---------------------------------------------------------------------------
# Repository init tests
# ---------------------------------------------------------------------------

class TestInit:

    def test_creates_git_structure(self, tmp_path):
        """init() should create all required .git subdirectories."""
        from pygitlib.repository import init
        git_dir = init(str(tmp_path))

        assert (git_dir / "HEAD").exists()
        assert (git_dir / "objects").is_dir()
        assert (git_dir / "refs" / "heads").is_dir()
        assert (git_dir / "refs" / "tags").is_dir()
        assert (git_dir / "config").exists()

    def test_head_points_to_main(self, tmp_path):
        """HEAD should reference the main branch after init."""
        from pygitlib.repository import init
        git_dir = init(str(tmp_path))
        head_content = (git_dir / "HEAD").read_text()
        assert head_content == "ref: refs/heads/main\n"
