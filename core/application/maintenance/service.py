"""Application-owned maintenance enable, drain, status, and disable policy."""

from __future__ import annotations

from core.application.lifecycle.errors import LifecycleError
from core.application.maintenance.contracts import (
    MaintenanceChangeResult,
    MaintenanceOwnerKind,
    MaintenancePhase,
    MaintenanceState,
    MaintenanceStatusResult,
)
from core.application.maintenance.ports import (
    MaintenanceActiveWork,
    MaintenanceHealthProvider,
    MaintenanceReadinessReconciler,
    MaintenanceStateGateway,
    MaintenanceUnfinishedOperations,
)

_MESSAGE_INSPECT_MAINTENANCE_STATUS_BEFORE_RETRYING = "Inspect maintenance status before retrying."
_MESSAGE_FLOW_STEWARD_MAINTENANCE_STATUS = "flow-steward maintenance status"


class MaintenanceService:
    """Own idempotent maintenance policy while infrastructure owns persistence."""

    def __init__(
        self,
        *,
        state: MaintenanceStateGateway,
        active_work: MaintenanceActiveWork,
        unfinished_operations: MaintenanceUnfinishedOperations,
        health: MaintenanceHealthProvider,
        readiness_reconciler: MaintenanceReadinessReconciler,
    ) -> None:
        self._state = state
        self._active_work = active_work
        self._unfinished_operations = unfinished_operations
        self._health = health
        self._readiness_reconciler = readiness_reconciler

    def status(self) -> MaintenanceStatusResult:
        state = self._state.read()
        if state is None:
            return MaintenanceStatusResult(status="disabled")
        return self._public_status(state)

    def owned_by(
        self,
        *,
        owner_kind: MaintenanceOwnerKind,
        owner_id: str,
        phase: MaintenancePhase = "enabled",
    ) -> bool:
        """Return exact internal ownership without exposing owner IDs publicly."""
        state = self._state.read()
        return bool(
            state is not None
            and state.owner_kind == owner_kind
            and state.owner_id == owner_id
            and state.phase == phase
        )

    def enable(
        self,
        *,
        owner_kind: MaintenanceOwnerKind = "manual",
        owner_id: str = "manual-maintenance",
    ) -> MaintenanceChangeResult:
        previous = self._state.read()
        state = self._state.begin(owner_kind=owner_kind, owner_id=owner_id)
        self._active_work.wait_for_idle()
        if not self._state.mark_enabled(owner_id=state.owner_id):
            raise LifecycleError.recovery_required(
                code="maintenance_owner_changed",
                cause="Maintenance ownership changed while active work was draining.",
                remediation=_MESSAGE_INSPECT_MAINTENANCE_STATUS_BEFORE_RETRYING,
                step="maintenance_enable",
                recovery_command=_MESSAGE_FLOW_STEWARD_MAINTENANCE_STATUS,
            )
        enabled = self._state.read()
        if enabled is None:  # pragma: no cover - gateway postcondition invariant
            raise LifecycleError.recovery_required(
                code="maintenance_state_missing",
                cause="Maintenance state disappeared while it was being enabled.",
                remediation=_MESSAGE_INSPECT_MAINTENANCE_STATUS_BEFORE_RETRYING,
                step="maintenance_enable",
                recovery_command=_MESSAGE_FLOW_STEWARD_MAINTENANCE_STATUS,
            )
        already_enabled = previous is not None and previous.phase == "enabled"
        return MaintenanceChangeResult(
            status="already_enabled" if already_enabled else "enabled",
            changed=not already_enabled,
            activated_at=enabled.activated_at,
            owner_kind=enabled.owner_kind,
        )

    def disable(self) -> MaintenanceChangeResult:
        state = self._state.read()
        if state is None:
            return MaintenanceChangeResult(status="already_disabled", changed=False)
        unfinished = self._unfinished_operations.unfinished_operation_ids()
        if unfinished:
            raise LifecycleError.stable_failure(
                code="maintenance_unfinished_operations",
                cause="Maintenance cannot be disabled while lifecycle operations are unfinished.",
                remediation="Resume or reconcile unfinished lifecycle operations, then retry.",
                step="maintenance_disable",
            )
        try:
            self._readiness_reconciler.reconcile()
        except Exception as exc:
            raise LifecycleError.stable_failure(
                code="maintenance_reconciliation_failed",
                cause="Derived readiness state could not be reconciled while maintenance was enabled.",
                remediation="Resolve the reconciliation failure, then retry maintenance disable.",
                step="maintenance_disable",
            ) from exc
        health_issues = self._health.mandatory_health_issues()
        if health_issues:
            raise LifecycleError.stable_failure(
                code="maintenance_health_failed",
                cause="Mandatory health checks failed while disabling maintenance.",
                remediation="Restore mandatory service health, then retry maintenance disable.",
                step="maintenance_disable",
            )
        if not self._state.clear(expected_owner_id=state.owner_id):
            raise LifecycleError.recovery_required(
                code="maintenance_owner_changed",
                cause="Maintenance ownership changed while it was being disabled.",
                remediation=_MESSAGE_INSPECT_MAINTENANCE_STATUS_BEFORE_RETRYING,
                step="maintenance_disable",
                recovery_command=_MESSAGE_FLOW_STEWARD_MAINTENANCE_STATUS,
            )
        return MaintenanceChangeResult(status="disabled", changed=True)

    @staticmethod
    def _public_status(state: MaintenanceState) -> MaintenanceStatusResult:
        return MaintenanceStatusResult(
            status=state.phase,
            activated_at=state.activated_at,
            owner_kind=state.owner_kind,
        )


__all__ = ["MaintenanceService"]
