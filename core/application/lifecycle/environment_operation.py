"""Durable environment operation steps, postconditions and rollback execution."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from core.application.lifecycle.backup_service import (
    BackupCreateRequest,
    BackupResult,
    BackupVerificationStatus,
)
from core.application.lifecycle.contracts import (
    CancellationToken,
    ComposeTarget,
    DeploymentConfig,
    LifecycleOperationDefinition,
    LifecycleOperationStep,
)
from core.application.lifecycle.environment_storage_policy import (
    _artifact_decision_required,
    _storage_decision_required,
)
from core.application.lifecycle.errors import LifecycleError
from core.application.lifecycle.ports import HeartbeatSink, TechnicalSink
from core.application.lifecycle.stack_service import StackGatewayError

if TYPE_CHECKING:
    from core.application.lifecycle.environment_service import (
        EnvironmentService,
        EnvironmentSwitchRequest,
        StorageInventory,
    )


class _EnvironmentContext:
    def __init__(
        self,
        *,
        operation_id: str,
        request: EnvironmentSwitchRequest,
        previous: DeploymentConfig,
        candidate: DeploymentConfig,
        previous_target: ComposeTarget,
        target: ComposeTarget,
        prepared: tuple[ComposeTarget, Path, dict[str, str]],
        source_release: str,
        target_storage_identity: str,
        source_storage_identity: str,
        storage_changed: bool,
        probe_target_storage: bool,
        durable_completed_steps: tuple[str, ...],
        durable_step_outputs: Mapping[str, object],
    ) -> None:
        self.operation_id = operation_id
        self.request = request
        self.previous = previous
        self.candidate = candidate
        self.previous_target = previous_target
        self.target = target
        self.prepared = prepared
        self.source_release = source_release
        self.target_storage_identity = target_storage_identity
        self.source_storage_identity = source_storage_identity
        self.storage_changed = storage_changed
        self.probe_target_storage = probe_target_storage
        self.durable_completed_steps = frozenset(durable_completed_steps)
        self.durable_step_outputs = dict(durable_step_outputs)
        self.runtime_snapshot: Mapping[str, object] | None = None
        self.before_manifest: Mapping[str, object] | None = None
        self.rendered_path: Path | None = None


class _EnvironmentOperationGateway:
    def __init__(self, *, service: EnvironmentService, context: _EnvironmentContext) -> None:
        self.service = service
        self.context = context
        self.jobs_quiesced = False
        self.mutated = False
        self.after_manifest: Mapping[str, object] | None = None
        self.backup_result: BackupResult | None = None
        self.storage_inventory: StorageInventory | None = None
        self.preservation_verified = False
        self.target_release = context.source_release
        self.credential_provisioning_evidence: Mapping[str, object] = {}

    def postcondition_holds(
        self,
        _definition: LifecycleOperationDefinition,
        step: LifecycleOperationStep,
    ) -> bool:
        if step.step_id == "prepare_target_images":
            return False
        if step.step_id == "quiesce_jobs":
            return self.service.jobs.pause_owned(self.context.operation_id)
        if step.step_id == "capture_before":
            return bool(
                self.context.runtime_snapshot is not None
                and self.context.before_manifest is not None
                and self.service.runtime.recovery_available(self.context.operation_id)
            )
        if step.step_id == "create_backup":
            self.backup_result = self.service.backup.find_verified(self.context.operation_id)
            return self.backup_result is not None
        if step.step_id == "provision_storage_credentials":
            return self.service.storage.provisioning_matches(self.context.candidate)
        if step.step_id == "render_target":
            return self.service.configuration.transition_environment_matches(
                self.context.previous,
                self.context.candidate,
                self.context.prepared,
            )
        if step.step_id == "install_storage_credentials":
            return self.service.storage.credentials_match(
                self.context.candidate,
                self.context.prepared[1],
            )
        if step.step_id == "apply_runtime_artifacts":
            return self.service.runtime.artifacts_match(self.context.candidate)
        if step.step_id == "activate_target":
            try:
                holds = self.service.runtime.healthy(
                    self.context.candidate,
                    self.context.target,
                ) and (
                    self.service.runtime.runtime_release(
                        self.context.candidate,
                        self.context.target,
                    )
                    == self.context.source_release
                )
            except (OSError, RuntimeError, ValueError):
                holds = False
            self.mutated = holds or step.step_id in self.context.durable_completed_steps
            return holds
        if step.step_id == "verify_health":
            try:
                self.target_release = self.service.runtime.runtime_release(
                    self.context.candidate,
                    self.context.target,
                )
                return (
                    self.target_release == self.context.source_release
                    and self.service.runtime.healthy(
                        self.context.candidate,
                        self.context.target,
                    )
                )
            except (OSError, RuntimeError, ValueError):
                return False
        if step.step_id == "capture_after":
            try:
                self.after_manifest = self.service.runtime.load_manifest(
                    self.context.operation_id,
                    "after",
                )
            except (OSError, ValueError):
                return False
            return self.after_manifest is not None
        if step.step_id == "verify_preservation":
            if self.context.before_manifest is None or self.after_manifest is None:
                return False
            try:
                self.service.runtime.compare_environment_manifests(
                    self.context.before_manifest,
                    self.after_manifest,
                )
            except (OSError, RuntimeError, ValueError):
                return False
            self.preservation_verified = True
            return True
        if step.step_id == "mark_active":
            try:
                return self.service.configuration.active_publication_matches(
                    self.context.candidate,
                    self.context.prepared,
                )
            except Exception:
                return False
        if step.step_id == "resume_jobs":
            return not self.service.jobs.pause_owned(self.context.operation_id)
        return False

    def step_output(
        self,
        _definition: LifecycleOperationDefinition,
        step: LifecycleOperationStep,
    ) -> dict[str, object]:
        if step.step_id == "quiesce_jobs":
            return {
                "owner_operation_id": self.context.operation_id,
                "previously_paused": not self.jobs_quiesced,
            }
        if step.step_id == "capture_before":
            return {
                "recovery_snapshot_path": str(
                    self.service.lifecycle_state.previous_config_path(self.context.operation_id)
                ),
                "manifest_checksum": _mapping_checksum(self.context.before_manifest),
            }
        if step.step_id == "create_backup" and self.backup_result is not None:
            return {
                "path": self.backup_result.path,
                "size_bytes": self.backup_result.size_bytes,
                "evidence_checksum": _mapping_checksum(self.backup_result.model_dump(mode="json")),
            }
        if step.step_id == "provision_storage_credentials":
            return dict(self.credential_provisioning_evidence)
        if step.step_id == "render_target":
            path = self.context.rendered_path or self.context.prepared[1]
            return {
                "path": str(path),
                "sha256": _path_checksum(path),
            }
        if step.step_id == "install_storage_credentials":
            return dict(
                self.service.storage.credential_evidence(
                    self.context.candidate,
                    self.context.prepared[1],
                )
            )
        if step.step_id == "apply_runtime_artifacts":
            return {
                "candidate_checksum": _mapping_checksum(
                    self.context.candidate.model_dump(mode="json")
                ),
                "postcondition": self.service.runtime.artifacts_match(self.context.candidate),
            }
        if step.step_id in {"activate_target", "verify_health"}:
            return {
                "target_environment": self.context.candidate.environment,
                "release": self.target_release,
            }
        if step.step_id == "capture_after":
            return {"manifest_checksum": _mapping_checksum(self.after_manifest)}
        return {}

    def execute_step(
        self,
        definition: LifecycleOperationDefinition,
        step: LifecycleOperationStep,
        *,
        output_sink: TechnicalSink,
        heartbeat: HeartbeatSink,
        cancellation: CancellationToken,
    ) -> None:
        del output_sink
        try:
            self._execute(step.step_id, heartbeat=heartbeat, cancellation=cancellation)
        except LifecycleError as exc:
            self._handle_failure(exc)
        except StackGatewayError as exc:
            failure = LifecycleError.stable_failure(
                code=f"environment_{step.step_id}_failed",
                cause=exc.detail,
                remediation=(
                    "Inspect the lifecycle log, correct the reported dependency, and retry."
                ),
                step=step.step_id,
            )
            self._handle_failure(failure, cause=exc)
        except (OSError, RuntimeError, ValueError) as exc:
            failure = LifecycleError.stable_failure(
                code=f"environment_{step.step_id}_failed",
                cause="The environment transition gate failed.",
                remediation="Inspect the lifecycle log, correct the reported dependency, and retry.",
                step=step.step_id,
            )
            self._handle_failure(failure, cause=exc)
        except Exception as exc:
            failure = LifecycleError.internal(
                step=step.step_id,
                recovery_command=definition.recovery_command,
            )
            self._handle_failure(failure, cause=exc)
        except BaseException as exc:
            if not self.mutated:
                raise
            failure = LifecycleError.interrupted(
                step=step.step_id,
                recovery_command=definition.recovery_command,
                forced=True,
            )
            self._handle_failure(failure, cause=exc)

    def _execute(
        self,
        step_id: str,
        *,
        heartbeat: HeartbeatSink,
        cancellation: CancellationToken,
    ) -> None:
        if step_id == "prepare_target_images":
            self.service.runtime.prepare_target_images(
                self.context.candidate,
                self.context.target,
                self.context.prepared[2],
            )
            return
        if step_id == "quiesce_jobs":
            self.service.jobs.stop_accepting(self.context.operation_id)
            self.jobs_quiesced = True
            self.service.jobs.wait_for_idle(
                cancellation=cancellation,
                heartbeat=heartbeat,
            )
            return
        if step_id == "capture_before":
            if self.context.runtime_snapshot is None:
                self.context.runtime_snapshot = self.service.runtime.capture_recovery(
                    self.context.previous_target,
                    self.context.target,
                    operation_id=self.context.operation_id,
                )
            if self.context.before_manifest is None:
                self.context.before_manifest = self.service.runtime.capture_manifest(
                    self.context.previous,
                    self.context.previous_target,
                    operation_id=self.context.operation_id,
                    stage="before",
                )
            self.service.lifecycle_state.save_previous_config(
                self.context.operation_id,
                {
                    "configuration": self.context.previous.model_dump(mode="json"),
                    "runtime": dict(self.context.runtime_snapshot),
                },
            )
            return
        if step_id == "validate_preservation_baseline":
            if self.context.before_manifest is None:
                raise ValueError("Pre-transition preservation evidence is unavailable")
            self.service.runtime.validate_preservation_baseline(
                self.context.before_manifest,
            )
            return
        if step_id == "create_backup":
            self.backup_result = self.service.backup.create(
                BackupCreateRequest(operation_id=self.context.operation_id)
            )
            if (
                self.backup_result.verification_status is not BackupVerificationStatus.VERIFIED
                or self.backup_result.size_bytes <= 0
            ):
                raise LifecycleError.dependency(
                    code="environment_backup_unverified",
                    cause="The protected PostgreSQL backup was not verified.",
                    remediation="Repair backup configuration and retry.",
                    step="create_backup",
                )
            return
        if step_id == "provision_storage_credentials":
            self.mutated = True
            self.credential_provisioning_evidence = self.service.storage.provision_credentials(
                self.context.candidate
            )
            environment = self.service.storage.candidate_environment(self.context.candidate, {})
            prepared = self.service.configuration.prepare_transition_environment(
                self.context.previous,
                self.context.candidate,
                environment,
            )
            if prepared[0] != self.context.target or prepared[1] != self.context.prepared[1]:
                raise ValueError("Provisioning changed the selected target identity")
            self.context.prepared = prepared
            return
        if step_id == "validate_target":
            self.service.runtime.validate_target(
                self.context.candidate,
                self.context.target,
                self.context.prepared[2],
                managed_target=self.context.previous_target,
            )
            return
        if step_id == "inventory_storage":
            self.storage_inventory = self.service.storage.inventory(self.context.previous)
            return
        if step_id == "require_storage_decision":
            if self.storage_inventory is None:
                raise ValueError("Source storage inventory is unavailable")
            if (
                _storage_decision_required(self.storage_inventory)
                and self.context.request.artifact_decision is None
            ):
                raise _artifact_decision_required()
            return
        if step_id == "probe_target_storage":
            self.mutated = True
            self.service.storage.probe_write(
                self.context.candidate,
                self.context.prepared[2],
            )
            if cancellation.requested or cancellation.forced:
                raise LifecycleError.interrupted(
                    step=step_id,
                    recovery_command=(
                        "./flow-steward environment switch "
                        + self.context.previous.environment
                        + " --confirm"
                    ),
                    forced=cancellation.forced,
                )
            return
        if step_id == "render_target":
            self.mutated = True
            path = self.service.configuration.stage_transition_environment(
                self.context.previous,
                self.context.candidate,
                self.context.prepared,
            )
            self.context.rendered_path = path
            return
        if step_id == "install_storage_credentials":
            self.mutated = True
            self.service.storage.install_credentials(
                self.context.candidate,
                self.context.prepared[1],
            )
            return
        if step_id == "apply_runtime_artifacts":
            self.mutated = True
            self.service.runtime.apply_artifacts(
                self.context.candidate,
                operation_id=self.context.operation_id,
            )
            return
        if step_id == "activate_target":
            self.mutated = True
            if self.context.runtime_snapshot is None:
                raise ValueError("Exact runtime recovery inventory is unavailable")
            self.service.runtime.activate(
                self.context.previous,
                self.context.candidate,
                self.context.previous_target,
                self.context.target,
                self.context.runtime_snapshot,
            )
            return
        if step_id == "verify_health":
            if not self.service.runtime.healthy(
                self.context.candidate,
                self.context.target,
            ):
                raise RuntimeError("Target runtime health was not proven")
            self.target_release = self.service.runtime.runtime_release(
                self.context.candidate,
                self.context.target,
            )
            if self.target_release != self.context.source_release:
                raise RuntimeError("Target runtime release differs from the source release")
            return
        if step_id == "capture_after":
            self.after_manifest = self.service.runtime.capture_manifest(
                self.context.candidate,
                self.context.target,
                operation_id=self.context.operation_id,
                stage="after",
            )
            return
        if step_id == "verify_preservation":
            if self.context.before_manifest is None or self.after_manifest is None:
                raise ValueError("Preservation manifests are incomplete")
            self.service.runtime.compare_environment_manifests(
                self.context.before_manifest,
                self.after_manifest,
            )
            self.preservation_verified = True
            return
        if step_id == "mark_active":
            self.service.configuration.mark_active(
                self.context.previous,
                self.context.candidate,
                self.context.prepared,
            )
            return
        if step_id == "resume_jobs":
            self.service.jobs.resume_accepting(self.context.operation_id)
            self.jobs_quiesced = False
            return
        raise ValueError(f"Unknown environment step: {step_id}")

    def _handle_failure(
        self,
        failure: LifecycleError,
        *,
        cause: BaseException | None = None,
    ) -> NoReturn:
        if not self.mutated:
            if self.jobs_quiesced:
                try:
                    self.service.jobs.resume_accepting(self.context.operation_id)
                    self.jobs_quiesced = False
                except (OSError, RuntimeError, ValueError) as resume_exc:
                    raise LifecycleError.recovery_required(
                        code="environment_recovery_required",
                        cause="Job admission could not be restored after a failed preflight.",
                        remediation="Restore job admission before retrying.",
                        step="recovery",
                        recovery_command="flow-steward environment show",
                    ) from resume_exc
            raise failure from cause
        if self._restore_previous():
            if failure.code in {"lifecycle_cancelled", "lifecycle_forced_interrupt"}:
                raise failure from cause
            raise LifecycleError.stable_failure(
                code="environment_switch_failed",
                cause="The target environment failed; the exact previous runtime was restored.",
                remediation="Inspect the lifecycle log, correct the target, and retry.",
                step="recovery",
            ) from cause or failure
        raise LifecycleError.recovery_required(
            code="environment_recovery_required",
            cause=(
                "The target environment failed and exact previous runtime recovery was not "
                f"verified. Original failure: {failure.cause}"
            ),
            remediation="Inspect the lifecycle journal and restore the previous runtime.",
            step="recovery",
            recovery_command="flow-steward environment switch " + self.context.previous.environment,
        ) from cause or failure

    def _restore_previous(self) -> bool:
        snapshot = self.context.runtime_snapshot
        if snapshot is None or self.context.before_manifest is None:
            return False
        try:
            restored = self.service.runtime.restore_recovery(
                self.context.previous_target,
                operation_id=self.context.operation_id,
                snapshot=snapshot,
            )
            if not restored or not self.service.runtime.healthy(
                self.context.previous,
                self.context.previous_target,
            ):
                return False
            recovered = self.service.runtime.capture_manifest(
                self.context.previous,
                self.context.previous_target,
                operation_id=self.context.operation_id,
                stage="after",
            )
            self.service.runtime.compare_environment_manifests(
                self.context.before_manifest,
                recovered,
            )
            if self.jobs_quiesced:
                self.service.jobs.resume_accepting(self.context.operation_id)
                self.jobs_quiesced = False
            return True
        except Exception:
            return False


def _mapping_checksum(value: object) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _path_checksum(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""
