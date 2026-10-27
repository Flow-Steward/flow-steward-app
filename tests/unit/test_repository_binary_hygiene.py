from __future__ import annotations

import os
import subprocess
from fnmatch import fnmatch
from pathlib import Path


def _source_repo_root() -> Path:
    """The git checkout, which the isolated core-only workspace is not.

    That workspace is a plain directory, so `git ls-files` exits 128 there and
    this check failed for a reason unrelated to what it verifies. The gate
    runner already exports PYTEST_SOURCE_REPO_ROOT for reading immutable source
    artifacts; a git query about tracked files belongs on the same boundary.
    """
    configured = str(os.environ.get("PYTEST_SOURCE_REPO_ROOT") or "").strip()
    return Path(configured).resolve() if configured else Path(__file__).resolve().parents[2]


ROOT = _source_repo_root()


def _git_lines(*args: str) -> list[str]:
    completed = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def _tracked_extension_paths() -> list[str]:
    manifest_candidates = (
        Path(str(os.environ.get("FS_BOUNDARY_TRACKED_FILES_MANIFEST") or "")),
        Path("/app/logs/quality/tracked-files.txt"),
    )
    for manifest in manifest_candidates:
        if manifest.is_file():
            return [
                line.strip()
                for line in manifest.read_text(encoding="utf-8").splitlines()
                if line.strip().startswith("extensions/")
            ]
    return _git_lines("ls-files", "extensions")


def _lfs_patterns() -> tuple[str, ...]:
    patterns: list[str] = []
    for line in (ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if fields and "filter=lfs" in fields[1:]:
            patterns.append(fields[0])
    return tuple(patterns)


def test_core_tracks_no_extension_payload() -> None:
    """Core ships no extension: extensions/ holds only the .gitignore for local installs.

    Bundles used to live here, with their wheelhouses in Git LFS. They moved to their
    own repositories, so any tracked file but the .gitignore is one that came back.
    """
    tracked = _tracked_extension_paths()

    assert set(tracked) <= {"extensions/.gitignore"}, tracked
    assert not [pattern for pattern in _lfs_patterns() if pattern.startswith("extensions/")]
