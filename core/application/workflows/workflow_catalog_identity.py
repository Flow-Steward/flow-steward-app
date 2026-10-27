"""Deterministic identity helpers for projected workflow and block rows."""

from __future__ import annotations

import hashlib
from typing import Any

_WORKFLOW_PREFIX = "wfproj"
_BLOCK_PREFIX = "blkproj"
_MAX_IDENTIFIER_LENGTH = 255


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _stable_suffix(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _composite_identifier(prefix: str, install_id: str, local_id: str) -> str:
    normalized_prefix = _normalize_text(prefix)
    normalized_install_id = _normalize_text(install_id)
    normalized_local_id = _normalize_text(local_id)
    if not normalized_prefix:
        raise ValueError("prefix is required")
    if not normalized_install_id:
        raise ValueError("install_id is required")
    if not normalized_local_id:
        raise ValueError("local_id is required")

    identifier = f"{normalized_prefix}:{normalized_install_id}:{normalized_local_id}"
    if len(identifier) <= _MAX_IDENTIFIER_LENGTH:
        return identifier

    digest = _stable_suffix(identifier)
    overflow = len(identifier) - _MAX_IDENTIFIER_LENGTH
    keep = max(8, len(normalized_local_id) - overflow - len(digest) - 1)
    shortened_local_id = normalized_local_id[:keep].rstrip(":")
    return f"{normalized_prefix}:{normalized_install_id}:{shortened_local_id}:{digest}"


def projected_workflow_id(install_id: str, local_workflow_id: str) -> str:
    """Build the stable projected workflow id (wfproj:<install_id>:<local_workflow_id>)."""
    return _composite_identifier(_WORKFLOW_PREFIX, install_id, local_workflow_id)


def projected_block_key(install_id: str, local_block_key: str) -> str:
    """Build the stable projected block key (blkproj:<install_id>:<local_block_key>)."""
    return _composite_identifier(_BLOCK_PREFIX, install_id, local_block_key)


def parse_projected_identity(value: str) -> dict[str, str]:
    """Parse a projected identifier into prefix/install/local parts."""
    text = _normalize_text(value)
    if not text:
        return {}
    parts = text.split(":", 2)
    if len(parts) != 3:
        return {}
    prefix, install_id, local_id = parts
    if not prefix or not install_id or not local_id:
        return {}
    return {
        "prefix": prefix,
        "install_id": install_id,
        "local_id": local_id,
    }
