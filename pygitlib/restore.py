"""
pygitlib/restore.py

git restore — unstage files or discard working-tree changes.

Two operations:

  restore_staged(git_dir, paths)
      Removes each path from the staging area, rolling it back to the HEAD
      version.  If the file was brand-new (not in HEAD) the entry is simply
      dropped from the index.  The working tree is NOT touched.
      Equivalent to:  git restore --staged <file>

  restore_worktree(git_dir, work_dir, paths)
      Overwrites each file on disk with the content stored in the index,
      recreating the file if it was deleted.  The index is NOT touched.
      Equivalent to:  git restore <file>
"""

from pathlib import Path

from .objects import read_commit, read_tree, read_object
from .index import IndexEntry, read_index, write_index
from .branch import resolve_ref


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

def _head_tree(git_dir: Path) -> dict[str, tuple[str, str]]:
    """
    Return {path: (blob_sha, mode_str)} for every file in the HEAD commit.
    Returns an empty dict when the repo has no commits yet.
    """
    head_sha = resolve_ref(git_dir, "HEAD")
    if not head_sha:
        return {}
    try:
        commit = read_commit(git_dir, head_sha)
    except Exception:
        return {}

    def _flatten(tree_sha: str, prefix: str) -> dict[str, tuple[str, str]]:
        result: dict[str, tuple[str, str]] = {}
        try:
            for entry in read_tree(git_dir, tree_sha):
                path = f"{prefix}/{entry.name}" if prefix else entry.name
                if entry.mode in ("040000", "40000"):
                    result.update(_flatten(entry.sha, path))
                else:
                    result[path] = (entry.sha, entry.mode)
        except Exception:
            pass
        return result

    return _flatten(commit.tree, "")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def restore_staged(git_dir: Path, paths: list[str]) -> list[str]:
    """
    Unstage one or more files (git restore --staged).

    For each path:
      - New file (in index, not in HEAD): entry is removed from the index.
      - Modified/deleted file (in HEAD): index entry is restored to the HEAD
        blob so the staged change is removed.

    The working tree is never modified.

    Returns a list of raw path strings that could not be resolved (errors).
    """
    head    = _head_tree(git_dir)
    entries = {e.path: e for e in read_index(git_dir)}
    errors: list[str] = []

    for raw_path in paths:
        norm = raw_path.replace("\\", "/")
        in_index = norm in entries
        in_head  = norm in head

        if not in_index and not in_head:
            errors.append(raw_path)
            continue

        if not in_head:
            # Brand-new staged file → drop from index
            del entries[norm]
        else:
            # Staged modification or staged deletion →
            # restore the HEAD blob into the index
            blob_sha, mode_str = head[norm]
            _, data = read_object(git_dir, blob_sha)

            disk_path = git_dir.parent / norm
            if disk_path.exists():
                st       = disk_path.stat()
                ctime_ns = st.st_ctime_ns
                mtime_ns = st.st_mtime_ns
                size     = st.st_size & 0xFFFFFFFF
                dev      = st.st_dev & 0xFFFFFFFF
                ino      = st.st_ino & 0xFFFFFFFF
            else:
                ctime_ns = mtime_ns = 0
                size = len(data)
                dev = ino = 0

            entries[norm] = IndexEntry(
                ctime_s=ctime_ns  // 1_000_000_000,
                ctime_ns=ctime_ns  % 1_000_000_000,
                mtime_s=mtime_ns  // 1_000_000_000,
                mtime_ns=mtime_ns  % 1_000_000_000,
                dev=dev, ino=ino,
                mode=0o100755 if mode_str == "100755" else 0o100644,
                uid=0, gid=0,
                size=size,
                sha=blob_sha,
                flags=0,
                path=norm,
            )

    write_index(git_dir, list(entries.values()))
    return errors


def restore_worktree(git_dir: Path, work_dir: Path, paths: list[str]) -> list[str]:
    """
    Discard working-tree changes (git restore).

    For each path:
      - In the index: overwrite the on-disk file with the indexed blob content,
        creating parent directories if necessary (also recreates deleted files).
      - Not in the index: cannot be restored — added to the error list.

    The index is never modified.

    Returns a list of raw path strings that could not be resolved (errors).
    """
    entries = {e.path: e for e in read_index(git_dir)}
    errors: list[str] = []

    for raw_path in paths:
        norm = raw_path.replace("\\", "/")
        if norm not in entries:
            errors.append(raw_path)
            continue

        entry = entries[norm]
        _, data = read_object(git_dir, entry.sha)
        disk_path = work_dir / norm
        disk_path.parent.mkdir(parents=True, exist_ok=True)
        disk_path.write_bytes(data)

    return errors
