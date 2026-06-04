# PyGit — Git Implementation from Scratch

A byte-for-byte compatible Git implementation written in pure Python.
Produces identical SHA-1 hashes and object formats to the real `git` binary.
No external dependencies — only the Python standard library.

## What this implements

**Object storage (Phase 1)**
- Content-addressable object store: blobs, trees, commits
- SHA-1 hashing + zlib compression in the exact Git binary format
- `hash-object`, `cat-file`, `ls-tree`, `log`

**Staging area (Phase 2)**
- Binary index file (version 2, 8-byte aligned entries, SHA-1 trailer)
- `add`, `status` with three-way comparison (HEAD / index / working tree)

**Committing (Phase 3)**
- Tree builder that handles nested directories
- Reads user identity from `.git/config` or `~/.gitconfig`
- `commit -m`

**Diffing (Phase 4)**
- Myers O(ND) algorithm — the same algorithm real Git uses
- Unified diff output byte-compatible with `git diff`
- `diff` (unstaged) and `diff --staged`

**Branching, switching, merging (Phase 5)**
- Ref management, symbolic HEAD, branch create/delete
- Safe checkout that refuses to switch with a dirty working tree
- Three-way merge with LCA detection via BFS, fast-forward detection,
  and conflict markers (`<<<<<<< / ======= / >>>>>>>`)
- `branch`, `switch`, `merge`

## Setup

```
git clone https://github.com/your-username/pygit.git
cd pygit
pip install -e .
```

After installing, both `python -m pygit` and the `pygit` command work.

## Usage

### Initialize a repository

```
pygit init [path]
```

### Hash and store a file

```
pygit hash-object -w <file>        # hash and write to object store
pygit hash-object <file>           # hash only, don't write
```

### Inspect objects

```
pygit cat-file -p <sha>            # pretty-print content
pygit cat-file -t <sha>            # print object type (blob/tree/commit)
pygit cat-file -s <sha>            # print object size in bytes
pygit ls-tree <tree-sha>           # list tree entries
```

### View history

```
pygit log <commit-sha>             # walk commit history from given SHA
```

### Stage and commit

```
pygit add <file> [<file> ...]      # stage specific files
pygit add .                        # stage all files
pygit status                       # show staged, unstaged, and untracked files
pygit commit -m "message"          # record staged changes as a commit
```

### Diff

```
pygit diff                         # working tree vs index (unstaged changes)
pygit diff --staged                # index vs HEAD (staged changes)
pygit diff --cached                # alias for --staged
```

### Branches

```
pygit branch                       # list all branches
pygit branch <name>                # create a new branch at HEAD
pygit branch -d <name>             # delete a branch
pygit switch <branch>              # switch to an existing branch
pygit switch -c <branch>           # create and switch in one step
pygit merge <branch>               # merge a branch into the current branch
```

## Run tests

```
pip install pytest
pytest tests/ -v
# → 126 passed, cross-validated against real git
```

Test files cover every subsystem:

| File | What it tests |
|------|---------------|
| `tests/test_objects.py` | Object storage, hashing, tree/commit serialization |
| `tests/test_index.py` | Staging area, index parsing, `add` / `status` |
| `tests/test_commit.py` | Commit creation, tree building, identity reading |
| `tests/test_diff.py` | Myers diff algorithm, unified diff formatting |
| `tests/test_branch.py` | Ref management, branch create/delete/switch |
| `tests/test_merge.py` | Three-way merge, fast-forward, conflict markers |

## Project structure

```
pygit/           CLI entry point (__main__.py) and package marker
pygitlib/
  objects.py     Object store: blobs, trees, commits (read/write)
  index.py       Binary index (staging area): add, status
  commit.py      Commit creation, tree building
  diff.py        Myers O(ND) diff, unified diff formatter
  branch.py      Ref management, HEAD, branch CRUD
  checkout.py    Safe branch switching, working-tree rewrite
  merge.py       Three-way merge, BFS merge-base, conflict markers
  repository.py  Repository initialization (git init)
tests/           Pytest test suite (126 tests)
```

## Tech stack

Python 3.10+, stdlib only: `hashlib`, `zlib`, `struct`, `argparse`, `pathlib`
