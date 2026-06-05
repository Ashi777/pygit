"""
tests/test_graph.py

Tests for git log --graph ASCII branch visualization.

Test structure
──────────────
  TestConnectorRows  — unit tests for the connector-row generator
  TestLinearGraph    — single chain of commits (no branches)
  TestDiamondGraph   — classic branch-and-merge (the doc example)
  TestEdgeCases      — root-only repo, multiple merges

Run with:  pytest tests/ -v
"""

import re
import subprocess
from pathlib import Path
import pytest
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from pygitlib.graph import render_graph, _collect, _connector_rows
from pygitlib.objects import write_commit, Commit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def strip_ansi(s: str) -> str:
    """Remove ANSI escape codes so assertions are readable."""
    return re.sub(r'\033\[[0-9;]*m', '', s)


def clean(git_dir: Path, sha: str) -> list[str]:
    """render_graph output with ANSI stripped."""
    return [strip_ansi(l) for l in render_graph(git_dir, sha)]


def mkcommit(git_dir: Path, message: str, parents: list[str]) -> str:
    """Write a minimal commit object (tree SHA is a placeholder)."""
    identity = "Test <t@t.dev> 1700000000 +0000"
    c = Commit(
        tree="0" * 40,
        author=identity,
        committer=identity,
        message=message,
        parents=parents,
    )
    return write_commit(git_dir, c)


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    return tmp_path


# ---------------------------------------------------------------------------
# Unit: _connector_rows
# ---------------------------------------------------------------------------

class TestConnectorRows:

    def test_same_count_no_rows(self):
        assert _connector_rows(1, 1) == []
        assert _connector_rows(3, 3) == []

    def test_expansion_1_to_2(self):
        assert _connector_rows(1, 2) == ["|\\"]

    def test_expansion_2_to_3(self):
        assert _connector_rows(2, 3) == ["||\\"]

    def test_expansion_1_to_3(self):
        assert _connector_rows(1, 3) == ["|\\\\"]

    def test_contraction_2_to_1(self):
        assert _connector_rows(2, 1) == ["|/"]

    def test_contraction_3_to_2(self):
        assert _connector_rows(3, 2) == ["||/"]

    def test_contraction_3_to_1(self):
        # Two rows: first closes rightmost, second closes the next
        assert _connector_rows(3, 1) == ["||/", "|/"]

    def test_contraction_4_to_1(self):
        assert _connector_rows(4, 1) == ["|||/", "||/", "|/"]


# ---------------------------------------------------------------------------
# Linear history
# ---------------------------------------------------------------------------

class TestLinearGraph:

    def test_single_commit(self, repo):
        git_dir = repo / ".git"
        sha = mkcommit(git_dir, "init", [])
        lines = clean(git_dir, sha)
        assert lines == [f"* {sha[:7]} init"]

    def test_two_commits_no_connectors(self, repo):
        git_dir = repo / ".git"
        c1 = mkcommit(git_dir, "first", [])
        c2 = mkcommit(git_dir, "second", [c1])
        lines = clean(git_dir, c2)
        # Linear history: no connector rows
        assert len(lines) == 2
        assert lines[0] == f"* {c2[:7]} second"
        assert lines[1] == f"* {c1[:7]} first"

    def test_three_commits_all_star(self, repo):
        git_dir = repo / ".git"
        c1 = mkcommit(git_dir, "a", [])
        c2 = mkcommit(git_dir, "b", [c1])
        c3 = mkcommit(git_dir, "c", [c2])
        lines = clean(git_dir, c3)
        assert len(lines) == 3
        for line in lines:
            assert line.startswith("* ")

    def test_newest_first_oldest_last(self, repo):
        git_dir = repo / ".git"
        c1 = mkcommit(git_dir, "old", [])
        c2 = mkcommit(git_dir, "mid", [c1])
        c3 = mkcommit(git_dir, "new", [c2])
        lines = clean(git_dir, c3)
        assert c3[:7] in lines[0]
        assert c2[:7] in lines[1]
        assert c1[:7] in lines[2]


# ---------------------------------------------------------------------------
# Diamond (branch + merge) — matches the doc example structure
# ---------------------------------------------------------------------------

class TestDiamondGraph:
    """
    Commit graph:
        base  ←  main_c  ←  merge_c
          ↑                  ↗
        feat_c  ────────────
    Rendered (topo order: merge → feat → main → base):
        * {merge_c} Merge branch feature
        |\\
        | * {feat_c} feature commit
        * | {main_c} main commit
        |/
        * {base} base
    """

    @pytest.fixture
    def diamond(self, repo):
        git_dir = repo / ".git"
        base    = mkcommit(git_dir, "base",                  [])
        main_c  = mkcommit(git_dir, "main commit",           [base])
        feat_c  = mkcommit(git_dir, "feature commit",        [base])
        merge_c = mkcommit(git_dir, "Merge branch feature",  [main_c, feat_c])
        return git_dir, base, main_c, feat_c, merge_c

    def test_line_count(self, diamond):
        git_dir, base, main_c, feat_c, merge_c = diamond
        lines = clean(git_dir, merge_c)
        # 4 commit rows + "|\" expansion + "|/" contraction = 6 lines
        assert len(lines) == 6

    def test_merge_is_first_line(self, diamond):
        git_dir, base, main_c, feat_c, merge_c = diamond
        lines = clean(git_dir, merge_c)
        assert lines[0] == f"* {merge_c[:7]} Merge branch feature"

    def test_expansion_connector(self, diamond):
        git_dir, base, main_c, feat_c, merge_c = diamond
        lines = clean(git_dir, merge_c)
        assert lines[1] == "|\\"

    def test_feat_on_right_lane(self, diamond):
        git_dir, base, main_c, feat_c, merge_c = diamond
        lines = clean(git_dir, merge_c)
        assert lines[2] == f"| * {feat_c[:7]} feature commit"

    def test_main_on_left_lane(self, diamond):
        git_dir, base, main_c, feat_c, merge_c = diamond
        lines = clean(git_dir, merge_c)
        assert lines[3] == f"* | {main_c[:7]} main commit"

    def test_convergence_connector(self, diamond):
        git_dir, base, main_c, feat_c, merge_c = diamond
        lines = clean(git_dir, merge_c)
        assert lines[4] == "|/"

    def test_base_is_last_line(self, diamond):
        git_dir, base, main_c, feat_c, merge_c = diamond
        lines = clean(git_dir, merge_c)
        assert lines[5] == f"* {base[:7]} base"

    def test_full_output(self, diamond):
        """End-to-end: verify exact output matches the doc example structure."""
        git_dir, base, main_c, feat_c, merge_c = diamond
        lines = clean(git_dir, merge_c)
        expected = [
            f"* {merge_c[:7]} Merge branch feature",
            "|\\",
            f"| * {feat_c[:7]} feature commit",
            f"* | {main_c[:7]} main commit",
            "|/",
            f"* {base[:7]} base",
        ]
        assert lines == expected


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:

    def test_root_only(self, repo):
        git_dir = repo / ".git"
        sha = mkcommit(git_dir, "root", [])
        lines = clean(git_dir, sha)
        assert lines == [f"* {sha[:7]} root"]

    def test_message_first_line_only(self, repo):
        """Multiline messages: only the first line appears in the graph."""
        git_dir = repo / ".git"
        sha = mkcommit(git_dir, "summary\n\ndetails here", [])
        lines = clean(git_dir, sha)
        assert "summary" in lines[0]
        assert "details" not in lines[0]

    def test_collect_topo_order(self, repo):
        """_collect returns children before parents."""
        git_dir = repo / ".git"
        c1 = mkcommit(git_dir, "p1", [])
        c2 = mkcommit(git_dir, "p2", [c1])
        c3 = mkcommit(git_dir, "p3", [c2])
        _, order = _collect(git_dir, c3)
        assert order.index(c3) < order.index(c2) < order.index(c1)

    def test_shared_base_appears_once(self, repo):
        """Commits with a shared ancestor should appear exactly once."""
        git_dir = repo / ".git"
        base   = mkcommit(git_dir, "base",  [])
        left   = mkcommit(git_dir, "left",  [base])
        right  = mkcommit(git_dir, "right", [base])
        merge  = mkcommit(git_dir, "merge", [left, right])
        lines  = clean(git_dir, merge)
        base_lines = [l for l in lines if base[:7] in l]
        assert len(base_lines) == 1

    def test_ansi_codes_present(self, repo):
        """SHA should be wrapped in yellow ANSI codes."""
        git_dir = repo / ".git"
        sha = mkcommit(git_dir, "c", [])
        raw = render_graph(git_dir, sha)
        assert "\033[33m" in raw[0]
        assert "\033[0m"  in raw[0]
