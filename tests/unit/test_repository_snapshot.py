from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from core.application.tooling.quality.repository_snapshot import repository_snapshot


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_repository_snapshot_changes_for_content_symlink_mode_and_untracked_mutations(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    tracked = repo / "tracked.txt"
    tracked.write_text("baseline\n", encoding="utf-8")
    link = repo / "current"
    link.symlink_to("tracked.txt")
    _git(repo, "add", "tracked.txt", "current")

    baseline = repository_snapshot(repo)

    tracked.write_text("changed\n", encoding="utf-8")
    assert repository_snapshot(repo) != baseline
    tracked.write_text("baseline\n", encoding="utf-8")

    link.unlink()
    link.symlink_to("other.txt")
    assert repository_snapshot(repo) != baseline
    link.unlink()
    link.symlink_to("tracked.txt")

    tracked.chmod(0o755)
    assert repository_snapshot(repo) != baseline
    tracked.chmod(0o644)

    (repo / "untracked.txt").write_text("new\n", encoding="utf-8")
    assert repository_snapshot(repo) != baseline


def test_repository_snapshot_cli_avoids_lfs_filter_that_makes_git_status_fail(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    wheel = repo / "fixture.whl"
    wheel.write_bytes(b"initial wheel bytes")
    (repo / ".gitattributes").write_text("*.whl filter=lfs\n", encoding="utf-8")
    _git(repo, "add", ".gitattributes", "fixture.whl")
    indexed_stat = wheel.stat()
    filter_marker = tmp_path / "lfs-filter-invoked"
    filter_script = tmp_path / "failing-lfs-clean"
    filter_script.write_text(
        f"#!/bin/sh\ntouch '{filter_marker}'\nexit 97\n",
        encoding="utf-8",
    )
    filter_script.chmod(0o755)
    _git(repo, "config", "filter.lfs.clean", str(filter_script))
    _git(repo, "config", "filter.lfs.required", "true")
    _git(repo, "config", "core.trustctime", "false")
    # Equal size and a changed timestamp force Git to compare content through
    # the filter, rather than trusting unchanged stats or only a size difference.
    wheel.write_bytes(b"changed wheel bytes")
    os.utime(wheel, ns=(indexed_stat.st_atime_ns, indexed_stat.st_mtime_ns + 2_000_000_000))
    git_env = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }

    old_guard = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=repo,
        env=git_env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert old_guard.returncode != 0
    assert filter_marker.exists()
    filter_marker.unlink()

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "core.application.tooling.quality.repository_snapshot",
            "--repository-root",
            str(repo),
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=git_env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert len(result.stdout.strip()) == 64
    assert not filter_marker.exists()
