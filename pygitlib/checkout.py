"""
pygitlib/checkout.py

Working-tree checkout — writes a commit's tree to the filesystem and
updates the index. Used by `git switch` to change branches.

The three-step switch:
  1. Safety: refuse if the working tree is dirty (staged or unstaged changes).
  2. Diff:   find files present in the current tree but absent in the target.
  3. Apply:  delete those files, write the target tree, update HEAD + index.

Cross-check with real git:
  $ git switch <branch>     # matches switch_branch(... create=False)
  $ git switch -c <branch>  # matches switch_branch(... create=True)
"""

from pathlib import Path

from .objects import read_object, read_tree as _read_tree, read_commit
from .index import IndexEntry, write_index
from .branch import (
    resolve_ref,
    current_branch,
    create_branch,
    set_head_to_branch,
    list_branches,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _flatten_tree(git_dir: Path, tree_sha: str, prefix: str = "") -> dict[str, tuple[str, str]]:
    """
    Recursively expand a tree object into a flat dict:
        { "src/main.py": ("blob_sha", "100644"), ... }

    Directories (mode "040000") are recursed; blobs are recorded.
    """
    result: dict[str, tuple[str, str]] = {}
    for entry in _read_tree(git_dir, tree_sha):
        path = f"{prefix}/{entry.name}" if prefix else entry.name
        if entry.mode in ("040000", "40000"):
            result.update(_flatten_tree(git_dir, entry.sha, path))
        else:
            result[path] = (entry.sha, entry.mode)
    return result


def _write_files(git_dir: Path, work_dir: Path,
                 files: dict[str, tuple[str, str]]) -> list[IndexEntry]:
    """
    Write every file in *files* to the working directory.
    Returns a list of IndexEntry objects suitable for write_index().
    """
    entries: list[IndexEntry] = []
    for rel_path, (blob_sha, mode_str) in sorted(files.items()):
        _, data = read_object(git_dir, blob_sha)
        disk_path = work_dir / rel_path
        disk_path.parent.mkdir(parents=True, exist_ok=True)
        disk_path.write_bytes(data)

        st = disk_path.stat()
        mode = 0o100755 if mode_str == "100755" else 0o100644
        ctime_ns = st.st_ctime_ns
        mtime_ns = st.st_mtime_ns

        entries.append(IndexEntry(
            ctime_s=ctime_ns // 1_000_000_000,
            ctime_ns=ctime_ns % 1_000_000_000,
            mtime_s=mtime_ns // 1_000_000_000,
            mtime_ns=mtime_ns % 1_000_000_000,
            dev=st.st_dev & 0xFFFFFFFF,
            ino=st.st_ino & 0xFFFFFFFF,
            mode=mode,
            uid=0, gid=0,
            size=st.st_size & 0xFFFFFFFF,
            sha=blob_sha,
            flags=0,
            path=rel_path,
        ))
    return entries


def _remove_files(work_dir: Path, paths: list[str]) -> None:
    """
    Delete files from the working directory and clean up empty parent dirs.
    Silently skips files that have already been deleted.
    """
    for rel_path in paths:
        fp = work_dir / rel_path
        if fp.exists():
            fp.unlink()
        # Walk up, removing directories only while they are empty
        parent = fp.parent
        while parent != work_dir:
            try:
                parent.rmdir()   # raises OSError if not empty
                parent = parent.parent
            except OSError:
                break


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def switch_branch(git_dir: Path, work_dir: Path,
                  target: str, create: bool = False) -> None:
    """
    Switch the working tree to a different branch.

    Args:
        git_dir:  path to the .git directory
        work_dir: root of the working tree (git_dir.parent)
        target:   name of the branch to switch to
        create:   when True, create the branch first (git switch -c)

    Raises ValueError for:
      - target branch does not exist (and create=False)
      - target branch already exists (and create=True)
      - no commits yet (cannot create a branch yet)
      - dirty working tree (staged or unstaged changes present)
    """
    # ------------------------------------------------------------------ #
    # 0. Already on the target branch?                                     #
    # ------------------------------------------------------------------ #
    if current_branch(git_dir) == target:
        print(f"Already on '{target}'")
        return

    # ------------------------------------------------------------------ #
    # 1. Resolve / create the target branch                                #
    # ------------------------------------------------------------------ #
    if create:
        # create_branch raises ValueError if branch exists or no commits
        create_branch(git_dir, target)
        set_head_to_branch(git_dir, target)
        print(f"Switched to a new branch '{target}'")
        return          # same commit → no file changes needed

    if target not in list_branches(git_dir):
        raise ValueError(
            f"error: pathspec '{target}' did not match any branch known to pygit.\n"
            f"Hint:  pygit branch {target}       # create from current HEAD\n"
            f"       pygit switch -c {target}    # create and switch in one step"
        )
    target_sha = resolve_ref(git_dir, target)
    assert target_sha is not None  # branch exists (checked above) so ref resolves

    # ------------------------------------------------------------------ #
    # 2. Refuse if working tree is dirty                                   #
    # ------------------------------------------------------------------ #
    from .index import status as _status
    st = _status(git_dir, work_dir)
    dirty = (
        any(st["staged"][k] for k in st["staged"]) or
        any(st["unstaged"][k] for k in st["unstaged"])
    )
    if dirty:
        raise ValueError(
            "error: Your local changes would be overwritten by checkout.\n"
            "Please commit or stash your changes before switching branches."
        )

    # ------------------------------------------------------------------ #
    # 3. Build the file sets for current HEAD and target branch            #
    # ------------------------------------------------------------------ #
    current_sha = resolve_ref(git_dir, "HEAD")
    current_files: dict[str, tuple[str, str]] = {}
    if current_sha:
        try:
            cc = read_commit(git_dir, current_sha)
            current_files = _flatten_tree(git_dir, cc.tree)
        except Exception:
            pass

    tc = read_commit(git_dir, target_sha)
    target_files = _flatten_tree(git_dir, tc.tree)

    # ------------------------------------------------------------------ #
    # 4. Remove files that exist in current tree but not in target tree    #
    # ------------------------------------------------------------------ #
    to_remove = [p for p in current_files if p not in target_files]
    _remove_files(work_dir, to_remove)

    # ------------------------------------------------------------------ #
    # 5. Write target tree files to disk, rebuild index                   #
    # ------------------------------------------------------------------ #
    new_index = _write_files(git_dir, work_dir, target_files)

    # ------------------------------------------------------------------ #
    # 6. Update HEAD → new branch, update index                           #
    # ------------------------------------------------------------------ #
    set_head_to_branch(git_dir, target)
    write_index(git_dir, new_index)

    print(f"Switched to branch '{target}'")
