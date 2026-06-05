"""
tests/test_tag.py

Tests for pygit tag — lightweight and annotated tags.

Test structure
──────────────
  TestTagObjectEncoding  — Tag encode/decode round-trip
  TestListTags           — list_tags() returns sorted names
  TestLightweightTag     — create a ref-only tag
  TestAnnotatedTag       — create a tag object + ref
  TestDeleteTag          — delete_tag() removes the ref
  TestResolveTag         — resolve_tag() dereferences to commit SHA
  TestTagGcIntegration   — tagged commits survive gc --prune

Run with:  pytest tests/ -v
"""

import subprocess
from pathlib import Path
import pytest
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from pygitlib.tag import (
    Tag, encode_tag, decode_tag,
    list_tags, create_tag, delete_tag, resolve_tag,
)
from pygitlib.objects import read_object, object_path
from pygitlib.index import add


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


# ---------------------------------------------------------------------------
# Tag object encode / decode
# ---------------------------------------------------------------------------

class TestTagObjectEncoding:

    def _sample_tag(self) -> Tag:
        return Tag(
            obj_sha  = "a" * 40,
            obj_type = "commit",
            name     = "v1.0",
            tagger   = "T <t@t.dev> 1700000000 +0000",
            message  = "Release 1.0",
        )

    def test_round_trip(self):
        t = self._sample_tag()
        decoded = decode_tag(encode_tag(t))
        assert decoded.obj_sha  == t.obj_sha
        assert decoded.obj_type == t.obj_type
        assert decoded.name     == t.name
        assert decoded.tagger   == t.tagger
        assert decoded.message  == t.message

    def test_encoded_contains_object_line(self):
        t = self._sample_tag()
        raw = encode_tag(t).decode()
        assert f"object {t.obj_sha}" in raw

    def test_encoded_contains_type_line(self):
        t = self._sample_tag()
        raw = encode_tag(t).decode()
        assert "type commit" in raw

    def test_encoded_contains_tag_line(self):
        t = self._sample_tag()
        raw = encode_tag(t).decode()
        assert f"tag {t.name}" in raw

    def test_encoded_contains_tagger_line(self):
        t = self._sample_tag()
        raw = encode_tag(t).decode()
        assert f"tagger {t.tagger}" in raw

    def test_encoded_contains_message(self):
        t = self._sample_tag()
        raw = encode_tag(t).decode()
        assert t.message in raw

    def test_multiline_message_round_trip(self):
        t = self._sample_tag()
        t.message = "First line.\n\nThird line."
        decoded = decode_tag(encode_tag(t))
        assert decoded.message == t.message


# ---------------------------------------------------------------------------
# list_tags
# ---------------------------------------------------------------------------

class TestListTags:

    def test_empty_list_when_no_tags(self, repo):
        assert list_tags(repo / ".git") == []

    def test_returns_sorted_names(self, repo):
        git_dir = repo / ".git"
        _commit(repo, {"f.txt": "v"})
        create_tag(git_dir, "v1.2")
        create_tag(git_dir, "v1.0")
        create_tag(git_dir, "v1.1")
        assert list_tags(git_dir) == ["v1.0", "v1.1", "v1.2"]

    def test_includes_annotated_tags(self, repo):
        git_dir = repo / ".git"
        _commit(repo, {"f.txt": "v"})
        create_tag(git_dir, "light")
        create_tag(git_dir, "heavy", message="annotated")
        names = list_tags(git_dir)
        assert "light" in names
        assert "heavy" in names


# ---------------------------------------------------------------------------
# Lightweight tags
# ---------------------------------------------------------------------------

class TestLightweightTag:

    def test_creates_ref_file(self, repo):
        git_dir = repo / ".git"
        _commit(repo, {"f.txt": "v1"})
        create_tag(git_dir, "v1.0")
        assert (git_dir / "refs" / "tags" / "v1.0").exists()

    def test_ref_points_to_commit(self, repo):
        git_dir = repo / ".git"
        commit_sha = _commit(repo, {"f.txt": "v1"})
        create_tag(git_dir, "v1.0")
        tag_sha = (git_dir / "refs" / "tags" / "v1.0").read_text().strip()
        assert tag_sha == commit_sha

    def test_no_object_written(self, repo):
        """A lightweight tag must not create any new object in the store."""
        from pygitlib.gc import find_loose_objects
        git_dir = repo / ".git"
        _commit(repo, {"f.txt": "v1"})
        before = set(find_loose_objects(git_dir))
        create_tag(git_dir, "light")
        after = set(find_loose_objects(git_dir))
        assert before == after

    def test_tag_specific_commit(self, repo):
        git_dir = repo / ".git"
        sha1 = _commit(repo, {"f.txt": "v1"}, "first")
        _commit(repo, {"f.txt": "v2"}, "second")
        create_tag(git_dir, "old", target=sha1)
        stored = (git_dir / "refs" / "tags" / "old").read_text().strip()
        assert stored == sha1

    def test_duplicate_name_raises(self, repo):
        git_dir = repo / ".git"
        _commit(repo, {"f.txt": "v1"})
        create_tag(git_dir, "v1.0")
        with pytest.raises(ValueError, match="already exists"):
            create_tag(git_dir, "v1.0")

    def test_invalid_name_raises(self, repo):
        git_dir = repo / ".git"
        _commit(repo, {"f.txt": "v"})
        with pytest.raises(ValueError):
            create_tag(git_dir, "")
        with pytest.raises(ValueError):
            create_tag(git_dir, "a..b")
        with pytest.raises(ValueError):
            create_tag(git_dir, "-bad")

    def test_no_commit_raises(self, repo):
        git_dir = repo / ".git"
        with pytest.raises(ValueError):
            create_tag(git_dir, "v1.0")


# ---------------------------------------------------------------------------
# Annotated tags
# ---------------------------------------------------------------------------

class TestAnnotatedTag:

    def test_ref_points_to_tag_object_not_commit(self, repo):
        git_dir = repo / ".git"
        commit_sha = _commit(repo, {"f.txt": "v1"})
        create_tag(git_dir, "v1.0", message="Release 1.0")
        tag_sha = (git_dir / "refs" / "tags" / "v1.0").read_text().strip()
        # The ref must NOT point directly to the commit
        assert tag_sha != commit_sha

    def test_tag_object_is_stored(self, repo):
        git_dir = repo / ".git"
        _commit(repo, {"f.txt": "v1"})
        tag_sha = create_tag(git_dir, "v1.0", message="Release 1.0")
        assert object_path(git_dir, tag_sha).exists()

    def test_tag_object_type_is_tag(self, repo):
        git_dir = repo / ".git"
        _commit(repo, {"f.txt": "v1"})
        tag_sha = create_tag(git_dir, "v1.0", message="Release")
        obj_type, _ = read_object(git_dir, tag_sha)
        assert obj_type == "tag"

    def test_tag_object_contains_commit_sha(self, repo):
        git_dir = repo / ".git"
        commit_sha = _commit(repo, {"f.txt": "v1"})
        tag_sha = create_tag(git_dir, "v1.0", message="Release")
        _, data = read_object(git_dir, tag_sha)
        parsed = decode_tag(data)
        assert parsed.obj_sha == commit_sha

    def test_tag_object_contains_message(self, repo):
        git_dir = repo / ".git"
        _commit(repo, {"f.txt": "v1"})
        create_tag(git_dir, "v1.0", message="My release message")
        tag_sha = (git_dir / "refs" / "tags" / "v1.0").read_text().strip()
        _, data = read_object(git_dir, tag_sha)
        parsed = decode_tag(data)
        assert parsed.message == "My release message"

    def test_tag_object_name_matches(self, repo):
        git_dir = repo / ".git"
        _commit(repo, {"f.txt": "v1"})
        create_tag(git_dir, "v1.0", message="msg")
        tag_sha = (git_dir / "refs" / "tags" / "v1.0").read_text().strip()
        _, data = read_object(git_dir, tag_sha)
        parsed = decode_tag(data)
        assert parsed.name == "v1.0"

    def test_tagger_identity_recorded(self, repo):
        git_dir = repo / ".git"
        _commit(repo, {"f.txt": "v1"})
        create_tag(git_dir, "v1.0", message="msg")
        tag_sha = (git_dir / "refs" / "tags" / "v1.0").read_text().strip()
        _, data = read_object(git_dir, tag_sha)
        parsed = decode_tag(data)
        assert "t@t.dev" in parsed.tagger


# ---------------------------------------------------------------------------
# delete_tag
# ---------------------------------------------------------------------------

class TestDeleteTag:

    def test_deletes_ref_file(self, repo):
        git_dir = repo / ".git"
        _commit(repo, {"f.txt": "v1"})
        create_tag(git_dir, "v1.0")
        delete_tag(git_dir, "v1.0")
        assert not (git_dir / "refs" / "tags" / "v1.0").exists()

    def test_tag_not_in_list_after_delete(self, repo):
        git_dir = repo / ".git"
        _commit(repo, {"f.txt": "v1"})
        create_tag(git_dir, "v1.0")
        delete_tag(git_dir, "v1.0")
        assert "v1.0" not in list_tags(git_dir)

    def test_delete_nonexistent_raises(self, repo):
        with pytest.raises(ValueError, match="not found"):
            delete_tag(repo / ".git", "nonexistent")

    def test_delete_annotated_removes_ref_not_object(self, repo):
        """Deleting an annotated tag removes the ref but not the tag object."""
        git_dir = repo / ".git"
        _commit(repo, {"f.txt": "v1"})
        tag_sha = create_tag(git_dir, "v1.0", message="msg")
        delete_tag(git_dir, "v1.0")
        assert not (git_dir / "refs" / "tags" / "v1.0").exists()
        # Object remains (same as real git — gc would clean it later)
        assert object_path(git_dir, tag_sha).exists()


# ---------------------------------------------------------------------------
# resolve_tag
# ---------------------------------------------------------------------------

class TestResolveTag:

    def test_lightweight_returns_commit_sha(self, repo):
        git_dir = repo / ".git"
        commit_sha = _commit(repo, {"f.txt": "v1"})
        create_tag(git_dir, "light")
        sha, msg = resolve_tag(git_dir, "light")
        assert sha == commit_sha
        assert msg is None

    def test_annotated_returns_commit_sha(self, repo):
        git_dir = repo / ".git"
        commit_sha = _commit(repo, {"f.txt": "v1"})
        create_tag(git_dir, "heavy", message="annotated")
        sha, msg = resolve_tag(git_dir, "heavy")
        assert sha == commit_sha

    def test_annotated_returns_message(self, repo):
        git_dir = repo / ".git"
        _commit(repo, {"f.txt": "v1"})
        create_tag(git_dir, "heavy", message="Release 2.0")
        _, msg = resolve_tag(git_dir, "heavy")
        assert msg == "Release 2.0"

    def test_nonexistent_tag_raises(self, repo):
        with pytest.raises(ValueError, match="not found"):
            resolve_tag(repo / ".git", "nosuchtag")


# ---------------------------------------------------------------------------
# GC integration — tagged objects must survive pruning
# ---------------------------------------------------------------------------

class TestTagGcIntegration:

    def test_lightweight_tagged_commit_survives_prune(self, repo):
        """A commit only reachable via a lightweight tag must not be deleted."""
        from pygitlib.gc import run_gc
        git_dir = repo / ".git"
        commit_sha = _commit(repo, {"f.txt": "v1"})
        create_tag(git_dir, "v1.0")

        result = run_gc(git_dir, prune=True)
        assert commit_sha not in result.get("unreachable_shas", [])
        assert object_path(git_dir, commit_sha).exists()

    def test_annotated_tag_object_survives_prune(self, repo):
        """The tag object itself must be considered reachable (not pruned)."""
        from pygitlib.gc import run_gc
        git_dir = repo / ".git"
        _commit(repo, {"f.txt": "v1"})
        tag_sha = create_tag(git_dir, "v1.0", message="Release")

        result = run_gc(git_dir, prune=True)
        assert tag_sha not in result.get("unreachable_shas", [])
        assert object_path(git_dir, tag_sha).exists()

    def test_annotated_tagged_commit_survives_prune(self, repo):
        """
        A commit reachable ONLY via an annotated tag must survive.
        (Tests the gc.py fix that follows tag objects → commit objects.)
        """
        from pygitlib.gc import run_gc, find_reachable
        from pygitlib.branch import update_branch_ref, resolve_ref
        git_dir = repo / ".git"

        # Commit 1: will be tagged
        sha1 = _commit(repo, {"f.txt": "v1"}, "tagged commit")
        # Commit 2: advances main branch
        sha2 = _commit(repo, {"f.txt": "v2"}, "later commit")

        # Create annotated tag pointing at the OLD commit
        create_tag(git_dir, "v1.0", target=sha1, message="Release 1.0")

        reachable = find_reachable(git_dir)
        assert sha1 in reachable, "Old commit must be reachable via annotated tag"
