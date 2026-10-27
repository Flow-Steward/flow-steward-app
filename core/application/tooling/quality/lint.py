#!/usr/bin/env python3
"""Proxy to the canonical repository quality gate script.

Usage examples:
  flow-steward quality lint
  flow-steward quality lint --mode fast
  flow-steward quality lint --checks frontend-workspace
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root = _repo_root()
    script = root / "core" / "entrypoints" / "cli" / "scripts" / "lint.sh"
    if not script.exists():
        print(f"[tooling lint] lint script not found: {script}", file=sys.stderr)
        return 1

    env = dict(_runtime_env)
    env.setdefault("FS_PYTHON_BIN", sys.executable)
    cmd = ["bash", str(script), *args]
    completed = subprocess.run(cmd, cwd=root, env=env, check=False)
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
