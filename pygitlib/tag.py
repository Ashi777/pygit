"""
pygitlib/tag.py

git tag — lightweight and annotated tags.

Lightweight tag
───────────────
  A plain ref file at .git/refs/tags/<name> that points directly to a
  commit SHA.  Takes one line to create; nothing is added to the object
  database.
  Created with:  pygit tag <name>

Annotated tag
─────────────
  A full "tag" object is written to the object database, then a ref at
  .git/refs/tags/<name> is made to point to that tag object's SHA.
  The tag object stores the tagger identity, a message, and the SHA of the
  tagged commit.
  Created with:  pygit tag -a <name> -m <message>

Tag object format (text, stored like commit / tree / blob objects)
──────────────────────────────────────────────────────────────────
  object <sha-of-tagged-commit>
  type commit
  tag <tagname>
  tagger <name> <email> <timestamp> <tz>

  <message>
"""

from dataclasses import dataclass
from pathlib import Path

from .objects import write_object, read_object
from .branch import resolve_ref
from .commit import _make_identity


# ---------------------------------------------------------------------------
# Tag object data class + serialisation
# ---------------------------------------------------------------------------

@dataclass
class Tag:
    """Represents a git annotated tag object."""
    obj_sha:  str   # SHA-1 of the tagged object (usually a commit)
    obj_type: str   # type of the tagged object  ("commit", "tree", "blob")
    name:     str   # tag name
    tagger:   str   # identity string: "Name <email> timestamp tz"
    message:  str   # tag message


def encode_tag(tag: Tag) -> bytes:
    """Serialise a Tag into the git tag-object wire format."""
    lines = [
        f"object {tag.obj_sha}",
        f"type {tag.obj_type}",
        f"tag {tag.name}",
        f"tagger {tag.tagger}",
        "",
        tag.message,
    ]
    return "\n".join(lines).encode()


def decode_tag(data: bytes) -> Tag:
    """Parse a raw tag-object payload back into a Tag."""
    text = data.decode()
    obj_sha = obj_type = name = tagger = ""
    message_lines: list[str] = []
    reading_message = False

    for line in text.split("\n"):
        if reading_message:
            message_lines.append(line)
        elif line == "":
            reading_message = True
        elif line.startswith("object "):
            obj_sha = line[7:]
        elif line.startswith("type "):
            obj_type = line[5:]
        elif line.startswith("tag "):
            name = line[4:]
        elif line.startswith("tagger "):
            tagger = line[7:]

    return Tag(
        obj_sha=obj_sha,
        obj_type=obj_type,
        name=name,
        tagger=tagger,
        message="\n".join(message_lines).strip(),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tag_path(git_dir: Path, name: str) -> Path:
    return git_dir / "refs" / "tags" / name


def _validate_name(name: str) -> None:
    """Reject names that git would also reject."""
    if not name:
        raise ValueError("tag name cannot be empty")
    if ".." in name:
        raise ValueError(f"Invalid tag name '{name}': contains '..'")
    if name.startswith("-"):
        raise ValueError(f"Invalid tag name '{name}': starts with '-'")
    if name.endswith(".lock"):
        raise ValueError(f"Invalid tag name '{name}': ends with '.lock'")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def list_tags(git_dir: Path) -> list[str]:
    """Return a sorted list of all tag names."""
    tags_dir = git_dir / "refs" / "tags"
    if not tags_dir.exists():
        return []
    return sorted(p.name for p in tags_dir.iterdir() if p.is_file())


def create_tag(git_dir: Path, name: str,
               target: str = "HEAD",
               message: str | None = None) -> str:
    """
    Create a tag named *name* pointing to *target*.

    Lightweight (message=None):
      Writes .git/refs/tags/<name> → <commit_sha>.

    Annotated (message given):
      Writes a tag object to the object store, then writes
      .git/refs/tags/<name> → <tag_object_sha>.

    Returns the SHA stored in the ref (tag object SHA for annotated,
    commit SHA for lightweight).

    Raises ValueError for: invalid name, duplicate name, unresolvable target.
    """
    _validate_name(name)

    tag_path = _tag_path(git_dir, name)
    if tag_path.exists():
        raise ValueError(f"tag '{name}' already exists")

    target_sha = resolve_ref(git_dir, target)
    if target_sha is None:
        raise ValueError(
            f"Not a valid object name: '{target}'\n"
            "You need at least one commit before creating a tag."
        )

    if message is None:
        # Lightweight: ref points directly to the commit
        ref_sha = target_sha
    else:
        # Annotated: write a tag object first
        identity = _make_identity(git_dir)

        # Determine what type of object we're tagging
        try:
            obj_type, _ = read_object(git_dir, target_sha)
        except Exception:
            obj_type = "commit"

        tag_obj = Tag(
            obj_sha=target_sha,
            obj_type=obj_type,
            name=name,
            tagger=identity,
            message=message,
        )
        ref_sha = write_object(git_dir, "tag", encode_tag(tag_obj))

    tag_path.parent.mkdir(parents=True, exist_ok=True)
    tag_path.write_text(ref_sha + "\n")
    return ref_sha


def delete_tag(git_dir: Path, name: str) -> None:
    """
    Delete the tag *name*.
    Raises ValueError when the tag does not exist.
    """
    tag_path = _tag_path(git_dir, name)
    if not tag_path.exists():
        raise ValueError(f"error: tag '{name}' not found")
    tag_path.unlink()


def resolve_tag(git_dir: Path, name: str) -> tuple[str, str | None]:
    """
    Resolve a tag name to the commit it points at.

    Returns ``(commit_sha, message_or_None)``:
      - Lightweight tag  →  (commit_sha, None)
      - Annotated tag    →  (commit_sha, message_string)

    Raises ValueError when the tag does not exist.
    """
    tag_path = _tag_path(git_dir, name)
    if not tag_path.exists():
        raise ValueError(f"error: tag '{name}' not found")

    ref_sha = tag_path.read_text().strip()

    try:
        obj_type, data = read_object(git_dir, ref_sha)
    except Exception:
        return ref_sha, None

    if obj_type == "tag":
        tag = decode_tag(data)
        return tag.obj_sha, tag.message

    # Lightweight: ref already points to the commit
    return ref_sha, None
