"""Fail-closed installation mode from the canonical server environment markers."""

from __future__ import annotations

from typing import Literal

from core.infrastructure.runtime_env import runtime_env

InstallationMode = Literal["development", "production"]


def installation_mode() -> InstallationMode:
    """Enable Development only when every present marker explicitly agrees."""
    markers = [
        value
        for name in ("ENVIRONMENT", "FS_STACK_ENVIRONMENT")
        if (value := runtime_env.get(name)) is not None
    ]
    if markers and all(
        value.strip().lower() in {"development", "local_development"} for value in markers
    ):
        return "development"
    return "production"
