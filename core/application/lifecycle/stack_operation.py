"""Execute and recover application-owned stack lifecycle steps."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal, NoReturn

from core.application.installer.redaction import redact_installer_text
from core.application.lifecycle.contracts import (
    CancellationToken,
    ComposeTarget,
    DeploymentConfig,
    LifecycleOperationDefinition,
    LifecycleOperationJournal,
    LifecycleOperationKind,
    LifecycleOperationStep,
)
from core.application.lifecycle.errors import LifecycleError
from core.application.lifecycle.ports import (
    HeartbeatSink,
    LifecycleState,
    TechnicalSink,
)
from core.application.lifecycle.preservation_service import (
    PreservationManifest,
)
from core.application.lifecycle.stack_models import (
    ReadinessReconciler,
    StackDockerGateway,
    StackGatewayError,
    StackImageTransition,
    StackManifestStore,
    StackPreservationService,
    StackReadinessService,
    StackRollbackSnapshot,
    StackRuntimeState,
    StorageConnectivityProvider,
)

_ADOPT_IMAGE_PINS_COMMAND = "./flow-steward stack start --confirm --adopt-image-pins"


class _StackOperationContext:
    def __init__(
        self,
        *,
        action: Literal["start", "stop", "restart", "rebuild"],
        operation_id: str,
        config: DeploymentConfig,
        target: ComposeTarget,
        target_services: tuple[str, ...],
        managed_services: tuple[str, ...],
        release: str,
        lifecycle_state: LifecycleState,
        readiness_service: StackReadinessService,
        readiness_reconciler: ReadinessReconciler | None,
        storage_connectivity_provider: StorageConnectivityProvider,
        rollback_snapshot: StackRollbackSnapshot | None,
        durable_completed_steps: tuple[str, ...],
        adopt_image_pins: bool = False,
    ) -> None:
        self.action = action
        self.operation_id = operation_id
        self.config = config
        self.target = target
        self.target_services = target_services
        self.managed_services = managed_services
        self.release = release
        self.lifecycle_state = lifecycle_state
        self.readiness_service = readiness_service
        self.readiness_reconciler = readiness_reconciler
        self.storage_connectivity_provider = storage_connectivity_provider
        self.rollback_snapshot = rollback_snapshot
        self.durable_completed_steps = frozenset(durable_completed_steps)
        self.adopt_image_pins = adopt_image_pins


class _StackOperationGateway:
    """Interpret application-owned stack steps using technical gateway operations."""

    def __init__(
        self,
        *,
        docker: StackDockerGateway,
        preservation: StackPreservationService,
        manifest_store: StackManifestStore,
        context: _StackOperationContext,
        health_timeout_seconds: int,
        readiness_poll_interval_seconds: float,
        readiness_sleep: Callable[[float], None],
        before_manifest: PreservationManifest | None = None,
    ) -> None:
        self.docker = docker
        self.preservation = preservation
        self.manifest_store = manifest_store
        self.context = context
        self.health_timeout_seconds = health_timeout_seconds
        self.readiness_poll_interval_seconds = readiness_poll_interval_seconds
        self.readiness_sleep = readiness_sleep
        self.before_state: StackRuntimeState | None = None
        self.after_state: StackRuntimeState | None = None
        self.before_manifest = before_manifest
        self.after_manifest: PreservationManifest | None = None
        self.mutated = False
        self.preservation_verified = False
        self.readiness_reconciled = False
        self.last_readiness_failed_ids: tuple[str, ...] = ()
        self.image_transitions: tuple[StackImageTransition, ...] = ()
        self.rollback_pins_pending = False

    def postcondition_holds(
        self,
        _definition: LifecycleOperationDefinition,
        step: LifecycleOperationStep,
    ) -> bool:
        if step.step_id == "capture_before":
            return self.context.rollback_snapshot is not None and self.before_manifest is not None
        if step.step_id not in {"apply_action", "verify_runtime"}:
            return False
        if step.step_id == "apply_action":
            completed = "apply_action" in self.context.durable_completed_steps
            self.mutated = completed
            return completed
        try:
            state = self.docker.status(self.context.target)
        except StackGatewayError:
            return False
        if not _runtime_postcondition(
            self.context.action,
            state,
            self.context.target_services,
        ):
            return False
        if self.context.action == "stop":
            return True
        self._reconcile_readiness(output_sink=lambda _line: None, heartbeat=lambda _detail: None)
        return self._readiness_holds()

    def execute_step(
        self,
        definition: LifecycleOperationDefinition,
        step: LifecycleOperationStep,
        *,
        output_sink: TechnicalSink,
        heartbeat: HeartbeatSink,
        cancellation: CancellationToken,
    ) -> None:
        del cancellation
        if step.step_id == "capture_before":
            self._capture_before()
            return
        if step.step_id == "apply_action":
            self._apply(output_sink=output_sink, heartbeat=heartbeat)
            return
        if step.step_id == "verify_runtime":
            self._verify_runtime(output_sink=output_sink, heartbeat=heartbeat)
            return
        if step.step_id == "capture_after":
            self._capture_after(output_sink=output_sink, heartbeat=heartbeat)
            return
        if step.step_id == "verify_preservation":
            self._verify_preservation(
                definition,
                output_sink=output_sink,
                heartbeat=heartbeat,
            )
            return
        raise LifecycleError.internal(
            step=step.step_id,
            recovery_command=definition.recovery_command,
        )

    def _capture_before(self) -> None:
        try:
            self.before_state = self.docker.status(self.context.target)
            if self.context.rollback_snapshot is None:
                self.context.rollback_snapshot = self.docker.capture_rollback(
                    self.context.target,
                    operation_id=self.context.operation_id,
                    selected_services=self.context.target_services,
                    managed_services=self.context.managed_services,
                )
                self.context.lifecycle_state.save_previous_config(
                    self.context.operation_id,
                    self.context.rollback_snapshot.model_dump(mode="json"),
                )
            if self.before_manifest is None:
                self.before_manifest = self._capture_manifest("before")
                self.manifest_store.save(self.before_manifest)
            baseline = PreservationManifest(
                operation_id=self.before_manifest.operation_id,
                stage="after",
                database_reachable=self.before_manifest.database_reachable,
                fields=self.before_manifest.fields,
            )
            comparison = self.preservation.compare(
                self.before_manifest,
                baseline,
                operation={
                    "start": LifecycleOperationKind.STACK_START,
                    "stop": LifecycleOperationKind.STACK_STOP,
                    "restart": LifecycleOperationKind.STACK_RESTART,
                    "rebuild": LifecycleOperationKind.STACK_REBUILD,
                }[self.context.action],
            )
            if not comparison.success:
                raise ValueError("Pre-operation preservation evidence is incomplete")
        except StackGatewayError as exc:
            raise _dependency_failure(exc) from exc
        except (OSError, ValueError) as exc:
            raise LifecycleError.stable_failure(
                code="stack_preservation_capture_failed",
                cause="The pre-operation preservation manifest could not be captured.",
                remediation="Repair the reported observation before changing the runtime.",
                step="capture_before",
            ) from exc

    def _apply(self, *, output_sink: TechnicalSink, heartbeat: HeartbeatSink) -> None:
        method = getattr(self.docker, self.context.action)
        self.mutated = True
        try:
            method(
                self.context.target,
                self.context.target_services,
                timeout_seconds=self.health_timeout_seconds,
                output_sink=output_sink,
                heartbeat=heartbeat,
            )
        except StackGatewayError as exc:
            self._recover_and_raise(exc.detail, output_sink=output_sink, heartbeat=heartbeat)

    def _verify_runtime(
        self,
        *,
        output_sink: TechnicalSink,
        heartbeat: HeartbeatSink,
    ) -> None:
        try:
            self.after_state = self.docker.status(self.context.target)
        except StackGatewayError as exc:
            self._recover_and_raise(exc.detail, output_sink=output_sink, heartbeat=heartbeat)
            return
        if not _runtime_postcondition(
            self.context.action,
            self.after_state,
            self.context.target_services,
        ):
            self._recover_and_raise(
                "The selected runtime did not reach its required state.",
                output_sink=output_sink,
                heartbeat=heartbeat,
            )
        if self.context.action != "stop":
            self._reconcile_readiness(output_sink=output_sink, heartbeat=heartbeat)
        if self.context.action != "stop" and not self._readiness_holds():
            failed = ", ".join(self.last_readiness_failed_ids)
            detail = (
                f"Application readiness failed: {failed}."
                if failed
                else "Application, database, or artifact storage readiness was not proven."
            )
            self._recover_and_raise(
                detail,
                output_sink=output_sink,
                heartbeat=heartbeat,
            )

    def _reconcile_readiness(
        self,
        *,
        output_sink: TechnicalSink,
        heartbeat: HeartbeatSink,
    ) -> None:
        if self.readiness_reconciled or self.context.readiness_reconciler is None:
            return
        self.readiness_reconciled = True
        try:
            self.context.readiness_reconciler(float(self.health_timeout_seconds))
        except Exception as exc:
            self._recover_and_raise(
                f"Maintenance readiness reconciliation failed: {type(exc).__name__}.",
                output_sink=output_sink,
                heartbeat=heartbeat,
            )

    def _capture_after(
        self,
        *,
        output_sink: TechnicalSink,
        heartbeat: HeartbeatSink,
    ) -> None:
        try:
            self.after_manifest = self._capture_manifest("after")
            self.manifest_store.save(self.after_manifest)
        except (OSError, ValueError):
            self._recover_and_raise(
                "The post-operation preservation manifest could not be captured.",
                output_sink=output_sink,
                heartbeat=heartbeat,
            )

    def _verify_preservation(
        self,
        definition: LifecycleOperationDefinition,
        *,
        output_sink: TechnicalSink,
        heartbeat: HeartbeatSink,
    ) -> None:
        if self.before_manifest is None or self.after_manifest is None:
            raise LifecycleError.internal(
                step="verify_preservation",
                recovery_command=definition.recovery_command,
            )
        if self.context.action == "start" and self.context.rollback_snapshot is not None:
            self._verify_start_image_identity(output_sink=output_sink, heartbeat=heartbeat)
        try:
            comparison = self.preservation.compare(
                self.before_manifest,
                self.after_manifest,
                operation=definition.kind,
            )
        except ValueError:
            self._recover_and_raise(
                "The post-operation preservation comparison could not be completed.",
                output_sink=output_sink,
                heartbeat=heartbeat,
            )
        if not comparison.success:
            self._recover_and_raise(
                "Preservation postconditions were not proven.",
                output_sink=output_sink,
                heartbeat=heartbeat,
            )
        self.preservation_verified = True

    def _verify_start_image_identity(
        self,
        *,
        output_sink: TechnicalSink,
        heartbeat: HeartbeatSink,
    ) -> None:
        """Hold the recorded image identity unless the operator declared a change."""
        snapshot = self.context.rollback_snapshot
        if snapshot is None:  # pragma: no cover - guarded by the caller
            return
        try:
            drift = self.docker.runtime_image_drift(self.context.target, snapshot)
        except StackGatewayError:
            self._recover_and_raise(
                "The running container image identity could not be verified.",
                output_sink=output_sink,
                heartbeat=heartbeat,
            )
        self.image_transitions = drift
        if not drift:
            return
        described = "; ".join(item.describe() for item in drift)
        if not self.context.adopt_image_pins:
            self._recover_and_raise(
                "The running images differ from the recorded runtime: "
                f"{described}. Stack start preserves the running image identity; "
                f"declare an image change with {_ADOPT_IMAGE_PINS_COMMAND}.",
                output_sink=output_sink,
                heartbeat=heartbeat,
            )
        try:
            configured = self.docker.configured_image_identities(self.context.target)
        except StackGatewayError:
            self._recover_and_raise(
                "The configured image pins could not be resolved to exact identities.",
                output_sink=output_sink,
                heartbeat=heartbeat,
            )
        undeclared = tuple(
            item
            for item in drift
            if not item.observed_image_id
            or str(configured.get(item.service, "")) != item.observed_image_id
        )
        if undeclared:
            self._recover_and_raise(
                "The running images left the recorded runtime without matching the "
                "configured image pins: " + "; ".join(item.describe() for item in undeclared) + ".",
                output_sink=output_sink,
                heartbeat=heartbeat,
            )
        for item in drift:
            output_sink(f"adopted the configured image pin: {item.describe()}")

    def _restore_mismatches(self, snapshot: StackRollbackSnapshot) -> tuple[str, ...]:
        """Ask the gateway which service left its recorded topology or image."""
        try:
            return self.docker.restore_mismatches(self.context.target, snapshot)
        except StackGatewayError as exc:
            return (f"the restored runtime could not be re-observed: {exc.detail}",)

    def _release_rollback_pins(self, output_sink: TechnicalSink) -> None:
        """Release the temporary rollback pins once verification can no longer need them."""
        snapshot = self.context.rollback_snapshot
        if snapshot is None or not self.rollback_pins_pending:
            return
        self.rollback_pins_pending = False
        try:
            retained = self.docker.release_rollback_pins(snapshot)
        except StackGatewayError as exc:
            output_sink(f"temporary rollback pins could not be released: {exc.detail}")
            return
        for reference in retained:
            output_sink(f"retained temporary rollback pin {reference}")

    def observed_image_drift(self) -> tuple[StackImageTransition, ...]:
        """Report start-time image drift without failing when it cannot be read.

        Only ``start`` promises an unchanged image identity, so only ``start``
        can explain a failure through drift or point at a declared redeploy.
        """
        snapshot = self.context.rollback_snapshot
        if snapshot is None or self.context.action != "start":
            return ()
        try:
            return self.docker.runtime_image_drift(self.context.target, snapshot)
        except StackGatewayError:
            return ()

    def _capture_manifest(
        self,
        stage: Literal["before", "after"],
    ) -> PreservationManifest:
        return self.preservation.capture(
            operation_id=self.context.operation_id,
            stage=stage,
            compose_project=self.context.target.project_name,
            release=self.context.release,
            environment=self.context.config.environment,
            storage_backend=self.context.config.file_storage,
            storage_connectivity=self.context.storage_connectivity_provider(
                self.context.config,
                self.context.target,
            ),
        )

    def _recover_and_raise(
        self,
        detail: str,
        *,
        output_sink: TechnicalSink,
        heartbeat: HeartbeatSink,
    ) -> NoReturn:
        safe_detail = redact_installer_text(str(detail))[:2000]
        failed_readiness_ids = list(self.last_readiness_failed_ids)
        snapshot = self.context.rollback_snapshot
        restored = False
        if self.mutated and snapshot is not None:
            try:
                self.rollback_pins_pending = True
                restored = self.docker.restore(
                    self.context.target,
                    action=self.context.action,
                    snapshot=snapshot,
                    timeout_seconds=self.health_timeout_seconds,
                    output_sink=output_sink,
                    heartbeat=heartbeat,
                )
                if restored and _snapshot_requires_readiness(snapshot):
                    restored = self._readiness_holds()
                if restored:
                    restored = self._preservation_holds_after_recovery()
            except StackGatewayError:
                restored = False
            finally:
                # Only after every check that could resolve the rollback override.
                self._release_rollback_pins(output_sink)
        if restored:
            raise LifecycleError.stable_failure(
                code=f"stack_{self.context.action}_failed",
                cause=(
                    f"Stack {self.context.action} failed; the prior runnable runtime was restored. "
                    f"Primary failure: {safe_detail}"
                ),
                remediation="Inspect the operation log, correct the reported cause, and retry.",
                step="recovery",
                details={
                    "primary_failure": safe_detail,
                    "failed_readiness_ids": failed_readiness_ids,
                },
            )
        raise self._recovery_required(
            cause=(
                f"Stack {self.context.action} failed and prior runtime recovery was not verified."
            ),
            detail=safe_detail,
            failed_readiness_ids=failed_readiness_ids,
        )

    def _recovery_required(
        self,
        *,
        cause: str,
        detail: str,
        failed_readiness_ids: list[str] | None = None,
    ) -> LifecycleError:
        """Name the observed cause and one next action that is not this same command."""
        recovery_command = self.docker.manual_recovery_command(self.context.action)
        drift = self.observed_image_drift()
        unavailable = tuple(item for item in drift if not item.expected_image_available)
        details: dict[str, object] = {
            "primary_failure": detail,
            "failed_readiness_ids": list(failed_readiness_ids or []),
            "image_transitions": [item.describe() for item in drift],
        }
        if not drift:
            return LifecycleError.recovery_required(
                code=f"stack_{self.context.action}_recovery_required",
                cause=cause,
                remediation=f"{detail} Run exactly: {recovery_command}",
                step="recovery",
                recovery_command=recovery_command,
                details=details,
            )
        observed = "; ".join(item.describe() for item in drift)
        remediation = f"{detail} The running images differ from the recorded runtime: {observed}."
        if unavailable:
            missing = ", ".join(item.expected_image_id for item in unavailable)
            remediation += (
                f" The recorded prior image is no longer in the local image store "
                f"({missing}), so this command cannot restore it. Either restore that "
                "exact image into the local store and rerun the recovery command, or "
                f"adopt the configured pins with {_ADOPT_IMAGE_PINS_COMMAND}."
            )
            details["unrestorable_images"] = [item.expected_image_id for item in unavailable]
        else:
            remediation += (
                f" Run exactly: {recovery_command} to restore the recorded images, or "
                f"adopt the configured pins with {_ADOPT_IMAGE_PINS_COMMAND}."
            )
        return LifecycleError.recovery_required(
            code=f"stack_{self.context.action}_recovery_required",
            cause=cause,
            remediation=remediation,
            step="recovery",
            recovery_command=recovery_command,
            details=details,
        )

    def _preservation_holds_after_recovery(self) -> bool:
        if self.before_manifest is None:
            return False
        try:
            recovered = self._capture_manifest("after")
            self.manifest_store.save(recovered)
            comparison = self.preservation.compare(
                self.before_manifest,
                recovered,
                operation={
                    "start": LifecycleOperationKind.STACK_START,
                    "stop": LifecycleOperationKind.STACK_STOP,
                    "restart": LifecycleOperationKind.STACK_RESTART,
                    "rebuild": LifecycleOperationKind.STACK_REBUILD,
                }[self.context.action],
            )
        except (OSError, ValueError):
            return False
        return comparison.success

    def _readiness_holds(self) -> bool:
        attempts = max(
            1,
            int(self.health_timeout_seconds / self.readiness_poll_interval_seconds),
        )
        for attempt in range(attempts):
            self.last_readiness_failed_ids = ()
            if self._readiness_probe_holds():
                return True
            if attempt + 1 < attempts:
                self.readiness_sleep(self.readiness_poll_interval_seconds)
        return False

    def _readiness_probe_holds(self) -> bool:
        try:
            report = self.context.readiness_service.check()
        except Exception as exc:
            self.last_readiness_failed_ids = (f"readiness_probe:{type(exc).__name__}",)
            return False
        core_ready = (
            str(getattr(report, "status", "")).lower() == "ready"
            and int(getattr(report, "core_failed", 1)) == 0
        )
        if not core_ready:
            failed_ids: set[str] = set()
            for finding in getattr(report, "findings", ()) or ():
                raw_area = getattr(finding, "area", "")
                area = str(getattr(raw_area, "value", raw_area)).strip().lower()
                raw_status = getattr(finding, "status", "")
                status = str(getattr(raw_status, "value", raw_status)).strip().lower()
                check_id = str(getattr(finding, "check_id", "")).strip()
                if area == "core" and status in {"fail", "failed"} and check_id:
                    failed_ids.add(check_id)
            self.last_readiness_failed_ids = tuple(sorted(failed_ids))
            return False
        try:
            storage = self.context.storage_connectivity_provider(
                self.context.config,
                self.context.target,
            )
        except Exception as exc:
            self.last_readiness_failed_ids = (f"storage_connectivity:{type(exc).__name__}",)
            return False
        if storage is not True:
            self.last_readiness_failed_ids = ("storage_connectivity",)
            return False
        return True

    def recover(
        self,
        *,
        output_sink: TechnicalSink,
        heartbeat: HeartbeatSink,
    ) -> StackRuntimeState:
        snapshot = self.context.rollback_snapshot
        if snapshot is None or self.before_manifest is None:
            raise _journal_recovery_required(
                action=self.context.action,
                operation_id=self.context.operation_id,
                cause="Exact rollback evidence is unavailable.",
            )
        # Derived readiness goes stale while maintenance holds the control loops, so
        # reconcile before polling — otherwise recovery fails on staleness it caused.
        self._reconcile_readiness(output_sink=output_sink, heartbeat=heartbeat)
        try:
            self._run_recovery_substeps(
                snapshot,
                output_sink=output_sink,
                heartbeat=heartbeat,
            )
        except (StackGatewayError, OSError, ValueError) as exc:
            detail = redact_installer_text(getattr(exc, "detail", None) or str(exc))[:2000]
            output_sink(f"recovery failed: {detail}")
            raise self._recovery_required(
                cause="Exact prior runtime recovery was not verified.",
                detail=f"Inspect the operation log. Observed: {detail}",
                failed_readiness_ids=list(self.last_readiness_failed_ids),
            ) from exc
        finally:
            self._release_rollback_pins(output_sink)
        if self.after_state is None:  # pragma: no cover - set by the substeps above
            raise LifecycleError.internal(
                step="recovery",
                recovery_command=self.docker.manual_recovery_command(self.context.action),
            )
        self.preservation_verified = True
        return self.after_state

    def _run_recovery_substeps(
        self,
        snapshot: StackRollbackSnapshot,
        *,
        output_sink: TechnicalSink,
        heartbeat: HeartbeatSink,
    ) -> None:
        """Run each recovery postcondition so the log names the one that failed."""
        if self.before_manifest is None:  # pragma: no cover - guarded by the caller
            raise ValueError("Pre-operation preservation evidence is unavailable.")
        output_sink("recovery step: restore exact prior images and topology")
        self.rollback_pins_pending = True
        if not self.docker.restore(
            self.context.target,
            action=self.context.action,
            snapshot=snapshot,
            timeout_seconds=self.health_timeout_seconds,
            output_sink=output_sink,
            heartbeat=heartbeat,
        ):
            named = self._restore_mismatches(snapshot)
            detail = "Exact image and topology recovery was not proven."
            if named:
                detail = f"{detail} {'; '.join(named)}."
            raise StackGatewayError(action="recovery", detail=detail)
        if _snapshot_requires_readiness(snapshot):
            output_sink("recovery step: prove application, database, and storage readiness")
            if not self._readiness_holds():
                failed = ", ".join(self.last_readiness_failed_ids) or "no check reported an id"
                raise StackGatewayError(
                    action="recovery",
                    detail=f"Readiness was not proven after the exact restore: {failed}.",
                )
        output_sink("recovery step: compare preservation evidence")
        self.after_state = self.docker.status(self.context.target)
        self.after_manifest = self._capture_manifest("after")
        self.manifest_store.save(self.after_manifest)
        comparison = self.preservation.compare(
            self.before_manifest,
            self.after_manifest,
            operation={
                "start": LifecycleOperationKind.STACK_START,
                "stop": LifecycleOperationKind.STACK_STOP,
                "restart": LifecycleOperationKind.STACK_RESTART,
                "rebuild": LifecycleOperationKind.STACK_REBUILD,
            }[self.context.action],
        )
        if not comparison.success:
            failed = ", ".join(failure.field for failure in comparison.failures)
            raise StackGatewayError(
                action="recovery",
                detail=f"Preservation recovery was not proven: {failed}.",
            )
        output_sink("recovery completed: exact prior runtime restored and preservation proven")


def _operation_steps(
    action: Literal["start", "stop", "restart", "rebuild"],
) -> tuple[LifecycleOperationStep, ...]:
    return (
        LifecycleOperationStep(
            step_id="capture_before",
            label="Capture pre-operation preservation state",
        ),
        LifecycleOperationStep(
            step_id="apply_action",
            label=f"{action.title()} selected runtime services",
            safe_boundary=False,
        ),
        LifecycleOperationStep(
            step_id="verify_runtime",
            label="Verify selected runtime state",
        ),
        LifecycleOperationStep(
            step_id="capture_after",
            label="Capture post-operation preservation state",
        ),
        LifecycleOperationStep(
            step_id="verify_preservation",
            label="Verify preserved project and volume state",
        ),
    )


def _runtime_postcondition(
    action: str,
    state: StackRuntimeState,
    services: tuple[str, ...],
) -> bool:
    by_service = {item.service: item for item in state.services}
    if action == "stop":
        return all(
            service not in by_service or by_service[service].state.lower() != "running"
            for service in services
        )
    return all(
        service in by_service
        and by_service[service].state.lower() == "running"
        and by_service[service].health.lower() not in {"starting", "unhealthy"}
        for service in services
    )


def _snapshot_requires_readiness(snapshot: StackRollbackSnapshot) -> bool:
    running = {item.service for item in snapshot.services if item.topology == "running"}
    return {"fs-app", "pg-fs"}.issubset(running)


def _dependency_failure(exc: StackGatewayError) -> LifecycleError:
    return LifecycleError.dependency(
        code="docker_lifecycle_unavailable",
        cause=exc.detail,
        remediation="Verify Docker Compose is available and the installed files are readable.",
        step=exc.action,
    )


def _recovery_step_output(journal: LifecycleOperationJournal) -> dict[str, Any]:
    recorded = journal.step_outputs.get("recovery")
    return dict(recorded) if isinstance(recorded, dict) else {}


def _recovery_attempt_record(journal: LifecycleOperationJournal) -> dict[str, Any]:
    recorded = _recovery_step_output(journal)
    attempts = recorded.get("attempts")
    return {
        **recorded,
        "attempts": (attempts if isinstance(attempts, int) else 0) + 1,
        "last_attempt_at": datetime.now(UTC).isoformat(),
    }


def _journal_recovery_required(
    *,
    action: str,
    operation_id: str,
    cause: str,
    recovery_command: str | None = None,
) -> LifecycleError:
    command = recovery_command or f"./flow-steward stack {action} --confirm"
    failure = LifecycleError.recovery_required(
        code="stack_operation_recovery_required",
        cause=cause,
        remediation=f"Inspect the journal, then run exactly: {command}",
        step="recovery",
        recovery_command=command,
    )
    failure.operation_id = operation_id
    return failure


def _journal_ambiguity_required(
    *,
    action: str,
    journals: tuple[LifecycleOperationJournal, ...],
) -> LifecycleError:
    paths = ", ".join(
        sorted(f".flow-steward/operations/{journal.operation_id}.json" for journal in journals)
    )
    operator_action = (
        f"operator: reconcile lifecycle journals {paths}; retain exactly one unfinished journal"
    )
    failure = _journal_recovery_required(
        action=action,
        operation_id=journals[0].operation_id,
        cause="Multiple unfinished lifecycle journals require operator reconciliation.",
        recovery_command=operator_action,
    )
    failure.remediation = (
        f"{operator_action}. Then run exactly: ./flow-steward stack {action} --confirm"
    )
    return failure
