"""
tests/test_stash.py

Tests for pygit stash / stash pop / stash list.

Test structure
──────────────
  TestStashPush        — stash_push() saves state and restores HEAD
  TestStashPop         — stash_pop() restores saved state
  TestStashStack       — multiple stash entries (LIFO order)
  TestStashList        — stash_list() formatting
  TestStashIntegration — full workflow through status()

Run with:  pytest tests/ -v
"""

import subprocess
from pathlib import Path
import pytest
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from pygitlib.stash import stash_push, stash_pop, stash_list, _stack_read
from pygitlib.index import add, read_index, status
from pygitlib.objects import read_commit


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


def _commit(repo, files: dict[str, str], message: str = "commit") -> str:
    from pygitlib.commit import commit as make_commit
    git_dir = repo / ".git"
    for fname, content in files.items():
        p = repo / fname
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    add(git_dir, repo, list(files.keys()))
    return make_commit(git_dir, message)


def _disk(repo, path: str) -> str:
    return (repo / path).read_text()


def _index_sha(repo, path: str) -> str | None:
    for e in read_index(repo / ".git"):
        if e.path == path:
            return e.sha
    return None


# ---------------------------------------------------------------------------
# stash push
# ---------------------------------------------------------------------------

class TestStashPush:

    def test_push_saves_staged_change(self, repo):
        """Staged modification is captured in the stash commit."""
        git_dir = repo / ".git"
        _commit(repo, {"f.txt": "v1"})
        (repo / "f.txt").write_text("v2")
        add(git_dir, repo, ["f.txt"])

        stash_push(git_dir, repo)

        stack = _stack_read(git_dir)
        assert len(stack) == 1
        stash_commit = read_commit(git_dir, stack[0])
        assert stash_commit.message.startswith("WIP on")

    def test_push_saves_unstaged_change(self, repo):
        git_dir = repo / ".git"
        _commit(repo, {"f.txt": "v1"})
        (repo / "f.txt").write_text("v2")  # NOT staged

        stash_push(git_dir, repo)

        assert len(_stack_read(git_dir)) == 1

    def test_push_restores_working_tree_to_head(self, repo):
        """After push, the disk file must match HEAD."""
        git_dir = repo / ".git"
        _commit(repo, {"f.txt": "original"})
        (repo / "f.txt").write_text("modified")
        add(git_dir, repo, ["f.txt"])

        stash_push(git_dir, repo)

        assert _disk(repo, "f.txt") == "original"

    def test_push_restores_index_to_head(self, repo):
        """After push, the index must match HEAD (no staged changes)."""
        git_dir = repo / ".git"
        _commit(repo, {"f.txt": "original"})
        # Capture the HEAD sha BEFORE staging the modification
        original_sha = _index_sha(repo, "f.txt")

        (repo / "f.txt").write_text("modified")
        add(git_dir, repo, ["f.txt"])  # index now holds sha("modified")

        stash_push(git_dir, repo)

        # Index must be back to the HEAD sha — sha("original")
        assert _index_sha(repo, "f.txt") == original_sha

    def test_push_removes_new_staged_file_from_disk(self, repo):
        """A brand-new staged file must be deleted from disk after push."""
        git_dir = repo / ".git"
        _commit(repo, {"base.txt": "base"})
        (repo / "new.txt").write_text("new content")
        add(git_dir, repo, ["new.txt"])

        stash_push(git_dir, repo)

        assert not (repo / "new.txt").exists()
        assert _index_sha(repo, "new.txt") is None

    def test_push_nothing_to_save(self, repo, capsys):
        """With no changes stash_push prints a message and does nothing."""
        git_dir = repo / ".git"
        _commit(repo, {"f.txt": "v1"})

        stash_push(git_dir, repo)

        assert _stack_read(git_dir) == []
        out = capsys.readouterr().out
        assert "No local changes" in out

    def test_push_requires_initial_commit(self, repo):
        git_dir = repo / ".git"
        (repo / "f.txt").write_text("x")
        add(git_dir, repo, ["f.txt"])
        with pytest.raises(ValueError, match="initial commit"):
            stash_push(git_dir, repo)

    def test_stash_commit_has_two_parents(self, repo):
        """Stash commit must have [HEAD, index_commit] as parents."""
        git_dir = repo / ".git"
        head_sha = _commit(repo, {"f.txt": "v1"})
        (repo / "f.txt").write_text("v2")
        add(git_dir, repo, ["f.txt"])

        stash_push(git_dir, repo)

        stash_sha = _stack_read(git_dir)[0]
        sc = read_commit(git_dir, stash_sha)
        assert len(sc.parents) == 2
        assert sc.parents[0] == head_sha

    def test_index_commit_message(self, repo):
        """parent[1] of stash commit must say 'index on'."""
        git_dir = repo / ".git"
        _commit(repo, {"f.txt": "v1"})
        (repo / "f.txt").write_text("v2")
        add(git_dir, repo, ["f.txt"])

        stash_push(git_dir, repo)

        stash_sha = _stack_read(git_dir)[0]
        sc = read_commit(git_dir, stash_sha)
        ic = read_commit(git_dir, sc.parents[1])
        assert ic.message.startswith("index on")


# ---------------------------------------------------------------------------
# stash pop
# ---------------------------------------------------------------------------

class TestStashPop:

    def test_pop_restores_staged_change_to_disk(self, repo):
        git_dir = repo / ".git"
        _commit(repo, {"f.txt": "v1"})
        (repo / "f.txt").write_text("v2")
        add(git_dir, repo, ["f.txt"])
        stash_push(git_dir, repo)
        assert _disk(repo, "f.txt") == "v1"  # HEAD restored

        stash_pop(git_dir, repo)

        assert _disk(repo, "f.txt") == "v2"

    def test_pop_restores_staged_sha_to_index(self, repo):
        """After pop, the index should carry the stashed blob SHA."""
        from pygitlib.objects import hash_object
        git_dir = repo / ".git"
        _commit(repo, {"f.txt": "v1"})
        (repo / "f.txt").write_text("v2")
        add(git_dir, repo, ["f.txt"])
        v2_sha = hash_object(git_dir, b"v2", write=False)
        stash_push(git_dir, repo)

        stash_pop(git_dir, repo)

        assert _index_sha(repo, "f.txt") == v2_sha

    def test_pop_restores_unstaged_change(self, repo):
        """Unstaged modification: disk has new content, index has old."""
        from pygitlib.objects import hash_object
        git_dir = repo / ".git"
        _commit(repo, {"f.txt": "v1"})
        v1_sha = hash_object(git_dir, b"v1", write=False)

        (repo / "f.txt").write_text("v2")  # NOT staged
        stash_push(git_dir, repo)

        stash_pop(git_dir, repo)

        # Disk should have v2 (from worktree snapshot)
        assert _disk(repo, "f.txt") == "v2"
        # Index should have v1 (unchanged at stash time)
        assert _index_sha(repo, "f.txt") == v1_sha

    def test_pop_restores_new_staged_file(self, repo):
        """A new file that was staged must come back after pop."""
        git_dir = repo / ".git"
        _commit(repo, {"base.txt": "base"})
        (repo / "new.txt").write_text("brand new")
        add(git_dir, repo, ["new.txt"])
        stash_push(git_dir, repo)
        assert not (repo / "new.txt").exists()

        stash_pop(git_dir, repo)

        assert (repo / "new.txt").read_text() == "brand new"
        assert _index_sha(repo, "new.txt") is not None

    def test_pop_removes_entry_from_stack(self, repo):
        git_dir = repo / ".git"
        _commit(repo, {"f.txt": "v1"})
        (repo / "f.txt").write_text("v2")
        add(git_dir, repo, ["f.txt"])
        stash_push(git_dir, repo)
        assert len(_stack_read(git_dir)) == 1

        stash_pop(git_dir, repo)

        assert _stack_read(git_dir) == []

    def test_pop_empty_stack_raises(self, repo):
        git_dir = repo / ".git"
        with pytest.raises(ValueError, match="No stash"):
            stash_pop(git_dir, repo)

    def test_pop_nested_file(self, repo):
        git_dir = repo / ".git"
        _commit(repo, {"src/main.py": "print('v1')"})
        (repo / "src" / "main.py").write_text("print('v2')")
        add(git_dir, repo, ["src/main.py"])
        stash_push(git_dir, repo)

        stash_pop(git_dir, repo)

        assert (repo / "src" / "main.py").read_text() == "print('v2')"


# ---------------------------------------------------------------------------
# Stash stack (multiple entries)
# ---------------------------------------------------------------------------

class TestStashStack:

    def test_two_pushes_two_entries(self, repo):
        git_dir = repo / ".git"
        _commit(repo, {"f.txt": "v1"})

        (repo / "f.txt").write_text("v2")
        add(git_dir, repo, ["f.txt"])
        stash_push(git_dir, repo)

        (repo / "f.txt").write_text("v3")
        add(git_dir, repo, ["f.txt"])
        stash_push(git_dir, repo)

        assert len(_stack_read(git_dir)) == 2

    def test_lifo_order(self, repo):
        """Pop must restore the MOST RECENT stash first."""
        git_dir = repo / ".git"
        _commit(repo, {"f.txt": "v1"})

        (repo / "f.txt").write_text("second")
        add(git_dir, repo, ["f.txt"])
        stash_push(git_dir, repo)       # stash@{1}

        (repo / "f.txt").write_text("first")
        add(git_dir, repo, ["f.txt"])
        stash_push(git_dir, repo)       # stash@{0}

        stash_pop(git_dir, repo)        # pops stash@{0} → restores "first"
        assert _disk(repo, "f.txt") == "first"
        assert len(_stack_read(git_dir)) == 1

        # Stage the restored file so we can stash again to check
        add(git_dir, repo, ["f.txt"])
        stash_push(git_dir, repo)
        stash_pop(git_dir, repo)
        assert len(_stack_read(git_dir)) == 1  # the original stash@{1} remains

    def test_stack_empty_after_all_pops(self, repo):
        git_dir = repo / ".git"
        _commit(repo, {"f.txt": "v1"})

        for v in ("v2", "v3"):
            (repo / "f.txt").write_text(v)
            add(git_dir, repo, ["f.txt"])
            stash_push(git_dir, repo)

        stash_pop(git_dir, repo)
        stash_pop(git_dir, repo)
        assert _stack_read(git_dir) == []


# ---------------------------------------------------------------------------
# stash list
# ---------------------------------------------------------------------------

class TestStashList:

    def test_empty_list(self, repo):
        assert stash_list(repo / ".git") == []

    def test_list_after_one_push(self, repo):
        git_dir = repo / ".git"
        _commit(repo, {"f.txt": "v1"})
        (repo / "f.txt").write_text("v2")
        add(git_dir, repo, ["f.txt"])
        stash_push(git_dir, repo)

        entries = stash_list(git_dir)
        assert len(entries) == 1
        assert entries[0][0] == 0
        assert "WIP on" in entries[0][1]

    def test_list_newest_first(self, repo):
        git_dir = repo / ".git"
        _commit(repo, {"f.txt": "v1"})

        (repo / "f.txt").write_text("v2")
        add(git_dir, repo, ["f.txt"])
        stash_push(git_dir, repo)   # older

        (repo / "f.txt").write_text("v3")
        add(git_dir, repo, ["f.txt"])
        stash_push(git_dir, repo)   # newer

        entries = stash_list(git_dir)
        assert len(entries) == 2
        # stash@{0} must be the most recent entry
        assert entries[0][0] == 0
        assert entries[1][0] == 1


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------

class TestStashIntegration:

    def test_stash_and_pop_full_cycle(self, repo):
        """
        Modify → stash → working tree is clean → pop → modifications back.
        """
        git_dir = repo / ".git"
        _commit(repo, {"f.txt": "original"})

        (repo / "f.txt").write_text("work in progress")
        add(git_dir, repo, ["f.txt"])

        # After stash: clean
        stash_push(git_dir, repo)
        st = status(git_dir, repo)
        assert not any(st["staged"][k]   for k in st["staged"])
        assert not any(st["unstaged"][k] for k in st["unstaged"])

        # After pop: modification is back as staged
        stash_pop(git_dir, repo)
        st = status(git_dir, repo)
        assert "f.txt" in st["staged"]["modified"]

    def test_stash_unstaged_shows_unstaged_after_pop(self, repo):
        """Unstaged change: after pop it must still be unstaged."""
        git_dir = repo / ".git"
        _commit(repo, {"f.txt": "original"})

        (repo / "f.txt").write_text("work in progress")  # NOT staged

        stash_push(git_dir, repo)
        st = status(git_dir, repo)
        assert not any(st["unstaged"][k] for k in st["unstaged"])

        stash_pop(git_dir, repo)
        st = status(git_dir, repo)
        assert "f.txt" in st["unstaged"]["modified"]
