"""Ports owned by the lifecycle operation application service."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Protocol

from core.application.lifecycle.contracts import (
    CancellationToken,
    LifecycleLockDiagnosis,
    LifecycleOperationDefinition,
    LifecycleOperationJournal,
    LifecycleOperationStep,
)
from core.application.management.command_specs.contracts import OperationProgressEvent

ProgressSink = Callable[[OperationProgressEvent], None]
TechnicalSink = Callable[[str], None]
HeartbeatSink = Callable[[str], None]


class LifecycleLog(Protocol):
    path: Path

    def write(self, message: str) -> None: ...

    def close(self) -> None: ...


class LifecycleState(Protocol):
    def journal_path(self, operation_id: str) -> Path: ...

    def previous_config_path(self, operation_id: str) -> Path: ...

    def load_journal(self, operation_id: str) -> LifecycleOperationJournal | None: ...

    def save_journal(self, journal: LifecycleOperationJournal) -> None: ...

    def save_previous_config(self, operation_id: str, snapshot: Mapping[str, object]) -> Path: ...

    def load_previous_config(self, operation_id: str) -> dict[str, object] | None: ...

    def unfinished_journals(self) -> tuple[LifecycleOperationJournal, ...]: ...

    def open_log(self, operation_id: str) -> LifecycleLog: ...

    def operation_lock(
        self, operation_id: str, *, mutating: bool
    ) -> AbstractContextManager[None]: ...

    def diagnose_active_lock(self) -> LifecycleLockDiagnosis: ...

    def mark_recovery_required(
        self,
        operation_id: str,
        *,
        step: str,
        error_code: str,
        initial_journal: LifecycleOperationJournal | None = None,
    ) -> None: ...


class LifecycleOperationGateway(Protocol):
    """Execute technical steps selected by an application-owned definition."""

    def postcondition_holds(
        self,
        definition: LifecycleOperationDefinition,
        step: LifecycleOperationStep,
    ) -> bool: ...

    def execute_step(
        self,
        definition: LifecycleOperationDefinition,
        step: LifecycleOperationStep,
        *,
        output_sink: TechnicalSink,
        heartbeat: HeartbeatSink,
        cancellation: CancellationToken,
    ) -> None: ...
