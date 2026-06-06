"""
pygitlib/diff.py

Myers O(ND) diff algorithm and unified diff formatting.

The Myers algorithm finds the *shortest edit script* (SES) between two
sequences — the minimum number of insertions and deletions needed to
transform sequence A into sequence B.  This is the same algorithm that
real git uses for 'git diff'.

Output format is byte-compatible with real git:
  diff --git a/path b/path
  index <old_sha>..<new_sha> <mode>
  --- a/path
  +++ b/path
  @@ -old_start[,old_count] +new_start[,new_count] @@
   context line
  -deleted line
  +added line

Cross-check with real git:
  $ git diff            # matches diff_unstaged()
  $ git diff --staged   # matches diff_staged()
"""

from collections.abc import Generator
from pathlib import Path
from typing import Any

from .objects import hash_object, read_object, read_commit, read_tree
from .index import read_index, IndexEntry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_binary(data: bytes) -> bool:
    """True if data looks like a binary file (contains a null byte in first 8 KB)."""
    return b"\x00" in data[:8000]


def _decode_lines(data: bytes) -> list[str] | None:
    """
    Decode raw bytes into a list of lines (no trailing newlines).
    Returns None for binary files.
    """
    if _is_binary(data):
        return None
    return data.decode("utf-8", errors="replace").splitlines()


# ---------------------------------------------------------------------------
# Myers O(ND) diff — forward pass
# ---------------------------------------------------------------------------

def _shortest_edit(a: list[Any], b: list[Any]) -> list[list[int]]:
    """
    Myers forward pass.  Returns the *trace* — a snapshot of the furthest-
    reaching x-coordinate on each diagonal k after each edit step d.

    V is a flat array indexed by k + max_d (to handle negative k).
    V[k] = the largest x such that there is a d-path ending at (x, x-k).
    """
    n, m = len(a), len(b)
    max_d = n + m
    v = [0] * (2 * max_d + 1)
    trace: list[list[int]] = []

    for d in range(max_d + 1):
        trace.append(v[:])                  # snapshot before this step
        for k in range(-d, d + 1, 2):
            idx = k + max_d
            # Choose: move right (delete from a) or down (insert from b)
            if k == -d or (k != d and v[idx - 1] < v[idx + 1]):
                x = v[idx + 1]              # down  → insert b[y]
            else:
                x = v[idx - 1] + 1          # right → delete a[x]
            y = x - k
            # Extend snake (matching characters = free diagonal moves)
            while x < n and y < m and a[x] == b[y]:
                x += 1
                y += 1
            v[idx] = x
            if x >= n and y >= m:
                return trace                # shortest path found
    return trace                            # fallback (shouldn't happen)


# ---------------------------------------------------------------------------
# Myers O(ND) diff — backtrack
# ---------------------------------------------------------------------------

def _backtrack(trace: list[list[int]], a: list[Any], b: list[Any]) -> list[tuple[str, str]]:
    """
    Reconstruct the edit script by walking the trace in reverse.
    Returns list of (op, element) where op is '=', '+', or '-'.
    """
    n, m = len(a), len(b)
    max_d = n + m
    x, y = n, m
    ops: list[tuple[str, str]] = []

    for d in range(len(trace) - 1, 0, -1):
        v = trace[d]
        k = x - y
        idx = k + max_d

        # Reproduce the direction decision made at step d
        if k == -d or (k != d and v[idx - 1] < v[idx + 1]):
            prev_k = k + 1          # came from diagonal k+1 → insert
        else:
            prev_k = k - 1          # came from diagonal k-1 → delete

        prev_x = v[prev_k + max_d]
        prev_y = prev_x - prev_k

        # Walk the snake backwards (diagonal = '=' matches)
        while x > prev_x and y > prev_y:
            x -= 1
            y -= 1
            ops.append(("=", a[x]))

        # The single edit that preceded the snake
        if prev_k == k + 1:         # came from k+1 → insert
            y -= 1
            ops.append(("+", b[y]))
        else:                       # came from k-1 → delete
            x -= 1
            ops.append(("-", a[x]))

    # Any remaining snake at d=0 (matches from the very beginning)
    while x > 0 and y > 0:
        x -= 1
        y -= 1
        ops.append(("=", a[x]))

    ops.reverse()
    return ops


# ---------------------------------------------------------------------------
# Public diff API
# ---------------------------------------------------------------------------

def myers_diff(a: list[Any], b: list[Any]) -> list[tuple[str, str]]:
    """
    Compute the shortest edit script from sequence *a* to sequence *b*.

    Returns a list of (op, element) tuples:
      ('=', elem)  — element is the same in both sequences
      ('-', elem)  — element was in a but not b  (delete)
      ('+', elem)  — element is in b but not a   (insert)

    Applying the '+' and '=' elements in order reconstructs *b*.
    Equivalent to: git diff (core algorithm)
    """
    if not a and not b:
        return []
    if not a:
        return [("+", x) for x in b]
    if not b:
        return [("-", x) for x in a]
    if a == b:
        return [("=", x) for x in a]

    trace = _shortest_edit(a, b)
    return _backtrack(trace, a, b)


# ---------------------------------------------------------------------------
# Unified diff formatting
# ---------------------------------------------------------------------------

def _hunk_range(start: int, count: int) -> str:
    """
    Format one side of an @@ hunk header.
    Git omits the count when it is 1; shows "start,0" when count is 0.
    """
    if count == 0:
        return f"{start},0"
    if count == 1:
        return str(start)
    return f"{start},{count}"


def _build_hunks(
    edits: list[tuple[str, str]], context: int = 3
) -> Generator[tuple[int, int, list[tuple[str, str, int, int]]], None, None]:
    """
    Partition an edit script into *hunks* — groups of nearby changes
    surrounded by up to *context* unchanged lines on each side.

    Yields (old_start, new_start, hunk_ops) where:
      old_start / new_start: 0-indexed positions in the respective sequences
      hunk_ops: annotated ops [(op, line, old_pos, new_pos), ...]
    """
    # Annotate every edit with its old/new line positions
    annotated: list[tuple[str, str, int, int]] = []
    old_pos = new_pos = 0
    for op, line in edits:
        annotated.append((op, line, old_pos, new_pos))
        if op in ("=", "-"):
            old_pos += 1
        if op in ("=", "+"):
            new_pos += 1

    change_idx = [i for i, (op, *_) in enumerate(annotated) if op != "="]
    if not change_idx:
        return

    i = 0
    while i < len(change_idx):
        # Expand hunk: merge nearby changes
        j = i
        while j + 1 < len(change_idx):
            if change_idx[j + 1] - change_idx[j] <= 2 * context:
                j += 1
            else:
                break

        lo = max(0, change_idx[i] - context)
        hi = min(len(annotated), change_idx[j] + context + 1)
        chunk = annotated[lo:hi]

        yield chunk[0][2], chunk[0][3], chunk
        i = j + 1


def format_unified_diff(
    old_lines: list[str],
    new_lines: list[str],
    old_name: str,
    new_name: str,
    context: int = 3,
) -> str:
    """
    Format a unified diff between *old_lines* and *new_lines*.

    *old_name* / *new_name* are written after '---' / '+++'.
    Returns an empty string when the sequences are identical.

    The caller is responsible for prepending the 'diff --git' and
    'index' header lines (see diff_unstaged / diff_staged).
    """
    edits = myers_diff(old_lines, new_lines)
    if all(op == "=" for op, _ in edits):
        return ""

    out: list[str] = [f"--- {old_name}", f"+++ {new_name}"]

    for old_start, new_start, chunk in _build_hunks(edits, context):
        old_count = sum(1 for op, *_ in chunk if op in ("=", "-"))
        new_count = sum(1 for op, *_ in chunk if op in ("=", "+"))

        # @@ header uses 1-indexed start lines
        old_hdr = _hunk_range(old_start + 1 if old_count else 0, old_count)
        new_hdr = _hunk_range(new_start + 1 if new_count else 0, new_count)
        out.append(f"@@ -{old_hdr} +{new_hdr} @@")

        for op, line, *_ in chunk:
            prefix = {" =": " ", "=": " ", "-": "-", "+": "+"}
            out.append(f"{'-' if op == '-' else '+' if op == '+' else ' '}{line}")

    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# HEAD tree helper
# ---------------------------------------------------------------------------

def _head_tree(git_dir: Path) -> dict[str, tuple[str, str]]:
    """
    Return {path: (blob_sha, mode_str)} for every file in the HEAD commit.
    Returns {} when there are no commits yet.
    """
    from .branch import resolve_ref

    sha = resolve_ref(git_dir, "HEAD")
    if not sha:
        return {}
    try:
        commit = read_commit(git_dir, sha)
    except Exception:
        return {}

    def _flatten(tree_sha: str, prefix: str) -> dict[str, tuple[str, str]]:
        result: dict[str, tuple[str, str]] = {}
        for entry in read_tree(git_dir, tree_sha):
            path = f"{prefix}/{entry.name}" if prefix else entry.name
            if entry.mode in ("040000", "40000"):
                result.update(_flatten(entry.sha, path))
            else:
                result[path] = (entry.sha, entry.mode)
        return result

    return _flatten(commit.tree, "")


# ---------------------------------------------------------------------------
# High-level diff operations
# ---------------------------------------------------------------------------

def diff_unstaged(git_dir: Path, work_dir: Path) -> list[tuple[str, str]]:
    """
    Diff the working tree against the index.
    Equivalent to: git diff

    Returns a list of (path, diff_text) for every file that differs.
    diff_text is a complete git-compatible diff string including headers.
    """
    results: list[tuple[str, str]] = []

    for entry in read_index(git_dir):
        path = entry.path
        fp = work_dir / path

        if not fp.exists():
            # Deleted from working tree
            _, blob = read_object(git_dir, entry.sha)
            old_lines = _decode_lines(blob)
            header = (
                f"diff --git a/{path} b/{path}\n"
                f"deleted file mode {entry.mode:o}\n"
                f"index {entry.sha[:7]}..0000000\n"
            )
            if old_lines is None:
                results.append((path, header + f"Binary file a/{path} deleted\n"))
            else:
                body = format_unified_diff(old_lines, [], f"a/{path}", "/dev/null")
                results.append((path, header + body))

        else:
            disk_bytes = fp.read_bytes()
            disk_sha = hash_object(git_dir, disk_bytes, write=False)
            if disk_sha == entry.sha:
                continue                # no change

            _, blob = read_object(git_dir, entry.sha)
            old_lines = _decode_lines(blob)
            new_lines = _decode_lines(disk_bytes)
            header = (
                f"diff --git a/{path} b/{path}\n"
                f"index {entry.sha[:7]}..{disk_sha[:7]} {entry.mode:o}\n"
            )
            if old_lines is None or new_lines is None:
                results.append((path, header + f"Binary files a/{path} and b/{path} differ\n"))
            else:
                body = format_unified_diff(old_lines, new_lines, f"a/{path}", f"b/{path}")
                results.append((path, header + body))

    return results


def diff_staged(git_dir: Path) -> list[tuple[str, str]]:
    """
    Diff the index against HEAD.
    Equivalent to: git diff --staged  /  git diff --cached

    Returns a list of (path, diff_text) for every file that differs.
    """
    head = _head_tree(git_dir)
    index: dict[str, IndexEntry] = {e.path: e for e in read_index(git_dir)}

    results: list[tuple[str, str]] = []

    for path in sorted(set(head) | set(index)):
        in_head = path in head
        in_idx = path in index

        if in_idx:
            mode_str = f"{index[path].mode:o}"
            idx_sha = index[path].sha
        else:
            mode_str = "100644"
            idx_sha = "0000000"

        head_sha = head[path][0] if in_head else None

        if in_head and not in_idx:
            # Deleted in index
            assert head_sha is not None
            _, blob = read_object(git_dir, head_sha)
            old_lines = _decode_lines(blob)
            header = (
                f"diff --git a/{path} b/{path}\n"
                f"deleted file mode {head[path][1]}\n"
                f"index {head_sha[:7]}..0000000\n"
            )
            if old_lines is None:
                results.append((path, header + f"Binary file a/{path} deleted\n"))
            else:
                body = format_unified_diff(old_lines, [], f"a/{path}", "/dev/null")
                results.append((path, header + body))

        elif not in_head and in_idx:
            # New file in index
            _, blob = read_object(git_dir, idx_sha)
            new_lines = _decode_lines(blob)
            header = (
                f"diff --git a/{path} b/{path}\n"
                f"new file mode {mode_str}\n"
                f"index 0000000..{idx_sha[:7]}\n"
            )
            if new_lines is None:
                results.append((path, header + f"Binary file b/{path} created\n"))
            else:
                body = format_unified_diff([], new_lines, "/dev/null", f"b/{path}")
                results.append((path, header + body))

        elif head_sha != idx_sha:
            # Modified
            assert head_sha is not None
            _, old_blob = read_object(git_dir, head_sha)
            _, new_blob = read_object(git_dir, idx_sha)
            old_lines = _decode_lines(old_blob)
            new_lines = _decode_lines(new_blob)
            header = (
                f"diff --git a/{path} b/{path}\n"
                f"index {head_sha[:7]}..{idx_sha[:7]} {mode_str}\n"
            )
            if old_lines is None or new_lines is None:
                results.append((path, header + f"Binary files a/{path} and b/{path} differ\n"))
            else:
                body = format_unified_diff(old_lines, new_lines, f"a/{path}", f"b/{path}")
                results.append((path, header + body))

    return results
