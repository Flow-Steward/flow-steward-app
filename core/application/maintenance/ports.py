"""Ports owned by the maintenance application service."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractContextManager
from typing import Protocol

from core.application.maintenance.contracts import (
    MaintenanceChangeResult,
    MaintenanceOwnerKind,
    MaintenanceState,
    MaintenanceStatusResult,
)


class CompactMaintenanceMutationState(Protocol):
    """Retained host mutation exclusion and unfinished-operation observation."""

    def operation_lock(
        self, operation_id: str, *, mutating: bool
    ) -> AbstractContextManager[None]: ...

    def unfinished_journals(self) -> Sequence[object]: ...


class MaintenanceServicePort(Protocol):
    """Public maintenance operations, whether local or runtime-delegated."""

    def status(self) -> MaintenanceStatusResult: ...

    def enable(
        self,
        *,
        owner_kind: MaintenanceOwnerKind = "manual",
        owner_id: str = "manual-maintenance",
    ) -> MaintenanceChangeResult: ...

    def disable(self) -> MaintenanceChangeResult: ...


class MaintenanceStateGateway(Protocol):
    def read(self) -> MaintenanceState | None: ...

    def begin(self, *, owner_kind: MaintenanceOwnerKind, owner_id: str) -> MaintenanceState: ...

    def mark_enabled(self, *, owner_id: str) -> bool: ...

    def clear(self, *, expected_owner_id: str) -> bool: ...


class MaintenanceActiveWork(Protocol):
    def wait_for_idle(self) -> None: ...


class MaintenanceHealthProvider(Protocol):
    def mandatory_health_issues(self) -> tuple[str, ...]: ...


class MaintenanceReadinessReconciler(Protocol):
    def reconcile(self) -> None: ...


class MaintenanceUnfinishedOperations(Protocol):
    def unfinished_operation_ids(self) -> tuple[str, ...]: ...


__all__ = [
    "CompactMaintenanceMutationState",
    "MaintenanceActiveWork",
    "MaintenanceHealthProvider",
    "MaintenanceReadinessReconciler",
    "MaintenanceServicePort",
    "MaintenanceStateGateway",
    "MaintenanceUnfinishedOperations",
]
