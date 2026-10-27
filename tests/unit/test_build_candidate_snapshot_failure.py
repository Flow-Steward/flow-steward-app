"""AC17-R159: build_candidate.sh says why the candidate snapshot refused.

Measured on a live Scalable rollback: the harness answered {"failure_class": "harness",
"reason": "candidate snapshot is stale: candidate source changed"} on stdout, the
script's `SNAPSHOT_OUTPUT="$(...)"` swallowed it under `set -e`, and the installer
showed "Error: exit code 2" with a hint about uncommitted changes that did not apply.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_STALE = (
    '{"failure_class":"harness","reason":"candidate snapshot is stale: '
    'candidate source changed","status":"failed"}'
)


def test_a_refused_snapshot_reaches_stderr(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "scripts/release").mkdir(parents=True)
    shutil.copy2(ROOT / "scripts/release/build_candidate.sh", repo / "scripts/release/")
    for command in (
        ["git", "init", "-q"],
        ["git", "-c", "user.email=t@example.test", "-c", "user.name=t", "add", "."],
        ["git", "-c", "user.email=t@example.test", "-c", "user.name=t", "commit", "-qm", "c"],
    ):
        subprocess.run(command, cwd=repo, check=True, capture_output=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "python3"
    fake.write_text(f"#!/bin/sh\nprintf '%s\\n' '{_STALE}'\nexit 2\n", encoding="utf-8")
    fake.chmod(0o755)
    environment = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    environment["FS_RELEASE_TLS_INPUTS"] = "ingress"

    result = subprocess.run(
        ["bash", str(repo / "scripts/release/build_candidate.sh")],
        cwd=repo,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 2
    assert "candidate snapshot failed:" in result.stderr
    assert "candidate snapshot is stale: candidate source changed" in result.stderr
