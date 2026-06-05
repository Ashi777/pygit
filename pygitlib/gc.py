"""
pygitlib/gc.py

Basic garbage collection — finds loose objects that are not reachable from
any branch, tag, HEAD, stash, or the current staging area (index), then
optionally removes them.

What "reachable" means
──────────────────────
An object is reachable if there exists a path of references that leads to it:

  any ref (branch / HEAD / stash / MERGE_HEAD)
    └── commit  ──► parent commits  (recurse)
                └── root tree
                      ├── blobs  (files)
                      └── sub-trees  (directories, recurse)

Blobs currently staged in the index are also considered reachable so that
`pygit gc` never deletes work that is about to be committed.

Anything not reachable via that graph is garbage — usually the result of
aborted operations that wrote objects but never completed a commit.

Public API
──────────
  find_reachable(git_dir) → set[str]          all reachable SHAs
  find_loose_objects(git_dir) → dict[sha, Path]  every loose object on disk
  run_gc(git_dir, prune=False) → dict          report (and optionally prune)
"""

from pathlib import Path


# ---------------------------------------------------------------------------
# Step 1 — collect all root SHAs from every known ref
# ---------------------------------------------------------------------------

def _collect_refs(git_dir: Path) -> set[str]:
    """
    Return the starting set of SHAs by reading every ref in the repo:
    branches, HEAD (symbolic or detached), MERGE_HEAD, ORIG_HEAD, and
    pygit's stash stack.
    """
    roots: set[str] = set()

    def _add(sha: str) -> None:
        sha = sha.strip()
        if len(sha) == 40 and all(c in "0123456789abcdef" for c in sha):
            roots.add(sha)

    # Branch refs (refs/heads/**)
    heads_dir = git_dir / "refs" / "heads"
    if heads_dir.exists():
        for f in heads_dir.rglob("*"):
            if f.is_file():
                _add(f.read_text())

    # Tag refs (refs/tags/**)
    tags_dir = git_dir / "refs" / "tags"
    if tags_dir.exists():
        for f in tags_dir.rglob("*"):
            if f.is_file():
                _add(f.read_text())

    # HEAD — symbolic ref or detached SHA
    head_file = git_dir / "HEAD"
    if head_file.exists():
        text = head_file.read_text().strip()
        if text.startswith("ref: "):
            ref_path = git_dir / text[5:]
            if ref_path.exists():
                _add(ref_path.read_text())
        else:
            _add(text)

    # MERGE_HEAD, ORIG_HEAD (left behind by merge / reset operations)
    for fname in ("MERGE_HEAD", "ORIG_HEAD"):
        p = git_dir / fname
        if p.exists():
            _add(p.read_text())

    # Stash stack (our custom .git/stash-stack file)
    stash_file = git_dir / "stash-stack"
    if stash_file.exists():
        for line in stash_file.read_text().splitlines():
            _add(line)

    return roots


# ---------------------------------------------------------------------------
# Step 2 — walk the object graph to find every reachable SHA
# ---------------------------------------------------------------------------

def find_reachable(git_dir: Path) -> set[str]:
    """
    Traverse the full object graph starting from every ref.
    Also marks every blob currently in the staging area as reachable so that
    staged work is never accidentally deleted.

    Returns the complete set of reachable SHA-1 strings.
    """
    from .objects import read_object, decode_commit, decode_tree
    from .index import read_index

    reachable: set[str] = set()
    queue: list[str] = list(_collect_refs(git_dir))

    # Protect staged blobs — they may not be committed yet
    for entry in read_index(git_dir):
        reachable.add(entry.sha)

    while queue:
        sha = queue.pop()
        if sha in reachable:
            continue
        reachable.add(sha)

        try:
            obj_type, data = read_object(git_dir, sha)
        except Exception:
            continue   # skip missing / corrupt objects

        if obj_type == "commit":
            c = decode_commit(data)
            for parent in c.parents:
                if parent not in reachable:
                    queue.append(parent)
            if c.tree not in reachable:
                queue.append(c.tree)

        elif obj_type == "tree":
            for entry in decode_tree(data):
                if entry.sha not in reachable:
                    queue.append(entry.sha)

        # blobs and tags: no further objects to enqueue

    return reachable


# ---------------------------------------------------------------------------
# Step 3 — enumerate every loose object on disk
# ---------------------------------------------------------------------------

def find_loose_objects(git_dir: Path) -> dict[str, Path]:
    """
    Return ``{sha: path}`` for every loose object stored under
    ``.git/objects/<XX>/<38-char-suffix>``.
    The ``info/`` and ``pack/`` subdirectories are ignored.
    """
    loose: dict[str, Path] = {}
    obj_dir = git_dir / "objects"
    if not obj_dir.is_dir():
        return loose

    for prefix_dir in obj_dir.iterdir():
        if not prefix_dir.is_dir():
            continue
        name = prefix_dir.name
        if name in ("info", "pack") or len(name) != 2:
            continue
        for obj_file in prefix_dir.iterdir():
            if obj_file.is_file() and len(obj_file.name) == 38:
                loose[name + obj_file.name] = obj_file

    return loose


# ---------------------------------------------------------------------------
# Step 4 — report / prune
# ---------------------------------------------------------------------------

def run_gc(git_dir: Path, prune: bool = False) -> dict:
    """
    Identify loose objects that are not reachable from any ref or the index.

    Args:
        git_dir:  path to the ``.git`` directory
        prune:    when ``True``, delete the unreachable objects from disk
                  and remove any now-empty two-letter prefix directories

    Returns a dict:
        ``reachable``         int   objects reachable from refs/index
        ``loose_total``       int   total loose objects found on disk
        ``unreachable``       int   loose objects NOT in the reachable set
        ``unreachable_bytes`` int   combined size of unreachable objects (bytes)
        ``pruned``            bool  whether pruning was performed
        ``unreachable_shas``  list  sorted list of unreachable SHA-1 strings
    """
    reachable = find_reachable(git_dir)
    loose     = find_loose_objects(git_dir)

    unreachable = {
        sha: path
        for sha, path in loose.items()
        if sha not in reachable
    }

    total_bytes = sum(p.stat().st_size for p in unreachable.values())

    if prune:
        for path in unreachable.values():
            path.unlink()
        # Remove now-empty two-letter prefix directories
        obj_dir = git_dir / "objects"
        for prefix_dir in list(obj_dir.iterdir()):
            if (prefix_dir.is_dir()
                    and len(prefix_dir.name) == 2
                    and prefix_dir.name not in ("info", "pack")):
                try:
                    prefix_dir.rmdir()   # silently ignored if still non-empty
                except OSError:
                    pass

    return {
        "reachable":         len(reachable),
        "loose_total":       len(loose),
        "unreachable":       len(unreachable),
        "unreachable_bytes": total_bytes,
        "pruned":            prune,
        "unreachable_shas":  sorted(unreachable.keys()),
    }
