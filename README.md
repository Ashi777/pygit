# PyGit — Git Implementation from Scratch

A byte-for-byte compatible Git implementation written in pure Python.
Produces identical SHA-1 hashes to the real `git` binary.
No external dependencies — only Python standard library.

## What this implements
- Content-addressable object storage (blobs, trees, commits)
- SHA-1 hashing + zlib compression (identical format to real Git)
- Binary tree format with correct entry sorting
- Commit object parsing and serialization

## Usage
    git init .
    python3 -m pygit hash-object -w file.txt
    python3 -m pygit cat-file -p <sha>
    python3 -m pygit ls-tree <tree-sha>
    python3 -m pygit log <commit-sha>

## Run tests
    pip install pytest
    pytest tests/ -v
    # → 14 passed, cross-validated against real git

## Tech stack
Python 3.8+, stdlib only: hashlib, zlib, struct, argparse