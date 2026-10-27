"""Canonical environment-backed configuration for the private installer."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env


def installer_state_root(repo_root: Path, *, environment: Mapping[str, str] | None = None) -> Path:
    """Resolve the one installer state root shared by journal and runtime artifacts."""
    source = _runtime_env if environment is None else environment
    configured = str(source.get("FS_INSTALLER_STATE_DIR") or "").strip()
    path = Path(configured).expanduser() if configured else repo_root / ".flow-steward"
    if not path.is_absolute():
        path = repo_root / path
    return path.resolve()
