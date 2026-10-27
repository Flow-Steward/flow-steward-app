from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.application.lifecycle.backup_command_service import BackupCommandService
from core.application.lifecycle.backup_service import (
    BackupArtifactSnapshot,
    BackupCreateRequest,
    BackupListRequest,
    BackupService,
    BackupVerificationStatus,
    BackupVerifyRequest,
    BackupWorkspace,
)
from core.application.lifecycle.contracts import (
    CancellationToken,
    LifecycleOperationJournal,
    LifecycleOperationKind,
    LifecycleOperationStatus,
)
from core.application.lifecycle.errors import LifecycleError
from core.application.management.command_specs.contracts import (
    ExitCode,
    OperationProgressEvent,
    OperationStepStatus,
)
from core.infrastructure.lifecycle.state_store import LifecycleStateStore


class _Log:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.messages: list[str] = []

    def write(self, message: str) -> None:
        self.messages.append(message)

    def close(self) -> None:
        pass


class RecordingBackupGateway:
    def __init__(self, tmp_path: Path) -> None:
        backups = tmp_path / "backups"
        self.workspace = BackupWorkspace(
            operation_id="pending",
            dump_path=backups / ".pending.dump",
            encrypted_path=backups / ".pending.dump.enc",
            validation_path=backups / ".pending.validate.dump",
            final_path=backups / "flow-steward.dump.enc",
        )
        self.calls: list[str] = []
        self.snapshots: dict[str, BackupArtifactSnapshot] = {}
        self.on_call: dict[str, Callable[[], None]] = {}

    def _call(self, name: str) -> None:
        self.calls.append(name)
        callback = self.on_call.get(name)
        if callback is not None:
            callback()

    def prepare_create(self, operation_id: str | None = None) -> BackupWorkspace:
        assert operation_id is not None
        self._call(f"prepare_create:{operation_id}")
        self.workspace = replace(self.workspace, operation_id=operation_id)
        return self.workspace

    def find_verified(self, operation_id: str) -> BackupArtifactSnapshot | None:
        self._call(f"find_verified:{operation_id}")
        return self.snapshots.get(operation_id)

    def open_log(self, operation_id: str) -> _Log:
        self._call(f"open_log:{operation_id}")
        return _Log(self.workspace.final_path.parent / f"{operation_id}.log")

    def validate_key(self) -> None:
        self._call("validate_key")

    def dump_database(self, destination: Path, *, log: _Log) -> None:
        del destination, log
        self._call("dump")

    def encrypt_dump(self, source: Path, destination: Path) -> None:
        del source, destination
        self._call("encrypt")

    def verify_envelope(self, source: Path) -> None:
        del source
        self._call("envelope")

    def decrypt_dump(self, source: Path, destination: Path) -> None:
        del source, destination
        self._call("decrypt")

    def inspect_dump(self, source: Path, *, log: _Log) -> None:
        del source, log
        self._call("inspect")

    def publish_backup(self, workspace: BackupWorkspace) -> BackupArtifactSnapshot:
        self._call("publish")
        snapshot = BackupArtifactSnapshot(
            path=workspace.final_path,
            size_bytes=1024,
            created_at=datetime(2026, 7, 25, tzinfo=UTC),
            verification_status=BackupVerificationStatus.VERIFIED,
            verified_at=datetime(2026, 7, 25, tzinfo=UTC),
        )
        self.snapshots[workspace.operation_id] = snapshot
        return snapshot

    def list_backups(self) -> tuple[BackupArtifactSnapshot, ...]:
        self._call("list")
        return tuple(self.snapshots.values())

    def resolve_backup(self, raw_path: str) -> Path:
        self._call(f"resolve:{raw_path}")
        return next(iter(self.snapshots.values())).path

    def prepare_validation(self, source: Path) -> BackupWorkspace:
        del source
        self._call("prepare_validation")
        return self.workspace

    def record_verification(self, source: Path) -> BackupArtifactSnapshot:
        del source
        self._call("record_verification")
        return next(iter(self.snapshots.values()))

    def remove_file(self, path: Path) -> None:
        self._call(f"remove:{path.name}")

    def remove_plaintext(self, path: Path) -> None:
        self._call(f"remove:{path.name}")

    def remove_incomplete_output(self, workspace: BackupWorkspace) -> None:
        del workspace
        self._call("remove_incomplete")


class _SimulatedCrash(BaseException):
    pass


class CrashAfterPublishState(LifecycleStateStore):
    def __init__(self, project_root: Path) -> None:
        super().__init__(project_root)
        self.crashed = False

    def save_journal(self, journal: LifecycleOperationJournal) -> None:
        if (
            not self.crashed
            and journal.status is LifecycleOperationStatus.RUNNING
            and "create_verified_backup" in journal.completed_steps
        ):
            self.crashed = True
            raise _SimulatedCrash
        super().save_journal(journal)


def _service(
    gateway: RecordingBackupGateway,
    state: LifecycleStateStore,
) -> BackupCommandService:
    return BackupCommandService(
        backup=BackupService(gateway=gateway),
        lifecycle_state=state,
    )


def test_public_create_conflicts_with_concurrent_mutation_without_calling_backup(
    tmp_path: Path,
) -> None:
    gateway = RecordingBackupGateway(tmp_path)
    state = LifecycleStateStore(tmp_path)

    _raises_callable_181_1 = _service(gateway, state).create
    _raises_input_181_2 = BackupCreateRequest()
    with (
        state.operation_lock("other-mutation", mutating=True),
        pytest.raises(LifecycleError) as raised,
    ):
        _raises_callable_181_1(_raises_input_181_2)

    assert raised.value.exit_code is ExitCode.CONFLICT
    assert raised.value.code == "lifecycle_operation_active"
    assert not any(call.startswith("prepare_create:") for call in gateway.calls)


def test_public_create_blocks_on_recovery_required_unfinished_journal(
    tmp_path: Path,
) -> None:
    gateway = RecordingBackupGateway(tmp_path)
    state = LifecycleStateStore(tmp_path)
    state.save_journal(
        LifecycleOperationJournal(
            operation_id="unfinished-environment",
            kind=LifecycleOperationKind.ENVIRONMENT_SWITCH,
            status=LifecycleOperationStatus.RECOVERY_REQUIRED,
            recovery_command="flow-steward environment switch production",
        )
    )

    _raises_callable_206_1 = _service(gateway, state).create
    _raises_input_206_2 = BackupCreateRequest()
    with pytest.raises(LifecycleError) as raised:
        _raises_callable_206_1(_raises_input_206_2)

    assert raised.value.exit_code is ExitCode.RECOVERY_REQUIRED
    assert raised.value.operation_id == "unfinished-environment"
    assert not any(call.startswith("prepare_create:") for call in gateway.calls)


def test_public_create_first_cancel_stops_before_next_effect_and_records_cancelled(
    tmp_path: Path,
) -> None:
    gateway = RecordingBackupGateway(tmp_path)
    state = LifecycleStateStore(tmp_path)
    cancellation = CancellationToken()
    gateway.on_call["dump"] = cancellation.request
    events: list[OperationProgressEvent] = []

    _raises_callable_223_1 = _service(gateway, state).create
    _raises_input_223_2 = BackupCreateRequest(operation_id="cancelled-backup")
    with pytest.raises(LifecycleError) as raised:
        _raises_callable_223_1(
            _raises_input_223_2, cancellation=cancellation, progress_sink=events.append
        )

    journal = state.load_journal("cancelled-backup")
    assert raised.value.code == "lifecycle_cancelled"
    assert raised.value.step == "create_verified_backup"
    assert raised.value.details["internal_step"] == "dump"
    assert journal is not None
    assert journal.status is LifecycleOperationStatus.CANCELLED
    assert journal.failed_step == "create_verified_backup"
    assert tuple(step.status for step in journal.steps) == (OperationStepStatus.CANCELLED,)
    cancelled = [
        event
        for event in events
        if event.status is OperationStepStatus.CANCELLED
        and event.step_id == "create_verified_backup"
    ]
    assert len(cancelled) == 1
    assert all(step.status is not OperationStepStatus.RUNNING for step in journal.steps)
    assert "encrypt" not in gateway.calls
    assert "publish" not in gateway.calls
    assert "remove_incomplete" in gateway.calls


def test_public_create_forced_interrupt_marks_shared_journal_recovery_required(
    tmp_path: Path,
) -> None:
    gateway = RecordingBackupGateway(tmp_path)
    state = LifecycleStateStore(tmp_path)
    cancellation = CancellationToken()
    gateway.on_call["dump"] = cancellation.force

    _raises_callable_259_1 = _service(gateway, state).create
    _raises_input_259_2 = BackupCreateRequest(operation_id="forced-backup")
    with pytest.raises(LifecycleError) as raised:
        _raises_callable_259_1(_raises_input_259_2, cancellation=cancellation)

    journal = state.load_journal("forced-backup")
    assert raised.value.code == "lifecycle_forced_interrupt"
    assert journal is not None
    assert journal.status is LifecycleOperationStatus.RECOVERY_REQUIRED
    assert journal.last_error_code == "lifecycle_forced_interrupt"
    assert "encrypt" not in gateway.calls


def test_public_create_resumes_after_publish_crash_without_duplicate_artifact(
    tmp_path: Path,
) -> None:
    gateway = RecordingBackupGateway(tmp_path)
    crashing_state = CrashAfterPublishState(tmp_path)

    _raises_callable_279_1 = _service(gateway, crashing_state).create
    _raises_input_279_2 = BackupCreateRequest(operation_id="published-backup")
    with pytest.raises(_SimulatedCrash):
        _raises_callable_279_1(_raises_input_279_2)

    unfinished = LifecycleStateStore(tmp_path).unfinished_journals()
    assert tuple(journal.operation_id for journal in unfinished) == ("published-backup",)

    result = _service(gateway, LifecycleStateStore(tmp_path)).create(BackupCreateRequest())
    journal = LifecycleStateStore(tmp_path).load_journal("published-backup")

    assert result.verification_status is BackupVerificationStatus.VERIFIED
    assert gateway.calls.count("dump") == 1
    assert gateway.calls.count("publish") == 1
    assert len(gateway.snapshots) == 1
    assert journal is not None
    assert journal.status is LifecycleOperationStatus.COMPLETED
    assert journal.skipped_steps == ("create_verified_backup",)


def test_public_list_and_verify_remain_available_while_mutation_lock_is_held(
    tmp_path: Path,
) -> None:
    gateway = RecordingBackupGateway(tmp_path)
    core = BackupService(gateway=gateway)
    created = core.create(BackupCreateRequest(operation_id="seed-backup"))
    state = LifecycleStateStore(tmp_path)
    service = _service(gateway, state)

    with state.operation_lock("other-mutation", mutating=True):
        listed = service.list(BackupListRequest())
        verified = service.verify(BackupVerifyRequest(backup=created.path))

    assert tuple(item.path for item in listed.backups) == (created.path,)
    assert verified.verification_status is BackupVerificationStatus.VERIFIED
    assert "list" in gateway.calls
    assert "record_verification" in gateway.calls
