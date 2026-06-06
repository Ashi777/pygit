#!/usr/bin/env python3
"""
benchmarks/bench.py

Stage-throughput benchmark: pygit add vs real git add.

Creates N source-like text files in a fresh temporary repository,
stages them with each tool, and reports:
  - elapsed time (seconds)
  - throughput (files / second, MB / second)
  - speed ratio  (pygit_time / git_time)

Usage
-----
    python benchmarks/bench.py                   # 100/500/1000 files, 1 KB each
    python benchmarks/bench.py --file-kb 5       # 5 KB per file
    python benchmarks/bench.py --sizes 50,200    # custom file counts
    python benchmarks/bench.py --runs 3          # median of 3 timed runs
    python benchmarks/bench.py --no-git          # skip real-git comparison
    python benchmarks/bench.py --markdown        # emit a GitHub markdown table
"""

from __future__ import annotations

import argparse
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path


# ---------------------------------------------------------------------------
# Realistic file-content generation
# ---------------------------------------------------------------------------

_WORDS = (
    "def class return import from as if else elif while for in not and or "
    "True False None self super try except raise finally with yield lambda "
    "global nonlocal pass break continue assert print len range type "
    "str int float bool list dict set tuple object property staticmethod "
    "alpha beta gamma delta epsilon theta kappa sigma omega rho tau mu "
    "data model view controller service repository factory builder loader "
    "error result status message payload request response context session "
    "config setup teardown validate transform serialize deserialize cache "
    "read write flush sync lock unlock acquire release dispatch register "
    "base index count size value name key path root node leaf parent child"
).split()


def _gen_content(approx_bytes: int, seed: int = 0) -> str:
    """
    Return a Python-source-like string of approximately *approx_bytes* length.
    Each *seed* value produces a distinct string so every file differs.
    """
    rng = random.Random(seed)
    lines: list[str] = []
    written = 0
    while written < approx_bytes:
        indent = "    " * rng.randint(0, 2)
        words  = rng.choices(_WORDS, k=rng.randint(4, 10))
        line   = indent + " ".join(words)
        lines.append(line)
        written += len(line) + 1          # +1 for the trailing newline
    return "\n".join(lines) + "\n"


def _create_files(root: Path, n: int, file_bytes: int) -> int:
    """
    Write *n* text files into *root*, each ~*file_bytes* bytes.
    Files are spread across 8 subdirectories to mimic a real project.
    Returns the total bytes actually written.
    """
    total = 0
    for i in range(n):
        pkg = root / f"pkg{i % 8}"
        pkg.mkdir(exist_ok=True)
        text = _gen_content(file_bytes, seed=i)
        (pkg / f"mod_{i:05d}.py").write_text(text, encoding="utf-8")
        total += len(text.encode("utf-8"))
    return total


# ---------------------------------------------------------------------------
# Repo init helpers
# ---------------------------------------------------------------------------

def _init_repo(root: Path, tool: str, git_exe: str) -> None:
    """Initialise a fresh repo inside *root* for the given *tool*."""
    if tool == "pygit":
        subprocess.run(["pygit", "init", "."],
                       cwd=root, capture_output=True, check=True)
        with (root / ".git" / "config").open("a") as fh:
            fh.write("\n[user]\n    name = Bench\n    email = bench@bench.local\n")
    else:
        subprocess.run([git_exe, "init", "."],
                       cwd=root, capture_output=True, check=True)
        subprocess.run([git_exe, "config", "user.email", "bench@bench.local"],
                       cwd=root, capture_output=True, check=True)
        subprocess.run([git_exe, "config", "user.name", "Bench"],
                       cwd=root, capture_output=True, check=True)


# ---------------------------------------------------------------------------
# Core timing function
# ---------------------------------------------------------------------------

def _bench(
    n:          int,
    file_bytes: int,
    tool:       str,
    git_exe:    str,
    n_runs:     int,
) -> tuple[float, int]:
    """
    Run `tool add .` on *n* freshly-created files, *n_runs* times.
    Returns (median_elapsed_seconds, total_bytes_staged).

    Each run gets its own temp directory so there is zero contamination
    between repetitions.
    """
    times:   list[float] = []
    total_b: int         = 0

    for _ in range(n_runs):
        with tempfile.TemporaryDirectory(prefix="pygit_bench_") as tmp:
            root    = Path(tmp)
            total_b = _create_files(root, n, file_bytes)
            _init_repo(root, tool, git_exe)

            cmd = (["pygit", "add", "."] if tool == "pygit"
                   else [git_exe,  "add", "."])
            t0 = time.perf_counter()
            subprocess.run(cmd, cwd=root, capture_output=True, check=True)
            times.append(time.perf_counter() - t0)

    return statistics.median(times), total_b


# ---------------------------------------------------------------------------
# Throughput helpers
# ---------------------------------------------------------------------------

def _fps(n: int, elapsed: float) -> float:
    return n / elapsed


def _mbs(total_bytes: int, elapsed: float) -> float:
    return (total_bytes / (1024 * 1024)) / elapsed


# ---------------------------------------------------------------------------
# Plain-text report  (ASCII only so it works on any terminal / encoding)
# ---------------------------------------------------------------------------

def _print_plain(
    sizes:      list[int],
    file_kb:    int,
    runs_lbl:   str,
    pygit_data: list[tuple[float, int]],
    git_data:   list[tuple[float, int]] | None,
) -> None:
    vs = "real git add" if git_data else "(no git)"
    title = f"pygit add throughput vs {vs}  ~{file_kb} KB/file  {runs_lbl}"
    W = max(76, len(title) + 4)

    print()
    print("=" * W)
    print("  " + title)
    print("=" * W)
    print()

    if git_data:
        hdr = ("  {Files:>6}  |  {pygit:>8}  | {fps1:>7}  | {mbs1:>5}"
               "  ||  {git:>8}  | {fps2:>7}  | {mbs2:>5}  ||  {ratio}").format(
            Files="Files", pygit="pygit", fps1="f/s", mbs1="MB/s",
            git="git", fps2="f/s", mbs2="MB/s", ratio="ratio")
        sep = ("  " + "-" * 6 + "--+--" + "-" * 8 + "--+-" + "-" * 7
               + "--+-" + "-" * 5 + "--++--" + "-" * 8 + "--+-" + "-" * 7
               + "--+-" + "-" * 5 + "--++--" + "-" * 7)
    else:
        hdr = ("  {Files:>6}  |  {pygit:>8}  | {fps:>7}  | {mbs:>5}").format(
            Files="Files", pygit="pygit", fps="f/s", mbs="MB/s")
        sep = "  " + "-" * 6 + "--+--" + "-" * 8 + "--+-" + "-" * 7 + "--+-" + "-" * 5

    print(hdr)
    print(sep)

    for i, n in enumerate(sizes):
        py_t, py_b = pygit_data[i]
        py_fps = _fps(n, py_t)
        py_mbs = _mbs(py_b, py_t)

        if git_data:
            g_t, g_b = git_data[i]
            g_fps    = _fps(n, g_t)
            g_mbs    = _mbs(g_b, g_t)
            ratio    = py_t / g_t
            print(f"  {n:>6}  |  {py_t:>6.2f} s  | {py_fps:>6.0f}/s  | {py_mbs:>5.2f}"
                  f"  ||  {g_t:>6.2f} s  | {g_fps:>6.0f}/s  | {g_mbs:>5.2f}"
                  f"  ||  {ratio:>5.1f}x")
        else:
            print(f"  {n:>6}  |  {py_t:>6.2f} s  | {py_fps:>6.0f}/s  | {py_mbs:>5.2f}")

    print()
    if git_data:
        print("  ratio = pygit_time / git_time"
              "  (e.g. 10x means pygit took 10x longer)")
    print()


# ---------------------------------------------------------------------------
# Markdown table report
# ---------------------------------------------------------------------------

def _print_markdown(
    sizes:      list[int],
    file_kb:    int,
    runs_lbl:   str,
    pygit_data: list[tuple[float, int]],
    git_data:   list[tuple[float, int]] | None,
) -> None:
    print()
    print(f"<!-- ~{file_kb} KB/file -- {runs_lbl} -->")
    print()
    if git_data:
        print("| Files | pygit time | files/s | MB/s | git time | files/s | MB/s | ratio |")
        print("|------:|-----------:|--------:|-----:|---------:|--------:|-----:|------:|")
        for i, n in enumerate(sizes):
            py_t, py_b = pygit_data[i]
            g_t,  g_b  = git_data[i]
            ratio = py_t / g_t
            print(f"| {n:>5} | {py_t:>8.2f} s | {_fps(n,py_t):>6.0f}/s"
                  f" | {_mbs(py_b,py_t):>4.2f}"
                  f" | {g_t:>6.2f} s | {_fps(n,g_t):>6.0f}/s"
                  f" | {_mbs(g_b,g_t):>4.2f} | {ratio:>5.1f}x |")
    else:
        print("| Files | pygit time | files/s | MB/s |")
        print("|------:|-----------:|--------:|-----:|")
        for i, n in enumerate(sizes):
            py_t, py_b = pygit_data[i]
            print(f"| {n:>5} | {py_t:>8.2f} s | {_fps(n,py_t):>6.0f}/s"
                  f" | {_mbs(py_b,py_t):>4.2f} |")
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark pygit add throughput vs real git add",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--sizes", default="100,500,1000",
        help="Comma-separated file counts to benchmark",
    )
    parser.add_argument(
        "--file-kb", type=int, default=1, metavar="KB",
        help="Approximate size of each generated file (kilobytes)",
    )
    parser.add_argument(
        "--runs", type=int, default=1, metavar="N",
        help="Timed runs per configuration; median is reported",
    )
    parser.add_argument(
        "--no-git", action="store_true",
        help="Skip the real git comparison",
    )
    parser.add_argument(
        "--git-exe", default="git", metavar="EXE",
        help="Name or path of the real git executable",
    )
    parser.add_argument(
        "--markdown", action="store_true",
        help="Emit results as a GitHub Markdown table",
    )
    args = parser.parse_args()

    sizes      = [int(s.strip()) for s in args.sizes.split(",") if s.strip()]
    file_bytes = max(64, args.file_kb * 1024)
    n_runs     = max(1, args.runs)

    # Sanity checks
    if shutil.which("pygit") is None:
        sys.exit("error: 'pygit' not found on PATH -- run: pip install -e .")

    has_git = (not args.no_git) and (shutil.which(args.git_exe) is not None)
    if not has_git and not args.no_git:
        print(f"warning: '{args.git_exe}' not found -- skipping comparison",
              file=sys.stderr)

    runs_lbl = f"median of {n_runs} runs" if n_runs > 1 else "single run"
    print(f"\n  Benchmarking {len(sizes)} sizes"
          f"  {args.file_kb} KB/file"
          f"  {runs_lbl}")
    if has_git:
        print("  Comparing pygit (pure Python) vs git (C binary)")
    print()

    pygit_data: list[tuple[float, int]] = []
    git_data:   list[tuple[float, int]] = []

    for n in sizes:
        print(f"  pygit add  {n:>5} files ...", end="", flush=True)
        py_t, py_b = _bench(n, file_bytes, "pygit", args.git_exe, n_runs)
        pygit_data.append((py_t, py_b))
        print(f"  {py_t:.2f} s"
              f"  ({_fps(n,py_t):.0f} f/s,"
              f"  {_mbs(py_b,py_t):.2f} MB/s)")

        if has_git:
            print(f"  git   add  {n:>5} files ...", end="", flush=True)
            g_t, g_b = _bench(n, file_bytes, "git", args.git_exe, n_runs)
            git_data.append((g_t, g_b))
            ratio = py_t / g_t
            print(f"  {g_t:.2f} s"
                  f"  ({_fps(n,g_t):.0f} f/s,"
                  f"  {_mbs(g_b,g_t):.2f} MB/s)"
                  f"  ratio {ratio:.1f}x")
        print()

    if args.markdown:
        _print_markdown(sizes, args.file_kb, runs_lbl,
                        pygit_data, git_data if has_git else None)
    else:
        _print_plain(sizes, args.file_kb, runs_lbl,
                     pygit_data, git_data if has_git else None)


if __name__ == "__main__":
    main()
