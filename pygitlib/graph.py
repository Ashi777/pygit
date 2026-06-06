"""
pygitlib/graph.py

ASCII graph renderer for git log --graph.

Algorithm:
  1. Collect all commits reachable from start_sha via iterative DFS.
  2. Topological sort: children before parents (reversed DFS post-order).
  3. Maintain a list of "lanes" — each position holds the SHA expected next
     in that column.
  4. For each commit:
       a. Find its column (leftmost lane containing its SHA; create a new
          rightmost lane if not found).
       b. Emit the commit row:  "*" at its column, "|" at every other lane.
       c. Update lanes: replace the commit's SHA with its first parent;
          append additional parents as new rightmost lanes; deduplicate
          (when a parent already exists in another lane, collapse).
       d. Emit connector rows when the lane count changes:
            expansion  (merge commit)  →  "|\" row
            contraction (convergence)  →  "|/" rows (one per closed lane)
"""

from pathlib import Path
from .objects import read_commit, Commit

_YELLOW = "\033[33m"
_RESET  = "\033[0m"


# ---------------------------------------------------------------------------
# Step 1 — collect commits
# ---------------------------------------------------------------------------

def _collect(git_dir: Path, sha: str) -> tuple[dict[str, Commit], list[str]]:
    """
    Return (commits, topo_order) for all commits reachable from sha.
    topo_order is children-before-parents (reversed DFS post-order).
    Iterative to avoid Python recursion limits on deep histories.
    """
    commits: dict[str, Commit] = {}
    temp_visited: set[str] = set()
    done: set[str] = set()
    post_order: list[str] = []

    # Stack entries: (sha, children_already_pushed)
    stack: list[tuple[str, bool]] = [(sha, False)]

    while stack:
        node, pushed = stack[-1]

        if pushed:
            stack.pop()
            if node not in done:
                done.add(node)
                post_order.append(node)
            continue

        stack[-1] = (node, True)

        if node in temp_visited:
            continue
        temp_visited.add(node)

        try:
            c = read_commit(git_dir, node)
        except Exception:
            continue
        commits[node] = c

        # Push parents in reverse so the first parent is processed first
        for p in reversed(c.parents):
            if p not in temp_visited:
                stack.append((p, False))

    post_order.reverse()   # now: children first
    return commits, post_order


# ---------------------------------------------------------------------------
# Step 2 — connector row generation
# ---------------------------------------------------------------------------

def _connector_rows(n_old: int, n_new: int) -> list[str]:
    """
    Return the connector lines between two consecutive commit rows when the
    active lane count changes.

    Expansion  (n_new > n_old, after a merge commit):
      One row — existing lanes keep '|', each new lane gets '\\'.
      1→2:  "|\\",   2→3:  "||\\"

    Contraction (n_new < n_old, when lanes converge):
      One row per closed lane, rightmost closes first.
      2→1:  ["|/"],   3→1:  ["||/", "|/"]
    """
    rows: list[str] = []

    if n_new > n_old:
        parts = ["|"] * n_old + ["\\"] * (n_new - n_old)
        rows.append("".join(parts))

    elif n_new < n_old:
        n = n_old
        while n > n_new:
            parts = ["|"] * (n - 1) + ["/"]
            rows.append("".join(parts))
            n -= 1

    return rows


# ---------------------------------------------------------------------------
# Step 3 — render
# ---------------------------------------------------------------------------

def render_graph(git_dir: Path, sha: str) -> list[str]:
    """
    Render git log --graph output for all commits reachable from sha.
    Returns one string per output line (ANSI colour codes included).
    """
    commits, order = _collect(git_dir, sha)
    output: list[str] = []
    lanes: list[str] = []   # lanes[i] = SHA expected at column i

    for node in order:
        commit = commits.get(node)
        if commit is None:
            continue

        # --- Find / assign column ---
        positions = [i for i, s in enumerate(lanes) if s == node]
        if not positions:
            col = len(lanes)
            lanes = lanes + [node]
        else:
            col = positions[0]

        # --- Commit row: "*" at col, "|" everywhere else ---
        parts = ["*" if i == col else "|" for i in range(len(lanes))]
        prefix = " ".join(parts)
        msg = commit.message.split("\n")[0]
        output.append(f"{prefix} {_YELLOW}{node[:7]}{_RESET} {msg}")

        # --- Update lanes ---
        parents = commit.parents
        old_n = len(lanes)

        new_lanes: list[str] = []
        first_replaced = False
        for s in lanes:
            if s == node:
                if not first_replaced and parents:
                    new_lanes.append(parents[0])   # first parent inherits lane
                    first_replaced = True
                # else: duplicate lane — it closes here
            else:
                new_lanes.append(s)

        # Append extra parents not already present
        for p in parents[1:]:
            if p not in new_lanes:
                new_lanes.append(p)

        # Deduplicate: when a parent SHA already exists in another lane,
        # keep only its leftmost occurrence (the lanes have converged).
        seen: set[str] = set()
        deduped: list[str] = []
        for s in new_lanes:
            if s not in seen:
                seen.add(s)
                deduped.append(s)
        lanes = deduped
        new_n = len(lanes)

        # --- Connector rows (only when lanes remain) ---
        if new_n > 0:
            output.extend(_connector_rows(old_n, new_n))

    return output
