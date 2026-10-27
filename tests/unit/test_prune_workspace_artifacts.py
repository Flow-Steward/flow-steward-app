from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
SCRIPT = ROOT / "core" / "application" / "tooling" / "ops" / "prune_workspace_artifacts.py"


def _set_mtime(path: Path, *, days_ago: int) -> None:
    ts = time.time() - (days_ago * 24 * 60 * 60)
    os.utime(path, (ts, ts))


def test_prunes_stale_workspace(tmp_path: Path) -> None:
    workspace_root = tmp_path / "logs" / "workspaces"
    workspace = workspace_root / "nightfire-ui-abc123"
    workspace.mkdir(parents=True, exist_ok=True)
    _set_mtime(workspace, days_ago=10)

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--workspace-root",
            str(workspace_root),
            "--max-age-days",
            "7",
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=ROOT,
    )

    payload = json.loads(completed.stdout.strip())
    assert payload["deleted_count"] >= 1
    assert not workspace.exists()
