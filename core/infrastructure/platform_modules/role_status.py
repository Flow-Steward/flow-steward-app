"""What each running role loaded and skipped of the platform-module set (ADR 0082 §5).

A role that loads platform modules writes one small JSON file when it has started:
the modules it loaded and the failure record of each module it skipped. The release
workflow reads these files after a module-only change, through ``docker compose exec``
into the running role container, and treats a module that failed to load as a failed
change.

The files live directly in the container's ``/dev/shm``: Docker gives every container
start a fresh ``/dev/shm``, so a file there was written by the role's current start and
never by a start before a restart. ``/dev/shm`` is the sticky, world-writable tmpfs, so
the roles of one Compact container, which run as different users, each own their file
and none can replace another's. Nothing else is shared or persisted; a role whose file
cannot be written only logs it.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

ROLE_STATUS_DIR_ENV = "FS_PLATFORM_MODULE_STATUS_DIR"
# The container's own tmpfs, fresh at every container start (see the module docstring).
DEFAULT_ROLE_STATUS_DIR = Path("/dev/shm")  # nosec B108  # NOSONAR
_FILE_PREFIX = "flow-steward-platform-module-status-"
ROLE_STATUS_SCHEMA_VERSION = 1
#: Bounds of what one role reports and what a reader accepts.
_MAX_MODULES = 64
_MAX_TEXT_CHARS = 500
_MAX_FILE_BYTES = 256 * 1024
_ROLE_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

_logger = logging.getLogger(__name__)


def role_status_dir() -> Path:
    configured = str(_runtime_env.get(ROLE_STATUS_DIR_ENV) or "").strip()
    return Path(configured) if configured else DEFAULT_ROLE_STATUS_DIR


def _text(value: Any) -> str:
    text = str(value or "").strip()
    return text[:_MAX_TEXT_CHARS]


def _modules(records: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for record in records:
        if len(result) >= _MAX_MODULES:
            break
        module_id = _text(record.get("id"))
        if module_id:
            result.append({"id": module_id, "version": _text(record.get("version"))})
    return result


def _failures(records: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for record in records:
        if len(result) >= _MAX_MODULES:
            break
        module_id = _text(record.get("id"))
        if module_id:
            result.append(
                {
                    "id": module_id,
                    "version": _text(record.get("version")),
                    "load_error": _text(record.get("load_error")),
                }
            )
    return result


def module_status_records(
    modules: Sequence[Any],
) -> list[dict[str, str]]:
    """``{"id", "version"}`` of prepared modules (``PreparedPlatformModule``)."""
    return [{"id": str(module.module_id), "version": str(module.version)} for module in modules]


def write_role_module_status(
    role: str,
    *,
    loaded: Sequence[Mapping[str, Any]],
    failures: Sequence[Mapping[str, Any]],
    directory: Path | None = None,
) -> bool:
    """Record what ``role`` loaded and skipped; ``False`` when it could not be written.

    Called by a role once its modules are registered, and only when it had a module
    list. An I/O problem is logged, never raised: the status must not stop a role.
    """
    if not _ROLE_PATTERN.fullmatch(role):
        raise ValueError(f"invalid role name for a platform-module status: {role!r}")
    target = role_status_dir() if directory is None else Path(directory)
    payload = {
        "schema_version": ROLE_STATUS_SCHEMA_VERSION,
        "role": role,
        "loaded": _modules(loaded),
        "failures": _failures(failures),
    }
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    temporary: str | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=f".{_FILE_PREFIX}{role}.", dir=target)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(body)
        os.chmod(temporary, 0o644)
        os.replace(temporary, target / f"{_FILE_PREFIX}{role}.json")
        temporary = None
    except OSError as exc:
        _logger.warning(
            "Platform module status of role %s could not be written to %s: %s",
            role,
            target,
            exc,
        )
        return False
    finally:
        if temporary is not None:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
    return True


def read_role_module_statuses(directory: Path | None = None) -> dict[str, dict[str, Any]]:
    """Every role status in ``directory``, by role; unreadable files are left out."""
    target = role_status_dir() if directory is None else Path(directory)
    statuses: dict[str, dict[str, Any]] = {}
    try:
        candidates = sorted(target.glob(f"{_FILE_PREFIX}*.json"))
    except OSError:
        return statuses
    for path in candidates:
        role = path.stem.removeprefix(_FILE_PREFIX)
        if not _ROLE_PATTERN.fullmatch(role):
            continue
        try:
            if path.stat().st_size > _MAX_FILE_BYTES:
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != ROLE_STATUS_SCHEMA_VERSION
            or payload.get("role") != role
        ):
            continue
        loaded = payload.get("loaded")
        failures = payload.get("failures")
        statuses[role] = {
            "loaded": _modules([item for item in loaded or [] if isinstance(item, dict)]),
            "failures": _failures([item for item in failures or [] if isinstance(item, dict)]),
        }
    return statuses


__all__ = [
    "DEFAULT_ROLE_STATUS_DIR",
    "ROLE_STATUS_DIR_ENV",
    "ROLE_STATUS_SCHEMA_VERSION",
    "read_role_module_statuses",
    "role_status_dir",
    "write_role_module_status",
]
