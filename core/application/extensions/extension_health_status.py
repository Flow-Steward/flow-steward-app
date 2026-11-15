"""How an extension's health is reported: an active probe or its persisted state."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from core.application.management.query_models import ExtensionDetailView

_PROBE_STATUS = {
    "healthy": "healthy",
    "ok": "healthy",
    "succeeded": "healthy",
    "success": "healthy",
    "failed": "failed",
    "unhealthy": "failed",
    "error": "failed",
    "degraded": "degraded",
}


def probe_health(probe: dict[str, Any]) -> dict[str, Any]:
    """Normalize a runtime health probe answer to healthy/failed/degraded/unknown."""
    raw_status = str(probe.get("status") or "unknown").strip().lower()
    reason = str(probe.get("detail") or "No probe detail returned.").strip()
    return {
        **probe,
        "status": _PROBE_STATUS.get(raw_status, "unknown"),
        "detail": reason,
        "reason": reason,
        "checked_at": datetime.now(UTC).isoformat(),
    }


def persisted_health(detail: ExtensionDetailView) -> dict[str, Any]:
    """Health from the stored runtime/health state when no probe is run."""
    persisted = str(detail.health_status or "").strip().lower()
    runtime_status = str(detail.runtime_status or "").strip().lower()
    if runtime_status in {"failed", "incompatible", "unavailable"} or persisted in {
        "failed",
        "unhealthy",
    }:
        status = "failed"
    elif persisted == "degraded":
        status = "degraded"
    elif persisted in {"healthy", "ok"}:
        status = "healthy"
    else:
        status = "unknown"
    return {
        "status": status,
        "detail": "Persisted extension health state; no active probe was run.",
    }
