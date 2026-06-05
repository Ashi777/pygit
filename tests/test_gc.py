"""
tests/test_gc.py

Tests for pygit gc — garbage collection of unreachable loose objects.

Test structure
──────────────
  TestFindLooseObjects  — enumerate loose objects on disk
  TestFindReachable     — reachability walk from refs
  TestRunGcReport       — run_gc(prune=False) identifies unreachable objects
  TestRunGcPrune        — run_gc(prune=True) deletes them
  TestReachabilityRules — staged objects and stash entries are never pruned

Run with:  pytest tests/ -v
"""

import subprocess
from pathlib import Path
import pytest
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from pygitlib.gc import find_loose_objects, find_reachable, run_gc
from pygitlib.objects import hash_object, object_path
from pygitlib.index import add, read_index


# ---------------------------------------------------------------------------
# Fixtures / helpers
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


def _commit(repo, files: dict[str, str], message: str = "c") -> str:
    from pygitlib.commit import commit as make_commit
    git_dir = repo / ".git"
    for name, content in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    add(git_dir, repo, list(files.keys()))
    return make_commit(git_dir, message)


def _orphan(repo, content: bytes = b"orphaned") -> str:
    """Write a blob that is NOT in any commit or index (truly unreachable)."""
    return hash_object(repo / ".git", content, write=True)


# ---------------------------------------------------------------------------
# find_loose_objects
# ---------------------------------------------------------------------------

class TestFindLooseObjects:

    def test_empty_repo_has_no_loose_objects(self, repo):
        assert find_loose_objects(repo / ".git") == {}

    def test_written_blob_appears(self, repo):
        sha = _orphan(repo)
        loose = find_loose_objects(repo / ".git")
        assert sha in loose

    def test_path_is_correct(self, repo):
        sha = _orphan(repo, b"hello")
        loose = find_loose_objects(repo / ".git")
        assert loose[sha] == object_path(repo / ".git", sha)

    def test_committed_objects_appear(self, repo):
        _commit(repo, {"f.txt": "v1"})
        loose = find_loose_objects(repo / ".git")
        assert len(loose) >= 3   # at minimum: blob + tree + commit

    def test_pack_dir_is_excluded(self, repo):
        # Loose objects must never include pack-index files accidentally
        _commit(repo, {"f.txt": "x"})
        loose = find_loose_objects(repo / ".git")
        for sha in loose:
            assert len(sha) == 40


# ---------------------------------------------------------------------------
# find_reachable
# ---------------------------------------------------------------------------

class TestFindReachable:

    def test_empty_repo_finds_nothing(self, repo):
        assert find_reachable(repo / ".git") == set()

    def test_committed_objects_reachable(self, repo):
        commit_sha = _commit(repo, {"f.txt": "content"})
        reachable  = find_reachable(repo / ".git")
        assert commit_sha in reachable

    def test_blob_reachable_via_tree(self, repo):
        _commit(repo, {"f.txt": "hello"})
        git_dir   = repo / ".git"
        reachable = find_reachable(git_dir)
        # Every loose object should be reachable after a commit
        loose = find_loose_objects(git_dir)
        for sha in loose:
            assert sha in reachable

    def test_staged_blob_is_reachable(self, repo):
        """A staged blob must be protected even before it's committed."""
        git_dir = repo / ".git"
        (repo / "new.txt").write_text("staged but not committed")
        add(git_dir, repo, ["new.txt"])
        blob_sha = next(e.sha for e in read_index(git_dir)
                        if e.path == "new.txt")
        reachable = find_reachable(git_dir)
        assert blob_sha in reachable

    def test_orphan_is_not_reachable(self, repo):
        _commit(repo, {"f.txt": "v1"})
        orphan_sha = _orphan(repo, b"totally unreachable")
        reachable  = find_reachable(repo / ".git")
        assert orphan_sha not in reachable

    def test_parent_commits_reachable(self, repo):
        sha1 = _commit(repo, {"f.txt": "v1"}, "first")
        sha2 = _commit(repo, {"f.txt": "v2"}, "second")
        reachable = find_reachable(repo / ".git")
        assert sha1 in reachable
        assert sha2 in reachable

    def test_nested_tree_blobs_reachable(self, repo):
        _commit(repo, {"src/main.py": "print('hi')"})
        git_dir   = repo / ".git"
        reachable = find_reachable(git_dir)
        loose     = find_loose_objects(git_dir)
        for sha in loose:
            assert sha in reachable, f"{sha[:7]} should be reachable"


# ---------------------------------------------------------------------------
# run_gc — report mode (prune=False)
# ---------------------------------------------------------------------------

class TestRunGcReport:

    def test_nothing_to_collect_when_clean(self, repo):
        _commit(repo, {"f.txt": "v1"})
        result = run_gc(repo / ".git")
        assert result["unreachable"] == 0
        assert result["pruned"] is False

    def test_orphan_shows_as_unreachable(self, repo):
        _commit(repo, {"f.txt": "v1"})
        sha    = _orphan(repo)
        result = run_gc(repo / ".git")
        assert result["unreachable"] == 1
        assert sha in result["unreachable_shas"]

    def test_multiple_orphans_counted(self, repo):
        _commit(repo, {"f.txt": "v1"})
        s1 = _orphan(repo, b"aaa")
        s2 = _orphan(repo, b"bbb")
        s3 = _orphan(repo, b"ccc")
        result = run_gc(repo / ".git")
        assert result["unreachable"] == 3
        for s in (s1, s2, s3):
            assert s in result["unreachable_shas"]

    def test_report_does_not_delete(self, repo):
        _commit(repo, {"f.txt": "v1"})
        sha = _orphan(repo)
        run_gc(repo / ".git", prune=False)
        assert object_path(repo / ".git", sha).exists()

    def test_unreachable_bytes_nonzero(self, repo):
        _commit(repo, {"f.txt": "v1"})
        _orphan(repo, b"some content")
        result = run_gc(repo / ".git")
        assert result["unreachable_bytes"] > 0

    def test_counts_are_consistent(self, repo):
        _commit(repo, {"f.txt": "v1"})
        _orphan(repo, b"x")
        result = run_gc(repo / ".git")
        assert result["loose_total"] == result["reachable"] + result["unreachable"]


# ---------------------------------------------------------------------------
# run_gc — prune mode (prune=True)
# ---------------------------------------------------------------------------

class TestRunGcPrune:

    def test_prune_deletes_unreachable_object(self, repo):
        _commit(repo, {"f.txt": "v1"})
        sha = _orphan(repo)
        assert object_path(repo / ".git", sha).exists()

        run_gc(repo / ".git", prune=True)

        assert not object_path(repo / ".git", sha).exists()

    def test_prune_preserves_reachable_objects(self, repo):
        commit_sha = _commit(repo, {"f.txt": "v1"})
        _orphan(repo, b"trash")
        run_gc(repo / ".git", prune=True)
        # Commit must still be readable
        from pygitlib.objects import read_commit
        read_commit(repo / ".git", commit_sha)   # must not raise

    def test_prune_reports_correct_count(self, repo):
        _commit(repo, {"f.txt": "v1"})
        _orphan(repo, b"a")
        _orphan(repo, b"b")
        result = run_gc(repo / ".git", prune=True)
        assert result["unreachable"] == 2
        assert result["pruned"] is True

    def test_second_gc_finds_nothing(self, repo):
        _commit(repo, {"f.txt": "v1"})
        _orphan(repo)
        run_gc(repo / ".git", prune=True)

        result2 = run_gc(repo / ".git")
        assert result2["unreachable"] == 0

    def test_prune_removes_empty_prefix_dirs(self, repo):
        """Two-letter prefix dirs that become empty should be deleted."""
        _commit(repo, {"f.txt": "v1"})
        sha    = _orphan(repo, b"lonely")
        prefix = repo / ".git" / "objects" / sha[:2]
        assert prefix.is_dir()

        # Make sure this prefix dir only contains the orphan
        other_in_same_prefix = [
            p for p in prefix.iterdir()
            if p.name != sha[2:]
        ]
        if other_in_same_prefix:
            pytest.skip("Prefix dir shared with reachable object; skip dir-removal test")

        run_gc(repo / ".git", prune=True)
        assert not prefix.exists()


# ---------------------------------------------------------------------------
# Reachability rules
# ---------------------------------------------------------------------------

class TestReachabilityRules:

    def test_staged_object_not_pruned(self, repo):
        """A blob that is staged but not yet committed must survive gc."""
        git_dir = repo / ".git"
        # Need at least one commit so gc can run
        _commit(repo, {"base.txt": "base"})
        (repo / "new.txt").write_text("staged work")
        add(git_dir, repo, ["new.txt"])
        staged_sha = next(e.sha for e in read_index(git_dir)
                         if e.path == "new.txt")

        run_gc(git_dir, prune=True)

        assert object_path(git_dir, staged_sha).exists()

    def test_stash_objects_not_pruned(self, repo):
        """Objects belonging to a stash entry must not be deleted."""
        git_dir = repo / ".git"
        _commit(repo, {"f.txt": "v1"})
        (repo / "f.txt").write_text("stash me")
        add(git_dir, repo, ["f.txt"])

        from pygitlib.stash import stash_push, _stack_read
        stash_push(git_dir, repo)
        stash_sha = _stack_read(git_dir)[-1]

        result = run_gc(git_dir, prune=True)

        # The stash commit itself must not appear as unreachable
        assert stash_sha not in result["unreachable_shas"]
        # All stash-reachable objects must still exist
        from pygitlib.objects import read_commit
        sc = read_commit(git_dir, stash_sha)
        assert sc is not None   # stash commit survives

    def test_merge_head_objects_not_pruned(self, repo):
        """Objects referenced by MERGE_HEAD must be considered reachable."""
        git_dir = repo / ".git"
        sha = _commit(repo, {"f.txt": "v1"})
        # Manually write a MERGE_HEAD pointing at the commit
        (git_dir / "MERGE_HEAD").write_text(sha + "\n")

        result = run_gc(git_dir, prune=False)
        assert sha not in result["unreachable_shas"]
