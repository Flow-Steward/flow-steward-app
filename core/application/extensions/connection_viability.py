"""Provider-neutral viability policy for project connection metadata."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_FAILED_HEALTH_STATUSES = frozenset({"error", "failed", "unavailable"})
_FAILED_RUNTIME_STATUSES = frozenset({"failed", "incompatible", "unavailable"})


def project_connection_is_viable(connection: Mapping[str, Any] | None) -> bool:
    """Return whether non-secret metadata permits selecting the connection.

    Empty or ``unknown`` health is allowed for a newly created token connection.
    ``degraded`` is also usable while runtime remains available; OAuth lifecycle
    checks can still require reauthorization independently. Explicit failure
    states and disabled rows always fail closed.
    """
    if connection is None or connection.get("enabled") is False:
        return False
    health_status = str(connection.get("health_status") or "").strip().lower()
    runtime_status = str(connection.get("runtime_status") or "").strip().lower()
    return (
        health_status not in _FAILED_HEALTH_STATUSES
        and runtime_status not in _FAILED_RUNTIME_STATUSES
    )


__all__ = ["project_connection_is_viable"]
