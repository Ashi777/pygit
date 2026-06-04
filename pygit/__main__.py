#!/usr/bin/env python3
"""
pygit - a Git implementation from scratch.

Usage:
    python -m pygit init
    python -m pygit hash-object [-w] <file>
    python -m pygit cat-file <sha>
    python -m pygit ls-tree <sha>
    python -m pygit log <sha>
    python -m pygit add <file> [<file> ...]
    python -m pygit add .
    python -m pygit status
"""

import os
import sys
import argparse
from pathlib import Path
from pygitlib.repository import init
from pygitlib.objects import (
    get_git_dir, hash_object, cat_file,
    write_tree, read_tree, read_commit, TreeEntry
)
from pygitlib.index import add as stage_files, status as repo_status


def cmd_init(args):
    init(args.path)


def cmd_hash_object(args):
    git_dir = get_git_dir()
    data = Path(args.file).read_bytes()
    sha = hash_object(git_dir, data, write=args.write)
    print(sha)


def cmd_cat_file(args):
    git_dir = get_git_dir()
    obj_type, data = cat_file(git_dir, args.sha)

    if args.type:
        print(obj_type)
    elif args.size:
        print(len(data))
    else:
        if obj_type == "blob":
            sys.stdout.buffer.write(data)
        elif obj_type == "tree":
            entries = read_tree(git_dir, args.sha)
            for e in entries:
                obj_t, _ = cat_file(git_dir, e.sha)
                print(f"{e.mode} {obj_t} {e.sha}\t{e.name}")
        elif obj_type == "commit":
            print(data.decode(), end="")


def cmd_ls_tree(args):
    git_dir = get_git_dir()
    entries = read_tree(git_dir, args.sha)
    for e in entries:
        obj_type, _ = cat_file(git_dir, e.sha)
        print(f"{e.mode} {obj_type} {e.sha}\t{e.name}")


def cmd_log(args):
    git_dir = get_git_dir()
    sha = args.sha
    while sha:
        commit = read_commit(git_dir, sha)
        print(f"\033[33mcommit {sha}\033[0m")
        print(f"Author: {commit.author}")
        print(f"\n    {commit.message}\n")
        sha = commit.parents[0] if commit.parents else None


def cmd_add(args):
    git_dir = get_git_dir()
    work_dir = git_dir.parent

    if args.pathspec == ["."]:
        # Expand "." to every file in the work tree (excluding .git etc.)
        _SKIP = {".git", "__pycache__", ".pytest_cache", ".mypy_cache"}
        paths = []
        for root, dirs, files in os.walk(work_dir):
            dirs[:] = sorted(d for d in dirs if d not in _SKIP)
            root_path = Path(root)
            for fname in sorted(files):
                rel = str((root_path / fname).relative_to(work_dir)).replace("\\", "/")
                paths.append(rel)
    else:
        paths = args.pathspec

    if not paths:
        return

    try:
        stage_files(git_dir, work_dir, paths)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)


def cmd_status(args):
    git_dir = get_git_dir()
    work_dir = git_dir.parent

    # Read current branch name
    head_text = (git_dir / "HEAD").read_text().strip()
    if head_text.startswith("ref: refs/heads/"):
        branch = head_text[len("ref: refs/heads/"):]
    else:
        branch = f"(HEAD detached at {head_text[:7]})"

    print(f"On branch {branch}")

    ref_path = git_dir / "refs" / "heads" / branch
    if not ref_path.exists():
        print("\nNo commits yet\n")

    result = repo_status(git_dir, work_dir)
    staged    = result["staged"]
    unstaged  = result["unstaged"]
    untracked = result["untracked"]

    has_staged   = any(staged[k] for k in staged)
    has_unstaged = any(unstaged[k] for k in unstaged)

    if not has_staged and not has_unstaged and not untracked:
        print("nothing to commit, working tree clean")
        return

    if has_staged:
        print('Changes to be committed:')
        print('  (use "pygit restore --staged <file>" to unstage)\n')
        for path in staged["new_file"]:
            print(f"\tnew file:   {path}")
        for path in staged["modified"]:
            print(f"\tmodified:   {path}")
        for path in staged["deleted"]:
            print(f"\tdeleted:    {path}")
        print()

    if has_unstaged:
        print('Changes not staged for commit:')
        print('  (use "pygit add <file>" to update what will be committed)\n')
        for path in unstaged["modified"]:
            print(f"\tmodified:   {path}")
        for path in unstaged["deleted"]:
            print(f"\tdeleted:    {path}")
        print()

    if untracked:
        print('Untracked files:')
        print('  (use "pygit add <file>" to include in what will be committed)\n')
        for path in untracked:
            print(f"\t{path}")
        print()

    if not has_staged:
        if untracked:
            print("nothing added to commit but untracked files present "
                  "(use \"pygit add\" to track)")
        else:
            print("no changes added to commit (use \"pygit add\")")


def main():
    parser = argparse.ArgumentParser(
        prog="pygit",
        description="A Git implementation from scratch"
    )
    sub = parser.add_subparsers(dest="command")

    p_init = sub.add_parser("init", help="Initialize repository")
    p_init.add_argument("path", nargs="?", default=".", help="Directory")
    p_init.set_defaults(func=cmd_init)

    p_hash = sub.add_parser("hash-object", help="Hash a file as a blob")
    p_hash.add_argument("-w", dest="write", action="store_true",
                        help="Write object to store")
    p_hash.add_argument("file")
    p_hash.set_defaults(func=cmd_hash_object)

    p_cat = sub.add_parser("cat-file", help="Read an object")
    p_cat.add_argument("-p", dest="pretty", action="store_true")
    p_cat.add_argument("-t", dest="type", action="store_true")
    p_cat.add_argument("-s", dest="size", action="store_true")
    p_cat.add_argument("sha")
    p_cat.set_defaults(func=cmd_cat_file)

    p_ls = sub.add_parser("ls-tree", help="List tree contents")
    p_ls.add_argument("sha")
    p_ls.set_defaults(func=cmd_ls_tree)

    p_log = sub.add_parser("log", help="Show commit history")
    p_log.add_argument("sha")
    p_log.set_defaults(func=cmd_log)

    p_add = sub.add_parser("add", help="Stage files")
    p_add.add_argument("pathspec", nargs="+",
                       metavar="file",
                       help="Files to stage, or '.' to stage everything")
    p_add.set_defaults(func=cmd_add)

    p_status = sub.add_parser("status", help="Show working tree status")
    p_status.set_defaults(func=cmd_status)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
