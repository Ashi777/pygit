"""
tests/test_commit.py

Tests for pygit commit — recording staged changes as a commit object.

Validation strategy
───────────────────
  • Unit-level: verify the commit object fields (tree SHA, parent, message)
  • Cross-validation: real git must be able to read every commit we write
  • Integration: full workflow add → commit → status / log → branch

Run with:  pytest tests/ -v
"""

import subprocess
from pathlib import Path
import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from pygitlib.commit import commit
from pygitlib.index import add, status
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


def _stage(repo, *name_content_pairs):
    """Create files and stage them with pygit add."""
    git_dir = repo / ".git"
    paths = []
    for name, content in name_content_pairs:
        (repo / name).write_bytes(content)
        paths.append(name)
    add(git_dir, repo, paths)


# ---------------------------------------------------------------------------
# Basic commit creation
# ---------------------------------------------------------------------------

class TestCommitCreation:

    def test_initial_commit_returns_sha(self, repo):
        """commit() returns a 40-char hex SHA-1."""
        git_dir = repo / ".git"
        _stage(repo, ("README.md", b"# Hello\n"))
        sha = commit(git_dir, "initial commit")
        assert len(sha) == 40
        assert all(c in "0123456789abcdef" for c in sha)

    def test_commit_updates_branch_ref(self, repo):
        """After commit, the main branch ref points to the new SHA."""
        git_dir = repo / ".git"
        _stage(repo, ("file.txt", b"content\n"))
        sha = commit(git_dir, "first")
        assert resolve_ref(git_dir, "main") == sha

    def test_commit_head_resolves_to_sha(self, repo):
        """HEAD must resolve to the commit we just created."""
        git_dir = repo / ".git"
        _stage(repo, ("a.txt", b"aaa\n"))
        sha = commit(git_dir, "initial")
        assert resolve_ref(git_dir, "HEAD") == sha

    def test_commit_message_stored(self, repo):
        """The commit message is stored correctly in the commit object."""
        git_dir = repo / ".git"
        _stage(repo, ("msg.txt", b"content\n"))
        sha = commit(git_dir, "my special message")
        c = read_commit(git_dir, sha)
        assert c.message == "my special message"

    def test_initial_commit_has_no_parents(self, repo):
        """The very first commit must have an empty parents list."""
        git_dir = repo / ".git"
        _stage(repo, ("init.txt", b"init\n"))
        sha = commit(git_dir, "root commit")
        c = read_commit(git_dir, sha)
        assert c.parents == []

    def test_commit_tree_sha_matches_git_write_tree(self, repo):
        """
        The tree SHA stored in our commit must equal what real git write-tree
        would produce for the same staged content.
        """
        git_dir = repo / ".git"

        # Stage via real git so the index is authoritative
        (repo / "hello.py").write_bytes(b"print('hello')\n")
        subprocess.run(["git", "add", "hello.py"], cwd=repo, capture_output=True)

        # Get git's expected tree SHA
        git_tree = subprocess.run(
            ["git", "write-tree"], cwd=repo, capture_output=True
        ).stdout.strip().decode()

        # Our commit
        sha = commit(git_dir, "test tree")
        c = read_commit(git_dir, sha)
        assert c.tree == git_tree, (
            f"Tree SHA mismatch: ours={c.tree!r}, git={git_tree!r}"
        )


# ---------------------------------------------------------------------------
# Sequential commits and parent chain
# ---------------------------------------------------------------------------

class TestParentChain:

    def test_second_commit_has_first_as_parent(self, repo):
        """The parent of the second commit is the SHA of the first."""
        git_dir = repo / ".git"

        _stage(repo, ("a.txt", b"aaa\n"))
        sha1 = commit(git_dir, "first")

        _stage(repo, ("b.txt", b"bbb\n"))
        sha2 = commit(git_dir, "second")

        c2 = read_commit(git_dir, sha2)
        assert c2.parents == [sha1]

    def test_three_commit_chain(self, repo):
        """A three-commit chain has the correct parent lineage."""
        git_dir = repo / ".git"

        _stage(repo, ("a.txt", b"a\n"))
        sha1 = commit(git_dir, "c1")

        _stage(repo, ("b.txt", b"b\n"))
        sha2 = commit(git_dir, "c2")

        _stage(repo, ("c.txt", b"c\n"))
        sha3 = commit(git_dir, "c3")

        assert read_commit(git_dir, sha3).parents == [sha2]
        assert read_commit(git_dir, sha2).parents == [sha1]
        assert read_commit(git_dir, sha1).parents == []


# ---------------------------------------------------------------------------
# Cross-validation with real git
# ---------------------------------------------------------------------------

class TestRealGitCompatibility:

    def test_commit_readable_by_git_cat_file(self, repo):
        """Real git cat-file must classify our commit as a 'commit' object."""
        git_dir = repo / ".git"
        _stage(repo, ("readme.txt", b"hello\n"))
        sha = commit(git_dir, "cross-validated commit")

        result = subprocess.run(
            ["git", "cat-file", "-t", sha],
            cwd=repo, capture_output=True
        )
        assert result.stdout.strip().decode() == "commit"

    def test_commit_content_parseable_by_git(self, repo):
        """git cat-file -p must show the correct tree and message."""
        git_dir = repo / ".git"
        _stage(repo, ("main.py", b"x = 1\n"))
        sha = commit(git_dir, "parseable commit")
        c = read_commit(git_dir, sha)

        result = subprocess.run(
            ["git", "cat-file", "-p", sha],
            cwd=repo, capture_output=True
        )
        output = result.stdout.decode()
        assert f"tree {c.tree}" in output
        assert "parseable commit" in output

    def test_commit_appears_in_git_log(self, repo):
        """After commit, real git log shows the commit."""
        git_dir = repo / ".git"
        _stage(repo, ("log_test.txt", b"log me\n"))
        sha = commit(git_dir, "appears in log")

        result = subprocess.run(
            ["git", "log", "--oneline"],
            cwd=repo, capture_output=True
        )
        output = result.stdout.decode()
        assert sha[:7] in output
        assert "appears in log" in output

    def test_commit_files_visible_via_git_show(self, repo):
        """Files committed must be visible via real git show."""
        git_dir = repo / ".git"
        _stage(repo, ("visible.txt", b"see me\n"))
        sha = commit(git_dir, "file visibility")

        result = subprocess.run(
            ["git", "show", "--stat", sha],
            cwd=repo, capture_output=True
        )
        assert "visible.txt" in result.stdout.decode()


# ---------------------------------------------------------------------------
# Status and staging interaction
# ---------------------------------------------------------------------------

class TestStatusInteraction:

    def test_status_clean_after_commit(self, repo):
        """After commit, pygit status shows nothing staged."""
        git_dir = repo / ".git"
        _stage(repo, ("clean.txt", b"content\n"))
        commit(git_dir, "initial")

        result = status(git_dir, repo)
        assert not result["staged"]["new_file"]
        assert not result["staged"]["modified"]
        assert not result["staged"]["deleted"]

    def test_untracked_file_not_committed(self, repo):
        """An untracked file (not staged) must not appear in the commit tree."""
        git_dir = repo / ".git"
        _stage(repo, ("staged.txt", b"staged\n"))
        (repo / "untracked.txt").write_bytes(b"not staged\n")  # NOT staged

        sha = commit(git_dir, "only staged")
        c = read_commit(git_dir, sha)

        # Check git show doesn't list the untracked file
        result = subprocess.run(
            ["git", "show", "--name-only", "--format=", sha],
            cwd=repo, capture_output=True
        )
        files = result.stdout.decode().strip().splitlines()
        assert "staged.txt" in files
        assert "untracked.txt" not in files

    def test_modified_unstaged_not_committed(self, repo):
        """A file modified on disk (not re-staged) keeps its old content in the commit."""
        git_dir = repo / ".git"
        _stage(repo, ("file.txt", b"original\n"))
        sha1 = commit(git_dir, "original")

        # Modify the file without re-staging
        (repo / "file.txt").write_bytes(b"modified but not staged\n")

        # Stage a different file and commit
        _stage(repo, ("other.txt", b"other\n"))
        sha2 = commit(git_dir, "second")
        c2 = read_commit(git_dir, sha2)

        # The tree should still have the ORIGINAL content of file.txt
        result = subprocess.run(
            ["git", "show", f"{sha2}:file.txt"],
            cwd=repo, capture_output=True
        )
        assert result.stdout == b"original\n"


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------

class TestCommitErrors:

    def test_empty_index_raises(self, repo):
        """Committing with nothing staged raises ValueError."""
        git_dir = repo / ".git"
        with pytest.raises(ValueError, match="nothing to commit"):
            commit(git_dir, "empty")

    def test_nothing_new_raises(self, repo):
        """Committing twice with no changes between raises ValueError."""
        git_dir = repo / ".git"
        _stage(repo, ("file.txt", b"content\n"))
        commit(git_dir, "first")

        # Nothing changed in the index since the last commit
        with pytest.raises(ValueError, match="nothing to commit"):
            commit(git_dir, "second with no changes")


# ---------------------------------------------------------------------------
# Merge commit completion
# ---------------------------------------------------------------------------

class TestMergeCommit:

    def test_commit_consumes_merge_head(self, repo):
        """When MERGE_HEAD exists, commit creates a two-parent commit."""
        git_dir = repo / ".git"

        # First commit
        _stage(repo, ("base.txt", b"base\n"))
        sha1 = commit(git_dir, "base commit")

        # Fake MERGE_HEAD (simulate an in-progress merge)
        fake_second_parent = sha1  # use same SHA as a stand-in
        (git_dir / "MERGE_HEAD").write_text(fake_second_parent + "\n")

        _stage(repo, ("merged.txt", b"merged\n"))
        merge_sha = commit(git_dir, "merge commit")

        c = read_commit(git_dir, merge_sha)
        assert len(c.parents) == 2
        assert sha1 in c.parents
        assert fake_second_parent in c.parents

    def test_commit_deletes_merge_head(self, repo):
        """MERGE_HEAD file is removed after commit."""
        git_dir = repo / ".git"
        _stage(repo, ("a.txt", b"a\n"))
        sha1 = commit(git_dir, "base")

        (git_dir / "MERGE_HEAD").write_text(sha1 + "\n")
        (git_dir / "MERGE_MSG").write_text("Merge branch 'feature'\n")

        _stage(repo, ("b.txt", b"b\n"))
        commit(git_dir, "merge")

        assert not (git_dir / "MERGE_HEAD").exists()
        assert not (git_dir / "MERGE_MSG").exists()


# ---------------------------------------------------------------------------
# Subdirectory handling
# ---------------------------------------------------------------------------

class TestSubdirectories:

    def test_commit_with_nested_paths(self, repo):
        """Files in subdirectories are committed with correct tree structure."""
        git_dir = repo / ".git"

        (repo / "src").mkdir()
        _stage(repo,
               ("README.md",      b"# Project\n"),
               ("src/main.py",    b"print('hi')\n"))

        sha = commit(git_dir, "nested dirs")

        # Real git should see both files
        result = subprocess.run(
            ["git", "show", "--name-only", "--format=", sha],
            cwd=repo, capture_output=True
        )
        files = result.stdout.decode().strip().splitlines()
        assert "README.md" in files
        assert "src/main.py" in files

    def test_tree_sha_with_subdirectory_matches_git(self, repo):
        """Tree SHA for nested paths must equal git write-tree."""
        git_dir = repo / ".git"

        (repo / "lib").mkdir()
        # Stage via real git so the index is clean
        (repo / "main.py").write_bytes(b"x=1\n")
        (repo / "lib" / "util.py").write_bytes(b"y=2\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True)

        git_tree = subprocess.run(
            ["git", "write-tree"], cwd=repo, capture_output=True
        ).stdout.strip().decode()

        sha = commit(git_dir, "nested tree test")
        c = read_commit(git_dir, sha)
        assert c.tree == git_tree
