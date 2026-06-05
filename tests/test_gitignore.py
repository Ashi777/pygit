"""
tests/test_gitignore.py

Tests for .gitignore pattern parsing and working-tree filtering.

Test structure
──────────────
  TestGlobToRe         — unit tests for the glob→regex translator
  TestCompileRule      — unit tests for single-line parsing
  TestGitIgnorePatterns — end-to-end is_ignored() checks for every
                          syntax feature
  TestSubdirGitignore  — rules scoped to a sub-.gitignore
  TestStatusIntegration — status() hides gitignored untracked files

Run with:  pytest tests/ -v
"""

import re
import subprocess
from pathlib import Path
import pytest
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from pygitlib.gitignore import GitIgnore, _compile_rule, _glob_to_re


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def repo(tmp_path):
    """A minimal repo directory (just the .git folder created by git init)."""
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.dev"],
                   cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"],
                   cwd=tmp_path, capture_output=True)
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    return tmp_path


# ---------------------------------------------------------------------------
# Unit: _glob_to_re
# ---------------------------------------------------------------------------

class TestGlobToRe:

    def test_literal_chars(self):
        assert re.fullmatch(_glob_to_re("hello"), "hello")
        assert not re.fullmatch(_glob_to_re("hello"), "world")

    def test_star_no_slash(self):
        pat = re.compile(_glob_to_re("*.py"))
        assert pat.fullmatch("foo.py")
        assert pat.fullmatch("bar.py")
        assert not pat.fullmatch("a/b.py")   # * must not cross /

    def test_double_star_crosses_slash(self):
        pat = re.compile(_glob_to_re("**"))
        assert pat.fullmatch("a/b/c")
        assert pat.fullmatch("foo")

    def test_question_mark(self):
        pat = re.compile(_glob_to_re("?.py"))
        assert pat.fullmatch("a.py")
        assert not pat.fullmatch("ab.py")    # ? = exactly one char
        assert not pat.fullmatch("a/b.py")   # ? won't cross /

    def test_bracket_class(self):
        pat = re.compile(_glob_to_re("*.py[codz]"))
        assert pat.fullmatch("foo.pyc")
        assert pat.fullmatch("foo.pyo")
        assert not pat.fullmatch("foo.pyx")

    def test_bracket_negation(self):
        pat = re.compile(_glob_to_re("*.[!py]*"))
        assert pat.fullmatch("foo.c")
        assert not pat.fullmatch("foo.p")

    def test_dot_is_literal(self):
        pat = re.compile(_glob_to_re("*.txt"))
        assert not pat.fullmatch("foostxt")   # . must be literal

    def test_hyphen_literal(self):
        pat = re.compile(_glob_to_re("*.egg-info"))
        assert pat.fullmatch("pygit.egg-info")


# ---------------------------------------------------------------------------
# Unit: _compile_rule
# ---------------------------------------------------------------------------

class TestCompileRule:

    def test_blank_is_none(self):
        assert _compile_rule("", "") is None
        assert _compile_rule("   ", "") is None

    def test_comment_is_none(self):
        assert _compile_rule("# this is a comment", "") is None

    def test_dir_only_flag(self):
        result = _compile_rule("__pycache__/", "")
        assert result is not None
        _, negate, dir_only, _ = result
        assert dir_only is True
        assert negate is False

    def test_negate_flag(self):
        result = _compile_rule("!keep.pyc", "")
        assert result is not None
        _, negate, dir_only, _ = result
        assert negate is True
        assert dir_only is False

    def test_anchored_leading_slash(self):
        _, _, _, regex = _compile_rule("/site", "")
        assert regex.search("site")
        assert not regex.search("docs/site")

    def test_anchored_middle_slash(self):
        _, _, _, regex = _compile_rule("docs/_build", "")
        assert regex.search("docs/_build")
        assert not regex.search("other/docs/_build")

    def test_unanchored_matches_any_depth(self):
        _, _, _, regex = _compile_rule("*.pyc", "")
        assert regex.search("foo.pyc")
        assert regex.search("pkg/foo.pyc")
        assert regex.search("a/b/c/foo.pyc")


# ---------------------------------------------------------------------------
# Integration: GitIgnore.is_ignored
# ---------------------------------------------------------------------------

class TestGitIgnorePatterns:

    def _gi(self, tmp_path, patterns: str) -> GitIgnore:
        (tmp_path / ".gitignore").write_text(patterns)
        return GitIgnore(tmp_path)

    def test_no_gitignore_hides_nothing(self, tmp_path):
        gi = GitIgnore(tmp_path)
        assert not gi.is_ignored("foo.pyc")
        assert not gi.is_ignored("__pycache__", is_dir=True)

    # --- Basic wildcard ---

    def test_star_extension_any_depth(self, tmp_path):
        gi = self._gi(tmp_path, "*.pyc\n")
        assert gi.is_ignored("foo.pyc")
        assert gi.is_ignored("pkg/foo.pyc")
        assert gi.is_ignored("a/b/c/foo.pyc")
        assert not gi.is_ignored("foo.py")

    def test_star_bracket_class(self, tmp_path):
        gi = self._gi(tmp_path, "*.py[codz]\n")
        assert gi.is_ignored("cache.pyc")
        assert gi.is_ignored("mod.pyo")
        assert not gi.is_ignored("mod.pyx")

    # --- Directory-only rules ---

    def test_dir_only_ignores_dir_not_file(self, tmp_path):
        gi = self._gi(tmp_path, "__pycache__/\n")
        assert gi.is_ignored("__pycache__", is_dir=True)
        assert gi.is_ignored("pkg/__pycache__", is_dir=True)
        assert not gi.is_ignored("__pycache__", is_dir=False)  # file, not dir

    def test_egg_info_dir(self, tmp_path):
        gi = self._gi(tmp_path, "*.egg-info/\n")
        assert gi.is_ignored("pygit.egg-info", is_dir=True)
        assert gi.is_ignored("sub/pygit.egg-info", is_dir=True)
        assert not gi.is_ignored("pygit.egg-info", is_dir=False)

    def test_pytest_cache_dir(self, tmp_path):
        gi = self._gi(tmp_path, ".pytest_cache/\n")
        assert gi.is_ignored(".pytest_cache", is_dir=True)
        assert not gi.is_ignored(".pytest_cache", is_dir=False)

    # --- Anchored patterns ---

    def test_leading_slash_anchors_to_root(self, tmp_path):
        gi = self._gi(tmp_path, "/site\n")
        assert gi.is_ignored("site", is_dir=True)
        assert not gi.is_ignored("docs/site", is_dir=True)

    def test_slash_in_body_anchors(self, tmp_path):
        gi = self._gi(tmp_path, "docs/_build/\n")
        assert gi.is_ignored("docs/_build", is_dir=True)
        assert not gi.is_ignored("other/docs/_build", is_dir=True)

    # --- Negation ---

    def test_negation_un_ignores(self, tmp_path):
        gi = self._gi(tmp_path, "*.pyc\n!important.pyc\n")
        assert gi.is_ignored("foo.pyc")
        assert not gi.is_ignored("important.pyc")

    def test_negation_last_rule_wins(self, tmp_path):
        gi = self._gi(tmp_path, "!important.pyc\n*.pyc\n")
        # *.pyc comes AFTER the negation → *.pyc wins → ignored
        assert gi.is_ignored("important.pyc")

    # --- Comments and blanks ---

    def test_comments_ignored(self, tmp_path):
        gi = self._gi(tmp_path, "# this is a comment\n*.pyc\n")
        assert gi.is_ignored("foo.pyc")

    def test_blank_lines_ignored(self, tmp_path):
        gi = self._gi(tmp_path, "\n\n*.pyc\n\n")
        assert gi.is_ignored("foo.pyc")

    # --- Patterns from the project's own .gitignore ---

    def test_docx_pattern(self, tmp_path):
        gi = self._gi(tmp_path, "*.docx\n")
        assert gi.is_ignored("Project Phases and Future Work.docx")
        assert not gi.is_ignored("README.md")

    def test_mypy_cache(self, tmp_path):
        gi = self._gi(tmp_path, ".mypy_cache/\n")
        assert gi.is_ignored(".mypy_cache", is_dir=True)
        assert not gi.is_ignored(".mypy_cache", is_dir=False)

    # --- Encoding: PowerShell writes UTF-16 LE with BOM ---

    def test_utf16_le_gitignore(self, tmp_path):
        """Files written by PowerShell's > operator (UTF-16 LE BOM) must work."""
        content = "*.pyc\n__pycache__/\n"
        (tmp_path / ".gitignore").write_bytes(
            b'\xff\xfe' + content.encode('utf-16-le')
        )
        gi = GitIgnore(tmp_path)
        assert gi.is_ignored("foo.pyc")
        assert gi.is_ignored("pkg/foo.pyc")
        assert gi.is_ignored("__pycache__", is_dir=True)
        assert not gi.is_ignored("foo.py")

    def test_utf8_bom_gitignore(self, tmp_path):
        """UTF-8 BOM (written by some editors) must also be stripped."""
        content = "*.log\n"
        (tmp_path / ".gitignore").write_bytes(b'\xef\xbb\xbf' + content.encode('utf-8'))
        gi = GitIgnore(tmp_path)
        assert gi.is_ignored("debug.log")
        assert not gi.is_ignored("debug.txt")


# ---------------------------------------------------------------------------
# Sub-directory .gitignore scoping
# ---------------------------------------------------------------------------

class TestSubdirGitignore:

    def test_subdir_rule_only_applies_in_subdir(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / ".gitignore").write_text("*.o\n")
        gi = GitIgnore(tmp_path)
        gi.load_dir("src")
        assert gi.is_ignored("src/main.o")
        assert not gi.is_ignored("main.o")     # not under src/

    def test_subdir_rule_applies_to_nested_paths(self, tmp_path):
        lib = tmp_path / "lib"
        lib.mkdir()
        (lib / ".gitignore").write_text("*.log\n")
        gi = GitIgnore(tmp_path)
        gi.load_dir("lib")
        assert gi.is_ignored("lib/build.log")
        assert gi.is_ignored("lib/sub/build.log")

    def test_load_dir_idempotent(self, tmp_path):
        (tmp_path / ".gitignore").write_text("*.pyc\n")
        gi = GitIgnore(tmp_path)
        before = len(gi._rules)
        gi.load_dir("")   # already loaded in __init__
        assert len(gi._rules) == before


# ---------------------------------------------------------------------------
# Integration: status() hides gitignored untracked files
# ---------------------------------------------------------------------------

class TestStatusIntegration:

    def test_pycache_dir_hidden(self, repo):
        from pygitlib.index import status
        pycache = repo / "__pycache__"
        pycache.mkdir()
        (pycache / "foo.cpython-310.pyc").write_bytes(b"fake pyc")
        (repo / ".gitignore").write_text("__pycache__/\n")
        result = status(repo / ".git", repo)
        assert not any("__pycache__" in p for p in result["untracked"])

    def test_pyc_file_hidden(self, repo):
        from pygitlib.index import status
        (repo / "module.pyc").write_bytes(b"fake pyc")
        (repo / ".gitignore").write_text("*.pyc\n")
        result = status(repo / ".git", repo)
        assert "module.pyc" not in result["untracked"]

    def test_non_ignored_file_still_untracked(self, repo):
        from pygitlib.index import status
        (repo / "hello.txt").write_text("hello")
        (repo / ".gitignore").write_text("*.pyc\n")
        result = status(repo / ".git", repo)
        assert "hello.txt" in result["untracked"]

    def test_gitignore_itself_is_untracked(self, repo):
        from pygitlib.index import status
        (repo / ".gitignore").write_text("*.pyc\n")
        result = status(repo / ".git", repo)
        assert ".gitignore" in result["untracked"]

    def test_no_gitignore_still_works(self, repo):
        from pygitlib.index import status
        (repo / "foo.txt").write_text("foo")
        result = status(repo / ".git", repo)
        assert "foo.txt" in result["untracked"]

    def test_egg_info_dir_hidden(self, repo):
        from pygitlib.index import status
        egg = repo / "pygit.egg-info"
        egg.mkdir()
        (egg / "PKG-INFO").write_text("Name: pygit")
        (repo / ".gitignore").write_text("*.egg-info/\n")
        result = status(repo / ".git", repo)
        assert not any("egg-info" in p for p in result["untracked"])

    def test_negation_shows_file(self, repo):
        from pygitlib.index import status
        (repo / "debug.log").write_text("log")
        (repo / "keep.log").write_text("important")
        (repo / ".gitignore").write_text("*.log\n!keep.log\n")
        result = status(repo / ".git", repo)
        assert "debug.log" not in result["untracked"]
        assert "keep.log" in result["untracked"]
