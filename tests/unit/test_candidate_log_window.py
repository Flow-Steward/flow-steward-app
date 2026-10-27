from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCANNER = ROOT / "scripts" / "smoke" / "candidate_log_window_check.py"


def _run_scanner(tmp_path: Path, *, worker_log: str, allowlist: dict | None = None):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    calls = tmp_path / "docker-calls.txt"
    docker = bin_dir / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FS_TEST_DOCKER_CALLS"
if [ "${1:-}" = ps ]; then
  printf '%s\n' \
    'candidate-app|fs-app' \
    'candidate-worker|fs-worker' \
    'candidate-mcp|fs-mcp' \
    'candidate-control|control-plane'
  exit 0
fi
if [ "${1:-}" = logs ]; then
  container="${*: -1}"
  if [ "$container" = candidate-worker ]; then
    printf '%s\n' "$FS_TEST_WORKER_LOG"
  else
    printf '%s\n' 'INFO candidate service healthy'
  fi
  exit 0
fi
exit 2
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    allowlist_path = tmp_path / "allowlist.json"
    allowlist_path.write_text(json.dumps(allowlist or {"services": {}}), encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        "FS_TEST_DOCKER_CALLS": str(calls),
        "FS_TEST_WORKER_LOG": worker_log,
    }
    result = subprocess.run(
        [
            sys.executable,
            str(SCANNER),
            "--compose-project",
            "candidate-project",
            "--since",
            "2026-08-15T08:00:00Z",
            "--until",
            "2026-08-15T08:05:00Z",
            "--allowlist",
            str(allowlist_path),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, calls.read_text(encoding="utf-8") if calls.exists() else ""


def test_candidate_log_window_fails_on_worker_protocol_error_and_bounds_every_log_read(
    tmp_path: Path,
) -> None:
    result, calls = _run_scanner(
        tmp_path,
        worker_log="ERROR PostgreSQL protocol error while polling extension deliveries",
    )

    assert result.returncode == 1
    assert "fs-worker" in result.stdout
    assert "PostgreSQL protocol error" in result.stdout
    assert calls.count("logs --since 2026-08-15T08:00:00Z --until 2026-08-15T08:05:00Z") == 4


def test_candidate_log_window_allowlist_requires_an_exact_full_line(tmp_path: Path) -> None:
    exact = "ERROR This demo intentionally stops with stop_fail to verify failure paths."
    allowlist = {"services": {"fs-worker": [exact]}}

    accepted, _ = _run_scanner(tmp_path / "accepted", worker_log=exact, allowlist=allowlist)
    rejected, _ = _run_scanner(
        tmp_path / "rejected",
        worker_log=exact + " unexpected suffix",
        allowlist=allowlist,
    )

    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    assert rejected.returncode == 1
