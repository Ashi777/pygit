#!/usr/bin/env python3
"""
pygit - a Git implementation from scratch.

Usage:
    python -m pygit init
    python -m pygit hash-object [-w] <file>
    python -m pygit cat-file <sha>
    python -m pygit ls-tree <sha>
    python -m pygit log <sha>
"""

import sys
import argparse
from pathlib import Path
from pygitlib.repository import init
from pygitlib.objects import (
    get_git_dir, hash_object, cat_file,
    write_tree, read_tree, read_commit, TreeEntry
)


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

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
