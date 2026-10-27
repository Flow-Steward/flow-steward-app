"""Public Compact maintenance policy over the installed runtime operator."""

from __future__ import annotations

from collections.abc import Callable

from core.application.lifecycle.errors import LifecycleError
from core.application.maintenance.contracts import (
    MaintenanceChangeResult,
    MaintenanceOwnerKind,
    MaintenanceStatusResult,
)
from core.application.maintenance.ports import CompactMaintenanceMutationState


class CompactMaintenanceClient:
    """Invoke the existing non-root operator instead of reading its private volume."""

    def __init__(
        self,
        invoke: Callable[[str], dict[str, object]],
        *,
        mutation_state: CompactMaintenanceMutationState,
    ) -> None:
        self._invoke = invoke
        self._mutation_state = mutation_state

    def status(self) -> MaintenanceStatusResult:
        return MaintenanceStatusResult.model_validate(self._invoke("maintenance-status"))

    def enable(
        self,
        *,
        owner_kind: MaintenanceOwnerKind = "manual",
        owner_id: str = "manual-maintenance",
    ) -> MaintenanceChangeResult:
        if owner_kind != "manual" or owner_id != "manual-maintenance":
            raise ValueError("Compact public maintenance only supports the manual operator owner")
        with self._mutation_state.operation_lock("maintenance-enable", mutating=True):
            return MaintenanceChangeResult.model_validate(self._invoke("maintenance-enable"))

    def disable(self) -> MaintenanceChangeResult:
        with self._mutation_state.operation_lock("maintenance-disable", mutating=True):
            # The native marker owner checks volume-side journals. Host management
            # journals are deliberately not mounted into the product runtime.
            if self._mutation_state.unfinished_journals():
                raise LifecycleError.stable_failure(
                    code="maintenance_unfinished_operations",
                    cause="Maintenance cannot be disabled while lifecycle operations are unfinished.",
                    remediation="Resume or reconcile unfinished lifecycle operations, then retry.",
                    step="maintenance_disable",
                )
            return MaintenanceChangeResult.model_validate(self._invoke("maintenance-disable"))
