"""Application policy for safe installed-stack lifecycle operations."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from core.application.installer.redaction import redact_installer_text
from core.application.lifecycle.configuration_service import ConfigurationService
from core.application.lifecycle.contracts import (
    CancellationToken,
    ComposeTarget,
    DeploymentConfig,
    LifecycleLockStatus,
    LifecycleOperationDefinition,
    LifecycleOperationJournal,
    LifecycleOperationKind,
    LifecycleOperationStatus,
)
from core.application.lifecycle.errors import LifecycleError
from core.application.lifecycle.operation_service import LifecycleOperationRunner
from core.application.lifecycle.ports import (
    LifecycleState,
    ProgressSink,
    TechnicalSink,
)
from core.application.lifecycle.preservation_service import (
    UNKNOWN,
    PreservationManifest,
)
from core.application.lifecycle.stack_models import (
    DEFAULT_STACK_HEALTH_TIMEOUT_SECONDS as DEFAULT_STACK_HEALTH_TIMEOUT_SECONDS,
)
from core.application.lifecycle.stack_models import ReadinessReconciler as ReadinessReconciler
from core.application.lifecycle.stack_models import ReleaseProvider as ReleaseProvider
from core.application.lifecycle.stack_models import (
    StackContainerRollbackState as StackContainerRollbackState,
)
from core.application.lifecycle.stack_models import StackContainerState as StackContainerState
from core.application.lifecycle.stack_models import StackDockerGateway as StackDockerGateway
from core.application.lifecycle.stack_models import StackGatewayError as StackGatewayError
from core.application.lifecycle.stack_models import StackImageTransition as StackImageTransition
from core.application.lifecycle.stack_models import StackLogsRequest as StackLogsRequest
from core.application.lifecycle.stack_models import StackLogsResult as StackLogsResult
from core.application.lifecycle.stack_models import StackManifestStore as StackManifestStore
from core.application.lifecycle.stack_models import StackMutationResult as StackMutationResult
from core.application.lifecycle.stack_models import (
    StackPreservationService as StackPreservationService,
)
from core.application.lifecycle.stack_models import StackReadinessService as StackReadinessService
from core.application.lifecycle.stack_models import StackRebuildRequest as StackRebuildRequest
from core.application.lifecycle.stack_models import StackRestartRequest as StackRestartRequest
from core.application.lifecycle.stack_models import StackResult as StackResult
from core.application.lifecycle.stack_models import StackRollbackSnapshot as StackRollbackSnapshot
from core.application.lifecycle.stack_models import StackRuntimeState as StackRuntimeState
from core.application.lifecycle.stack_models import StackStartRequest as StackStartRequest
from core.application.lifecycle.stack_models import StackStatusRequest as StackStatusRequest
from core.application.lifecycle.stack_models import StackStatusResult as StackStatusResult
from core.application.lifecycle.stack_models import StackStopRequest as StackStopRequest
from core.application.lifecycle.stack_models import StackVolumeState as StackVolumeState
from core.application.lifecycle.stack_models import (
    StorageConnectivityProvider as StorageConnectivityProvider,
)
from core.application.lifecycle.stack_models import _StackMutationRequest
from core.application.lifecycle.stack_operation import (
    _dependency_failure,
    _journal_ambiguity_required,
    _journal_recovery_required,
    _operation_steps,
    _recovery_attempt_record,
    _recovery_step_output,
    _runtime_postcondition,
    _StackOperationContext,
    _StackOperationGateway,
)
from core.application.management.command_specs.contracts import ExitCode

_RETAINED_MANAGEMENT_SERVICES = frozenset({"fs-cli"})


class StackService:
    """Own allowed stack actions, targets, safety gates, and postconditions."""

    def __init__(
        self,
        *,
        configuration_service: ConfigurationService,
        docker_gateway: StackDockerGateway,
        lifecycle_state: LifecycleState,
        preservation_service: StackPreservationService,
        manifest_store: StackManifestStore,
        release_provider: ReleaseProvider,
        readiness_service: StackReadinessService,
        readiness_reconciler: ReadinessReconciler | None = None,
        storage_connectivity_provider: StorageConnectivityProvider,
        health_timeout_seconds: int = DEFAULT_STACK_HEALTH_TIMEOUT_SECONDS,
        readiness_poll_interval_seconds: float = 1.0,
        readiness_sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if health_timeout_seconds <= 0:
            raise ValueError("stack health timeout must be positive")
        if readiness_poll_interval_seconds <= 0:
            raise ValueError("stack readiness poll interval must be positive")
        self.configuration_service = configuration_service
        self.docker_gateway = docker_gateway
        self.lifecycle_state = lifecycle_state
        self.preservation_service = preservation_service
        self.manifest_store = manifest_store
        self.release_provider = release_provider
        self.readiness_service = readiness_service
        self.readiness_reconciler = readiness_reconciler
        self.storage_connectivity_provider = storage_connectivity_provider
        self.health_timeout_seconds = int(health_timeout_seconds)
        self.readiness_poll_interval_seconds = float(readiness_poll_interval_seconds)
        self.readiness_sleep = readiness_sleep

    def status(self, request: StackStatusRequest) -> StackResult:
        del request
        _config, target = self._installed_target()
        state = self._status(target)
        runtime_ready = _runtime_postcondition("start", state, target.runtime_services)
        return StackStatusResult(
            status="observed" if runtime_ready else "degraded",
            project_name=target.project_name,
            services=state.services,
            volumes=state.volumes,
            process_exit_code=0 if runtime_ready else int(ExitCode.PRECONDITION),
        )

    def log_service_choices(self) -> tuple[str, ...]:
        _config, target = self._installed_target()
        return self._compose_services(target)

    def plan(
        self,
        action: Literal["start", "stop", "restart", "rebuild"],
        request: _StackMutationRequest,
    ) -> dict[str, object]:
        adopt_image_pins = bool(getattr(request, "adopt_image_pins", False))
        config, target = self._installed_target()
        services = (
            tuple(target.rebuild_services)
            if action == "rebuild"
            else tuple(
                service
                for service in target.runtime_services
                if service not in _RETAINED_MANAGEMENT_SERVICES
            )
        )
        available = set(self._compose_services(target))
        missing = tuple(service for service in services if service not in available)
        if missing:
            raise LifecycleError.dependency(
                code="stack_services_unavailable",
                cause="Selected runtime services are absent from the installed Compose configuration.",
                remediation="Run flow-steward config validate and repair the installed Compose files.",
                step="validation",
            )
        preservation_gates = [
            "exact Compose project and durable volume identities",
            "database identities, counts, schema, and decryptability",
            "non-mutating artifact storage connectivity",
            "canonical application and database readiness",
        ]
        if action == "start":
            preservation_gates.append(
                "running container image identities move onto the configured pins"
                if adopt_image_pins
                else "running container image identities stay unchanged"
            )
        return {
            "action": action,
            "project_name": target.project_name,
            "environment": config.environment,
            "target_services": list(services),
            "adopt_image_pins": adopt_image_pins,
            "preservation_gates": preservation_gates,
            "rollback_policy": (
                "restore exact prior image IDs for recreated services and exact prior "
                "running/stopped topology; otherwise return recovery_required"
            ),
        }

    def _compose_services(self, target: ComposeTarget) -> tuple[str, ...]:
        try:
            return self.docker_gateway.services(target)
        except StackGatewayError as exc:
            raise _dependency_failure(exc) from exc

    def logs(
        self,
        request: StackLogsRequest,
        **operation_sinks: object,
    ) -> StackLogsResult:
        _config, target = self._installed_target()
        if request.service is not None and request.service not in self.log_service_choices():
            raise LifecycleError(
                code="stack_service_unavailable",
                summary="The selected log service is unavailable.",
                cause="The selected service is not present in the installed Compose configuration.",
                remediation="Choose a service shown by flow-steward stack logs.",
                retryable=False,
                step="validation",
                exit_code=ExitCode.VALIDATION,
            )
        sink = _technical_sink(operation_sinks)
        output_lines: list[str] = []

        def safe_output(message: str) -> None:
            redacted = redact_installer_text(str(message))
            output_lines.append(redacted)
            if sink is not None:
                sink(redacted)

        try:
            self.docker_gateway.logs(
                target,
                service=request.service,
                tail=request.tail,
                follow=request.follow,
                output_sink=safe_output,
            )
        except StackGatewayError as exc:
            raise _dependency_failure(exc) from exc
        return StackLogsResult(
            service=request.service,
            tail=request.tail,
            follow=request.follow,
            lines_streamed=len(output_lines),
            lines=tuple(output_lines),
        )

    def start(self, request: StackStartRequest, **operation_sinks: object) -> StackResult:
        return self._mutate("start", request.adopt_image_pins, **operation_sinks)

    def stop(self, request: StackStopRequest, **operation_sinks: object) -> StackResult:
        del request
        return self._mutate("stop", False, **operation_sinks)

    def restart(self, request: StackRestartRequest, **operation_sinks: object) -> StackResult:
        del request
        return self._mutate("restart", False, **operation_sinks)

    def rebuild(self, request: StackRebuildRequest, **operation_sinks: object) -> StackResult:
        del request
        return self._mutate("rebuild", False, **operation_sinks)

    def _mutate(
        self,
        action: Literal["start", "stop", "restart", "rebuild"],
        adopt_image_pins: bool,
        **operation_sinks: object,
    ) -> StackMutationResult:
        config, target = self._installed_target()
        services = (
            tuple(target.rebuild_services)
            if action == "rebuild"
            else tuple(
                service
                for service in target.runtime_services
                if service not in _RETAINED_MANAGEMENT_SERVICES
            )
        )
        if not services:
            raise LifecycleError.stable_failure(
                code="stack_target_empty",
                cause=f"The installed configuration selected no services for stack {action}.",
                remediation="Run flow-steward config validate.",
                step="validation",
            )
        available = set(self._compose_services(target))
        missing = tuple(service for service in services if service not in available)
        if missing:
            raise LifecycleError.dependency(
                code="stack_services_unavailable",
                cause="Selected runtime services are absent from the installed Compose configuration.",
                remediation="Run flow-steward config validate and repair the installed Compose files.",
                step="validation",
            )
        kind = {
            "start": LifecycleOperationKind.STACK_START,
            "stop": LifecycleOperationKind.STACK_STOP,
            "restart": LifecycleOperationKind.STACK_RESTART,
            "rebuild": LifecycleOperationKind.STACK_REBUILD,
        }[action]
        existing = self._resumable_journal(
            kind=kind,
            action=action,
        )
        operation_id = existing.operation_id if existing is not None else uuid4().hex
        release = str(self.release_provider() or "").strip() or UNKNOWN
        rollback_snapshot, before_manifest = self._resume_evidence(existing)
        context = _StackOperationContext(
            action=action,
            operation_id=operation_id,
            config=config,
            target=target,
            target_services=services,
            managed_services=tuple(
                service
                for service in dict.fromkeys((*target.runtime_services, *services))
                if service not in _RETAINED_MANAGEMENT_SERVICES
            ),
            release=release,
            lifecycle_state=self.lifecycle_state,
            readiness_service=self.readiness_service,
            readiness_reconciler=self.readiness_reconciler,
            storage_connectivity_provider=self.storage_connectivity_provider,
            rollback_snapshot=rollback_snapshot,
            durable_completed_steps=(existing.completed_steps if existing is not None else ()),
            adopt_image_pins=adopt_image_pins,
        )
        gateway = _StackOperationGateway(
            docker=self.docker_gateway,
            preservation=self.preservation_service,
            manifest_store=self.manifest_store,
            context=context,
            health_timeout_seconds=self.health_timeout_seconds,
            readiness_poll_interval_seconds=self.readiness_poll_interval_seconds,
            readiness_sleep=self.readiness_sleep,
            before_manifest=before_manifest,
        )
        if existing is not None and existing.status is LifecycleOperationStatus.RECOVERY_REQUIRED:
            return self._recover_existing(
                action=action,
                journal=existing,
                gateway=gateway,
                operation_sinks=operation_sinks,
            )
        runner = LifecycleOperationRunner(gateway=gateway, state=self.lifecycle_state)
        definition = LifecycleOperationDefinition(
            operation_id=operation_id,
            kind=kind,
            steps=_operation_steps(action),
            parameters={
                "action": action,
                "project_name": target.project_name,
                "environment": config.environment,
                "release": release,
                "target_services": list(services),
                "adopt_image_pins": adopt_image_pins,
            },
            previous_config={"configuration": config.model_dump(mode="json")},
            recovery_command=self.docker_gateway.manual_recovery_command(action),
        )
        operation = runner.execute(
            definition,
            progress_sink=_progress_sink(operation_sinks),
            technical_sink=_technical_sink(operation_sinks),
            cancellation=_cancellation(operation_sinks),
        )
        state = gateway.after_state or self._status(target)
        return StackMutationResult(
            action=action,
            project_name=target.project_name,
            services=state.services,
            volumes=state.volumes,
            target_services=services,
            operation_id=operation.operation_id,
            journal_path=operation.journal_path,
            log_path=operation.log_path,
            preservation_verified=gateway.preservation_verified,
            runtime_restored=False,
        )

    def _recover_existing(
        self,
        *,
        action: Literal["start", "stop", "restart", "rebuild"],
        journal: LifecycleOperationJournal,
        gateway: _StackOperationGateway,
        operation_sinks: Mapping[str, object],
    ) -> StackMutationResult:
        technical = _technical_sink(dict(operation_sinks))
        with self.lifecycle_state.operation_lock(journal.operation_id, mutating=True):
            log = self.lifecycle_state.open_log(journal.operation_id)
            # Record the attempt before recovery runs so an operator and support can
            # always see that this command was tried, whatever its outcome. The record
            # lives in the open step-output map, never in a new closed journal field:
            # a runtime role still running the previous image reads the same journal
            # and rejects unknown top-level keys.
            attempted = journal.model_copy(
                update={
                    "step_outputs": {
                        **journal.step_outputs,
                        "recovery": _recovery_attempt_record(journal),
                    },
                    "log_path": str(log.path),
                    "updated_at": datetime.now(UTC),
                }
            )
            self.lifecycle_state.save_journal(attempted)
            try:

                def output(message: str) -> None:
                    safe = redact_installer_text(str(message))
                    log.write(safe)
                    if technical is not None:
                        technical(safe)

                state = gateway.recover(output_sink=output, heartbeat=lambda _detail: None)
                recovered = attempted.model_copy(
                    update={
                        "status": LifecycleOperationStatus.FAILED,
                        "current_step": None,
                        "failed_step": "recovery",
                        "last_error_code": "stack_recovery_completed",
                        "retryable": False,
                        "updated_at": datetime.now(UTC),
                    }
                )
                self.lifecycle_state.save_journal(recovered)
            except LifecycleError as exc:
                exc.operation_id = journal.operation_id
                exc.log_path = str(log.path)
                self.lifecycle_state.save_journal(
                    attempted.model_copy(
                        update={
                            "last_error_code": exc.code,
                            "step_outputs": {
                                **attempted.step_outputs,
                                "recovery": {
                                    **_recovery_step_output(attempted),
                                    "last_error_code": exc.code,
                                },
                            },
                            "updated_at": datetime.now(UTC),
                        }
                    )
                )
                raise
            finally:
                log.close()
        return StackMutationResult(
            action=action,
            status="recovered",
            project_name=gateway.context.target.project_name,
            services=state.services,
            volumes=state.volumes,
            target_services=gateway.context.target_services,
            operation_id=journal.operation_id,
            journal_path=str(self.lifecycle_state.journal_path(journal.operation_id)),
            log_path=str(log.path),
            preservation_verified=gateway.preservation_verified,
            runtime_restored=True,
        )

    def _resumable_journal(
        self,
        *,
        kind: LifecycleOperationKind,
        action: str,
    ) -> LifecycleOperationJournal | None:
        diagnosis = self.lifecycle_state.diagnose_active_lock()
        if diagnosis.status is LifecycleLockStatus.ACTIVE:
            raise LifecycleError.conflict(
                operation_id=diagnosis.operation_id,
                cause=diagnosis.detail,
            )
        try:
            candidates = self.lifecycle_state.unfinished_journals()
        except (OSError, ValueError) as exc:
            raise _journal_recovery_required(
                action=action,
                operation_id=diagnosis.operation_id or "unknown-operation",
                cause="Lifecycle journals are unreadable and safe resume cannot be proven.",
                recovery_command=(
                    "operator: repair or quarantine unreadable lifecycle journals under "
                    ".flow-steward/operations"
                ),
            ) from exc
        if not candidates:
            return None
        if len(candidates) != 1:
            raise _journal_ambiguity_required(
                action=action,
                journals=candidates,
            )
        journal = candidates[0]
        if journal.kind is not kind:
            raise _journal_recovery_required(
                action=action,
                operation_id=journal.operation_id,
                cause="An unfinished lifecycle operation has a different action.",
                recovery_command=journal.recovery_command,
            )
        return journal

    def _resume_evidence(
        self,
        journal: LifecycleOperationJournal | None,
    ) -> tuple[StackRollbackSnapshot | None, PreservationManifest | None]:
        if journal is None:
            return None, None
        try:
            raw_snapshot = self.lifecycle_state.load_previous_config(journal.operation_id)
            snapshot = (
                StackRollbackSnapshot.model_validate(raw_snapshot)
                if raw_snapshot is not None and "operation_id" in raw_snapshot
                else None
            )
            before = self.manifest_store.load(journal.operation_id, "before")
        except (OSError, ValueError) as exc:
            raise _journal_recovery_required(
                action=journal.kind.value.removeprefix("stack_"),
                operation_id=journal.operation_id,
                cause="Saved rollback evidence is unreadable.",
                recovery_command=journal.recovery_command,
            ) from exc
        action_may_have_started = bool(
            set(journal.completed_steps).intersection({"apply_action", "verify_runtime"})
            or journal.current_step
            in {"apply_action", "verify_runtime", "capture_after", "verify_preservation"}
        )
        if action_may_have_started and (snapshot is None or before is None):
            raise _journal_recovery_required(
                action=journal.kind.value.removeprefix("stack_"),
                operation_id=journal.operation_id,
                cause="The interrupted operation lacks complete pre-mutation rollback evidence.",
                recovery_command=journal.recovery_command,
            )
        return snapshot, before

    def _installed_target(self) -> tuple[DeploymentConfig, ComposeTarget]:
        config = self.configuration_service.read_active()
        assert config is not None
        return config, self.configuration_service.derive_target(config)

    def _status(self, target: ComposeTarget) -> StackRuntimeState:
        try:
            return self.docker_gateway.status(target)
        except StackGatewayError as exc:
            raise _dependency_failure(exc) from exc


def _progress_sink(operation_sinks: dict[str, object]) -> ProgressSink | None:
    value = operation_sinks.get("progress_sink")
    return value if callable(value) else None


def _technical_sink(operation_sinks: dict[str, object]) -> TechnicalSink | None:
    value = operation_sinks.get("technical_sink")
    return value if callable(value) else None


def _cancellation(operation_sinks: dict[str, object]) -> CancellationToken | None:
    value = operation_sinks.get("cancellation")
    return value if isinstance(value, CancellationToken) else None


__all__ = [
    "StackContainerRollbackState",
    "StackContainerState",
    "StackGatewayError",
    "StackLogsRequest",
    "StackLogsResult",
    "StackMutationResult",
    "StackRebuildRequest",
    "StackRestartRequest",
    "StackResult",
    "StackRollbackSnapshot",
    "StackRuntimeState",
    "StackService",
    "StackStartRequest",
    "StackStatusRequest",
    "StackStatusResult",
    "StackStopRequest",
    "StackVolumeState",
]
