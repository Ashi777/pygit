"""
pygitlib/gitignore.py

.gitignore pattern matching.

Supported syntax:
  #comment          — whole-line comments
  blank line        — ignored
  pattern/          — matches directories only
  /pattern          — anchored to the .gitignore's own directory
  a/b               — slash in the body also anchors the pattern
  *                 — any characters except /
  **                — any characters including /  (cross-directory wildcard)
  ?                 — any single character except /
  [abc], [a-z]      — character class (including [!…] negation)
  !pattern          — negate: un-ignore a previously ignored path

Rules are evaluated in load order; the last matching rule wins, which matches
real git's semantics.
"""

import re
from pathlib import Path


# ---------------------------------------------------------------------------
# Glob → regex translation
# ---------------------------------------------------------------------------

def _glob_to_re(glob: str) -> str:
    """
    Translate wildcard characters in a gitignore pattern to a regex fragment.
    Does NOT add start/end anchors — the caller handles that.
    """
    i = 0
    parts: list[str] = []
    while i < len(glob):
        c = glob[i]

        if c == '\\' and i + 1 < len(glob):        # escaped character
            parts.append(re.escape(glob[i + 1]))
            i += 2

        elif c == '*':
            if i + 1 < len(glob) and glob[i + 1] == '*':
                parts.append('.*')                  # ** crosses directory boundaries
                i += 2
                if i < len(glob) and glob[i] == '/':
                    i += 1                          # consume the / that follows **
            else:
                parts.append('[^/]*')               # * stays within one component
                i += 1

        elif c == '?':
            parts.append('[^/]')
            i += 1

        elif c == '[':
            # Bracket expression — scan for the matching ]
            j = i + 1
            if j < len(glob) and glob[j] in ('!', '^'):
                j += 1
            if j < len(glob) and glob[j] == ']':   # ] right after [ or [!
                j += 1
            while j < len(glob) and glob[j] != ']':
                j += 1
            if j < len(glob):
                bracket = glob[i:j + 1]
                if len(bracket) > 1 and bracket[1] == '!':
                    bracket = '[^' + bracket[2:]    # [!…] → [^…] for regex
                parts.append(bracket)
                i = j + 1
            else:
                parts.append(r'\[')                 # malformed — treat as literal
                i += 1

        else:
            parts.append(re.escape(c))
            i += 1

    return ''.join(parts)


# ---------------------------------------------------------------------------
# Single-rule compiler
# ---------------------------------------------------------------------------

def _compile_rule(raw_line: str, scope: str) -> tuple | None:
    """
    Parse one line from a .gitignore file.

    scope  — directory that owns this .gitignore, relative to the repo root.
             Empty string means the root .gitignore.

    Returns ``(scope, negate, dir_only, compiled_regex)`` or ``None`` for
    blank lines and comments.

    The regex matches against a path that has already been made relative to
    ``scope``.
    """
    line = raw_line.rstrip()
    if not line or line.startswith('#'):
        return None

    negate = line.startswith('!')
    if negate:
        line = line[1:]
    if not line:
        return None

    dir_only = line.endswith('/')
    if dir_only:
        line = line.rstrip('/')
    if not line:
        return None

    # A pattern is anchored to its directory when it starts with /
    # or contains a / in its body (not counting any trailing /).
    if line.startswith('/'):
        anchored = True
        line = line[1:]
    elif '/' in line:
        anchored = True
    else:
        anchored = False

    body = _glob_to_re(line)

    if anchored:
        # Must match at the start of the scope-relative path.
        # The (?:/.*)? suffix lets "docs/_build" also cover
        # "docs/_build/html/index.html" when the directory is pruned whole.
        pattern = rf'^{body}(?:/.*)?$'
    else:
        # May match at any directory depth.
        pattern = rf'(?:^|.*/)(?:{body})$'

    try:
        compiled = re.compile(pattern)
    except re.error:
        return None

    return (scope, negate, dir_only, compiled)


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

class GitIgnore:
    """
    Loads and applies .gitignore rules for one working tree.

    Typical usage inside an os.walk loop::

        gi = GitIgnore(work_dir)          # loads root .gitignore

        for root, dirs, files in os.walk(work_dir):
            rel_root = ...                # path of root relative to work_dir
            gi.load_dir(rel_root)         # lazily load sub-.gitignore if present

            dirs[:] = [d for d in dirs
                       if not gi.is_ignored(f"{rel_root}/{d}", is_dir=True)]

            for f in files:
                if not gi.is_ignored(f"{rel_root}/{f}"):
                    process(f)
    """

    def __init__(self, work_dir: Path):
        self.work_dir = work_dir
        # (scope, negate, dir_only, compiled_regex)
        self._rules: list[tuple[str, bool, bool, re.Pattern]] = []
        self._loaded: set[str] = set()
        self.load_dir("")                   # always load the root .gitignore

    def load_dir(self, rel_dir: str) -> None:
        """
        Load the .gitignore inside ``rel_dir`` (if any, if not already loaded).
        ``rel_dir`` is a forward-slash path relative to ``work_dir``;
        pass an empty string for the repo root.
        """
        if rel_dir in self._loaded:
            return
        self._loaded.add(rel_dir)

        gi_path = (
            self.work_dir / rel_dir / ".gitignore"
            if rel_dir
            else self.work_dir / ".gitignore"
        )
        if not gi_path.exists():
            return

        # Detect encoding from BOM so files written by PowerShell's > operator
        # (UTF-16 LE with BOM) are read correctly.  Fall back to UTF-8.
        raw_bytes = gi_path.read_bytes()
        if raw_bytes[:2] in (b'\xff\xfe', b'\xfe\xff'):
            content = raw_bytes.decode('utf-16')   # handles LE and BE BOM
        else:
            content = raw_bytes.decode('utf-8-sig', errors='replace')  # strips UTF-8 BOM if present

        for raw in content.splitlines():
            rule = _compile_rule(raw, rel_dir)
            if rule is not None:
                self._rules.append(rule)

    def is_ignored(self, rel_path: str, is_dir: bool = False) -> bool:
        """
        Return ``True`` if ``rel_path`` should be ignored.

        rel_path — forward-slash path relative to ``work_dir``
        is_dir   — pass ``True`` when checking a directory name so that
                   directory-only rules (ending in /) are evaluated
        """
        result = False
        for scope, negate, dir_only, regex in self._rules:
            if dir_only and not is_dir:
                continue
            if scope:
                # Sub-directory rules only apply inside their own directory.
                if not rel_path.startswith(scope + '/'):
                    continue
                match_against = rel_path[len(scope) + 1:]
            else:
                match_against = rel_path
            if regex.search(match_against):
                result = not negate
        return result
