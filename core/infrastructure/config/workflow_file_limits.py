"""Operator-owned size limits for files that workflows make and test with."""

from __future__ import annotations

from core.infrastructure.config.env_registry import registry_default_value
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

_MIB = 1024 * 1024


def _bounded_bytes(name: str, *, fallback: int, minimum: int, maximum: int) -> int:
    try:
        default = int(registry_default_value(name))
    except (KeyError, TypeError, ValueError):
        default = fallback
    try:
        value = int(_runtime_env.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def base64_file_max_bytes() -> int:
    """Largest file the "Save file from base64" step writes; a step may set less."""
    return _bounded_bytes(
        "FS_BASE64_FILE_MAX_BYTES", fallback=50 * _MIB, minimum=1024, maximum=1024 * _MIB
    )


def workflow_test_file_max_bytes() -> int:
    """Largest test file an operator can attach to a workflow's file input."""
    return _bounded_bytes(
        "FS_WORKFLOW_TEST_FILE_MAX_BYTES", fallback=50 * _MIB, minimum=1024, maximum=1024 * _MIB
    )


def file_preview_max_bytes() -> int:
    """Largest generated image or PDF the run view previews inline."""
    return _bounded_bytes(
        "FS_FILE_PREVIEW_MAX_BYTES", fallback=20 * _MIB, minimum=1024, maximum=256 * _MIB
    )


__all__ = ["base64_file_max_bytes", "file_preview_max_bytes", "workflow_test_file_max_bytes"]
