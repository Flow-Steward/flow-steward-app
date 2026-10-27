"""Adapters from lifecycle services into CLI maintenance ports."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from http.client import HTTPException
from typing import Any

from core.application.lifecycle.contracts import CancellationToken
from core.infrastructure.lifecycle.job_quiescence_gateway import LifecycleJobQuiescenceGateway
from core.infrastructure.lifecycle.state_store import LifecycleStateStore


class _MaintenanceActiveWorkAdapter:
    def __init__(self, gateway: LifecycleJobQuiescenceGateway) -> None:
        self._gateway = gateway

    def wait_for_idle(self) -> None:
        self._gateway.wait_for_idle(
            cancellation=CancellationToken(),
            heartbeat=lambda _detail: None,
        )


class _MaintenanceUnfinishedOperationsAdapter:
    def __init__(self, state: LifecycleStateStore) -> None:
        self._state = state

    def unfinished_operation_ids(self) -> tuple[str, ...]:
        return tuple(journal.operation_id for journal in self._state.unfinished_journals())


def _is_persisted_stuck_jobs_only(check_id: object, value: object) -> bool:
    if check_id != "stuck_jobs" or not isinstance(value, Mapping):
        return False
    required_counts = ("persisted_count", "detection_count", "max_allowed")
    if any(type(value.get(field)) is not int for field in required_counts):
        return False
    error = value.get("error")
    if not isinstance(error, str) or error.strip():
        return False
    persisted_count = value["persisted_count"]
    detection_count = value["detection_count"]
    max_allowed = value["max_allowed"]
    return (
        value.get("ok") is False
        and persisted_count >= 0
        and detection_count == 0
        and max_allowed >= 0
        and persisted_count > max_allowed
    )


class _MaintenanceHealthAdapter:
    def __init__(
        self,
        probe: Callable[[], Mapping[str, Any]],
        *,
        ignored_check_ids: frozenset[str] = frozenset(),
    ) -> None:
        self._probe = probe
        self._ignored_check_ids = ignored_check_ids

    def mandatory_health_issues(self) -> tuple[str, ...]:
        issues: list[str] = []
        try:
            payload = self._probe()
        except (HTTPException, OSError, ValueError) as exc:
            issues.append(f"App readiness probe failed: {type(exc).__name__}")
            return tuple(issues)
        if payload.get("ready") is True:
            return tuple(issues)
        checks = payload.get("checks")
        if not isinstance(checks, Mapping):
            issues.append("App readiness response did not contain checks.")
            return tuple(issues)

        def ignored(check_id: object, value: object) -> bool:
            if check_id not in self._ignored_check_ids:
                return False
            return check_id != "stuck_jobs" or _is_persisted_stuck_jobs_only(check_id, value)

        issues.extend(
            str(check.get("detail") or check.get("error") or check_id)
            for check_id, value in checks.items()
            if not ignored(check_id, value)
            and isinstance(value, Mapping)
            and not bool((check := value).get("ok"))
        )
        issues.extend(
            str(check_id)
            for check_id, value in checks.items()
            if not ignored(check_id, value) and not isinstance(value, Mapping)
        )
        if not issues and not checks:
            issues.append("App readiness endpoint reported not ready.")
        return tuple(issues)
