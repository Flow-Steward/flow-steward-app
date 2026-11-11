"""Canonical extension package syntax and folder mapping (ADR 0013)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from core.infrastructure.security.path_containment import contained_path

CANONICAL_EXTENSION_ID_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)


def normalize_extension_id(value: Any, field_name: str = "extension_id") -> str:
    """Return a strict canonical extension_id or raise ValueError."""
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    if not CANONICAL_EXTENSION_ID_RE.fullmatch(normalized):
        raise ValueError(
            f"{field_name} must be a canonical lowercase dot-separated package id "
            "(for example 'acme.notification-provider' or 'com.acme.crm-sync')"
        )
    return normalized


def is_canonical_extension_id(value: Any) -> bool:
    normalized = str(value or "").strip()
    return bool(CANONICAL_EXTENSION_ID_RE.fullmatch(normalized))


def extension_folder_name(extension_id: Any) -> str:
    """Map canonical extension_id to the standard local folder/module name."""
    canonical = normalize_extension_id(extension_id)
    return canonical.replace(".", "_").replace("-", "_")


def managed_extension_dir(extensions_root: str | Path, extension_id: Any) -> Path:
    """The extension's standard folder directly under ``extensions_root``.

    Raises ``ValueError`` for a non-canonical id or a folder outside the root.
    Symlinks are not followed: an installed extension's folder is a link into
    its release directory, and callers need the link itself.
    """
    return contained_path(
        extensions_root,
        extension_folder_name(extension_id),
        follow_symlinks=False,
    )
