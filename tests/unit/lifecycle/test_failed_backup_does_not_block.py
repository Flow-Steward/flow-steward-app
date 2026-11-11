"""A failed `backup create` is reported, and blocks nothing (2026-11-11 demo upgrade).

The demo's backup failed at pg_dump. Its journal stayed "failed, retryable", so it read as
an unfinished mutation: `maintenance disable` refused with
maintenance_unfinished_operations, `doctor --fix maintenance_left_enabled` exited 8, and
doctor's only advice was to rerun the backup that kept failing. A backup writes a new file
and changes nothing in the installation; only real unfinished mutations block.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.application.lifecycle.contracts import (
    LifecycleOperationDefinition,
    LifecycleOperationJournal,
    LifecycleOperationKind,
    LifecycleOperationStatus,
    LifecycleOperationStep,
    journal_blocks_new_work,
)
from core.application.lifecycle.errors import LifecycleError
from core.application.lifecycle.operation_service import LifecycleOperationRunner
from core.application.maintenance.contracts import MaintenanceState
from core.application.maintenance.service import MaintenanceService
from core.application.management.command_specs.registry import complete_operator_command
from core.entrypoints.cli.maintenance_adapters import _MaintenanceUnfinishedOperationsAdapter
from core.infrastructure.installer.readiness_probe import StackReadinessProbe
from core.infrastructure.lifecycle.maintenance_gateway import FilesystemMaintenanceGateway
from core.infrastructure.lifecycle.state_store import LifecycleStateStore

_STEP = LifecycleOperationStep(
    step_id="create_verified_backup",
    label="Create and verify protected PostgreSQL backup",
)


class _PgDumpFails:
    """The demo's failure: pg_dump exits non-zero, a retryable dependency failure."""

    def postcondition_holds(self, _definition, _step) -> bool:
        return False

    def execute_step(self, _definition, step, **_sinks) -> None:
        raise LifecycleError.dependency(
            code="backup_dump_failed",
            cause="pg_dump exited with status 1.",
            remediation="Inspect the backup log.",
            step=step.step_id,
        )


def _failed_backup(store: LifecycleStateStore, operation_id: str = "backup-failed") -> None:
    with pytest.raises(LifecycleError):
        LifecycleOperationRunner(gateway=_PgDumpFails(), state=store).execute(
            LifecycleOperationDefinition(
                operation_id=operation_id,
                kind=LifecycleOperationKind.BACKUP_CREATE,
                steps=(_STEP,),
                recovery_command="flow-steward backup create",
            )
        )


def _journal(
    operation_id: str,
    kind: LifecycleOperationKind,
    status: LifecycleOperationStatus,
    *,
    retryable: bool = True,
    age: timedelta = timedelta(0),
) -> LifecycleOperationJournal:
    moment = datetime.now(UTC) - age
    return LifecycleOperationJournal(
        operation_id=operation_id,
        kind=kind,
        status=status,
        retryable=retryable,
        failed_step="create_verified_backup",
        recovery_command="flow-steward backup create",
        started_at=moment,
        updated_at=moment,
    )


def _maintenance(tmp_path: Path, store: LifecycleStateStore) -> MaintenanceService:
    class _Idle:
        def wait_for_idle(self) -> None:
            pass

    class _Healthy:
        def mandatory_health_issues(self) -> tuple[str, ...]:
            return ()

    class _Reconciled:
        def reconcile(self) -> None:
            pass

    return MaintenanceService(
        state=FilesystemMaintenanceGateway(
            project_root=tmp_path,
            state_model=MaintenanceState,
            lifecycle_error=LifecycleError,
        ),
        active_work=_Idle(),
        unfinished_operations=_MaintenanceUnfinishedOperationsAdapter(store),
        health=_Healthy(),
        readiness_reconciler=_Reconciled(),
    )


def test_a_failed_backup_is_recorded_but_is_not_an_unfinished_operation(tmp_path: Path) -> None:
    store = LifecycleStateStore(tmp_path)

    _failed_backup(store)

    journal = store.load_journal("backup-failed")
    assert journal is not None
    assert journal.status is LifecycleOperationStatus.FAILED
    assert journal.retryable is True  # what made it read as unfinished before
    assert store.unfinished_journals() == ()
    assert [item.operation_id for item in store.failed_artifact_operations()] == ["backup-failed"]


def test_maintenance_disable_succeeds_after_a_failed_backup(tmp_path: Path) -> None:
    store = LifecycleStateStore(tmp_path)
    service = _maintenance(tmp_path, store)
    service.enable()
    _failed_backup(store)

    result = service.disable()

    assert result.status == "disabled"


@pytest.mark.parametrize(
    "kind",
    [
        LifecycleOperationKind.CONFIG_APPLY,
        LifecycleOperationKind.RELEASE_UPGRADE,
        LifecycleOperationKind.CLONE_RESTORE,
        LifecycleOperationKind.STACK_STOP,
    ],
)
def test_an_unfinished_mutation_still_blocks_maintenance_disable(
    tmp_path: Path, kind: LifecycleOperationKind
) -> None:
    """The green direction: only the artifact-only kind is exempt."""
    store = LifecycleStateStore(tmp_path)
    service = _maintenance(tmp_path, store)
    service.enable()
    store.save_journal(_journal("mutation-1", kind, LifecycleOperationStatus.FAILED))

    with pytest.raises(LifecycleError) as raised:
        service.disable()

    assert raised.value.code == "maintenance_unfinished_operations"


def test_a_backup_that_may_still_run_stays_unfinished(tmp_path: Path) -> None:
    """A journal left "running" is resumed by the next `backup create`, as before."""
    store = LifecycleStateStore(tmp_path)
    store.save_journal(
        _journal(
            "backup-running", LifecycleOperationKind.BACKUP_CREATE, LifecycleOperationStatus.RUNNING
        )
    )

    assert [item.operation_id for item in store.unfinished_journals()] == ["backup-running"]
    assert store.failed_artifact_operations() == ()


@pytest.mark.parametrize(
    ("kind", "status", "retryable", "blocks"),
    [
        ("backup_create", "failed", True, False),
        ("backup_create", "cancelled", True, False),
        ("backup_create", "recovery_required", False, False),
        ("backup_create", "running", True, True),
        ("config_apply", "failed", True, True),
        ("config_apply", "failed", False, False),
        ("release_upgrade", "recovery_required", False, True),
        ("clone_restore", "cancelled", True, True),
    ],
)
def test_one_rule_for_journals_read_as_json(
    kind: str, status: str, retryable: bool, blocks: bool
) -> None:
    """Doctor reads Compact journals as plain JSON; it must apply the same rule."""
    assert journal_blocks_new_work(kind=kind, status=status, retryable=retryable) is blocks


def _probe(tmp_path: Path, store: LifecycleStateStore) -> StackReadinessProbe:
    class _Installed(StackReadinessProbe):
        def check_config(self, config, **_kwargs):
            del config
            return {"checks": []}

    class _Active:
        def read_active(self, *, required: bool = True):
            del required
            return object()

    def _doctor(_options):
        raise AssertionError("not called")

    return _Installed(
        configuration_service=_Active(),
        doctor=_doctor,
        doctor_options_factory=object,
        repo_root=tmp_path,
        installation_journal=lambda: None,
        lifecycle_state=store,
        active_lifecycle_operation=lambda: None,
        complete_command=complete_operator_command,
    )


def test_status_reports_the_failed_backup_as_a_warning_with_the_next_step(
    tmp_path: Path,
) -> None:
    store = LifecycleStateStore(tmp_path)
    _failed_backup(store)

    checks = _probe(tmp_path, store)()["checks"]

    assert [row["status"] for row in checks] == ["warn"]
    assert (
        "backup create backup-failed failed at step create_verified_backup" in (checks[0]["detail"])
    )
    assert "backup_dump_failed" in checks[0]["detail"]
    assert "./flow-steward backup create" in checks[0]["remediation"]
    assert "Continue it" not in checks[0]["remediation"]


def test_a_later_successful_backup_clears_the_report(tmp_path: Path) -> None:
    store = LifecycleStateStore(tmp_path)
    store.save_journal(
        _journal(
            "backup-old",
            LifecycleOperationKind.BACKUP_CREATE,
            LifecycleOperationStatus.FAILED,
            age=timedelta(hours=1),
        )
    )
    store.save_journal(
        _journal(
            "backup-new", LifecycleOperationKind.BACKUP_CREATE, LifecycleOperationStatus.COMPLETED
        )
    )

    assert store.failed_artifact_operations() == ()
    assert _probe(tmp_path, store)()["checks"] == []
