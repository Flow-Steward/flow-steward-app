"""Dashboard rollup and security posture scan helpers."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from core.infrastructure.runtime.control.config import ControlConfig

logger = logging.getLogger(__name__)


def dashboard_rollup_scan(bl, cfg: ControlConfig) -> dict:
    """Refresh dashboard facts/rollups for historical read-only summary paths."""
    summary: dict[str, Any] = {
        "refreshed_accounts": 0,
        "facts_rows_deleted": 0,
        "facts_rows_inserted": 0,
        "daily_rows_deleted": 0,
        "daily_rows_inserted": 0,
        "details": [],
    }
    if not cfg.dashboard_rollup_enabled:
        summary["disabled"] = True
        return summary
    refresh_fn = getattr(bl, "refresh_dashboard_rollups", None)
    if not callable(refresh_fn):
        summary["unavailable"] = True
        return summary
    try:
        payload = (
            refresh_fn(
                backfill_days=cfg.dashboard_rollup_backfill_days,
                lookback_days=cfg.dashboard_rollup_lookback_days,
            )
            or {}
        )
    except Exception as exc:
        return {
            **summary,
            "status": "failed",
            "error": str(exc),
        }

    details = payload.get("details")
    if isinstance(details, list):
        summary["details"] = [dict(row) for row in details if isinstance(row, dict)]
    summary["refreshed_accounts"] = int(payload.get("refreshed_accounts") or 0)
    summary["facts_rows_deleted"] = int(payload.get("facts_rows_deleted") or 0)
    summary["facts_rows_inserted"] = int(payload.get("facts_rows_inserted") or 0)
    summary["daily_rows_deleted"] = int(payload.get("daily_rows_deleted") or 0)
    summary["daily_rows_inserted"] = int(payload.get("daily_rows_inserted") or 0)
    if payload.get("unavailable"):
        summary["unavailable"] = True
    summary["refreshed_at"] = str(payload.get("refreshed_at") or datetime.now(UTC).isoformat())
    return summary


def security_posture_scan(bl, cfg: ControlConfig) -> dict:
    """Recompute security posture snapshots for all active projects."""
    summary: dict[str, Any] = {"projects_scanned": 0, "snapshots_saved": 0}
    if not cfg.security_posture_enabled:
        summary["disabled"] = True
        return summary

    try:
        from core.infrastructure.security.config import security_enabled

        if not security_enabled():
            summary["scanning_disabled"] = True
            return summary
    except ImportError:
        summary["unavailable"] = True
        return summary

    required_ports = (
        "list_active_project_ids",
        "record_security_posture_loop_attempt",
        "record_security_posture_loop_success",
        "record_security_posture_loop_failure",
    )
    missing = [name for name in required_ports if not callable(getattr(bl, name, None))]
    if missing:
        raise RuntimeError(
            "required security posture repository port is unavailable: " + ", ".join(missing)
        )

    bl.record_security_posture_loop_attempt()
    try:
        project_ids = bl.list_active_project_ids()
        from core.infrastructure.security.posture import compute_and_save_posture

        for pid in project_ids:
            snapshot = compute_and_save_posture(pid)
            summary["projects_scanned"] += 1
            if snapshot:
                summary["snapshots_saved"] += 1
        bl.record_security_posture_loop_success()
    except Exception as exc:
        reason = str(exc or "security posture iteration failed")[:512]
        try:
            bl.record_security_posture_loop_failure(reason)
        except Exception:
            logger.exception("security_posture_scan: failed to persist iteration failure")
        logger.exception("security_posture_scan: iteration failed: %s", reason)
        raise

    summary["computed_at"] = datetime.now(UTC).isoformat()
    return summary
