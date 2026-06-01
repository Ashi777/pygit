"""
pygit/repository.py

Handles repository initialization — creating the .git directory
with the exact structure that real Git expects.
"""

from pathlib import Path
from .objects import get_git_dir


def init(path: str = ".") -> Path:
    """
    Initialize a new Git repository.
    Equivalent to: git init

    Creates:
        .git/
        .git/objects/          — object store
        .git/refs/
        .git/refs/heads/       — branch pointers
        .git/refs/tags/        — tag pointers
        .git/HEAD              — points to current branch
        .git/config            — repo config
        .git/description       — ignored by pygit, present for compatibility
    """
    work_dir = Path(path).resolve()
    git_dir = work_dir / ".git"

    if git_dir.exists():
        print(f"Reinitialized existing Git repository in {git_dir}")
    else:
        print(f"Initialized empty Git repository in {git_dir}")

    # Create directory structure
    for subdir in ["objects", "objects/info", "objects/pack",
                   "refs", "refs/heads", "refs/tags"]:
        (git_dir / subdir).mkdir(parents=True, exist_ok=True)

    # HEAD starts pointing to 'main' branch (even before first commit)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")

    # Minimal config file
    (git_dir / "config").write_text(
        "[core]\n"
        "\trepositoryformatversion = 0\n"
        "\tfilemode = true\n"
        "\tbare = false\n"
        "\tlogallrefupdates = true\n"
    )

    (git_dir / "description").write_text(
        "Unnamed repository; edit this file 'description' "
        "to name the repository.\n"
    )

    return git_dir
