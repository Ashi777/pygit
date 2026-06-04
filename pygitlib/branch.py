"""
pygitlib/branch.py

Branch and ref management — list, create, delete branches and resolve refs.

Refs are stored as plain text files:
  .git/HEAD              → "ref: refs/heads/main\n"  (or a bare SHA when detached)
  .git/refs/heads/main   → "abc123...\n"              (the commit SHA)

Cross-check with real git:
  $ git branch            # matches list_branches() output
  $ git branch <name>     # matches create_branch()
  $ git branch -d <name>  # matches delete_branch()
"""

from pathlib import Path


# ---------------------------------------------------------------------------
# Ref resolution
# ---------------------------------------------------------------------------

def resolve_ref(git_dir: Path, ref: str) -> str | None:
    """
    Resolve any ref string to a raw 40-char commit SHA-1.

    Handles:
      "HEAD"              → follows the symbolic ref recursively
      "refs/heads/main"   → reads the file at .git/refs/heads/main
      "main"              → short branch name, tries refs/heads/main
      40-char hex string  → returned as-is (assumed to already be a SHA)

    Returns None if the ref cannot be resolved (branch has no commits yet,
    or the ref simply does not exist).
    """
    if ref == "HEAD":
        head = (git_dir / "HEAD").read_text().strip()
        if head.startswith("ref: "):
            return resolve_ref(git_dir, head[5:])
        return head or None

    # Try as a full ref path inside .git (e.g. "refs/heads/main")
    ref_path = git_dir / ref
    if ref_path.exists() and ref_path.is_file():
        return ref_path.read_text().strip() or None

    # Try as a short branch name → refs/heads/<ref>
    branch_path = git_dir / "refs" / "heads" / ref
    if branch_path.exists():
        return branch_path.read_text().strip() or None

    # Try as a short tag name → refs/tags/<ref>
    tag_path = git_dir / "refs" / "tags" / ref
    if tag_path.exists():
        return tag_path.read_text().strip() or None

    # Raw SHA-1 (40 lowercase hex chars)
    if len(ref) == 40 and all(c in "0123456789abcdef" for c in ref.lower()):
        return ref.lower()

    return None


# ---------------------------------------------------------------------------
# HEAD helpers
# ---------------------------------------------------------------------------

def current_branch(git_dir: Path) -> str | None:
    """
    Return the name of the currently checked-out branch.
    Returns None when HEAD is detached (points directly to a SHA).
    """
    head = (git_dir / "HEAD").read_text().strip()
    if head.startswith("ref: refs/heads/"):
        return head[len("ref: refs/heads/"):]
    return None


def set_head_to_branch(git_dir: Path, branch_name: str) -> None:
    """Make HEAD a symbolic ref pointing to branch_name."""
    (git_dir / "HEAD").write_text(f"ref: refs/heads/{branch_name}\n")


def update_branch_ref(git_dir: Path, branch_name: str, sha: str) -> None:
    """Point a branch ref at a specific commit SHA (used after write_commit)."""
    ref_path = git_dir / "refs" / "heads" / branch_name
    ref_path.parent.mkdir(parents=True, exist_ok=True)
    ref_path.write_text(sha + "\n")


# ---------------------------------------------------------------------------
# Branch CRUD
# ---------------------------------------------------------------------------

def list_branches(git_dir: Path) -> list[str]:
    """Return a sorted list of all local branch names."""
    heads_dir = git_dir / "refs" / "heads"
    if not heads_dir.exists():
        return []
    return sorted(p.name for p in heads_dir.iterdir() if p.is_file())


def create_branch(git_dir: Path, name: str, start_point: str = "HEAD") -> str:
    """
    Create a new branch pointing to start_point (defaults to HEAD).

    Returns the commit SHA the new branch points to.
    Raises ValueError when:
      - name is invalid (empty, contains "..", starts with "-", contains "/")
      - a branch with that name already exists
      - start_point cannot be resolved (e.g. no commits yet)
    """
    if not name or ".." in name or name.startswith("-") or "/" in name:
        raise ValueError(f"Invalid branch name: '{name}'")

    branch_path = git_dir / "refs" / "heads" / name
    if branch_path.exists():
        raise ValueError(f"A branch named '{name}' already exists")

    sha = resolve_ref(git_dir, start_point)
    if sha is None:
        raise ValueError(
            f"Not a valid object name: '{start_point}'.\n"
            "You need at least one commit before creating a branch."
        )

    branch_path.parent.mkdir(parents=True, exist_ok=True)
    branch_path.write_text(sha + "\n")
    return sha


def delete_branch(git_dir: Path, name: str) -> None:
    """
    Delete a local branch.
    Raises ValueError when the branch is currently checked out or does not exist.
    """
    if current_branch(git_dir) == name:
        raise ValueError(
            f"error: Cannot delete branch '{name}': it is currently checked out"
        )

    branch_path = git_dir / "refs" / "heads" / name
    if not branch_path.exists():
        raise ValueError(f"error: Branch '{name}' not found")

    branch_path.unlink()
