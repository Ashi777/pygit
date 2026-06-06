"""
pygitlib/commit.py

Creates a commit object from the current index state.

What git commit does
────────────────────
1. Read the staging area (.git/index) to know which files are staged.
2. Build a tree object from those entries (handles nested directories).
3. Read the current HEAD to find the parent commit SHA (if any).
4. Check for .git/MERGE_HEAD — if present, this is a merge commit and the
   second parent is appended automatically (completing an interrupted merge).
5. Read user identity from .git/config / ~/.gitconfig.
6. Write the commit object to the object store.
7. Advance the current branch ref to the new commit SHA.
8. Clean up any merge state (.git/MERGE_HEAD, .git/MERGE_MSG).

Cross-check with real git:
  $ git commit -m "message"   → matches commit()
"""

import time
from pathlib import Path
from typing import Any

from .objects import write_tree, write_commit, read_commit, TreeEntry, Commit
from .index import read_index, IndexEntry
from .branch import current_branch, resolve_ref, update_branch_ref


# ---------------------------------------------------------------------------
# Identity helpers (reads .git/config and ~/.gitconfig)
# ---------------------------------------------------------------------------

def _read_identity(git_dir: Path) -> tuple[str, str]:
    """
    Return (name, email) from the nearest git config.
    Checks .git/config first, then ~/.gitconfig.
    Falls back to ("Unknown", "unknown@example.com") if nothing is found.
    """
    configs = [git_dir / "config", Path.home() / ".gitconfig"]
    name = email = ""
    for cfg in configs:
        if not cfg.exists():
            continue
        in_user = False
        for raw in cfg.read_text(errors="replace").splitlines():
            line = raw.strip()
            if line.startswith("["):
                in_user = line.lower().startswith("[user")
            elif in_user and "=" in line:
                k, v = line.split("=", 1)
                k = k.strip().lower()
                v = v.strip()
                if k == "name" and not name:
                    name = v
                elif k == "email" and not email:
                    email = v
        if name and email:
            break
    return (name or "Unknown", email or "unknown@example.com")


def _make_identity(git_dir: Path) -> str:
    """Return a git-format identity string: "Name <email> timestamp tz"."""
    name, email = _read_identity(git_dir)
    ts = int(time.time())
    return f"{name} <{email}> {ts} +0000"


# ---------------------------------------------------------------------------
# Tree builder
# ---------------------------------------------------------------------------

def _build_tree(git_dir: Path, entries: list[IndexEntry]) -> str:
    """
    Construct a git tree object from index entries.

    Handles nested directories: "src/main.py" and "src/utils.py" are grouped
    under a single "src" subtree before the root tree is written.

    Returns the SHA-1 of the root tree.
    """
    # Build a nested dict: each leaf is (blob_sha, mode_str),
    # each interior node is a plain dict representing a subdirectory.
    tree: dict[str, Any] = {}
    for entry in entries:
        parts = entry.path.split("/")
        mode_str = f"{entry.mode:o}"   # 0o100644 → "100644"
        node = tree
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = (entry.sha, mode_str)

    def _write(subtree: dict[str, Any]) -> str:
        te: list[TreeEntry] = []
        for name, value in sorted(subtree.items()):
            if isinstance(value, dict):
                sub_sha = _write(value)
                # Git stores directory mode as "40000" (no leading zero) in the binary format
                te.append(TreeEntry(mode="40000", name=name, sha=sub_sha))
            else:
                sha, mode = value
                te.append(TreeEntry(mode=mode, name=name, sha=sha))
        return write_tree(git_dir, te)

    return _write(tree)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def commit(git_dir: Path, message: str) -> str:
    """
    Create a commit from the current index state.

    Equivalent to: git commit -m <message>

    Args:
        git_dir: path to the .git directory
        message: commit message (-m flag)

    Returns:
        The 40-char SHA-1 of the new commit.

    Raises:
        ValueError: when there is nothing new to commit, or the index
                    is empty and there is no existing HEAD.
    """
    # ── 1. Read current index ────────────────────────────────────────────
    entries = read_index(git_dir)

    if not entries:
        raise ValueError(
            "nothing to commit\n"
            "(use 'pygit add <file>...' to stage files before committing)"
        )

    # ── 2. Resolve current branch and parent ─────────────────────────────
    branch    = current_branch(git_dir)
    parent_sha = resolve_ref(git_dir, "HEAD")

    # ── 3. Build the tree from staged files ──────────────────────────────
    tree_sha = _build_tree(git_dir, entries)

    # ── 4. Reject if tree matches HEAD exactly (nothing new to commit) ───
    if parent_sha:
        head_commit = read_commit(git_dir, parent_sha)
        if head_commit.tree == tree_sha:
            raise ValueError(
                "nothing to commit, working tree clean\n"
                "(stage your changes with 'pygit add' first)"
            )

    # ── 5. Collect parents (also pick up MERGE_HEAD if present) ──────────
    parents: list[str] = [parent_sha] if parent_sha else []
    merge_head = git_dir / "MERGE_HEAD"
    if merge_head.exists():
        second_parent = merge_head.read_text().strip()
        if second_parent:
            parents.append(second_parent)

    # ── 6. Write the commit object ────────────────────────────────────────
    identity   = _make_identity(git_dir)
    commit_obj = Commit(
        tree=tree_sha,
        author=identity,
        committer=identity,
        message=message,
        parents=parents,
    )
    commit_sha = write_commit(git_dir, commit_obj)

    # ── 7. Advance branch ref (or update detached HEAD) ──────────────────
    if branch:
        update_branch_ref(git_dir, branch, commit_sha)
    else:
        (git_dir / "HEAD").write_text(commit_sha + "\n")

    # ── 8. Clean up merge state ───────────────────────────────────────────
    for fname in ("MERGE_HEAD", "MERGE_MSG"):
        p = git_dir / fname
        if p.exists():
            p.unlink()

    return commit_sha
