#!/usr/bin/env python3
"""Deterministically prune ephemeral workspace artifacts.

This script prunes stale artifacts from `logs/workspaces/`.
"""

from __future__ import annotations

import argparse
import json
import shutil
import time
from contextlib import suppress
from pathlib import Path

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

GITKEEP_VALUE = ".gitkeep"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prune stale workspace artifacts from logs/workspaces."
    )
    parser.add_argument(
        "--workspace-root",
        default=_runtime_env.get("FS_WORKSPACES_ROOT", "logs/workspaces"),
        help="Canonical workspace artifacts root.",
    )
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=max(1, int(_runtime_env.get("FS_WORKSPACE_MAX_AGE_DAYS", "7"))),
        help="Delete only artifacts older than this many days.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Show actions without deleting.")
    return parser.parse_args()


def _cutoff_epoch(max_age_days: int) -> float:
    return time.time() - (max_age_days * 24 * 60 * 60)


def _is_stale(path: Path, *, cutoff: float) -> bool:
    try:
        return path.stat().st_mtime <= cutoff
    except OSError:
        return False


def _remove_path(path: Path, *, dry_run: bool, deleted: list[str]) -> None:
    deleted.append(str(path))
    if dry_run:
        return
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        with suppress(FileNotFoundError):
            path.unlink()


def _prune_workspace_root(
    root: Path,
    *,
    cutoff: float,
    dry_run: bool,
    deleted: list[str],
) -> None:
    if not root.exists():
        return
    for child in sorted(root.iterdir()):
        if child.name == GITKEEP_VALUE or child.is_file():
            continue
        if _is_stale(child, cutoff=cutoff):
            _remove_path(child, dry_run=dry_run, deleted=deleted)


def main() -> int:
    args = _parse_args()
    cutoff = _cutoff_epoch(args.max_age_days)
    workspace_root = Path(args.workspace_root).expanduser()
    deleted: list[str] = []

    _prune_workspace_root(workspace_root, cutoff=cutoff, dry_run=args.dry_run, deleted=deleted)

    payload = {
        "workspace_root": str(workspace_root),
        "max_age_days": int(args.max_age_days),
        "dry_run": bool(args.dry_run),
        "deleted_count": len(deleted),
        "deleted": deleted,
    }
    print(json.dumps(payload, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
