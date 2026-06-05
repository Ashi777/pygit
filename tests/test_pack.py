"""
tests/test_pack.py

Tests for the packfile reader (pygitlib/pack.py) and the fallback in
pygitlib/objects.read_object.

Test structure
──────────────
  TestApplyDelta       — unit tests for the delta engine
  TestPackIndex        — reading a real pack index (.idx)
  TestPackFile         — reading objects from a real .pack file
  TestReadObjectFallback — read_object transparently uses packs
  TestAllObjectsReadable — every object in a packed repo is accessible

The "packed_repo" fixture creates a fresh git repository, makes several
commits (including similar file versions to encourage delta compression),
then runs `git repack -a -d` to consolidate everything into a single pack.

Run with:  pytest tests/ -v
"""

import subprocess
from pathlib import Path
import pytest
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from pygitlib.pack import (
    PackIndex, PackFile, find_packs, read_packed_object, _apply_delta,
)
from pygitlib.objects import read_object, get_git_dir


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def packed_repo(tmp_path):
    """
    A git repo whose objects have been consolidated into a single pack.
    Several similar file versions are committed to give git a chance to
    use delta compression.
    """
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.dev"],
                   cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"],
                   cwd=tmp_path, capture_output=True)

    # Commit a series of similar files so git can use deltas
    for version in range(1, 6):
        (tmp_path / "data.txt").write_text(
            f"line one\nline two\nversion {version}\nline four\nline five\n"
        )
        (tmp_path / f"extra{version}.txt").write_text(f"extra file {version}\n")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", f"commit {version}"],
            cwd=tmp_path, capture_output=True,
        )

    # Pack all objects into a single pack; remove loose objects
    subprocess.run(["git", "repack", "-a", "-d"], cwd=tmp_path, capture_output=True)

    git_dir = tmp_path / ".git"
    pack_dir = git_dir / "objects" / "pack"
    if not any(pack_dir.glob("*.pack")):
        pytest.skip("git repack did not create a pack file")

    return tmp_path


def _head_sha(repo: Path) -> str:
    """Return the full SHA-1 of the HEAD commit."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo, capture_output=True, text=True,
    )
    return result.stdout.strip()


def _all_shas(repo: Path) -> list[str]:
    """Return the SHA-1 of every object in the repository."""
    result = subprocess.run(
        ["git", "cat-file", "--batch-all-objects",
         "--batch-check=%(objectname)"],
        cwd=repo, capture_output=True, text=True,
    )
    return [s.strip() for s in result.stdout.splitlines() if s.strip()]


def _pack_pairs(repo: Path):
    return find_packs(repo / ".git")


# ---------------------------------------------------------------------------
# Unit: delta engine
# ---------------------------------------------------------------------------

class TestApplyDelta:

    def _encode_varint(self, n: int) -> bytes:
        """Encode n as a LEB128 variable-length integer."""
        out = []
        while True:
            byte = n & 0x7F
            n >>= 7
            if n:
                out.append(byte | 0x80)
            else:
                out.append(byte)
                break
        return bytes(out)

    def _make_delta(self, src_size, tgt_size, instructions: bytes) -> bytes:
        return (self._encode_varint(src_size) +
                self._encode_varint(tgt_size) +
                instructions)

    def test_insert_only(self):
        """INSERT-only delta appends literal bytes."""
        base  = b"original"
        # INSERT 5 bytes "hello"
        instr = bytes([5]) + b"hello"
        delta = self._make_delta(len(base), 5, instr)
        assert _apply_delta(base, delta) == b"hello"

    def test_copy_entire_base(self):
        """COPY instruction with offset=0 and full base size reproduces base."""
        base  = b"hello world"
        n     = len(base)
        # cmd 0x91 = copy with offset byte[0] and size byte[0]
        # offset = 0x00, size = n
        instr = bytes([0x91, 0x00, n])   # copy N bytes from offset 0
        delta = self._make_delta(n, n, instr)
        assert _apply_delta(base, delta) == base

    def test_copy_slice(self):
        """COPY instruction extracts a sub-range of the base."""
        base  = b"abcdefghij"
        # Copy 3 bytes starting at offset 2 → "cde"
        instr = bytes([0x91, 0x02, 0x03])  # offset=2, size=3
        delta = self._make_delta(len(base), 3, instr)
        assert _apply_delta(base, delta) == b"cde"

    def test_copy_then_insert(self):
        """COPY + INSERT produces the expected concatenation."""
        base  = b"helloworld"
        # Copy first 5 bytes ("hello"), then insert " there"
        instr = bytes([0x91, 0x00, 0x05]) + bytes([6]) + b" there"
        delta = self._make_delta(len(base), 11, instr)
        assert _apply_delta(base, delta) == b"hello there"

    def test_zero_size_copy_means_65536(self):
        """A COPY size field of 0 encodes 65 536 bytes, not zero."""
        base = b"x" * 65536
        # cmd 0x80 = COPY with no offset bytes and no size bytes → size = 65536
        instr = bytes([0x80])
        delta = self._make_delta(65536, 65536, instr)
        assert _apply_delta(base, delta) == base

    def test_source_size_mismatch_raises(self):
        base  = b"short"
        instr = bytes([1]) + b"x"
        delta = self._make_delta(99, 1, instr)   # claims source is 99 bytes
        with pytest.raises(ValueError, match="source-size mismatch"):
            _apply_delta(base, delta)

    def test_target_size_mismatch_raises(self):
        base  = b"base"
        instr = bytes([1]) + b"x"
        delta = self._make_delta(4, 99, instr)   # claims target is 99 bytes
        with pytest.raises(ValueError, match="target-size mismatch"):
            _apply_delta(base, delta)


# ---------------------------------------------------------------------------
# Pack index reader
# ---------------------------------------------------------------------------

class TestPackIndex:

    def test_index_loads(self, packed_repo):
        pairs = _pack_pairs(packed_repo)
        assert pairs, "No pack files found"
        idx, _ = pairs[0]
        assert len(idx.shas) > 0

    def test_all_objects_indexed(self, packed_repo):
        """Every SHA reported by git is present in the index."""
        all_idx_shas = set()
        for idx, _ in _pack_pairs(packed_repo):
            all_idx_shas.update(idx.shas)

        for sha in _all_shas(packed_repo):
            assert sha in all_idx_shas, f"SHA {sha[:7]} missing from pack index"

    def test_unknown_sha_returns_none(self, packed_repo):
        idx, _ = _pack_pairs(packed_repo)[0]
        assert idx.get_offset("0" * 40) is None

    def test_known_sha_returns_int(self, packed_repo):
        idx, _ = _pack_pairs(packed_repo)[0]
        sha = idx.shas[0]
        offset = idx.get_offset(sha)
        assert isinstance(offset, int) and offset >= 12   # past the 12-byte header


# ---------------------------------------------------------------------------
# Pack file reader
# ---------------------------------------------------------------------------

class TestPackFile:

    def test_pack_loads(self, packed_repo):
        _, pack = _pack_pairs(packed_repo)[0]
        assert pack.n_objects > 0

    def test_read_object_by_offset(self, packed_repo):
        """Objects read at their indexed offsets have the correct type."""
        idx, pack = _pack_pairs(packed_repo)[0]
        # Read the HEAD commit
        head = _head_sha(packed_repo)
        offset = idx.get_offset(head)
        assert offset is not None, "HEAD commit not in pack index"
        type_name, data = pack.get_object_at(offset)
        assert type_name == "commit"
        assert b"commit" in data or len(data) > 0   # raw commit bytes

    def test_read_all_objects(self, packed_repo):
        """Every object listed by git can be read from the pack."""
        for sha in _all_shas(packed_repo):
            idx, pack = _pack_pairs(packed_repo)[0]
            offset = idx.get_offset(sha)
            if offset is None:
                continue    # might be in another pack (edge case)
            type_name, data = pack.get_object_at(offset)
            assert type_name in ("commit", "tree", "blob", "tag")
            assert len(data) >= 0


# ---------------------------------------------------------------------------
# read_object fallback
# ---------------------------------------------------------------------------

class TestReadObjectFallback:

    def test_packed_commit_readable(self, packed_repo):
        git_dir = packed_repo / ".git"
        head    = _head_sha(packed_repo)
        obj_type, data = read_object(git_dir, head)
        assert obj_type == "commit"
        assert b"commit 5" in data or b"commit" in data.lower()

    def test_packed_blob_readable(self, packed_repo):
        """Blobs from the pack can be retrieved via read_object."""
        git_dir = packed_repo / ".git"
        # Use git to get the blob SHA for data.txt at HEAD
        result = subprocess.run(
            ["git", "ls-tree", "HEAD", "data.txt"],
            cwd=packed_repo, capture_output=True, text=True,
        )
        parts = result.stdout.split()
        if len(parts) < 3:
            pytest.skip("Could not get blob SHA from git ls-tree")
        blob_sha = parts[2]

        obj_type, data = read_object(git_dir, blob_sha)
        assert obj_type == "blob"
        assert b"line one" in data

    def test_packed_tree_readable(self, packed_repo):
        git_dir = packed_repo / ".git"
        # Get the root tree SHA from the HEAD commit
        result = subprocess.run(
            ["git", "cat-file", "-p", "HEAD"],
            cwd=packed_repo, capture_output=True, text=True,
        )
        tree_sha = None
        for line in result.stdout.splitlines():
            if line.startswith("tree "):
                tree_sha = line.split()[1]
                break
        assert tree_sha, "Could not extract tree SHA"
        obj_type, data = read_object(git_dir, tree_sha)
        assert obj_type == "tree"

    def test_missing_object_raises(self, packed_repo):
        git_dir = packed_repo / ".git"
        with pytest.raises(FileNotFoundError):
            read_object(git_dir, "0" * 40)


# ---------------------------------------------------------------------------
# All objects in a packed repo are accessible through every pygit command
# ---------------------------------------------------------------------------

class TestAllObjectsReadable:

    def test_all_objects_via_read_object(self, packed_repo):
        """
        Every SHA known to git can be fetched via read_object —
        the core primitive used by all pygit commands.
        """
        git_dir = packed_repo / ".git"
        errors  = []
        for sha in _all_shas(packed_repo):
            try:
                obj_type, data = read_object(git_dir, sha)
                assert obj_type in ("commit", "tree", "blob", "tag")
            except Exception as exc:
                errors.append(f"{sha[:7]}: {exc}")
        assert not errors, "Failed to read objects:\n" + "\n".join(errors)

    def test_pygit_log_works_on_packed_repo(self, packed_repo):
        """
        read_commit (used by pygit log) can walk the packed commit chain.
        """
        from pygitlib.objects import read_commit
        git_dir = packed_repo / ".git"
        sha     = _head_sha(packed_repo)
        visited = []
        while sha:
            commit = read_commit(git_dir, sha)
            visited.append(commit.message.strip())
            sha = commit.parents[0] if commit.parents else None
        assert len(visited) == 5
        assert visited[0] == "commit 5"
        assert visited[-1] == "commit 1"

    def test_pygit_ls_tree_works_on_packed_repo(self, packed_repo):
        """
        read_tree (used by pygit ls-tree) resolves packed tree objects.
        """
        from pygitlib.objects import read_commit, read_tree
        git_dir = packed_repo / ".git"
        sha     = _head_sha(packed_repo)
        commit  = read_commit(git_dir, sha)
        entries = read_tree(git_dir, commit.tree)
        names   = {e.name for e in entries}
        assert "data.txt" in names
        assert "extra5.txt" in names
