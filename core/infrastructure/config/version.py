"""Canonical application version — single source of truth for all runtime and deployment defaults."""

from __future__ import annotations

import pathlib

_VERSION_FILE = pathlib.Path(__file__).resolve().parents[3] / "VERSION"

APP_VERSION: str = _VERSION_FILE.read_text(encoding="utf-8").strip()


def get_app_version() -> str:
    """Return the canonical application version string."""
    return APP_VERSION
