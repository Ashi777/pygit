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
    python -m pygit commit -m <message>
    python -m pygit diff [--staged]
    python -m pygit branch
    python -m pygit branch <name>
    python -m pygit branch -d <name>
    python -m pygit switch <branch>
    python -m pygit switch -c <branch>
    python -m pygit merge <branch>
    python -m pygit restore --staged <file>
    python -m pygit restore <file>
    python -m pygit stash
    python -m pygit stash pop
    python -m pygit stash list
    python -m pygit gc
    python -m pygit gc --prune
    python -m pygit tag
    python -m pygit tag <name>
    python -m pygit tag -a <name> -m <message>
    python -m pygit tag -d <name>
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
from pygitlib.branch import (
    list_branches, create_branch, delete_branch,
    current_branch, resolve_ref,
)
from pygitlib.checkout import switch_branch
from pygitlib.diff import diff_unstaged, diff_staged
from pygitlib.merge import merge_branch
from pygitlib.commit import commit as make_commit
from pygitlib.restore import restore_staged, restore_worktree
from pygitlib.stash import stash_push, stash_pop, stash_list
from pygitlib.gc import run_gc
from pygitlib.tag import list_tags, create_tag, delete_tag, resolve_tag


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
    if sha is None:
        sha = resolve_ref(git_dir, "HEAD")
        if sha is None:
            print("fatal: your current branch has no commits yet")
            sys.exit(1)

    if args.graph:
        from pygitlib.graph import render_graph
        for line in render_graph(git_dir, sha):
            print(line)
        return

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
        # Expand "." to every non-ignored file in the work tree.
        from pygitlib.gitignore import GitIgnore
        gi = GitIgnore(work_dir)
        paths = []
        for root, dirs, files in os.walk(work_dir):
            root_path = Path(root)
            rel_root = str(root_path.relative_to(work_dir)).replace("\\", "/")
            if rel_root == ".":
                rel_root = ""
            gi.load_dir(rel_root)
            dirs[:] = sorted(
                d for d in dirs
                if d != ".git"
                and not gi.is_ignored(
                    f"{rel_root}/{d}" if rel_root else d, is_dir=True
                )
            )
            for fname in sorted(files):
                rel = f"{rel_root}/{fname}" if rel_root else fname
                if not gi.is_ignored(rel):
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
        print('  (use "pygit add <file>" to update what will be committed)')
        print('  (use "pygit restore <file>" to discard changes in working directory)\n')
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


def cmd_commit(args):
    git_dir  = get_git_dir()
    try:
        sha = make_commit(git_dir, args.message)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)

    branch = current_branch(git_dir) or "HEAD"
    # Show "(root-commit)" label for the very first commit on a branch
    head_commit = None
    try:
        from pygitlib.objects import read_commit as _rc
        head_commit = _rc(git_dir, sha)
    except Exception:
        pass
    is_root = head_commit is not None and len(head_commit.parents) == 0
    root_label = " (root-commit)" if is_root else ""
    print(f"[{branch}{root_label} {sha[:7]}] {args.message}")


def cmd_diff(args):
    git_dir = get_git_dir()
    work_dir = git_dir.parent

    if args.staged:
        entries = diff_staged(git_dir)
    else:
        entries = diff_unstaged(git_dir, work_dir)

    if not entries:
        return   # nothing to show, no output (same as real git)

    for _path, diff_text in entries:
        sys.stdout.write(diff_text)


def cmd_merge(args):
    git_dir = get_git_dir()
    work_dir = git_dir.parent
    result = merge_branch(git_dir, work_dir, args.branch)
    print(result.message)
    if not result.success:
        sys.exit(1)


def cmd_branch(args):
    git_dir = get_git_dir()

    if args.delete:
        if not args.name:
            print("error: branch name required with -d", file=sys.stderr)
            sys.exit(1)
        try:
            delete_branch(git_dir, args.name)
            print(f"Deleted branch {args.name}")
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(1)

    elif args.name:
        try:
            sha = create_branch(git_dir, args.name)
            # git branch <name> prints nothing on success (same as real git)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(1)

    else:
        # List all branches; mark the current one with *
        cur = current_branch(git_dir)
        branches = list_branches(git_dir)
        if not branches:
            print("  (no branches yet — make your first commit)")
            return
        for b in branches:
            marker = "* " if b == cur else "  "
            print(f"{marker}{b}")


def cmd_switch(args):
    git_dir = get_git_dir()
    work_dir = git_dir.parent
    try:
        switch_branch(git_dir, work_dir, args.branch, create=args.create)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)


def cmd_tag(args):
    git_dir = get_git_dir()

    if args.delete:
        if not args.name:
            print("error: tag name required with -d", file=sys.stderr)
            sys.exit(1)
        try:
            delete_tag(git_dir, args.name)
            print(f"Deleted tag '{args.name}'")
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(1)

    elif args.name:
        # Create lightweight or annotated tag
        message = args.message   # None for lightweight
        target  = args.commit or "HEAD"
        try:
            sha = create_tag(git_dir, args.name, target=target, message=message)
            if message is not None:
                print(f"Created annotated tag '{args.name}' ({sha[:7]})")
            # lightweight tags print nothing on success (same as real git)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(1)

    else:
        # List all tags
        for name in list_tags(git_dir):
            print(name)


def cmd_gc(args):
    git_dir = get_git_dir()
    result  = run_gc(git_dir, prune=args.prune)

    if result["unreachable"] == 0:
        print("Nothing to collect.")
        if result["loose_total"] > 0:
            print(f"  {result['loose_total']} loose object(s), all reachable.")
        return

    n     = result["unreachable"]
    size  = result["unreachable_bytes"]
    label = f"{n} unreachable loose object{'s' if n != 1 else ''} ({size} bytes)"

    if args.verbose:
        for sha in result["unreachable_shas"]:
            print(f"unreachable  {sha}")

    if args.prune:
        print(f"Deleted {label}.")
    else:
        print(f"Found {label}.")
        print("Run 'pygit gc --prune' to delete them.")


def cmd_stash(args):
    git_dir  = get_git_dir()
    work_dir = git_dir.parent
    subcmd   = args.stash_cmd or "push"

    if subcmd == "push":
        try:
            stash_push(git_dir, work_dir)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(1)

    elif subcmd == "pop":
        try:
            stash_pop(git_dir, work_dir)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(1)

    elif subcmd == "list":
        for i, msg in stash_list(git_dir):
            print(f"stash@{{{i}}}: {msg}")


def cmd_serve(args):
    try:
        from pygitlib.serve import serve
    except ImportError:
        print("error: flask is required — install it with: pip install flask",
              file=sys.stderr)
        sys.exit(1)
    git_dir = get_git_dir()
    serve(git_dir, port=args.port, open_browser=not args.no_browser)


def cmd_restore(args):
    git_dir  = get_git_dir()
    work_dir = git_dir.parent

    if args.staged:
        errors = restore_staged(git_dir, args.pathspec)
    else:
        errors = restore_worktree(git_dir, work_dir, args.pathspec)

    for p in errors:
        print(
            f"error: pathspec '{p}' did not match any file(s) known to pygit",
            file=sys.stderr,
        )
    if errors:
        sys.exit(1)


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
    p_log.add_argument("sha", nargs="?", default=None,
                       help="Starting commit SHA (default: HEAD)")
    p_log.add_argument("--graph", action="store_true",
                       help="Draw ASCII branch graph")
    p_log.set_defaults(func=cmd_log)

    p_add = sub.add_parser("add", help="Stage files")
    p_add.add_argument("pathspec", nargs="+",
                       metavar="file",
                       help="Files to stage, or '.' to stage everything")
    p_add.set_defaults(func=cmd_add)

    p_status = sub.add_parser("status", help="Show working tree status")
    p_status.set_defaults(func=cmd_status)

    p_commit = sub.add_parser("commit", help="Record staged changes as a commit")
    p_commit.add_argument("-m", dest="message", required=True,
                          metavar="message", help="Commit message")
    p_commit.set_defaults(func=cmd_commit)

    p_branch = sub.add_parser("branch", help="List, create, or delete branches")
    p_branch.add_argument("-d", "-D", dest="delete", action="store_true",
                          help="Delete a branch")
    p_branch.add_argument("name", nargs="?", help="Branch name")
    p_branch.set_defaults(func=cmd_branch)

    p_switch = sub.add_parser("switch", help="Switch to a branch")
    p_switch.add_argument("-c", dest="create", action="store_true",
                          help="Create and switch to a new branch")
    p_switch.add_argument("branch", help="Branch to switch to")
    p_switch.set_defaults(func=cmd_switch)

    p_merge = sub.add_parser("merge", help="Merge a branch into the current branch")
    p_merge.add_argument("branch", help="Branch to merge")
    p_merge.set_defaults(func=cmd_merge)

    p_diff = sub.add_parser("diff", help="Show changes")
    p_diff.add_argument(
        "--staged", "--cached", dest="staged", action="store_true",
        help="Show staged changes (index vs HEAD)",
    )
    p_diff.set_defaults(func=cmd_diff)

    p_restore = sub.add_parser("restore", help="Restore working tree or index files")
    p_restore.add_argument(
        "--staged", "-S", dest="staged", action="store_true",
        help="Restore the index (unstage the file)",
    )
    p_restore.add_argument(
        "pathspec", nargs="+", metavar="file",
        help="File(s) to restore",
    )
    p_restore.set_defaults(func=cmd_restore)

    p_tag = sub.add_parser("tag", help="Create, list, or delete tags")
    p_tag.add_argument("-a", dest="annotated", action="store_true",
                       help="Create an annotated tag object")
    p_tag.add_argument("-m", dest="message", metavar="message",
                       help="Tag message (implies -a / annotated tag)")
    p_tag.add_argument("-d", dest="delete", action="store_true",
                       help="Delete a tag")
    p_tag.add_argument("name",   nargs="?", default=None, help="Tag name")
    p_tag.add_argument("commit", nargs="?", default=None,
                       help="Commit to tag (default: HEAD)")
    p_tag.set_defaults(func=cmd_tag)

    p_gc = sub.add_parser("gc",
                          help="Report (and optionally remove) unreachable loose objects")
    p_gc.add_argument("--prune", action="store_true",
                      help="Delete unreachable objects (default: report only)")
    p_gc.add_argument("--verbose", "-v", action="store_true",
                      help="List the SHA of each unreachable object")
    p_gc.set_defaults(func=cmd_gc)

    p_stash = sub.add_parser("stash",
                             help="Save and restore the working directory state")
    stash_sub = p_stash.add_subparsers(dest="stash_cmd")
    stash_sub.add_parser("push", help="Save changes to the stash (default)")
    stash_sub.add_parser("pop",  help="Restore and drop the most recent stash")
    stash_sub.add_parser("list", help="List all stash entries")
    p_stash.set_defaults(func=cmd_stash)

    p_serve = sub.add_parser("serve", help="Launch the web commit-graph visualizer")
    p_serve.add_argument("--port", type=int, default=5000,
                         help="Port to listen on (default: 5000)")
    p_serve.add_argument("--no-browser", action="store_true",
                         help="Don't open the browser automatically")
    p_serve.set_defaults(func=cmd_serve)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
