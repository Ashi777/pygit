"""
pygitlib/merge.py

Three-way merge — the heart of git merge.

Algorithm overview
──────────────────
1. Find the merge base: the most recent common ancestor (LCA) of the
   two commits being merged, located by BFS over the commit graph.

2. Three-way merge at the file level:
     • File added by one side only     → take it
     • File deleted by both sides      → remove it
     • File changed by one side only   → take that side
     • File changed identically        → take it (no conflict)
     • File changed differently        → line-level merge

3. Three-way merge at the line level (diff3 / Myers):
     • Compute diff(base → ours) and diff(base → theirs) as change hunks
     • Walk hunks from both sides together in base order
     • Non-overlapping hunks: apply cleanly
     • Overlapping hunks with same result: apply cleanly
     • Overlapping hunks with different result: emit conflict markers

4. Post-merge:
     • If no conflicts  → write merge commit with two parents, update HEAD ref
     • If conflicts     → write files with <<<< / ==== / >>>> markers,
                          write .git/MERGE_HEAD so git commit can finish it

Cross-check with real git:
  $ git merge <branch>           # matches merge_branch()
  $ git merge-base <sha1> <sha2> # matches find_merge_base()
"""

import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .objects import (
    hash_object, read_object, read_commit, read_tree, write_tree,
    write_commit, TreeEntry, Commit,
)
from .branch import resolve_ref, current_branch, update_branch_ref
from .diff import myers_diff
from .index import IndexEntry, write_index


# ---------------------------------------------------------------------------
# Merge base (LCA)
# ---------------------------------------------------------------------------

def find_merge_base(git_dir: Path, sha_a: str, sha_b: str) -> str | None:
    """
    Find the merge base (LCA) of two commits by BFS over the commit graph.

    1. Collect every ancestor of sha_a (including itself) into a set.
    2. BFS from sha_b; return the first commit that is also in that set.

    For simple branch-and-merge workflows this reliably finds the commit
    where the two branches diverged.  Returns None for unrelated histories.

    Equivalent to: git merge-base <sha_a> <sha_b>
    """
    # Phase 1: mark all ancestors of sha_a
    ancestors_a: set[str] = set()
    queue: deque[str] = deque([sha_a])
    while queue:
        sha = queue.popleft()
        if sha in ancestors_a:
            continue
        ancestors_a.add(sha)
        try:
            for p in read_commit(git_dir, sha).parents:
                queue.append(p)
        except Exception:
            pass

    # Phase 2: BFS from sha_b; first hit in ancestors_a is the LCA
    visited: set[str] = set()
    queue = deque([sha_b])
    while queue:
        sha = queue.popleft()
        if sha in visited:
            continue
        visited.add(sha)
        if sha in ancestors_a:
            return sha
        try:
            for p in read_commit(git_dir, sha).parents:
                queue.append(p)
        except Exception:
            pass

    return None


# ---------------------------------------------------------------------------
# Line-level three-way merge
# ---------------------------------------------------------------------------

def _to_hunks(ops: list) -> list[tuple[int, int, list[str]]]:
    """
    Convert a myers_diff edit script into change hunks.
    Each hunk is (src_start, src_end, replacement_lines):
      src[src_start:src_end] is replaced by replacement_lines.
    """
    result: list[tuple[int, int, list[str]]] = []
    pos = 0
    i = 0
    while i < len(ops):
        if ops[i][0] == "=":
            pos += 1
            i += 1
            continue
        start = pos
        new_lines: list[str] = []
        while i < len(ops) and ops[i][0] != "=":
            op, ln = ops[i]
            if op == "-":
                pos += 1
            else:
                new_lines.append(ln)
            i += 1
        result.append((start, pos, new_lines))
    return result


def merge_lines(
    base: list[str],
    ours: list[str],
    theirs: list[str],
    our_label: str = "HEAD",
    their_label: str = "branch",
) -> tuple[list[str], bool]:
    """
    Three-way merge of line sequences.

    Returns (merged_lines, has_conflict).

    The algorithm sweeps through the base from left to right, collecting
    "zones" — base ranges touched by at least one hunk from either side.
    Each zone is processed as a whole so overlapping hunks are handled
    correctly:
      • Zone touched only by ours   → apply ours
      • Zone touched only by theirs → apply theirs
      • Zone touched by both, same result → apply once (no conflict)
      • Zone touched by both, different   → emit <<<, ===, >>> markers

    Equivalent to: git merge-file (three-way merge kernel)
    """
    o_hunks = _to_hunks(myers_diff(base, ours))
    t_hunks = _to_hunks(myers_diff(base, theirs))

    merged: list[str] = []
    has_conflict = False
    pos = 0
    oi = ti = 0

    def _apply_zone(
        zone: list[tuple[int, int, list[str]]], z_start: int, z_end: int
    ) -> list[str]:
        """Re-apply a list of hunks to base[z_start:z_end]."""
        out: list[str] = []
        p = z_start
        for h_s, h_e, h_new in zone:
            while p < h_s:
                out.append(base[p])
                p += 1
            out.extend(h_new)
            p = h_e
        while p < z_end:
            out.append(base[p])
            p += 1
        return out

    while oi < len(o_hunks) or ti < len(t_hunks):
        o_s = o_hunks[oi][0] if oi < len(o_hunks) else len(base)
        t_s = t_hunks[ti][0] if ti < len(t_hunks) else len(base)
        nxt = min(o_s, t_s)

        # Unchanged base lines before the next event
        while pos < nxt:
            merged.append(base[pos])
            pos += 1

        # Gather all overlapping hunks into one "zone"
        # Keep expanding zone_end as new hunks are swept in
        o_zone: list[tuple[int, int, list[str]]] = []
        t_zone: list[tuple[int, int, list[str]]] = []
        zone_end = pos

        growing = True
        while growing:
            growing = False
            while oi < len(o_hunks) and o_hunks[oi][0] <= zone_end:
                o_zone.append(o_hunks[oi])
                zone_end = max(zone_end, o_hunks[oi][1])
                oi += 1
                growing = True
            while ti < len(t_hunks) and t_hunks[ti][0] <= zone_end:
                t_zone.append(t_hunks[ti])
                zone_end = max(zone_end, t_hunks[ti][1])
                ti += 1
                growing = True

        if o_zone and not t_zone:
            merged.extend(_apply_zone(o_zone, pos, zone_end))
        elif t_zone and not o_zone:
            merged.extend(_apply_zone(t_zone, pos, zone_end))
        else:
            o_result = _apply_zone(o_zone, pos, zone_end)
            t_result = _apply_zone(t_zone, pos, zone_end)
            if o_result == t_result:
                merged.extend(o_result)
            else:
                has_conflict = True
                merged.append(f"<<<<<<< {our_label}")
                merged.extend(o_result)
                merged.append("=======")
                merged.extend(t_result)
                merged.append(f">>>>>>> {their_label}")

        pos = zone_end

    # Remaining unchanged base lines
    while pos < len(base):
        merged.append(base[pos])
        pos += 1

    return merged, has_conflict


# ---------------------------------------------------------------------------
# Tree helpers
# ---------------------------------------------------------------------------

def _flatten_tree(
    git_dir: Path, tree_sha: str, prefix: str = ""
) -> dict[str, tuple[str, str]]:
    """
    Recursively expand a tree into {path: (blob_sha, mode_str)}.
    Handles both "040000" and "40000" directory modes from real git objects.
    """
    result: dict[str, tuple[str, str]] = {}
    for entry in read_tree(git_dir, tree_sha):
        path = f"{prefix}/{entry.name}" if prefix else entry.name
        if entry.mode.lstrip("0") == "40000" or entry.mode == "040000":
            result.update(_flatten_tree(git_dir, entry.sha, path))
        else:
            result[path] = (entry.sha, entry.mode)
    return result


def _build_tree_dict(files: dict[str, tuple[str, str]]) -> dict:
    """
    Convert a flat {path: (sha, mode)} mapping into a nested directory dict
    suitable for _write_tree_recursive.
    """
    tree: dict = {}
    for path, value in files.items():
        parts = path.split("/")
        node = tree
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return tree


def _write_tree_recursive(git_dir: Path, tree: dict) -> str:
    """
    Recursively write nested dicts as git tree objects.
    Leaf values are (blob_sha, mode_str); subtree values are dicts.
    Returns the SHA-1 of the root tree.
    """
    entries: list[TreeEntry] = []
    for name, value in sorted(tree.items()):
        if isinstance(value, dict):
            sub_sha = _write_tree_recursive(git_dir, value)
            entries.append(TreeEntry(mode="40000", name=name, sha=sub_sha))
        else:
            sha, mode = value
            entries.append(TreeEntry(mode=mode, name=name, sha=sha))
    return write_tree(git_dir, entries)


# ---------------------------------------------------------------------------
# Identity / config helpers
# ---------------------------------------------------------------------------

def _read_identity(git_dir: Path) -> tuple[str, str]:
    """
    Read user.name and user.email from .git/config then ~/.gitconfig.
    Returns ("Unknown", "unknown@example.com") as fallback.
    """
    configs = [git_dir / "config", Path.home() / ".gitconfig"]
    name = email = ""
    for cfg_path in configs:
        if not cfg_path.exists():
            continue
        in_user = False
        for raw in cfg_path.read_text(errors="replace").splitlines():
            stripped = raw.strip()
            if stripped.startswith("["):
                in_user = stripped.lower().startswith("[user")
            elif in_user and "=" in stripped:
                k, v = stripped.split("=", 1)
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
    """Return a git-format identity string with a Unix timestamp."""
    name, email = _read_identity(git_dir)
    ts = int(time.time())
    return f"{name} <{email}> {ts} +0000"


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class MergeResult:
    """Outcome returned by merge_branch()."""
    success: bool
    fast_forward: bool = False
    conflicts: list[str] = field(default_factory=list)
    commit_sha: str = ""
    message: str = ""


# ---------------------------------------------------------------------------
# Internal merge steps
# ---------------------------------------------------------------------------

def _do_fast_forward(
    git_dir: Path, work_dir: Path, our_branch: str, their_sha: str
) -> MergeResult:
    """
    Fast-forward our_branch to their_sha.
    No merge commit needed; just advance the ref and update the work tree.
    """
    from .checkout import _flatten_tree as _ct_flatten, _write_files

    their_commit = read_commit(git_dir, their_sha)
    their_files = _ct_flatten(git_dir, their_commit.tree)

    # Remove files that no longer exist and write the new tree
    our_sha = resolve_ref(git_dir, "HEAD")
    if our_sha:
        try:
            our_commit = read_commit(git_dir, our_sha)
            our_files = _ct_flatten(git_dir, our_commit.tree)
            for path in our_files:
                if path not in their_files:
                    fp = work_dir / path
                    if fp.exists():
                        fp.unlink()
        except Exception:
            pass

    new_index = _write_files(git_dir, work_dir, their_files)
    write_index(git_dir, new_index)
    update_branch_ref(git_dir, our_branch, their_sha)

    short = their_sha[:7]
    return MergeResult(
        success=True,
        fast_forward=True,
        commit_sha=their_sha,
        message=f"Fast-forward\n  HEAD is now at {short}",
    )


def _do_three_way_merge(
    git_dir: Path,
    work_dir: Path,
    our_branch: str,
    our_sha: str,
    their_branch: str,
    their_sha: str,
    base_sha: str,
) -> MergeResult:
    """
    Full three-way merge. Handles file additions, deletions, and content merges.
    Creates a merge commit if no conflicts remain.
    """
    base_commit = read_commit(git_dir, base_sha)
    our_commit  = read_commit(git_dir, our_sha)
    their_commit = read_commit(git_dir, their_sha)

    base_files  = _flatten_tree(git_dir, base_commit.tree)
    our_files   = _flatten_tree(git_dir, our_commit.tree)
    their_files = _flatten_tree(git_dir, their_commit.tree)

    merged_files: dict[str, tuple[str, str]] = {}   # path → (sha, mode)
    written_to_disk: set[str] = set()               # paths already written
    conflicts: list[str] = []

    all_paths = sorted(set(base_files) | set(our_files) | set(their_files))

    for path in all_paths:
        b_sha, b_mode = base_files.get(path, (None, None))
        o_sha, o_mode = our_files.get(path, (None, None))
        t_sha, t_mode = their_files.get(path, (None, None))

        # ── Trivial cases ────────────────────────────────────────────────
        if o_sha == t_sha:
            # Both sides agree (both added same, both deleted, or unchanged)
            if o_sha is not None:
                merged_files[path] = (o_sha, o_mode or t_mode)
            continue

        if o_sha is None and t_sha is None:
            continue  # deleted by both

        if b_sha is None:
            # New file (added by one or both sides with different content)
            if o_sha is None:
                merged_files[path] = (t_sha, t_mode)
            elif t_sha is None:
                merged_files[path] = (o_sha, o_mode)
            else:
                # Both added different content
                _emit_conflict(git_dir, work_dir, path,
                               None, o_sha, t_sha,
                               "HEAD", their_branch,
                               merged_files, written_to_disk)
                conflicts.append(path)
            continue

        if o_sha is None:
            # We deleted it; they may have modified it
            if t_sha == b_sha:
                pass  # their side unchanged → honour our deletion
            else:
                # They modified, we deleted → conflict (keep theirs)
                merged_files[path] = (t_sha, t_mode)
                conflicts.append(path)
            continue

        if t_sha is None:
            # They deleted it; we may have modified it
            if o_sha == b_sha:
                pass  # our side unchanged → honour their deletion
            else:
                # We modified, they deleted → conflict (keep ours)
                merged_files[path] = (o_sha, o_mode)
                conflicts.append(path)
            continue

        # ── Content merge (all three versions exist, all different) ──────
        if o_sha == b_sha:
            merged_files[path] = (t_sha, t_mode)   # only theirs changed
            continue
        if t_sha == b_sha:
            merged_files[path] = (o_sha, o_mode)   # only ours changed
            continue

        # Both sides changed → line-level merge
        _emit_conflict(git_dir, work_dir, path,
                       b_sha, o_sha, t_sha,
                       "HEAD", their_branch,
                       merged_files, written_to_disk)
        if path in merged_files:
            # _emit_conflict detected a true conflict
            conflicts.append(path)

    # ── Write non-conflicted files to disk ───────────────────────────────
    for path, (sha, _mode) in merged_files.items():
        if path in written_to_disk:
            continue
        fp = work_dir / path
        fp.parent.mkdir(parents=True, exist_ok=True)
        _, data = read_object(git_dir, sha)
        fp.write_bytes(data)

    # ── Remove files not in the merged set ───────────────────────────────
    for path in all_paths:
        if path not in merged_files:
            fp = work_dir / path
            if fp.exists():
                fp.unlink()

    # ── Rebuild the index from merged_files ──────────────────────────────
    new_index: list[IndexEntry] = []
    for path, (sha, mode_str) in merged_files.items():
        fp = work_dir / path
        if not fp.exists():
            continue
        st = fp.stat()
        ctime_ns = st.st_ctime_ns
        mtime_ns = st.st_mtime_ns
        try:
            mode_int = int(mode_str, 8)
        except (ValueError, TypeError):
            mode_int = 0o100644
        new_index.append(IndexEntry(
            ctime_s=ctime_ns // 1_000_000_000,
            ctime_ns=ctime_ns % 1_000_000_000,
            mtime_s=mtime_ns // 1_000_000_000,
            mtime_ns=mtime_ns % 1_000_000_000,
            dev=st.st_dev & 0xFFFFFFFF,
            ino=st.st_ino & 0xFFFFFFFF,
            mode=mode_int,
            uid=0, gid=0,
            size=st.st_size & 0xFFFFFFFF,
            sha=sha,
            flags=0,
            path=path,
        ))
    write_index(git_dir, new_index)

    # ── Conflicts: write MERGE_HEAD and report ───────────────────────────
    if conflicts:
        (git_dir / "MERGE_HEAD").write_text(their_sha + "\n")
        (git_dir / "MERGE_MSG").write_text(
            f"Merge branch '{their_branch}'\n\n# Conflicts:\n"
            + "".join(f"#\t{c}\n" for c in conflicts)
        )
        return MergeResult(
            success=False,
            conflicts=conflicts,
            message=(
                "Automatic merge failed; fix conflicts and then commit the result.\n"
                + "\n".join(f"CONFLICT (content): Merge conflict in {c}"
                            for c in conflicts)
            ),
        )

    # ── No conflicts: create the merge commit ────────────────────────────
    merged_tree_sha = _write_tree_recursive(
        git_dir, _build_tree_dict(merged_files)
    )
    identity = _make_identity(git_dir)
    merge_commit = Commit(
        tree=merged_tree_sha,
        author=identity,
        committer=identity,
        message=f"Merge branch '{their_branch}'",
        parents=[our_sha, their_sha],
    )
    merge_sha = write_commit(git_dir, merge_commit)
    update_branch_ref(git_dir, our_branch, merge_sha)

    return MergeResult(
        success=True,
        commit_sha=merge_sha,
        message=(
            f"Merge made by the 'ort' strategy.\n"
            f"  {merge_sha[:7]} Merge branch '{their_branch}'"
        ),
    )


def _emit_conflict(
    git_dir: Path,
    work_dir: Path,
    path: str,
    base_sha: str | None,
    our_sha: str,
    their_sha: str,
    our_label: str,
    their_label: str,
    merged_files: dict,
    written_to_disk: set,
) -> None:
    """
    Perform a line-level three-way merge for *path*.
    If the merge is clean (both sides made the same change), update merged_files
    with the result.  If there are conflicts, write the conflict-marked file to
    disk and store the conflicted blob SHA in merged_files.
    Binary files always produce a conflict (we keep 'ours').
    """
    _, our_data = read_object(git_dir, our_sha)
    _, their_data = read_object(git_dir, their_sha)

    is_binary = (
        b"\x00" in our_data[:8000]
        or b"\x00" in their_data[:8000]
    )
    if base_sha:
        _, base_data = read_object(git_dir, base_sha)
        is_binary = is_binary or b"\x00" in base_data[:8000]
        base_lines = base_data.decode("utf-8", errors="replace").splitlines()
    else:
        base_lines = []

    if is_binary:
        # Binary conflict: keep ours, mark as conflict
        merged_files[path] = (our_sha,
                              _guess_mode(our_data))
        return

    our_lines   = our_data.decode("utf-8", errors="replace").splitlines()
    their_lines = their_data.decode("utf-8", errors="replace").splitlines()

    merged_lines, has_conflict = merge_lines(
        base_lines, our_lines, their_lines, our_label, their_label
    )

    # Reconstruct bytes: join lines with newline, preserve trailing newline
    merged_text = "\n".join(merged_lines)
    if merged_lines:
        merged_text += "\n"
    merged_data = merged_text.encode("utf-8")

    merged_sha = hash_object(git_dir, merged_data, write=True)
    mode = _guess_mode(our_data)
    merged_files[path] = (merged_sha, mode)

    # Write to disk (both conflict and clean line-merge)
    fp = work_dir / path
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_bytes(merged_data)
    written_to_disk.add(path)

    if not has_conflict:
        # Clean line-level merge; remove from conflicts
        # (the caller adds to conflicts AFTER we return for the conflict case)
        pass  # merged_files already set correctly; has_conflict is False


def _guess_mode(data: bytes) -> str:
    """Guess file mode string from content (simplified: always 100644)."""
    return "100644"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def merge_branch(
    git_dir: Path, work_dir: Path, their_branch: str
) -> MergeResult:
    """
    Merge *their_branch* into the current branch.
    Equivalent to: git merge <their_branch>

    Returns a MergeResult describing what happened:
      • Fast-forward: ref advanced, no commit created
      • Clean merge: merge commit with two parents written
      • Conflicted: conflict markers written to files, MERGE_HEAD written
    """
    our_branch = current_branch(git_dir)
    if not our_branch:
        return MergeResult(success=False,
                           message="HEAD is detached; cannot merge")

    our_sha = resolve_ref(git_dir, "HEAD")
    their_sha = resolve_ref(git_dir, their_branch)

    if not their_sha:
        return MergeResult(
            success=False,
            message=f"error: Branch '{their_branch}' not found or has no commits",
        )
    if not our_sha:
        return MergeResult(
            success=False,
            message="error: Nothing to merge into; make a first commit first",
        )
    if our_sha == their_sha:
        return MergeResult(success=True, message="Already up to date.")

    base_sha = find_merge_base(git_dir, our_sha, their_sha)

    if base_sha is None:
        return MergeResult(
            success=False,
            message=(
                "error: Refusing to merge unrelated histories.\n"
                "Hint:  use 'git merge --allow-unrelated-histories' if intentional"
            ),
        )

    if base_sha == their_sha:
        return MergeResult(success=True, message="Already up to date.")

    if base_sha == our_sha:
        return _do_fast_forward(git_dir, work_dir, our_branch, their_sha)

    return _do_three_way_merge(
        git_dir, work_dir,
        our_branch, our_sha,
        their_branch, their_sha,
        base_sha,
    )
