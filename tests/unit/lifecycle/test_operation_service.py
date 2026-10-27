from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from core.application.lifecycle.contracts import (
    CancellationToken,
    LifecycleLockStatus,
    LifecycleOperationDefinition,
    LifecycleOperationJournal,
    LifecycleOperationKind,
    LifecycleOperationStatus,
    LifecycleOperationStep,
)
from core.application.lifecycle.errors import ForcedLifecycleInterrupt, LifecycleError
from core.application.lifecycle.operation_service import LifecycleOperationRunner
from core.application.management.command_specs.contracts import (
    ExitCode,
    OperationStep,
    OperationStepStatus,
)
from core.infrastructure.lifecycle.state_store import LifecycleStateStore


class FakeLifecycleGateway:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.held: set[str] = set()
        self.failure: BaseException | None = None
        self.fail_step: str | None = None
        self.hold_after_execute: set[str] = set()
        self.quiet_seconds: float = 0.0
        self.request_cancellation_at: str | None = None
        self.outputs: dict[str, dict[str, object]] = {}

    def postcondition_holds(
        self,
        _definition: LifecycleOperationDefinition,
        step: LifecycleOperationStep,
    ) -> bool:
        self.calls.append(f"postcondition:{step.step_id}")
        return step.step_id in self.held

    def execute_step(
        self,
        _definition: LifecycleOperationDefinition,
        step: LifecycleOperationStep,
        *,
        output_sink,
        heartbeat,
        cancellation: CancellationToken,
    ) -> None:
        self.calls.append(f"execute:{step.step_id}")
        output_sink(f"{step.step_id}: POSTGRES_PASSWORD='must-not-appear'")
        if self.quiet_seconds:
            time.sleep(self.quiet_seconds)
        else:
            heartbeat(f"{step.label} is still running")
        if step.step_id in self.hold_after_execute:
            self.held.add(step.step_id)
        if self.request_cancellation_at == step.step_id:
            cancellation.request()
        if self.fail_step == step.step_id and self.failure is not None:
            raise self.failure

    def step_output(
        self,
        _definition: LifecycleOperationDefinition,
        step: LifecycleOperationStep,
    ) -> dict[str, object]:
        return self.outputs.get(step.step_id, {})


class InterruptAfterJournalStateStore(LifecycleStateStore):
    def __init__(self, project_root: Path, cancellation: CancellationToken) -> None:
        super().__init__(project_root)
        self.cancellation = cancellation
        self.interrupted = False

    def save_journal(self, journal: LifecycleOperationJournal) -> None:
        super().save_journal(journal)
        if not self.interrupted and journal.status is LifecycleOperationStatus.RUNNING:
            self.interrupted = True
            self.cancellation.force()
            raise ForcedLifecycleInterrupt


class InterruptBeforeJournalStateStore(LifecycleStateStore):
    def __init__(self, project_root: Path, cancellation: CancellationToken) -> None:
        super().__init__(project_root)
        self.cancellation = cancellation
        self.interrupted = False

    def save_journal(self, journal: LifecycleOperationJournal) -> None:
        if not self.interrupted and journal.status is LifecycleOperationStatus.RUNNING:
            self.interrupted = True
            self.cancellation.force()
            raise ForcedLifecycleInterrupt
        super().save_journal(journal)


class TrackingRecoveryStateStore(LifecycleStateStore):
    def __init__(self, project_root: Path) -> None:
        super().__init__(project_root)
        self.recovery_marks = 0

    def mark_recovery_required(self, operation_id: str, *, step: str, error_code: str) -> None:
        self.recovery_marks += 1
        super().mark_recovery_required(operation_id, step=step, error_code=error_code)


class StaleRecoveryReadStateStore(LifecycleStateStore):
    def __init__(self, project_root: Path) -> None:
        super().__init__(project_root)
        self.recovery_read = threading.Event()
        self.release_recovery_read = threading.Event()
        self.completion_started = threading.Event()
        self.completion_finished = threading.Event()

    def load_journal(self, operation_id: str) -> LifecycleOperationJournal | None:
        journal = super().load_journal(operation_id)
        if threading.current_thread().name == "recovery-marker":
            self.recovery_read.set()
            assert self.release_recovery_read.wait(timeout=5)
        return journal

    def save_journal(self, journal: LifecycleOperationJournal) -> None:
        if journal.status is LifecycleOperationStatus.COMPLETED:
            self.completion_started.set()
        super().save_journal(journal)
        if journal.status is LifecycleOperationStatus.COMPLETED:
            self.completion_finished.set()


def _definition(
    *,
    operation_id: str = "operation-1",
    mutating: bool = True,
    previous_config: dict[str, Any] | None = None,
) -> LifecycleOperationDefinition:
    return LifecycleOperationDefinition(
        operation_id=operation_id,
        kind=LifecycleOperationKind.STACK_RESTART,
        mutating=mutating,
        steps=(
            LifecycleOperationStep(step_id="prepare", label="Prepare services"),
            LifecycleOperationStep(step_id="restart", label="Restart services"),
        ),
        previous_config=previous_config or {},
        recovery_command="flow-steward stack restart",
    )


def _journal(*, operation_id: str = "operation-1") -> LifecycleOperationJournal:
    return LifecycleOperationJournal(
        operation_id=operation_id,
        kind=LifecycleOperationKind.STACK_START,
        status=LifecycleOperationStatus.RUNNING,
        mutating=True,
        steps=(
            OperationStep(
                step_id="start",
                status=OperationStepStatus.PENDING,
                label="Start services",
            ),
        ),
        recovery_command="flow-steward stack status",
    )


def test_operation_artifacts_are_owner_only_and_use_distinct_lifecycle_paths(
    tmp_path: Path,
) -> None:
    store = LifecycleStateStore(tmp_path)
    snapshot_path = store.save_previous_config(
        "operation-1", {"public_host": "flow.example.test", "public_port": 8099}
    )
    log = store.open_log("operation-1")
    log.write("POSTGRES_PASSWORD='must-not-appear'")  # pragma: allowlist secret
    log.close()
    journal = _journal().model_copy(
        update={
            "previous_config_path": str(snapshot_path),
            "log_path": str(log.path),
        }
    )

    store.save_journal(journal)

    assert store.operations_dir == tmp_path / ".flow-steward/operations"
    assert store.journal_path("operation-1") == store.operations_dir / "operation-1.json"
    assert store.lock_path == store.operations_dir / "active.lock"
    assert "install-state.json" not in str(store.journal_path("operation-1"))
    assert store.operations_dir.stat().st_mode & 0o777 == 0o700
    assert store.logs_dir.stat().st_mode & 0o777 == 0o700
    assert store.journal_path("operation-1").stat().st_mode & 0o777 == 0o600
    assert snapshot_path.stat().st_mode & 0o777 == 0o600
    assert log.path.stat().st_mode & 0o777 == 0o600
    assert "must-not-appear" not in log.path.read_text(encoding="utf-8")
    assert "[REDACTED]" in log.path.read_text(encoding="utf-8")


def test_runner_persists_secret_free_durable_step_outputs(tmp_path: Path) -> None:
    gateway = FakeLifecycleGateway()
    gateway.outputs = {
        "prepare": {
            "path": "backups/environment-operation-1.dump.enc",
            "sha256": "a" * 64,
        }
    }
    store = LifecycleStateStore(tmp_path)

    LifecycleOperationRunner(gateway=gateway, state=store).execute(_definition())

    journal = store.load_journal("operation-1")
    assert journal is not None
    assert journal.step_outputs == gateway.outputs
    assert "PASSWORD" not in store.journal_path("operation-1").read_text(encoding="utf-8")


def test_opening_log_first_secures_operation_and_log_directories(tmp_path: Path) -> None:
    store = LifecycleStateStore(tmp_path)

    log = store.open_log("operation-1")
    log.close()

    assert store.operations_dir.stat().st_mode & 0o777 == 0o700
    assert store.logs_dir.stat().st_mode & 0o777 == 0o700


def test_journal_and_previous_config_are_replaced_atomically_and_secret_free(
    tmp_path: Path,
) -> None:
    store = LifecycleStateStore(tmp_path)
    journal = _journal()
    store.save_journal(journal)
    store.save_journal(
        journal.model_copy(
            update={
                "status": LifecycleOperationStatus.COMPLETED,
                "completed_steps": ("start",),
            }
        )
    )
    snapshot_path = store.save_previous_config("operation-1", {"public_port": 8099})
    store.save_previous_config("operation-1", {"public_port": 8100})

    assert store.load_journal("operation-1").status is LifecycleOperationStatus.COMPLETED
    assert json.loads(snapshot_path.read_text(encoding="utf-8")) == {"public_port": 8100}
    assert list(store.operations_dir.rglob("*.tmp")) == []
    unsafe_snapshot = {
        "FS_SECRET_KEY": "must-not-persist"  # pragma: allowlist secret
    }
    with pytest.raises(ValueError, match="secret-shaped field"):
        store.save_previous_config("unsafe", unsafe_snapshot)
    assert not store.journal_path("unsafe").exists()


def test_unfinished_journals_ignore_release_prepared_marker(tmp_path: Path) -> None:
    store = LifecycleStateStore(tmp_path)
    journal = _journal()
    store.save_journal(journal)
    (store.operations_dir / "release-prepared-project-1.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "project_name": "project-1",
                "environment": "development",
                "release": {},
                "host_port_overrides": {},
            }
        ),
        encoding="utf-8",
    )

    assert store.unfinished_journals() == (journal,)


def test_unfinished_journals_fail_closed_for_malformed_lifecycle_journal(
    tmp_path: Path,
) -> None:
    store = LifecycleStateStore(tmp_path)
    store.operations_dir.mkdir(parents=True)
    store.journal_path("operation-1").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match=r"operation-1\.json.*unreadable"):
        store.unfinished_journals()


def test_unfinished_journals_fail_closed_for_malformed_prepared_prefixed_journal(
    tmp_path: Path,
) -> None:
    store = LifecycleStateStore(tmp_path)
    store.operations_dir.mkdir(parents=True)
    store.journal_path("release-prepared-operation-1").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match=r"release-prepared-operation-1\.json.*unreadable"):
        store.unfinished_journals()


def test_active_mutation_lock_is_exclusive_and_reports_owner(tmp_path: Path) -> None:
    first = LifecycleStateStore(tmp_path)
    second = LifecycleStateStore(tmp_path)

    with first.operation_lock("operation-1", mutating=True):
        diagnosis = second.diagnose_active_lock()
        with (
            pytest.raises(LifecycleError) as raised,
            second.operation_lock("operation-2", mutating=True),
        ):
            raise AssertionError("a second mutation must never enter the lock")

    assert diagnosis.status is LifecycleLockStatus.ACTIVE
    assert diagnosis.operation_id == "operation-1"
    assert diagnosis.pid is not None
    assert raised.value.code == "lifecycle_operation_active"
    assert raised.value.exit_code is ExitCode.CONFLICT
    assert "operation-1" in raised.value.cause


def test_active_mutation_exposes_verified_held_lock_descriptor(tmp_path: Path) -> None:
    store = LifecycleStateStore(tmp_path)

    with store.operation_lock("operation-1", mutating=True):
        held = store.held_lock_descriptors()

        assert set(held) == {str(store.lock_path.resolve())}
        descriptor = next(iter(held.values()))
        assert os.fstat(descriptor).st_ino == store.lock_path.stat().st_ino

    assert store.held_lock_descriptors() == {}


def test_unlocked_lock_metadata_is_diagnosed_as_stale_and_does_not_block(
    tmp_path: Path,
) -> None:
    store = LifecycleStateStore(tmp_path)
    store.operations_dir.mkdir(parents=True, mode=0o700)
    store.lock_path.write_text(
        json.dumps({"operation_id": "abandoned", "pid": 999_999, "acquired_at": "old"}),
        encoding="utf-8",
    )

    diagnosis = store.diagnose_active_lock()
    entered = False
    with store.operation_lock("replacement", mutating=True):
        entered = True

    assert diagnosis.status is LifecycleLockStatus.STALE
    assert diagnosis.operation_id == "abandoned"
    assert diagnosis.pid == 999_999
    assert "stale" in diagnosis.detail.lower()
    assert entered is True


def test_read_only_operation_remains_available_during_mutation_lock(tmp_path: Path) -> None:
    store = LifecycleStateStore(tmp_path)

    with (
        store.operation_lock("mutation", mutating=True),
        store.operation_lock("reader", mutating=False),
    ):
        diagnosis = store.diagnose_active_lock()

    assert diagnosis.status is LifecycleLockStatus.ACTIVE
    assert diagnosis.operation_id == "mutation"


def test_lifecycle_contracts_are_closed_and_json_safe() -> None:
    definition = _definition()
    journal = _journal()

    assert json.loads(journal.model_dump_json())["kind"] == "stack_start"
    assert json.dumps(definition.model_dump(mode="json"))
    with pytest.raises(ValidationError, match="extra_forbidden"):
        LifecycleOperationStep(step_id="prepare", label="Prepare", surprise=True)


def test_runner_emits_step_progress_heartbeats_and_redacted_full_output(
    tmp_path: Path,
) -> None:
    gateway = FakeLifecycleGateway()
    gateway.quiet_seconds = 0.04
    store = LifecycleStateStore(tmp_path)
    events = []
    technical: list[str] = []
    runner = LifecycleOperationRunner(
        gateway=gateway,
        state=store,
        heartbeat_interval_seconds=0.01,
    )

    result = runner.execute(
        _definition(previous_config={"public_host": "before.example.test"}),
        progress_sink=events.append,
        technical_sink=technical.append,
    )

    assert result.status is LifecycleOperationStatus.COMPLETED
    assert result.completed_steps == ("prepare", "restart")
    assert {event.step_id for event in events} == {"prepare", "restart"}
    assert all(
        any(event.step_id == step_id and event.status is status for event in events)
        for step_id in ("prepare", "restart")
        for status in (OperationStepStatus.RUNNING, OperationStepStatus.SUCCEEDED)
    )
    heartbeats = [event for event in events if event.heartbeat]
    assert heartbeats
    assert all(
        event.elapsed_seconds is not None and event.elapsed_seconds > 0 for event in heartbeats
    )
    log_text = Path(result.log_path).read_text(encoding="utf-8")
    assert "prepare:" in log_text
    assert "restart:" in log_text
    assert "must-not-appear" not in log_text
    assert "[REDACTED]" in log_text
    assert "must-not-appear" not in "\n".join(technical)
    assert json.loads(Path(result.previous_config_path).read_text(encoding="utf-8")) == {
        "public_host": "before.example.test"
    }


def test_resume_skips_completed_step_only_when_its_postcondition_still_holds(
    tmp_path: Path,
) -> None:
    gateway = FakeLifecycleGateway()
    gateway.fail_step = "restart"
    gateway.failure = LifecycleError.stable_failure(
        code="restart_failed",
        cause="restart failed",
        remediation="Inspect services and retry.",
        step="restart",
    )
    store = LifecycleStateStore(tmp_path)
    runner = LifecycleOperationRunner(gateway=gateway, state=store)
    definition = _definition()

    with pytest.raises(LifecycleError) as first_failure:
        runner.execute(definition)
    gateway.fail_step = None
    gateway.held = {"prepare"}
    result = runner.execute(definition)

    assert first_failure.value.exit_code is ExitCode.STABLE_FAILURE
    assert gateway.calls.count("execute:prepare") == 1
    assert gateway.calls.count("postcondition:prepare") == 1
    assert result.skipped_steps == ("prepare",)
    assert result.status is LifecycleOperationStatus.COMPLETED


def test_resume_reexecutes_completed_step_when_postcondition_no_longer_holds(
    tmp_path: Path,
) -> None:
    gateway = FakeLifecycleGateway()
    gateway.fail_step = "restart"
    gateway.failure = LifecycleError.stable_failure(
        code="restart_failed",
        cause="restart failed",
        remediation="Inspect services and retry.",
        step="restart",
    )
    runner = LifecycleOperationRunner(gateway=gateway, state=LifecycleStateStore(tmp_path))
    definition = _definition()

    with pytest.raises(LifecycleError):
        runner.execute(definition)
    gateway.fail_step = None
    runner.execute(definition)

    assert gateway.calls.count("execute:prepare") == 2
    assert gateway.calls.count("postcondition:prepare") == 1


def test_resume_skips_unjournaled_side_effect_when_postcondition_holds(
    tmp_path: Path,
) -> None:
    gateway = FakeLifecycleGateway()
    gateway.fail_step = "prepare"
    gateway.failure = SystemExit("simulated process crash")
    gateway.hold_after_execute = {"prepare"}
    store = LifecycleStateStore(tmp_path)
    runner = LifecycleOperationRunner(gateway=gateway, state=store)
    definition = _definition()

    with pytest.raises(SystemExit, match="simulated process crash"):
        runner.execute(definition)
    crashed = store.load_journal("operation-1")
    assert crashed.completed_steps == ()
    assert crashed.steps[0].status is OperationStepStatus.RUNNING

    gateway.fail_step = None
    result = runner.execute(definition)

    assert gateway.calls.count("execute:prepare") == 1
    assert result.completed_steps == ("prepare", "restart")
    assert result.skipped_steps == ("prepare",)


def test_cancellation_requested_at_skipped_safe_boundary_stops_before_next_step(
    tmp_path: Path,
) -> None:
    gateway = FakeLifecycleGateway()
    gateway.fail_step = "restart"
    gateway.failure = LifecycleError.stable_failure(
        code="restart_failed",
        cause="restart failed",
        remediation="Inspect services and retry.",
        step="restart",
    )
    store = LifecycleStateStore(tmp_path)
    runner = LifecycleOperationRunner(gateway=gateway, state=store)
    definition = _definition()

    with pytest.raises(LifecycleError):
        runner.execute(definition)
    gateway.fail_step = None
    gateway.held = {"prepare"}
    cancellation = CancellationToken()

    def request_after_skip(event) -> None:
        if event.status is OperationStepStatus.SKIPPED:
            cancellation.request()

    with pytest.raises(LifecycleError) as raised:
        runner.execute(
            definition,
            progress_sink=request_after_skip,
            cancellation=cancellation,
        )

    journal = store.load_journal("operation-1")
    assert raised.value.exit_code is ExitCode.INTERRUPTED
    assert gateway.calls.count("execute:restart") == 1
    assert tuple(step.status for step in journal.steps) == (
        OperationStepStatus.SKIPPED,
        OperationStepStatus.CANCELLED,
    )


def test_early_forced_interrupt_maps_to_interrupt_and_marks_existing_journal(
    tmp_path: Path,
) -> None:
    cancellation = CancellationToken()
    store = InterruptAfterJournalStateStore(tmp_path, cancellation)
    runner = LifecycleOperationRunner(gateway=FakeLifecycleGateway(), state=store)

    _raises_input_540_1 = _definition()
    with pytest.raises(LifecycleError) as raised:
        runner.execute(_raises_input_540_1, cancellation=cancellation)

    journal = store.load_journal("operation-1")
    assert raised.value.exit_code is ExitCode.INTERRUPTED
    assert raised.value.recovery_command == "flow-steward stack restart"
    assert journal.status is LifecycleOperationStatus.RECOVERY_REQUIRED
    assert journal.last_error_code == "lifecycle_forced_interrupt"


def test_forced_interrupt_before_first_save_persists_recovery_journal(
    tmp_path: Path,
) -> None:
    cancellation = CancellationToken()
    store = InterruptBeforeJournalStateStore(tmp_path, cancellation)
    runner = LifecycleOperationRunner(gateway=FakeLifecycleGateway(), state=store)

    _raises_input_557_1 = _definition()
    with pytest.raises(LifecycleError) as raised:
        runner.execute(_raises_input_557_1, cancellation=cancellation)

    journal = store.load_journal("operation-1")
    assert raised.value.code == "lifecycle_forced_interrupt"
    assert raised.value.exit_code is ExitCode.INTERRUPTED
    assert raised.value.recovery_command == "flow-steward stack restart"
    assert journal is not None
    assert journal.status is LifecycleOperationStatus.RECOVERY_REQUIRED
    assert journal.recovery_command == "flow-steward stack restart"
    assert journal.last_error_code == "lifecycle_forced_interrupt"


def test_gateway_wrapped_forced_interrupt_keeps_recovery_barrier(tmp_path: Path) -> None:
    gateway = FakeLifecycleGateway()
    gateway.fail_step = "restart"
    gateway.failure = LifecycleError.interrupted(
        step="restart",
        recovery_command="flow-steward stack restart",
        forced=True,
    )
    store = LifecycleStateStore(tmp_path)

    _raises_callable_580_1 = LifecycleOperationRunner(gateway=gateway, state=store).execute
    _raises_input_580_2 = _definition()
    with pytest.raises(LifecycleError) as raised:
        _raises_callable_580_1(_raises_input_580_2)

    journal = store.load_journal("operation-1")
    assert raised.value.code == "lifecycle_forced_interrupt"
    assert journal is not None
    assert journal.status is LifecycleOperationStatus.RECOVERY_REQUIRED
    assert journal.last_error_code == "lifecycle_forced_interrupt"
    assert store.unfinished_journals() == (journal,)


def test_lock_conflict_does_not_create_journal_for_rejected_operation(
    tmp_path: Path,
) -> None:
    lock_owner = LifecycleStateStore(tmp_path)
    contender = LifecycleStateStore(tmp_path)
    runner = LifecycleOperationRunner(gateway=FakeLifecycleGateway(), state=contender)

    _raises_input_598_1 = _definition(operation_id="operation-2")
    with (
        lock_owner.operation_lock("operation-1", mutating=True),
        pytest.raises(LifecycleError) as raised,
    ):
        runner.execute(_raises_input_598_1)

    assert raised.value.exit_code is ExitCode.CONFLICT
    assert contender.load_journal("operation-2") is None


def test_force_callback_is_removed_when_operation_completes(tmp_path: Path) -> None:
    cancellation = CancellationToken()
    store = TrackingRecoveryStateStore(tmp_path)
    runner = LifecycleOperationRunner(gateway=FakeLifecycleGateway(), state=store)

    runner.execute(_definition(), cancellation=cancellation)
    cancellation.force()

    assert store.recovery_marks == 0
    assert store.load_journal("operation-1").status is LifecycleOperationStatus.COMPLETED


def test_completed_journal_ignores_late_recovery_mark(tmp_path: Path) -> None:
    store = LifecycleStateStore(tmp_path)
    runner = LifecycleOperationRunner(gateway=FakeLifecycleGateway(), state=store)
    runner.execute(_definition())

    store.mark_recovery_required(
        "operation-1",
        step="restart",
        error_code="lifecycle_forced_interrupt",
    )

    journal = store.load_journal("operation-1")
    assert journal.status is LifecycleOperationStatus.COMPLETED
    assert journal.last_error_code is None


def test_concurrent_stale_recovery_mark_cannot_overwrite_completed_journal(
    tmp_path: Path,
) -> None:
    store = StaleRecoveryReadStateStore(tmp_path)
    running = _journal()
    completed = running.model_copy(
        update={
            "status": LifecycleOperationStatus.COMPLETED,
            "completed_steps": ("start",),
        }
    )
    store.save_journal(running)

    recovery_thread = threading.Thread(
        name="recovery-marker",
        target=lambda: store.mark_recovery_required(
            "operation-1",
            step="start",
            error_code="lifecycle_forced_interrupt",
        ),
    )
    completion_thread = threading.Thread(
        name="completion-writer",
        target=lambda: store.save_journal(completed),
    )

    recovery_thread.start()
    assert store.recovery_read.wait(timeout=5)
    completion_thread.start()
    assert store.completion_started.wait(timeout=5)
    store.completion_finished.wait(timeout=1)
    store.release_recovery_read.set()
    recovery_thread.join(timeout=5)
    completion_thread.join(timeout=5)

    assert not recovery_thread.is_alive()
    assert not completion_thread.is_alive()
    assert store.load_journal("operation-1").status is LifecycleOperationStatus.COMPLETED


@pytest.mark.parametrize(
    "definition_update",
    (
        {"parameters": {"profile": "changed"}},
        {"mutating": False},
        {"recovery_command": "flow-steward stack status"},
        {
            "steps": (
                LifecycleOperationStep(step_id="prepare", label="Changed label"),
                LifecycleOperationStep(step_id="restart", label="Restart services"),
            )
        },
        {
            "steps": (
                LifecycleOperationStep(
                    step_id="prepare",
                    label="Prepare services",
                    safe_boundary=False,
                ),
                LifecycleOperationStep(step_id="restart", label="Restart services"),
            )
        },
    ),
    ids=("parameters", "mutating", "recovery", "step-label", "safe-boundary"),
)
def test_resume_rejects_materially_changed_durable_definition(
    tmp_path: Path,
    definition_update: dict[str, object],
) -> None:
    gateway = FakeLifecycleGateway()
    gateway.fail_step = "restart"
    gateway.failure = LifecycleError.stable_failure(
        code="restart_failed",
        cause="restart failed",
        remediation="Inspect services and retry.",
        step="restart",
    )
    runner = LifecycleOperationRunner(gateway=gateway, state=LifecycleStateStore(tmp_path))
    definition = _definition()

    with pytest.raises(LifecycleError):
        runner.execute(definition)
    gateway.fail_step = None
    call_count = len(gateway.calls)

    _raises_input_721_1 = definition.model_copy(update=definition_update)
    with pytest.raises(LifecycleError) as raised:
        runner.execute(_raises_input_721_1)

    assert raised.value.code == "lifecycle_definition_mismatch"
    assert raised.value.exit_code is ExitCode.STABLE_FAILURE
    assert len(gateway.calls) == call_count


def test_requested_cancellation_stops_at_next_declared_safe_boundary(
    tmp_path: Path,
) -> None:
    gateway = FakeLifecycleGateway()
    gateway.request_cancellation_at = "prepare"
    store = LifecycleStateStore(tmp_path)
    events = []
    runner = LifecycleOperationRunner(gateway=gateway, state=store)

    _raises_input_738_1 = _definition()
    with pytest.raises(LifecycleError) as raised:
        runner.execute(_raises_input_738_1, progress_sink=events.append)

    journal = store.load_journal("operation-1")
    assert raised.value.exit_code is ExitCode.INTERRUPTED
    assert journal.status is LifecycleOperationStatus.CANCELLED
    assert journal.completed_steps == ("prepare",)
    assert tuple(step.status for step in journal.steps) == (
        OperationStepStatus.SUCCEEDED,
        OperationStepStatus.CANCELLED,
    )
    assert "execute:restart" not in gateway.calls
    assert any(
        event.step_id == "restart" and event.status is OperationStepStatus.CANCELLED
        for event in events
    )


def test_gateway_cancellation_uses_declared_step_and_retains_internal_diagnostic(
    tmp_path: Path,
) -> None:
    gateway = FakeLifecycleGateway()
    gateway.fail_step = "prepare"
    gateway.failure = LifecycleError.interrupted(
        step="internal_wait",
        recovery_command="flow-steward stack restart",
        forced=False,
    )
    store = LifecycleStateStore(tmp_path)
    events = []

    _raises_callable_769_1 = LifecycleOperationRunner(gateway=gateway, state=store).execute
    _raises_input_769_2 = _definition()
    with pytest.raises(LifecycleError) as raised:
        _raises_callable_769_1(_raises_input_769_2, progress_sink=events.append)

    journal = store.load_journal("operation-1")
    assert raised.value.step == "prepare"
    assert raised.value.details["internal_step"] == "internal_wait"
    assert journal.failed_step == "prepare"
    assert tuple(step.status for step in journal.steps) == (
        OperationStepStatus.CANCELLED,
        OperationStepStatus.PENDING,
    )
    assert (
        len(
            [
                event
                for event in events
                if event.step_id == "prepare" and event.status is OperationStepStatus.CANCELLED
            ]
        )
        == 1
    )
    assert all(step.status is not OperationStepStatus.RUNNING for step in journal.steps)


def test_runner_emits_terminal_failed_progress_for_failed_step(tmp_path: Path) -> None:
    gateway = FakeLifecycleGateway()
    gateway.fail_step = "prepare"
    gateway.failure = LifecycleError.stable_failure(
        code="prepare_failed",
        cause="Prepare failed.",
        remediation="Correct the cause and retry.",
        step="prepare",
    )
    events = []
    runner = LifecycleOperationRunner(
        gateway=gateway,
        state=LifecycleStateStore(tmp_path),
    )

    _raises_input_811_1 = _definition()
    with pytest.raises(LifecycleError):
        runner.execute(_raises_input_811_1, progress_sink=events.append)

    failed = [
        event
        for event in events
        if event.step_id == "prepare" and event.status is OperationStepStatus.FAILED
    ]
    assert len(failed) == 1
    assert failed[0].elapsed_seconds is not None


@pytest.mark.parametrize(
    ("failure", "expected_exit", "expected_status"),
    (
        (
            LifecycleError.dependency(
                code="docker_unavailable",
                cause="Docker is unavailable.",
                remediation="Start Docker and retry.",
                step="prepare",
            ),
            ExitCode.DEPENDENCY,
            LifecycleOperationStatus.FAILED,
        ),
        (
            LifecycleError.stable_failure(
                code="step_failed",
                cause="The step failed.",
                remediation="Correct the cause and retry.",
                step="prepare",
            ),
            ExitCode.STABLE_FAILURE,
            LifecycleOperationStatus.FAILED,
        ),
        (
            LifecycleError.recovery_required(
                code="state_uncertain",
                cause="The current state could not be verified.",
                remediation="Run the recovery command.",
                step="prepare",
                recovery_command="flow-steward stack status",
            ),
            ExitCode.RECOVERY_REQUIRED,
            LifecycleOperationStatus.RECOVERY_REQUIRED,
        ),
        (
            OSError("unexpected filesystem failure"),
            ExitCode.INTERNAL,
            LifecycleOperationStatus.FAILED,
        ),
    ),
)
def test_runner_preserves_stable_failure_mappings_and_maps_unknown_to_internal(
    tmp_path: Path,
    failure: LifecycleError | Exception,
    expected_exit: ExitCode,
    expected_status: LifecycleOperationStatus,
) -> None:
    gateway = FakeLifecycleGateway()
    gateway.fail_step = "prepare"
    gateway.failure = failure
    store = LifecycleStateStore(tmp_path)
    runner = LifecycleOperationRunner(gateway=gateway, state=store)

    _raises_input_876_1 = _definition()
    with pytest.raises(LifecycleError) as raised:
        runner.execute(_raises_input_876_1)

    assert raised.value.exit_code is expected_exit
    assert store.load_journal("operation-1").status is expected_status
