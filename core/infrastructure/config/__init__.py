"""Canonical infrastructure configuration package."""

from __future__ import annotations

from core.infrastructure.config.runtime_config import CONFIG
from core.infrastructure.config.version import APP_VERSION, get_app_version

__all__ = [
    "APP_VERSION",
    "CONFIG",
    "get_app_version",
]
