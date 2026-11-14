"""Warm pytest's assertion-rewrite cache before xdist workers start.

Every xdist worker collects every selected test module. pytest rewrites the
``assert`` statements of each test module on import and caches the result under
``sys.pycache_prefix``; the gate's workspace is a fresh copy per run, so without
a warm cache all workers start together and each rewrites all of them. Measured
on the backend suite (25,736 tests): collection took 180 s in one process, 120 s
of it assertion rewriting, and the four hosted workers spent 8¾ minutes before
the first test ran.

Loaded with ``-p`` by the Tier 3 backend suites. Just before the controller sets
up its workers, it collects the selected test files once, split across as many
processes as there are workers; the workers then load the rewritten bytecode.
A warm-up failure only costs time: the real run collects and reports everything.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

# Below this many files per worker the warm-up saves less than it costs.
_MIN_FILES_PER_WORKER = 8


@pytest.hookimpl(optionalhook=True)
def pytest_xdist_setupnodes(config: pytest.Config, specs: Sequence[object]) -> None:
    workers = len(specs)
    root = Path(config.invocation_params.dir)
    files = selected_test_files(
        root, [str(arg) for arg in config.args], config.getoption("ignore") or []
    )
    if workers < 2 or len(files) < workers * _MIN_FILES_PER_WORKER:
        return
    processes = [
        subprocess.Popen(
            (
                sys.executable,
                "-m",
                "pytest",
                "--collect-only",
                "-q",
                "-p",
                "no:xdist",
                "-p",
                "no:cacheprovider",
                *files[index::workers],
            ),
            cwd=root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for index in range(workers)
    ]
    for process in processes:
        process.wait()


def selected_test_files(root: Path, args: Sequence[str], ignored: Sequence[str]) -> list[str]:
    """The test modules ``args`` name, without the ``--ignore`` paths, relative to ``root``."""
    excluded = [(root / item).resolve() for item in ignored]
    found: set[str] = set()
    for arg in args:
        path = (root / arg.split("::", 1)[0]).resolve()
        candidates = sorted(path.rglob("test_*.py")) if path.is_dir() else [path]
        for candidate in candidates:
            if not candidate.is_file() or any(
                candidate == item or item in candidate.parents for item in excluded
            ):
                continue
            try:
                found.add(candidate.relative_to(root.resolve()).as_posix())
            except ValueError:
                continue
    return sorted(found)
