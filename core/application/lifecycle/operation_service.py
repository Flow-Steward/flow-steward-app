"""Postcondition-driven lifecycle operation runner."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime
from typing import TypeVar
from uuid import uuid4

from core.application.installer.redaction import redact_installer_text
from core.application.lifecycle.contracts import (
    CancellationToken,
    LifecycleOperationDefinition,
    LifecycleOperationJournal,
    LifecycleOperationResult,
    LifecycleOperationStatus,
    LifecycleOperationStep,
)
from core.application.lifecycle.errors import ForcedLifecycleInterrupt, LifecycleError
from core.application.lifecycle.ports import (
    LifecycleLog,
    LifecycleOperationGateway,
    LifecycleState,
    ProgressSink,
    TechnicalSink,
)
from core.application.management.command_specs.contracts import (
    ExitCode,
    OperationProgressEvent,
    OperationStep,
    OperationStepStatus,
)

DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 5.0
_SinkValue = TypeVar("_SinkValue")


class LifecycleOperationRunner:
    """Run, journal, resume, and cooperatively cancel lifecycle steps."""

    def __init__(
        self,
        *,
        gateway: LifecycleOperationGateway,
        state: LifecycleState,
        heartbeat_interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat interval must be positive")
        self.gateway = gateway
        self.state = state
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.clock = clock

    def execute(
        self,
        definition: LifecycleOperationDefinition,
        *,
        progress_sink: ProgressSink | None = None,
        technical_sink: TechnicalSink | None = None,
        cancellation: CancellationToken | None = None,
    ) -> LifecycleOperationResult:
        """Execute or resume one definition under the mutation policy."""
        operation_id = definition.operation_id or uuid4().hex
        definition = definition.model_copy(update={"operation_id": operation_id})
        token = cancellation or CancellationToken()
        token.set_recovery_command(definition.recovery_command)
        started = self.clock()
        current_step = ["operation"]
        lock_acquired = [False]
        recovery_journal = [
            self._starting_journal(
                definition,
                existing=None,
                log_path=None,
                previous_config_path=None,
            )
        ]

        def mark_forced_recovery() -> None:
            self.state.mark_recovery_required(
                operation_id,
                step=current_step[0],
                error_code="lifecycle_forced_interrupt",
                initial_journal=recovery_journal[0] if lock_acquired[0] else None,
            )

        remove_force_callback = token.register_force_callback(mark_forced_recovery)
        try:
            with self.state.operation_lock(operation_id, mutating=definition.mutating):
                lock_acquired[0] = True
                return self._execute_locked(
                    definition,
                    started=started,
                    progress_sink=progress_sink,
                    technical_sink=technical_sink,
                    cancellation=token,
                    current_step=current_step,
                    recovery_journal=recovery_journal,
                )
        except (ForcedLifecycleInterrupt, KeyboardInterrupt) as exc:
            with suppress(Exception):
                mark_forced_recovery()
            failure = LifecycleError.interrupted(
                step=current_step[0],
                recovery_command=definition.recovery_command,
                forced=True,
            )
            failure.operation_id = operation_id
            try:
                journal = self.state.load_journal(operation_id)
            except Exception:
                journal = None
            if journal is not None:
                failure.log_path = journal.log_path
            raise failure from exc
        except LifecycleError as exc:
            if exc.operation_id is None:
                exc.operation_id = operation_id
            if exc.recovery_command is None:
                exc.recovery_command = definition.recovery_command
            raise
        finally:
            remove_force_callback()

    def _execute_locked(
        self,
        definition: LifecycleOperationDefinition,
        *,
        started: float,
        progress_sink: ProgressSink | None,
        technical_sink: TechnicalSink | None,
        cancellation: CancellationToken,
        current_step: list[str],
        recovery_journal: list[LifecycleOperationJournal],
    ) -> LifecycleOperationResult:
        operation_id = definition.operation_id
        assert operation_id is not None
        existing = self.state.load_journal(operation_id)
        if existing is not None:
            self._validate_resume(existing, definition)
            if (
                existing.status is LifecycleOperationStatus.RECOVERY_REQUIRED
                and not definition.allow_recovery_resume
            ):
                failure = LifecycleError.recovery_required(
                    code="lifecycle_recovery_required",
                    cause="The operation journal requires explicit recovery before resume.",
                    remediation=f"Run: {existing.recovery_command}",
                    step=existing.failed_step or existing.current_step or "recovery",
                    recovery_command=existing.recovery_command,
                )
                failure.operation_id = operation_id
                failure.log_path = existing.log_path
                raise failure

        resuming = existing is not None
        log = self.state.open_log(operation_id)
        try:
            previous_config_path = existing.previous_config_path if existing else None
            if existing is None and definition.previous_config:
                previous_config_path = str(
                    self.state.save_previous_config(operation_id, definition.previous_config)
                )
            journal = self._starting_journal(
                definition,
                existing=existing,
                log_path=str(log.path),
                previous_config_path=previous_config_path,
            )
            recovery_journal[0] = journal
            self.state.save_journal(journal)
        except BaseException:
            log.close()
            raise
        current_step[0] = journal.current_step or "operation"
        completed = list(journal.completed_steps)
        skipped: list[str] = []
        try:
            for step in definition.steps:
                self._emit(
                    progress_sink,
                    step,
                    OperationStepStatus.PENDING,
                    elapsed=self.clock() - started,
                )
            self._raise_for_cancellation(
                cancellation,
                journal=journal,
                current_step="operation",
                definition=definition,
                progress_sink=progress_sink,
                log=log,
            )
            for step in definition.steps:
                current_step[0] = step.step_id
                if resuming and self.gateway.postcondition_holds(definition, step):
                    if step.step_id not in completed:
                        completed.append(step.step_id)
                    skipped.append(step.step_id)
                    step_outputs = self._step_outputs(journal, definition, step)
                    journal = self._update_journal(
                        journal,
                        step=step,
                        step_status=OperationStepStatus.SKIPPED,
                        completed=completed,
                        skipped=skipped,
                        step_outputs=step_outputs,
                    )
                    self.state.save_journal(journal)
                    self._emit(
                        progress_sink,
                        step,
                        OperationStepStatus.SKIPPED,
                        detail=f"{step.label}: postcondition already holds",
                        elapsed=self.clock() - started,
                    )
                    if step.safe_boundary:
                        self._raise_for_cancellation(
                            cancellation,
                            journal=journal,
                            current_step=step.step_id,
                            definition=definition,
                            progress_sink=progress_sink,
                            log=log,
                        )
                    continue
                journal = self._update_journal(
                    journal,
                    step=step,
                    step_status=OperationStepStatus.RUNNING,
                    completed=completed,
                    skipped=skipped,
                    step_outputs=dict(journal.step_outputs),
                )
                self.state.save_journal(journal)
                self._emit(
                    progress_sink,
                    step,
                    OperationStepStatus.RUNNING,
                    elapsed=self.clock() - started,
                )

                def output(message: str) -> None:
                    log.write(str(message))
                    _safe_sink_write(technical_sink, redact_installer_text(str(message)))

                def heartbeat(
                    detail: str = "",
                    *,
                    active_step: LifecycleOperationStep = step,
                ) -> None:
                    self._emit(
                        progress_sink,
                        active_step,
                        OperationStepStatus.RUNNING,
                        detail=detail or f"{active_step.label} is still running",
                        elapsed=self.clock() - started,
                        heartbeat=True,
                    )

                pulse = _PeriodicHeartbeat(
                    interval_seconds=self.heartbeat_interval_seconds,
                    emit=heartbeat,
                )
                pulse.start()
                try:
                    self.gateway.execute_step(
                        definition,
                        step,
                        output_sink=output,
                        heartbeat=heartbeat,
                        cancellation=cancellation,
                    )
                finally:
                    pulse.stop()
                if step.step_id not in completed:
                    completed.append(step.step_id)
                step_outputs = self._step_outputs(journal, definition, step)
                journal = self._update_journal(
                    journal,
                    step=step,
                    step_status=OperationStepStatus.SUCCEEDED,
                    completed=completed,
                    skipped=skipped,
                    step_outputs=step_outputs,
                )
                self.state.save_journal(journal)
                self._emit(
                    progress_sink,
                    step,
                    OperationStepStatus.SUCCEEDED,
                    elapsed=self.clock() - started,
                )
                if step.safe_boundary:
                    self._raise_for_cancellation(
                        cancellation,
                        journal=journal,
                        current_step=step.step_id,
                        definition=definition,
                        progress_sink=progress_sink,
                        log=log,
                    )
            journal = journal.model_copy(
                update={
                    "status": LifecycleOperationStatus.COMPLETED,
                    "current_step": None,
                    "failed_step": None,
                    "last_error_code": None,
                    "completed_steps": tuple(completed),
                    "skipped_steps": tuple(skipped),
                    "retryable": False,
                    "updated_at": datetime.now(UTC),
                }
            )
            self.state.save_journal(journal)
            return LifecycleOperationResult(
                operation_id=operation_id,
                kind=definition.kind,
                status=journal.status,
                completed_steps=journal.completed_steps,
                skipped_steps=journal.skipped_steps,
                journal_path=str(self.state.journal_path(operation_id)),
                log_path=str(log.path),
                previous_config_path=journal.previous_config_path,
                recovery_command=definition.recovery_command,
                elapsed_seconds=max(0.0, self.clock() - started),
            )
        except ForcedLifecycleInterrupt as exc:
            self.state.mark_recovery_required(
                operation_id,
                step=current_step[0],
                error_code="lifecycle_forced_interrupt",
            )
            failure = LifecycleError.interrupted(
                step=current_step[0],
                recovery_command=definition.recovery_command,
                forced=True,
            )
            failure.log_path = str(log.path)
            log.write(f"FAILED {failure.code}: {failure.cause}")
            self._emit_terminal_failure(
                progress_sink,
                definition,
                step_id=current_step[0],
                detail=failure.summary,
                elapsed=self.clock() - started,
            )
            raise failure from exc
        except KeyboardInterrupt as exc:
            self.state.mark_recovery_required(
                operation_id,
                step=current_step[0],
                error_code="lifecycle_forced_interrupt",
            )
            failure = LifecycleError.interrupted(
                step=current_step[0],
                recovery_command=definition.recovery_command,
                forced=True,
            )
            failure.log_path = str(log.path)
            log.write(f"FAILED {failure.code}: {failure.cause}")
            self._emit_terminal_failure(
                progress_sink,
                definition,
                step_id=current_step[0],
                detail=failure.summary,
                elapsed=self.clock() - started,
            )
            raise failure from exc
        except LifecycleError as exc:
            exc.operation_id = operation_id
            exc.log_path = str(log.path)
            exc.recovery_command = exc.recovery_command or definition.recovery_command
            status = LifecycleOperationStatus.FAILED
            if (
                exc.exit_code is ExitCode.RECOVERY_REQUIRED
                or exc.code == "lifecycle_forced_interrupt"
            ):
                status = LifecycleOperationStatus.RECOVERY_REQUIRED
            elif exc.code == "lifecycle_cancelled":
                status = LifecycleOperationStatus.CANCELLED
            if exc.code == "lifecycle_cancelled":
                declared_step = self._declared_step_id(definition, current_step[0])
                internal_step = exc.step
                if internal_step and internal_step != declared_step:
                    diagnostic_step = redact_installer_text(str(internal_step))[:128]
                    exc.details["internal_step"] = diagnostic_step
                    log.write(
                        "DIAGNOSTIC lifecycle_cancelled "
                        f"internal_step={diagnostic_step} declared_step={declared_step}"
                    )
                exc.step = declared_step
                persisted = self.state.load_journal(operation_id)
                if persisted is None or persisted.status is not LifecycleOperationStatus.CANCELLED:
                    journal = self._record_failure(
                        journal,
                        status=LifecycleOperationStatus.CANCELLED,
                        step=declared_step,
                        error_code=exc.code,
                        retryable=True,
                    )
                    self.state.save_journal(journal)
                    self._emit_terminal_cancelled(
                        progress_sink,
                        definition,
                        step_id=declared_step,
                        elapsed=self.clock() - started,
                    )
            else:
                journal = self._record_failure(
                    journal,
                    status=status,
                    step=exc.step or current_step[0],
                    error_code=exc.code,
                    retryable=exc.retryable,
                )
                self.state.save_journal(journal)
                self._emit_terminal_failure(
                    progress_sink,
                    definition,
                    step_id=exc.step or current_step[0],
                    detail=exc.summary,
                    elapsed=self.clock() - started,
                )
            log.write(f"FAILED {exc.code}: {exc.cause}")
            raise
        except Exception as exc:
            log.write(f"FAILED lifecycle_internal_error: {type(exc).__name__}: {exc}")
            failure = LifecycleError.internal(
                step=current_step[0], recovery_command=definition.recovery_command
            )
            failure.operation_id = operation_id
            failure.log_path = str(log.path)
            journal = self._record_failure(
                journal,
                status=LifecycleOperationStatus.FAILED,
                step=current_step[0],
                error_code=failure.code,
                retryable=False,
            )
            self.state.save_journal(journal)
            self._emit_terminal_failure(
                progress_sink,
                definition,
                step_id=current_step[0],
                detail=failure.summary,
                elapsed=self.clock() - started,
            )
            raise failure from exc
        finally:
            log.close()

    @staticmethod
    def _validate_resume(
        journal: LifecycleOperationJournal,
        definition: LifecycleOperationDefinition,
    ) -> None:
        journal_steps = tuple(step.step_id for step in journal.steps)
        definition_steps = tuple(step.step_id for step in definition.steps)
        if (
            journal.kind is not definition.kind
            or journal.mutating is not definition.mutating
            or journal.parameters != definition.parameters
            or journal.recovery_command != definition.recovery_command
            or journal.step_definitions != definition.steps
            or journal_steps != definition_steps
        ):
            raise LifecycleError.stable_failure(
                code="lifecycle_definition_mismatch",
                cause="The saved operation does not match the requested definition.",
                remediation="Inspect the journal and use its recovery command.",
                step="resume",
            )

    @staticmethod
    def _starting_journal(
        definition: LifecycleOperationDefinition,
        *,
        existing: LifecycleOperationJournal | None,
        log_path: str | None,
        previous_config_path: str | None,
    ) -> LifecycleOperationJournal:
        assert definition.operation_id is not None
        if existing is not None:
            completed = set(existing.completed_steps)
            return existing.model_copy(
                update={
                    "status": LifecycleOperationStatus.RUNNING,
                    "steps": tuple(
                        saved
                        if saved.step_id in completed
                        else saved.model_copy(update={"status": OperationStepStatus.PENDING})
                        for saved in existing.steps
                    ),
                    "current_step": None,
                    "failed_step": None,
                    "last_error_code": None,
                    "skipped_steps": (),
                    "log_path": log_path,
                    "updated_at": datetime.now(UTC),
                }
            )
        return LifecycleOperationJournal(
            operation_id=definition.operation_id,
            kind=definition.kind,
            mutating=definition.mutating,
            parameters=definition.parameters,
            step_definitions=definition.steps,
            steps=tuple(
                OperationStep(
                    step_id=step.step_id,
                    status=OperationStepStatus.PENDING,
                    label=step.label,
                )
                for step in definition.steps
            ),
            previous_config_path=previous_config_path,
            log_path=log_path,
            recovery_command=definition.recovery_command,
        )

    @staticmethod
    def _update_journal(
        journal: LifecycleOperationJournal,
        *,
        step: LifecycleOperationStep,
        step_status: OperationStepStatus,
        completed: list[str],
        skipped: list[str],
        step_outputs: dict[str, object],
    ) -> LifecycleOperationJournal:
        steps = tuple(
            saved.model_copy(update={"status": step_status})
            if saved.step_id == step.step_id
            else saved
            for saved in journal.steps
        )
        return journal.model_copy(
            update={
                "status": LifecycleOperationStatus.RUNNING,
                "steps": steps,
                "completed_steps": tuple(completed),
                "skipped_steps": tuple(skipped),
                "step_outputs": step_outputs,
                "current_step": step.step_id,
                "failed_step": None,
                "last_error_code": None,
                "updated_at": datetime.now(UTC),
            }
        )

    def _step_outputs(
        self,
        journal: LifecycleOperationJournal,
        definition: LifecycleOperationDefinition,
        step: LifecycleOperationStep,
    ) -> dict[str, object]:
        outputs: dict[str, object] = dict(journal.step_outputs)
        provider = getattr(self.gateway, "step_output", None)
        if not callable(provider):
            return outputs
        value = provider(definition, step)
        if value is None:
            return outputs
        if not isinstance(value, Mapping):
            raise TypeError("Lifecycle step output must be a mapping")
        if not value:
            return outputs
        outputs[step.step_id] = {str(key): nested for key, nested in value.items()}
        return outputs

    @staticmethod
    def _record_failure(
        journal: LifecycleOperationJournal,
        *,
        status: LifecycleOperationStatus,
        step: str,
        error_code: str,
        retryable: bool,
    ) -> LifecycleOperationJournal:
        step_status = (
            OperationStepStatus.CANCELLED
            if status is LifecycleOperationStatus.CANCELLED
            else OperationStepStatus.FAILED
        )
        steps = tuple(
            saved.model_copy(update={"status": step_status}) if saved.step_id == step else saved
            for saved in journal.steps
        )
        return journal.model_copy(
            update={
                "status": status,
                "steps": steps,
                "current_step": None,
                "failed_step": step,
                "last_error_code": error_code,
                "retryable": retryable,
                "updated_at": datetime.now(UTC),
            }
        )

    def _raise_for_cancellation(
        self,
        cancellation: CancellationToken,
        *,
        journal: LifecycleOperationJournal,
        current_step: str,
        definition: LifecycleOperationDefinition,
        progress_sink: ProgressSink | None,
        log: LifecycleLog,
    ) -> None:
        if cancellation.forced:
            raise ForcedLifecycleInterrupt
        if not cancellation.requested:
            return
        cancelled_steps = tuple(
            saved.model_copy(update={"status": OperationStepStatus.CANCELLED})
            if saved.status is OperationStepStatus.PENDING
            else saved
            for saved in journal.steps
        )
        journal = journal.model_copy(
            update={
                "status": LifecycleOperationStatus.CANCELLED,
                "steps": cancelled_steps,
                "current_step": None,
                "failed_step": current_step,
                "last_error_code": "lifecycle_cancelled",
                "retryable": True,
                "updated_at": datetime.now(UTC),
            }
        )
        self.state.save_journal(journal)
        for saved in cancelled_steps:
            if saved.status is OperationStepStatus.CANCELLED:
                _safe_sink_write(
                    progress_sink,
                    OperationProgressEvent(
                        step_id=saved.step_id,
                        status=OperationStepStatus.CANCELLED,
                        label=saved.label,
                        detail="Cancelled at the next safe boundary.",
                    ),
                )
        failure = LifecycleError.interrupted(
            step=current_step,
            recovery_command=definition.recovery_command,
            forced=False,
        )
        failure.log_path = str(log.path)
        raise failure

    @staticmethod
    def _emit(
        sink: ProgressSink | None,
        step: LifecycleOperationStep,
        status: OperationStepStatus,
        *,
        detail: str = "",
        elapsed: float,
        heartbeat: bool = False,
    ) -> None:
        _safe_sink_write(
            sink,
            OperationProgressEvent(
                step_id=step.step_id,
                status=status,
                label=step.label,
                detail=detail,
                elapsed_seconds=max(0.0, elapsed),
                heartbeat=heartbeat,
            ),
        )

    def _emit_terminal_failure(
        self,
        sink: ProgressSink | None,
        definition: LifecycleOperationDefinition,
        *,
        step_id: str,
        detail: str,
        elapsed: float,
    ) -> None:
        step = next(
            (candidate for candidate in definition.steps if candidate.step_id == step_id),
            None,
        )
        if step is not None:
            self._emit(
                sink,
                step,
                OperationStepStatus.FAILED,
                detail=detail,
                elapsed=elapsed,
            )

    def _emit_terminal_cancelled(
        self,
        sink: ProgressSink | None,
        definition: LifecycleOperationDefinition,
        *,
        step_id: str,
        elapsed: float,
    ) -> None:
        step = next(candidate for candidate in definition.steps if candidate.step_id == step_id)
        self._emit(
            sink,
            step,
            OperationStepStatus.CANCELLED,
            detail="Cancelled at the next safe boundary.",
            elapsed=elapsed,
        )

    @staticmethod
    def _declared_step_id(
        definition: LifecycleOperationDefinition,
        current_step: str,
    ) -> str:
        return next(
            (step.step_id for step in definition.steps if step.step_id == current_step),
            definition.steps[0].step_id,
        )


class _PeriodicHeartbeat:
    def __init__(self, *, interval_seconds: float, emit: Callable[[str], None]) -> None:
        self.interval_seconds = interval_seconds
        self.emit = emit
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        if self._thread is not None:
            self._thread.join(timeout=max(0.1, self.interval_seconds * 2))

    def _run(self) -> None:
        while not self._stopped.wait(self.interval_seconds):
            self.emit("")


def _safe_sink_write(sink: Callable[[_SinkValue], None] | None, value: _SinkValue) -> None:
    if sink is None:
        return
    try:
        sink(value)
    except Exception:
        return
