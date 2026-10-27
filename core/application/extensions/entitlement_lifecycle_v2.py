"""Install lifecycle entitlement and drain helpers for Extension Platform v2."""

from __future__ import annotations

import time
from typing import Any

ALIGNED_ENTITLEMENT_STATES: set[str] = {"active", "grace", "suspended", "terminated"}

_STATE_ALIASES: dict[str, str] = {
    "trial_active": "active",
    "active": "active",
    "grace_period": "grace",
    "grace": "grace",
    "suspended_nonpayment": "suspended",
    "suspended": "suspended",
    "disabled": "terminated",
    "trial_expired": "terminated",
    "terminated": "terminated",
}


def normalize_entitlement_state(value: str) -> str:
    normalized = str(value or "").strip().lower()
    return _STATE_ALIASES.get(normalized, "terminated")


def can_start_new_invocation(
    *, entitlement_state: str, inflight_count: int = 0
) -> tuple[bool, str]:
    state = normalize_entitlement_state(entitlement_state)
    if state in {"active", "grace"}:
        return True, ""
    if state == "suspended":
        if int(inflight_count or 0) > 0:
            return (
                False,
                "Install is suspended for new invocations; allow in-flight completion only",
            )
        return False, "Install is suspended"
    return False, "Install is terminated"


def build_uninstall_drain_plan(
    *,
    install_id: str,
    dependent_workflow_ids: list[str],
    inflight_invocations: int,
    force: bool = False,
) -> dict[str, Any]:
    deps = sorted(
        {str(item or "").strip() for item in dependent_workflow_ids if str(item or "").strip()}
    )
    inflight = max(0, int(inflight_invocations or 0))
    if force:
        return {
            "install_id": str(install_id or "").strip(),
            "allowed": True,
            "mode": "forced",
            "dependent_workflow_ids": deps,
            "inflight_invocations": inflight,
            "needs_drain": bool(deps or inflight),
        }
    if deps or inflight:
        return {
            "install_id": str(install_id or "").strip(),
            "allowed": False,
            "mode": "drain_required",
            "dependent_workflow_ids": deps,
            "inflight_invocations": inflight,
            "needs_drain": True,
        }
    return {
        "install_id": str(install_id or "").strip(),
        "allowed": True,
        "mode": "safe",
        "dependent_workflow_ids": [],
        "inflight_invocations": 0,
        "needs_drain": False,
    }


def begin_drain_window(
    *,
    install_id: str,
    ttl_seconds: int = 900,
    reason: str = "",
) -> dict[str, Any]:
    now = int(time.time())
    ttl = max(60, min(int(ttl_seconds or 900), 24 * 3600))
    return {
        "install_id": str(install_id or "").strip(),
        "drain_started_at": now,
        "drain_until": now + ttl,
        "reason": str(reason or "").strip(),
        "mode": "draining",
    }


def record_usage_event_idempotent(
    *,
    usage_store: dict[str, dict[str, Any]],
    event_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    normalized_event_id = str(event_id or "").strip()
    if not normalized_event_id:
        raise ValueError("event_id is required")
    existing = usage_store.get(normalized_event_id)
    if isinstance(existing, dict):
        return dict(existing)
    event = {
        "event_id": normalized_event_id,
        "install_id": str(payload.get("install_id") or "").strip(),
        "event_kind": str(payload.get("event_kind") or "").strip(),
        "quantity": float(payload.get("quantity") or 1.0),
        "occurred_at": float(payload.get("occurred_at") or time.time()),
        "metadata": dict(payload.get("metadata") or {}),
    }
    usage_store[normalized_event_id] = event
    return dict(event)
