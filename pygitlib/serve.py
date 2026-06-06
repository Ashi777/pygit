"""
pygitlib/serve.py

Flask web server that visualises a pygit / git repository as an
interactive commit graph in the browser.

Usage (via the CLI):
    pygit serve [--port PORT] [--no-browser]

API endpoints:
    GET /                  — single-page D3.js UI
    GET /api/graph         — all reachable commits, edges, branch/tag labels
    GET /api/commit/<sha>  — commit metadata + flat file list
    GET /api/blob/<sha>    — blob text content

Flask is an optional dependency; install it with:
    pip install flask
or
    pip install "pygit[serve]"
"""

from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, send_from_directory  # type: ignore[import]

# ---------------------------------------------------------------------------
# Locate the templates directory (pygitlib/templates/)
# ---------------------------------------------------------------------------

_TEMPLATES = Path(__file__).parent / "templates"


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(git_dir: Path) -> Flask:  # type: ignore[misc]
    """Return a configured Flask application for *git_dir*."""
    app = Flask(__name__)

    # ── internal helpers ────────────────────────────────────────────────

    def _branches() -> dict[str, str]:
        from .branch import list_branches, resolve_ref
        result: dict[str, str] = {}
        for name in list_branches(git_dir):
            sha = resolve_ref(git_dir, name)
            if sha:
                result[name] = sha
        return result

    def _tags() -> dict[str, str]:
        from .tag import list_tags, resolve_tag
        result: dict[str, str] = {}
        for name in list_tags(git_dir):
            try:
                sha, _ = resolve_tag(git_dir, name)
                result[name] = sha
            except Exception:
                pass
        return result

    def _collect_commits() -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        """
        Walk every commit reachable from any branch or tag.

        Returns
        -------
        nodes : list of commit dicts
        edges : list of {source: child_sha, target: parent_sha}
        """
        from .objects import read_commit

        branches = _branches()
        tags     = _tags()

        # sha → list[branch_name]
        commit_branches: dict[str, list[str]] = {}
        for name, sha in branches.items():
            commit_branches.setdefault(sha, []).append(name)

        # sha → list[tag_name]
        commit_tags: dict[str, list[str]] = {}
        for name, sha in tags.items():
            commit_tags.setdefault(sha, []).append(name)

        seen:  set[str]               = set()
        nodes: list[dict[str, Any]]   = []
        edges: list[dict[str, str]]   = []
        queue: deque[str]             = deque(
            list(branches.values()) + list(tags.values())
        )

        while queue:
            sha = queue.popleft()
            if sha in seen:
                continue
            seen.add(sha)

            try:
                c = read_commit(git_dir, sha)
            except Exception:
                continue

            # Parse Unix timestamp from author string "Name <email> ts tz"
            ts = 0
            parts = c.author.split()
            if len(parts) >= 2:
                try:
                    ts = int(parts[-2])
                except ValueError:
                    pass

            nodes.append({
                "sha":       sha,
                "short_sha": sha[:7],
                "message":   c.message,
                "author":    c.author,
                "timestamp": ts,
                "parents":   c.parents,
                "branches":  commit_branches.get(sha, []),
                "tags":      commit_tags.get(sha, []),
                "is_merge":  len(c.parents) > 1,
            })

            for parent_sha in c.parents:
                edges.append({"source": sha, "target": parent_sha})
                if parent_sha not in seen:
                    queue.append(parent_sha)

        nodes.sort(key=lambda n: -n["timestamp"])
        return nodes, edges

    def _flatten_tree(tree_sha: str, prefix: str = "") -> list[dict[str, str]]:
        """Recursively expand a tree object into a flat file list."""
        from .objects import read_tree
        result: list[dict[str, str]] = []
        try:
            for entry in read_tree(git_dir, tree_sha):
                path = f"{prefix}/{entry.name}" if prefix else entry.name
                if entry.mode in ("040000", "40000"):
                    result.extend(_flatten_tree(entry.sha, path))
                else:
                    result.append({
                        "path": path,
                        "sha":  entry.sha,
                        "mode": entry.mode,
                    })
        except Exception:
            pass
        return result

    # ── routes ──────────────────────────────────────────────────────────

    @app.route("/")
    def index() -> Response:
        return send_from_directory(str(_TEMPLATES), "index.html")  # type: ignore[return-value]

    @app.route("/api/graph")
    def graph() -> Response:
        try:
            nodes, edges = _collect_commits()
            return jsonify({
                "nodes": nodes,
                "edges": edges,
                "repo":  str(git_dir.parent.resolve()),
            })
        except Exception as exc:
            resp: Response = jsonify({"error": str(exc)})
            resp.status_code = 500
            return resp

    @app.route("/api/commit/<sha>")
    def commit_detail(sha: str) -> Response:
        try:
            from .objects import read_commit
            c       = read_commit(git_dir, sha)
            entries = _flatten_tree(c.tree)
            return jsonify({
                "sha":     sha,
                "message": c.message,
                "author":  c.author,
                "parents": c.parents,
                "tree":    c.tree,
                "entries": entries,
            })
        except Exception as exc:
            resp = jsonify({"error": str(exc)})
            resp.status_code = 404
            return resp

    @app.route("/api/blob/<sha>")
    def blob_content(sha: str) -> Response:
        try:
            from .objects import read_object
            _obj_type, data = read_object(git_dir, sha)
            if b"\x00" in data[:8000]:
                return jsonify({"content": "(binary file)", "binary": True})
            return jsonify({
                "content": data.decode("utf-8", errors="replace"),
                "binary":  False,
            })
        except Exception as exc:
            resp = jsonify({"error": str(exc)})
            resp.status_code = 404
            return resp

    return app


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def serve(git_dir: Path, port: int = 5000, open_browser: bool = True) -> None:
    """Start the Flask development server."""
    import threading
    import webbrowser

    app = create_app(git_dir)
    url = f"http://localhost:{port}"

    print(f"\n  PyGit Visualizer")
    print(f"  Repository : {git_dir.parent.resolve()}")
    print(f"  URL        : {url}")
    print(f"  Press Ctrl+C to stop.\n")

    if open_browser:
        threading.Timer(0.9, lambda: webbrowser.open(url)).start()

    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
