"""
pygitlib/stash.py

git stash / git stash pop — save and restore working-directory state.

Stash commit structure (mirrors real git):

  stash_commit
    tree    = worktree_tree   (on-disk content of every tracked file)
    parents = [HEAD_sha, index_commit_sha]
    message = "WIP on <branch>: <sha7> <head_msg>"

  index_commit  (parent[1] of the stash commit)
    tree    = index_tree      (content of the staging area at save time)
    parents = [HEAD_sha]
    message = "index on <branch>: <sha7> <head_msg>"

Having two trees lets pop restore both pieces independently:
  - Working-tree state  ← stash_commit.tree
  - Staged state        ← index_commit.tree  (parents[1])

Stack storage
─────────────
.git/stash-stack  — plain text, one SHA per line, oldest first / newest last.
pygit stash push  appends to this file.
pygit stash pop   reads and removes the last line.
pygit stash list  prints lines newest-first as stash@{0}, stash@{1}, …
"""

from pathlib import Path

from .objects import hash_object, read_object, read_commit, write_commit, \
                     read_tree, write_tree, TreeEntry, Commit
from .index import IndexEntry, read_index, write_index
from .branch import current_branch, resolve_ref
from .commit import _build_tree, _make_identity


# ---------------------------------------------------------------------------
# Stack helpers
# ---------------------------------------------------------------------------

def _stack_path(git_dir: Path) -> Path:
    return git_dir / "stash-stack"


def _stack_read(git_dir: Path) -> list[str]:
    p = _stack_path(git_dir)
    if not p.exists():
        return []
    return [s for s in p.read_text().splitlines() if s.strip()]


def _stack_write(git_dir: Path, entries: list[str]) -> None:
    p = _stack_path(git_dir)
    if entries:
        p.write_text("\n".join(entries) + "\n")
    elif p.exists():
        p.unlink()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _head_tree_flat(git_dir: Path) -> dict[str, tuple[str, str]]:
    """Return {path: (blob_sha, mode_str)} for every file in HEAD."""
    head_sha = resolve_ref(git_dir, "HEAD")
    if not head_sha:
        return {}
    try:
        commit = read_commit(git_dir, head_sha)
    except Exception:
        return {}

    def _flat(tree_sha: str, prefix: str) -> dict[str, tuple[str, str]]:
        result: dict[str, tuple[str, str]] = {}
        try:
            for entry in read_tree(git_dir, tree_sha):
                path = f"{prefix}/{entry.name}" if prefix else entry.name
                if entry.mode in ("040000", "40000"):
                    result.update(_flat(entry.sha, path))
                else:
                    result[path] = (entry.sha, entry.mode)
        except Exception:
            pass
        return result

    return _flat(commit.tree, "")


def _build_worktree_tree(git_dir: Path, work_dir: Path,
                         index_entries: list[IndexEntry]) -> str:
    """
    Build a git tree from the current on-disk content of every tracked file.

    Files deleted from disk (but still in the index) are omitted — their
    absence is the captured working-tree state for those paths.
    """
    synthetic: list[IndexEntry] = []
    for entry in index_entries:
        disk_path = work_dir / entry.path
        if not disk_path.exists():
            continue
        data = disk_path.read_bytes()
        blob_sha = hash_object(git_dir, data, write=True)
        synthetic.append(IndexEntry(
            ctime_s=0, ctime_ns=0, mtime_s=0, mtime_ns=0,
            dev=0, ino=0,
            mode=entry.mode,
            uid=0, gid=0,
            size=len(data),
            sha=blob_sha,
            flags=0,
            path=entry.path,
        ))
    return _build_tree(git_dir, synthetic)


def _write_blob_to_disk(git_dir: Path, work_dir: Path,
                        rel_path: str, blob_sha: str) -> None:
    """Write a blob object to the working tree."""
    _, data = read_object(git_dir, blob_sha)
    disk_path = work_dir / rel_path
    disk_path.parent.mkdir(parents=True, exist_ok=True)
    disk_path.write_bytes(data)


def _remove_file(work_dir: Path, rel_path: str) -> None:
    """Delete a file and clean up any now-empty parent directories."""
    disk_path = work_dir / rel_path
    if disk_path.exists():
        disk_path.unlink()
    parent = disk_path.parent
    while parent != work_dir:
        try:
            parent.rmdir()
            parent = parent.parent
        except OSError:
            break


def _make_index_entry(work_dir: Path, rel_path: str,
                      blob_sha: str, mode_str: str) -> IndexEntry:
    """Build an IndexEntry, reading stat data from disk if the file exists."""
    disk_path = work_dir / rel_path
    if disk_path.exists():
        st       = disk_path.stat()
        ctime_ns = st.st_ctime_ns
        mtime_ns = st.st_mtime_ns
        size     = st.st_size & 0xFFFFFFFF
        dev      = st.st_dev & 0xFFFFFFFF
        ino      = st.st_ino & 0xFFFFFFFF
    else:
        ctime_ns = mtime_ns = 0
        size = dev = ino = 0

    return IndexEntry(
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
        path=rel_path,
    )


def _restore_to_head(git_dir: Path, work_dir: Path) -> None:
    """
    Reset both the working tree and the index to match HEAD exactly.
    Called as the final step of stash push.
    """
    head_files = _head_tree_flat(git_dir)

    # Remove tracked files that are NOT in HEAD (newly staged files)
    for entry in read_index(git_dir):
        if entry.path not in head_files:
            _remove_file(work_dir, entry.path)

    # Write HEAD files to disk and rebuild the index
    new_index: list[IndexEntry] = []
    for rel_path, (blob_sha, mode_str) in head_files.items():
        _write_blob_to_disk(git_dir, work_dir, rel_path, blob_sha)
        new_index.append(_make_index_entry(work_dir, rel_path, blob_sha, mode_str))

    write_index(git_dir, new_index)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def stash_push(git_dir: Path, work_dir: Path) -> None:
    """
    Save the current working directory and index to the stash, then restore
    HEAD state.  Equivalent to:  git stash  (or git stash push)

    Raises ValueError when there is no initial commit or nothing to stash.
    """
    head_sha = resolve_ref(git_dir, "HEAD")
    if not head_sha:
        raise ValueError(
            "error: You do not have the initial commit yet"
        )

    from .index import status as _status
    st = _status(git_dir, work_dir)
    has_changes = (
        any(st["staged"][k]   for k in st["staged"])   or
        any(st["unstaged"][k] for k in st["unstaged"])
    )
    if not has_changes:
        print("No local changes to save")
        return

    index_entries = read_index(git_dir)
    head_commit   = read_commit(git_dir, head_sha)
    branch        = current_branch(git_dir) or "HEAD"
    head_msg      = head_commit.message.split("\n")[0]
    identity      = _make_identity(git_dir)
    label         = f"{branch}: {head_sha[:7]} {head_msg}"

    # 1. Index tree — staged state
    index_tree_sha = _build_tree(git_dir, index_entries)

    # 2. Index commit — parent[1] of the stash commit
    index_commit_sha = write_commit(git_dir, Commit(
        tree=index_tree_sha,
        parents=[head_sha],
        author=identity,
        committer=identity,
        message=f"index on {label}",
    ))

    # 3. Worktree tree — actual disk content of every tracked file
    worktree_tree_sha = _build_worktree_tree(git_dir, work_dir, index_entries)

    # 4. Stash (WIP) commit — parents = [HEAD, index_commit]
    stash_sha = write_commit(git_dir, Commit(
        tree=worktree_tree_sha,
        parents=[head_sha, index_commit_sha],
        author=identity,
        committer=identity,
        message=f"WIP on {label}",
    ))

    # 5. Push onto the stack
    stack = _stack_read(git_dir)
    stack.append(stash_sha)
    _stack_write(git_dir, stack)

    # 6. Reset working tree + index to HEAD
    _restore_to_head(git_dir, work_dir)

    n = len(stack) - 1           # 0-based index of the entry just pushed
    print(f"Saved working directory and index state WIP on {label}")


def stash_pop(git_dir: Path, work_dir: Path) -> None:
    """
    Apply the most-recently-saved stash and remove it from the stack.
    Equivalent to:  git stash pop

    Raises ValueError when the stash stack is empty.
    """
    stack = _stack_read(git_dir)
    if not stack:
        raise ValueError("error: No stash entries found.")

    stash_sha = stack[-1]
    stash_commit = read_commit(git_dir, stash_sha)

    if len(stash_commit.parents) < 2:
        raise ValueError(
            f"error: Stash entry {stash_sha[:7]} has unexpected structure."
        )

    index_commit = read_commit(git_dir, stash_commit.parents[1])

    # Flatten both trees: {path: (blob_sha, mode_str)}
    def _flat(tree_sha: str) -> dict[str, tuple[str, str]]:
        result: dict[str, tuple[str, str]] = {}
        try:
            for entry in read_tree(git_dir, tree_sha):
                path = entry.name
                if entry.mode in ("040000", "40000"):
                    for sub_path, v in _flat(entry.sha).items():
                        result[f"{path}/{sub_path}"] = v
                else:
                    result[path] = (entry.sha, entry.mode)
        except Exception:
            pass
        return result

    def _flat_full(tree_sha: str, prefix: str = "") -> dict[str, tuple[str, str]]:
        result: dict[str, tuple[str, str]] = {}
        try:
            for entry in read_tree(git_dir, tree_sha):
                p = f"{prefix}/{entry.name}" if prefix else entry.name
                if entry.mode in ("040000", "40000"):
                    result.update(_flat_full(entry.sha, p))
                else:
                    result[p] = (entry.sha, entry.mode)
        except Exception:
            pass
        return result

    worktree_files = _flat_full(stash_commit.tree)
    index_files    = _flat_full(index_commit.tree)

    # Remove files currently tracked that the stash's worktree doesn't have
    for entry in read_index(git_dir):
        if entry.path not in worktree_files:
            _remove_file(work_dir, entry.path)

    # Write stash's worktree files to disk
    for rel_path, (blob_sha, _) in worktree_files.items():
        _write_blob_to_disk(git_dir, work_dir, rel_path, blob_sha)

    # Restore the index to the stash's staged state
    new_index = [
        _make_index_entry(work_dir, rel_path, blob_sha, mode_str)
        for rel_path, (blob_sha, mode_str) in index_files.items()
    ]
    write_index(git_dir, new_index)

    # Remove the entry from the stack (only after everything succeeds)
    _stack_write(git_dir, stack[:-1])

    print(f"Dropped stash@{{0}}: {stash_commit.message}")


def stash_list(git_dir: Path) -> list[tuple[int, str]]:
    """
    Return [(index, message), …] for all stash entries, newest first.
    index 0 = most recent (same convention as real git stash list).
    """
    stack = _stack_read(git_dir)
    result: list[tuple[int, str]] = []
    for i, sha in enumerate(reversed(stack)):
        try:
            msg = read_commit(git_dir, sha).message.split("\n")[0]
        except Exception:
            msg = f"<corrupt: {sha[:7]}>"
        result.append((i, msg))
    return result
