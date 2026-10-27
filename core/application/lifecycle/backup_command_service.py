"""Public backup commands coordinated by the shared lifecycle kernel."""

from __future__ import annotations

from core.application.lifecycle.backup_service import (
    BackupCreateRequest,
    BackupListRequest,
    BackupListResult,
    BackupResult,
    BackupService,
    BackupVerificationStatus,
    BackupVerifyRequest,
)
from core.application.lifecycle.contracts import (
    CancellationToken,
    LifecycleLockStatus,
    LifecycleOperationDefinition,
    LifecycleOperationJournal,
    LifecycleOperationKind,
    LifecycleOperationStep,
)
from core.application.lifecycle.errors import LifecycleError
from core.application.lifecycle.operation_service import LifecycleOperationRunner
from core.application.lifecycle.ports import (
    HeartbeatSink,
    LifecycleState,
    ProgressSink,
    TechnicalSink,
)

_CREATE_STEP = LifecycleOperationStep(
    step_id="create_verified_backup",
    label="Create and verify protected PostgreSQL backup",
)
_RECOVERY_COMMAND = "flow-steward backup create"


class BackupCommandService:
    """Expose public backup commands without nesting locks in internal callers."""

    def __init__(self, *, backup: BackupService, lifecycle_state: LifecycleState) -> None:
        self._backup = backup
        self._lifecycle_state = lifecycle_state

    def create(
        self,
        request: BackupCreateRequest,
        *,
        progress_sink: ProgressSink | None = None,
        technical_sink: TechnicalSink | None = None,
        cancellation: CancellationToken | None = None,
    ) -> BackupResult:
        """Create through the shared mutation lock and durable operation journal."""
        existing = self._resumable_journal()
        if (
            existing is not None
            and request.operation_id is not None
            and request.operation_id != existing.operation_id
        ):
            raise _unfinished_error(existing)
        operation_id = existing.operation_id if existing is not None else request.operation_id
        gateway = _BackupCreateOperationGateway(
            backup=self._backup,
            progress_sink=progress_sink,
        )
        operation = LifecycleOperationRunner(
            gateway=gateway,
            state=self._lifecycle_state,
        ).execute(
            LifecycleOperationDefinition(
                operation_id=operation_id,
                kind=LifecycleOperationKind.BACKUP_CREATE,
                steps=(_CREATE_STEP,),
                recovery_command=_RECOVERY_COMMAND,
            ),
            progress_sink=progress_sink,
            technical_sink=technical_sink,
            cancellation=cancellation,
        )
        result = gateway.result or self._backup.find_verified(operation.operation_id)
        if result is None:
            failure = LifecycleError.internal(
                step=_CREATE_STEP.step_id,
                recovery_command=_RECOVERY_COMMAND,
            )
            failure.operation_id = operation.operation_id
            failure.log_path = operation.log_path
            raise failure
        return result.model_copy(update={"log_path": operation.log_path})

    def list(self, request: BackupListRequest) -> BackupListResult:
        """List metadata without acquiring the mutation lock."""
        return self._backup.list(request)

    def verify(
        self,
        request: BackupVerifyRequest,
        *,
        progress_sink: ProgressSink | None = None,
        technical_sink: TechnicalSink | None = None,
    ) -> BackupResult:
        """Verify one artifact without acquiring the mutation lock."""
        return self._backup.verify(
            request,
            progress_sink=progress_sink,
            technical_sink=technical_sink,
        )

    def _resumable_journal(self) -> LifecycleOperationJournal | None:
        diagnosis = self._lifecycle_state.diagnose_active_lock()
        if diagnosis.status is LifecycleLockStatus.ACTIVE:
            raise LifecycleError.conflict(
                operation_id=diagnosis.operation_id,
                cause=diagnosis.detail,
            )
        try:
            journals = self._lifecycle_state.unfinished_journals()
        except (OSError, ValueError) as exc:
            raise _unreadable_journal_error(diagnosis.operation_id) from exc
        if not journals:
            return None
        if len(journals) != 1 or journals[0].kind is not LifecycleOperationKind.BACKUP_CREATE:
            raise _unfinished_error(journals[0])
        return journals[0]


class _BackupCreateOperationGateway:
    def __init__(
        self,
        *,
        backup: BackupService,
        progress_sink: ProgressSink | None,
    ) -> None:
        self.backup = backup
        self.progress_sink = progress_sink
        self.result: BackupResult | None = None

    def postcondition_holds(
        self,
        definition: LifecycleOperationDefinition,
        step: LifecycleOperationStep,
    ) -> bool:
        if step.step_id != _CREATE_STEP.step_id or definition.operation_id is None:
            return False
        self.result = self.backup.find_verified(definition.operation_id)
        return bool(
            self.result is not None
            and self.result.verification_status is BackupVerificationStatus.VERIFIED
            and self.result.size_bytes > 0
        )

    def execute_step(
        self,
        definition: LifecycleOperationDefinition,
        step: LifecycleOperationStep,
        *,
        output_sink: TechnicalSink,
        heartbeat: HeartbeatSink,
        cancellation: CancellationToken,
    ) -> None:
        del heartbeat
        if step.step_id != _CREATE_STEP.step_id or definition.operation_id is None:
            raise ValueError(f"Unsupported backup lifecycle step: {step.step_id}")
        self.result = self.backup.create(
            BackupCreateRequest(operation_id=definition.operation_id),
            progress_sink=self.progress_sink,
            technical_sink=output_sink,
            cancellation=cancellation,
        )


def _unfinished_error(journal: LifecycleOperationJournal) -> LifecycleError:
    failure = LifecycleError.recovery_required(
        code="backup_operation_recovery_required",
        cause="An unfinished lifecycle mutation must be reconciled before creating a backup.",
        remediation=f"Resume or recover operation {journal.operation_id}.",
        step=journal.failed_step or journal.current_step or "recovery",
        recovery_command=journal.recovery_command,
    )
    failure.operation_id = journal.operation_id
    failure.log_path = journal.log_path
    return failure


def _unreadable_journal_error(operation_id: str | None) -> LifecycleError:
    failure = LifecycleError.recovery_required(
        code="backup_journal_unreadable",
        cause="Lifecycle journals are unreadable and safe backup creation cannot be proven.",
        remediation=(
            "Repair or quarantine unreadable lifecycle journals under .flow-steward/operations."
        ),
        step="recovery",
        recovery_command="flow-steward status",
    )
    failure.operation_id = operation_id
    return failure


__all__ = ["BackupCommandService"]
