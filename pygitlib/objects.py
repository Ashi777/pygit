"""
pygit/objects.py

Git object model — blob, tree, commit.
All objects are stored identically to real Git:
  - Header: "<type> <size>\0<content>"
  - Compressed with zlib
  - Named by SHA-1 of the uncompressed data
  - Stored at .git/objects/<first-2-hex>/<remaining-38-hex>

You can cross-check every hash with real git:
  $ git hash-object <file>          # should match our hash_object()
  $ git cat-file -p <hash>          # should match our read_object()
"""

import hashlib
import zlib
import os
import stat
import struct
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_git_dir(path: str = ".") -> Path:
    """Walk up directories to find .git — like real Git does."""
    current = Path(path).resolve()
    for directory in [current, *current.parents]:
        git_dir = directory / ".git"
        if git_dir.is_dir():
            return git_dir
    raise FileNotFoundError("Not a git repository (no .git directory found)")


def object_path(git_dir: Path, sha: str) -> Path:
    """Return the file path for a given SHA-1 hash."""
    return git_dir / "objects" / sha[:2] / sha[2:]


# ---------------------------------------------------------------------------
# Core: write any object
# ---------------------------------------------------------------------------

def write_object(git_dir: Path, obj_type: str, data: bytes) -> str:
    """
    Store a Git object and return its SHA-1 hash.

    Git object format (before compression):
        "<type> <byte-length>\x00<raw-content>"

    Example for a blob containing "hello\n":
        b"blob 6\x00hello\n"

    This is then zlib-compressed and written to disk.
    The SHA-1 is computed over the UNCOMPRESSED header+content.
    """
    # 1. Build the header
    header = f"{obj_type} {len(data)}\x00".encode()

    # 2. Full store content = header + raw data
    store = header + data

    # 3. SHA-1 hash of the full store content (not the compressed bytes)
    sha = hashlib.sha1(store).hexdigest()

    # 4. Compress with zlib (level 1 = fast, matches git's default)
    compressed = zlib.compress(store, level=1)

    # 5. Write to .git/objects/<xx>/<xxxxxx...>
    path = object_path(git_dir, sha)
    if not path.exists():            # Git skips writing if object exists
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(compressed)

    return sha


# ---------------------------------------------------------------------------
# Core: read any object
# ---------------------------------------------------------------------------

def read_object(git_dir: Path, sha: str) -> tuple[str, bytes]:
    """
    Read a Git object by SHA-1. Returns (type, raw_data).

    Checks the loose object store first; falls back to pack files so that
    repositories compacted with `git gc` (or received via clone) work
    transparently.

    Raises ValueError for a malformed object.
    Raises FileNotFoundError if the object is not found anywhere.
    """
    path = object_path(git_dir, sha)
    if not path.exists():
        from .pack import read_packed_object
        return read_packed_object(git_dir, sha)
    compressed = path.read_bytes()

    # Decompress
    raw = zlib.decompress(compressed)

    # Split on the null byte that separates header from content
    null_pos = raw.index(b"\x00")
    header = raw[:null_pos].decode()
    data = raw[null_pos + 1:]

    # Parse header: "blob 42" or "tree 89" or "commit 230"
    obj_type, size_str = header.split(" ", 1)
    size = int(size_str)

    if size != len(data):
        raise ValueError(
            f"Object {sha}: header claims {size} bytes but got {len(data)}"
        )

    return obj_type, data


# ---------------------------------------------------------------------------
# Blob objects
# ---------------------------------------------------------------------------

def hash_object(git_dir: Path, data: bytes, write: bool = True) -> str:
    """
    Hash (and optionally store) a blob object.

    Equivalent to:  git hash-object [-w] <file>

    Args:
        git_dir: path to the .git directory
        data:    raw file content as bytes
        write:   if False, compute the hash without writing to disk
                 (git hash-object without -w)
    """
    if write:
        return write_object(git_dir, "blob", data)
    else:
        header = f"blob {len(data)}\x00".encode()
        return hashlib.sha1(header + data).hexdigest()


def cat_file(git_dir: Path, sha: str) -> tuple[str, bytes]:
    """
    Read and return an object. Equivalent to: git cat-file -p <sha>
    """
    return read_object(git_dir, sha)


# ---------------------------------------------------------------------------
# Tree objects
# ---------------------------------------------------------------------------

@dataclass
class TreeEntry:
    """One entry in a Git tree (a file or subdirectory)."""
    mode: str        # "100644" = regular file, "100755" = executable,
                     # "040000" = directory, "120000" = symlink
    name: str        # filename (no path separators)
    sha: str         # 40-char hex SHA-1 of the referenced blob or tree


def encode_tree(entries: list[TreeEntry]) -> bytes:
    """
    Serialize a list of TreeEntry objects into Git's binary tree format.

    Each entry in the binary format:
        "<mode> <name>\x00<20-byte-binary-sha>"

    Entries MUST be sorted by name (Git requires this).
    Directories sort as if they have a trailing slash.
    """
    # Sort entries: directories (040000) sort with a trailing slash
    def sort_key(e: TreeEntry) -> str:
        return e.name + ("/" if e.mode in ("040000", "40000") else "")

    sorted_entries = sorted(entries, key=sort_key)

    parts = []
    for entry in sorted_entries:
        # Mode + space + name + null byte
        header = f"{entry.mode} {entry.name}\x00".encode()
        # SHA-1 as 20 raw bytes (not hex string)
        sha_bytes = bytes.fromhex(entry.sha)
        parts.append(header + sha_bytes)

    return b"".join(parts)


def decode_tree(data: bytes) -> list[TreeEntry]:
    """
    Deserialize Git's binary tree format back into TreeEntry objects.
    Used by read_tree / ls-tree.
    """
    entries = []
    i = 0
    while i < len(data):
        # Find the space separating mode from name
        space_pos = data.index(b" ", i)
        mode = data[i:space_pos].decode()

        # Find the null byte after the name
        null_pos = data.index(b"\x00", space_pos)
        name = data[space_pos + 1:null_pos].decode()

        # Read 20 bytes of binary SHA-1 and convert to hex
        sha_bytes = data[null_pos + 1: null_pos + 21]
        sha = sha_bytes.hex()

        entries.append(TreeEntry(mode=mode, name=name, sha=sha))
        i = null_pos + 21

    return entries


def write_tree(git_dir: Path, entries: list[TreeEntry]) -> str:
    """
    Write a tree object. Returns the SHA-1.
    Equivalent to: git write-tree
    """
    return write_object(git_dir, "tree", encode_tree(entries))


def read_tree(git_dir: Path, sha: str) -> list[TreeEntry]:
    """
    Read a tree object and return its entries.
    Equivalent to: git ls-tree <sha>
    """
    obj_type, data = read_object(git_dir, sha)
    if obj_type != "tree":
        raise ValueError(f"Object {sha} is a {obj_type}, not a tree")
    return decode_tree(data)


# ---------------------------------------------------------------------------
# Commit objects
# ---------------------------------------------------------------------------

@dataclass
class Commit:
    tree: str                        # SHA-1 of the root tree
    author: str                      # "Name <email> timestamp timezone"
    committer: str                   # usually same as author
    message: str                     # commit message
    parents: list[str] = field(default_factory=list)  # parent commit SHA-1s


def encode_commit(commit: Commit) -> bytes:
    """
    Serialize a Commit into Git's text-based commit format.

    Format:
        tree <sha>\n
        parent <sha>\n          (zero or more)
        author <identity>\n
        committer <identity>\n
        \n
        <message>\n
    """
    lines = [f"tree {commit.tree}"]
    for parent in commit.parents:
        lines.append(f"parent {parent}")
    lines.append(f"author {commit.author}")
    lines.append(f"committer {commit.committer}")
    lines.append("")              # blank line before message
    lines.append(commit.message)
    return "\n".join(lines).encode()


def decode_commit(data: bytes) -> Commit:
    """Parse Git's commit format back into a Commit object."""
    text = data.decode()
    lines = text.split("\n")

    tree = ""
    parents = []
    author = ""
    committer = ""
    message_lines = []
    reading_message = False

    for line in lines:
        if reading_message:
            message_lines.append(line)
        elif line == "":
            reading_message = True
        elif line.startswith("tree "):
            tree = line[5:]
        elif line.startswith("parent "):
            parents.append(line[7:])
        elif line.startswith("author "):
            author = line[7:]
        elif line.startswith("committer "):
            committer = line[10:]

    return Commit(
        tree=tree,
        parents=parents,
        author=author,
        committer=committer,
        message="\n".join(message_lines).strip(),
    )


def write_commit(git_dir: Path, commit: Commit) -> str:
    """Write a commit object and return its SHA-1."""
    return write_object(git_dir, "commit", encode_commit(commit))


def read_commit(git_dir: Path, sha: str) -> Commit:
    """Read and parse a commit object."""
    obj_type, data = read_object(git_dir, sha)
    if obj_type != "commit":
        raise ValueError(f"Object {sha} is a {obj_type}, not a commit")
    return decode_commit(data)
