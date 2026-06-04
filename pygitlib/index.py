"""
pygitlib/index.py

Git index (staging area) — reads and writes the .git/index binary file.

Index binary format (version 2):
  Header:  b"DIRC" (4) + version=2 (4) + num_entries (4)
  Entries: N entries, each padded to an 8-byte boundary, sorted by path
    ctime_s  (4)  ctime_ns (4)
    mtime_s  (4)  mtime_ns (4)
    dev      (4)  ino      (4)
    mode     (4)  uid      (4)  gid (4)  size (4)
    sha1     (20 bytes, raw binary)
    flags    (2)  — lower 12 bits = name length (capped at 0xFFF)
    name     (variable, null-terminated, padded)
    Total entry size = (62 + len(name) + 8) & ~7
  Trailer: SHA-1 of all preceding bytes (20 bytes)

Cross-check with real git:
  $ git ls-files --stage       # lists every entry in the index
  $ git ls-files               # lists tracked file names
"""

import hashlib
import os
import struct
from dataclasses import dataclass
from pathlib import Path

from .objects import hash_object, read_commit, read_tree


INDEX_MAGIC = b"DIRC"
INDEX_VERSION = 2

# Directories to skip when walking the working tree
_SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache"}


@dataclass
class IndexEntry:
    """One entry in the .git/index file."""
    ctime_s: int     # file-status-change time: seconds
    ctime_ns: int    # file-status-change time: nanosecond fraction
    mtime_s: int     # last-modified time: seconds
    mtime_ns: int    # last-modified time: nanosecond fraction
    dev: int         # device number
    ino: int         # inode number
    mode: int        # file mode, e.g. 0o100644 (regular) or 0o100755 (executable)
    uid: int         # owner user-id  (0 on Windows)
    gid: int         # owner group-id (0 on Windows)
    size: int        # file size in bytes
    sha: str         # 40-char hex SHA-1 of the blob
    flags: int       # lower 12 bits = name length; upper bits are git internals
    path: str        # path relative to work tree, forward-slash separated


# ---------------------------------------------------------------------------
# Entry size calculation
# ---------------------------------------------------------------------------

def _entry_size(name_len: int) -> int:
    """
    Total size (in bytes) of one serialised index entry, including padding.

    Git formula: (fixed_62_bytes + name_len + 8) rounded down to multiple of 8.
    The "+8" guarantees at least one null byte after the name.
    """
    return (62 + name_len + 8) & ~7


# ---------------------------------------------------------------------------
# Read / Write
# ---------------------------------------------------------------------------

def read_index(git_dir: Path) -> list[IndexEntry]:
    """
    Parse .git/index and return all entries in sorted order.
    Returns an empty list if the index file does not exist yet.
    """
    index_path = git_dir / "index"
    if not index_path.exists():
        return []

    data = index_path.read_bytes()

    if len(data) < 12 or data[:4] != INDEX_MAGIC:
        raise ValueError("Corrupted index: bad magic bytes")

    version, num_entries = struct.unpack(">II", data[4:12])
    if version not in (2, 3):
        raise ValueError(f"Unsupported index version: {version}")

    entries = []
    pos = 12  # start right after the 12-byte header

    for _ in range(num_entries):
        if pos + 62 > len(data):
            raise ValueError("Corrupted index: entry truncated")

        # Unpack the 40 bytes of stat data
        (ctime_s, ctime_ns, mtime_s, mtime_ns,
         dev, ino, mode, uid, gid, size) = struct.unpack(
            ">IIIIIIIIII", data[pos:pos + 40]
        )

        sha = data[pos + 40:pos + 60].hex()
        flags = struct.unpack(">H", data[pos + 60:pos + 62])[0]

        # Name is null-terminated starting at pos+62
        null_pos = data.index(b"\x00", pos + 62)
        path = data[pos + 62:null_pos].decode()

        entries.append(IndexEntry(
            ctime_s=ctime_s, ctime_ns=ctime_ns,
            mtime_s=mtime_s, mtime_ns=mtime_ns,
            dev=dev, ino=ino, mode=mode,
            uid=uid, gid=gid, size=size,
            sha=sha, flags=flags, path=path,
        ))

        pos += _entry_size(len(path))

    return entries


def write_index(git_dir: Path, entries: list[IndexEntry]) -> None:
    """
    Serialise entries to .git/index in Git's binary format.

    Entries are sorted by path before writing (Git requires this).
    A SHA-1 checksum of the entire content is appended as a 20-byte trailer.
    """
    sorted_entries = sorted(entries, key=lambda e: e.path)

    parts = [INDEX_MAGIC, struct.pack(">II", INDEX_VERSION, len(sorted_entries))]

    for entry in sorted_entries:
        name_bytes = entry.path.encode()

        fixed = struct.pack(
            ">IIIIIIIIII",
            entry.ctime_s, entry.ctime_ns,
            entry.mtime_s, entry.mtime_ns,
            entry.dev, entry.ino,
            entry.mode, entry.uid, entry.gid, entry.size,
        )
        sha_bytes = bytes.fromhex(entry.sha)
        # Lower 12 bits of flags = name length (capped at 0xFFF for long paths)
        flags = struct.pack(">H", min(len(name_bytes), 0xFFF))

        # Name section: name + enough null bytes to reach the padded entry size
        name_section_len = _entry_size(len(name_bytes)) - 62
        name_section = name_bytes + b"\x00" * (name_section_len - len(name_bytes))

        parts.append(fixed + sha_bytes + flags + name_section)

    content = b"".join(parts)
    checksum = hashlib.sha1(content).digest()
    (git_dir / "index").write_bytes(content + checksum)


# ---------------------------------------------------------------------------
# git add
# ---------------------------------------------------------------------------

def add(git_dir: Path, work_dir: Path, paths: list[str]) -> None:
    """
    Stage one or more files into .git/index.
    Equivalent to: git add <file> [<file> ...]

    For each path:
      1. Hash the file content as a blob and store it in the object database.
      2. Read file metadata (stat).
      3. Insert or update the index entry.
    """
    entries = {e.path: e for e in read_index(git_dir)}

    for path_str in paths:
        norm = path_str.replace("\\", "/")
        file_path = work_dir / norm

        if not file_path.exists():
            raise FileNotFoundError(
                f"error: pathspec '{path_str}' did not match any files"
            )
        if not file_path.is_file():
            raise ValueError(f"'{path_str}' is a directory, not a file")

        data = file_path.read_bytes()
        sha = hash_object(git_dir, data, write=True)

        st = file_path.stat()
        ctime_ns_total = st.st_ctime_ns
        mtime_ns_total = st.st_mtime_ns
        # On Windows all files lack the execute bit; use 100644 for all.
        mode = 0o100755 if (st.st_mode & 0o111) else 0o100644

        entries[norm] = IndexEntry(
            ctime_s=ctime_ns_total // 1_000_000_000,
            ctime_ns=ctime_ns_total % 1_000_000_000,
            mtime_s=mtime_ns_total // 1_000_000_000,
            mtime_ns=mtime_ns_total % 1_000_000_000,
            dev=st.st_dev & 0xFFFFFFFF,
            ino=st.st_ino & 0xFFFFFFFF,
            mode=mode,
            uid=0,   # meaningful only on Unix
            gid=0,
            size=st.st_size & 0xFFFFFFFF,
            sha=sha,
            flags=0,
            path=norm,
        )

    write_index(git_dir, list(entries.values()))


# ---------------------------------------------------------------------------
# git status helpers
# ---------------------------------------------------------------------------

def _get_head_files(git_dir: Path) -> dict[str, str]:
    """
    Return {path: blob_sha} for every file in the HEAD commit's tree.
    Returns an empty dict when the repository has no commits yet.
    """
    head_path = git_dir / "HEAD"
    head_text = head_path.read_text().strip()

    if head_text.startswith("ref: "):
        ref_path = git_dir / head_text[5:]
        if not ref_path.exists():
            return {}  # branch exists but has no commits yet
        commit_sha = ref_path.read_text().strip()
    else:
        commit_sha = head_text  # detached HEAD

    try:
        commit = read_commit(git_dir, commit_sha)
    except Exception:
        return {}

    def _flatten(tree_sha: str, prefix: str) -> dict[str, str]:
        result = {}
        try:
            for entry in read_tree(git_dir, tree_sha):
                full_path = f"{prefix}/{entry.name}" if prefix else entry.name
                if entry.mode == "040000":
                    result.update(_flatten(entry.sha, full_path))
                else:
                    result[full_path] = entry.sha
        except Exception:
            pass
        return result

    return _flatten(commit.tree, "")


def status(git_dir: Path, work_dir: Path) -> dict:
    """
    Compare HEAD, index, and working tree. Returns:

    {
      "staged": {
        "new_file":  [...],   # in index, not in HEAD
        "modified":  [...],   # in both, different SHA
        "deleted":   [...],   # in HEAD, not in index
      },
      "unstaged": {
        "modified":  [...],   # in index, different from disk
        "deleted":   [...],   # in index, missing from disk
      },
      "untracked": [...],     # on disk, not in index
    }

    Note: .gitignore is not implemented; __pycache__ and .git are always skipped.
    """
    index_map = {e.path: e for e in read_index(git_dir)}
    head_files = _get_head_files(git_dir)

    # ---- staged (index vs HEAD) ----
    staged_new, staged_modified, staged_deleted = [], [], []
    all_known = sorted(set(index_map) | set(head_files))
    for path in all_known:
        in_idx = path in index_map
        in_hd = path in head_files
        if in_idx and not in_hd:
            staged_new.append(path)
        elif not in_idx and in_hd:
            staged_deleted.append(path)
        elif in_idx and in_hd and index_map[path].sha != head_files[path]:
            staged_modified.append(path)

    # ---- unstaged (working tree vs index) ----
    unstaged_modified, unstaged_deleted = [], []
    for path, entry in sorted(index_map.items()):
        fp = work_dir / path
        if not fp.exists():
            unstaged_deleted.append(path)
        else:
            current_sha = hash_object(git_dir, fp.read_bytes(), write=False)
            if current_sha != entry.sha:
                unstaged_modified.append(path)

    # ---- untracked (on disk, not in index) ----
    untracked = []
    for root, dirs, files in os.walk(work_dir):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
        root_path = Path(root)
        for fname in sorted(files):
            rel = str((root_path / fname).relative_to(work_dir)).replace("\\", "/")
            if rel not in index_map:
                untracked.append(rel)

    return {
        "staged": {
            "new_file": staged_new,
            "modified": staged_modified,
            "deleted":  staged_deleted,
        },
        "unstaged": {
            "modified": unstaged_modified,
            "deleted":  unstaged_deleted,
        },
        "untracked": untracked,
    }
