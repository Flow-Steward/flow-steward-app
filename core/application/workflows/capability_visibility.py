from __future__ import annotations

from collections.abc import Mapping
from typing import Any

AVAILABLE_STATUSES = frozenset({"", "available", "active", "ok", "ready"})


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def capability_available_for_execution(
    capability_id: Any,
    *,
    capability: Mapping[str, Any] | None = None,
) -> bool:
    """Availability constraints shared by runtime use and catalog discovery."""
    row = _mapping(capability)
    normalized_id = _text(capability_id)
    if not normalized_id:
        return False
    if row.get("enabled") is False:
        return False
    if row.get("deleted_at"):
        return False
    if row.get("new_runs_blocked") is True:
        return False
    status = _text(row.get("availability_status")).lower()
    return status in AVAILABLE_STATUSES


def capability_visible_by_policy(
    capability_id: Any,
    *,
    capability: Mapping[str, Any] | None = None,
) -> bool:
    """Discovery additionally excludes available capabilities hidden from the catalog."""
    return (
        capability_available_for_execution(capability_id, capability=capability)
        and _mapping(capability).get("visible_in_catalog") is not False
    )
