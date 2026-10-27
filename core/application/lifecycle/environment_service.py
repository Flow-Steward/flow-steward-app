"""Safe environment transitions with explicit storage availability decisions."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.application.lifecycle.backup_service import (
    BackupService,
)
from core.application.lifecycle.contracts import (
    CancellationToken,
    ComposeTarget,
    DeploymentConfig,
    LifecycleLockStatus,
    LifecycleOperationDefinition,
    LifecycleOperationJournal,
    LifecycleOperationKind,
    LifecycleOperationStatus,
    LifecycleOperationStep,
)
from core.application.lifecycle.environment_operation import (
    _EnvironmentContext,
    _EnvironmentOperationGateway,
)
from core.application.lifecycle.environment_storage_policy import (
    _artifact_decision_required,
    _storage_decision_required,
)
from core.application.lifecycle.errors import LifecycleError
from core.application.lifecycle.operation_service import LifecycleOperationRunner
from core.application.lifecycle.ports import (
    HeartbeatSink,
    LifecycleState,
    ProgressSink,
    TechnicalSink,
)
from core.application.management.command_specs.contracts import OperationStepStatus

DevelopmentTool = Literal["test_runner", "audit_tools", "sonarqube"]
ArtifactDecision = Literal["already_copied", "accept_unavailable"]
_COMPLETE_DEVELOPMENT_TOOLS: tuple[DevelopmentTool, ...] = (
    "test_runner",
    "audit_tools",
    "sonarqube",
)


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EnvironmentShowRequest(_ClosedModel):
    pass


class EnvironmentSwitchRequest(_ClosedModel):
    target: Literal["development", "production"]
    development_tools: tuple[DevelopmentTool, ...] = ()
    file_storage: Literal["keep", "bundled", "external_s3"] = "keep"
    s3_endpoint: str = ""
    s3_bucket: str = ""
    s3_region: str = ""
    s3_credentials_file: str = ""
    allow_insecure_s3_http: bool = False
    artifact_decision: ArtifactDecision | None = None

    @model_validator(mode="after")
    def validate_conditional_inputs(self) -> EnvironmentSwitchRequest:
        if self.target == "production" and self.development_tools:
            raise ValueError("Production cannot enable development tools")
        if len(self.development_tools) != len(set(self.development_tools)):
            raise ValueError("Development tools must be unique")
        if self.file_storage == "external_s3":
            missing = [
                name
                for name in (
                    "s3_endpoint",
                    "s3_bucket",
                    "s3_region",
                    "s3_credentials_file",
                )
                if not str(getattr(self, name)).strip()
            ]
            if missing:
                raise ValueError("external_s3 requires: " + ", ".join(missing))
        elif any(
            (
                self.s3_endpoint,
                self.s3_bucket,
                self.s3_region,
                self.s3_credentials_file,
                self.allow_insecure_s3_http,
            )
        ):
            raise ValueError("S3 inputs require file_storage=external_s3")
        return self


class StorageInventory(_ClosedModel):
    backend: Literal["local_fs", "bundled", "external_s3"]
    approximate_count: int | None = Field(default=None, ge=0)
    approximate_size_bytes: int | None = Field(default=None, ge=0)
    source_identity: str = Field(min_length=1)
    completeness: Literal["complete", "partial", "unknown"]

    @model_validator(mode="after")
    def validate_completeness(self) -> StorageInventory:
        if self.completeness == "unknown":
            if self.approximate_count is not None or self.approximate_size_bytes is not None:
                raise ValueError("Unknown inventory must not synthesize count or size")
        elif self.approximate_count is None or self.approximate_size_bytes is None:
            raise ValueError("Known inventory requires approximate count and size")
        return self


class EnvironmentResult(_ClosedModel):
    status: Literal["active", "already_active", "recovered"] = "active"
    active: DeploymentConfig
    active_environment: Literal["development", "production"]
    source_environment: Literal["development", "production"] | None = None
    source_release: str
    target_release: str
    operation_id: str | None = None
    journal_path: str | None = None
    log_path: str | None = None
    backup_path: str | None = None
    storage_inventory: StorageInventory | None = None
    artifact_decision: ArtifactDecision | None = None
    target_storage_identity: str | None = None
    preservation_verified: bool = False
    runtime_restored: bool = False
    retained_resources: tuple[str, ...] = ()


class EnvironmentConfiguration(Protocol):
    def read_active(self) -> DeploymentConfig: ...

    def derive_target(self, config: DeploymentConfig) -> ComposeTarget: ...

    def prepare_transition_environment(
        self,
        active: DeploymentConfig,
        candidate: DeploymentConfig,
        target_overlay: Mapping[str, str] | None = None,
    ) -> tuple[ComposeTarget, Path, dict[str, str]]: ...

    def stage_transition_environment(
        self,
        active: DeploymentConfig,
        candidate: DeploymentConfig,
        prepared: tuple[ComposeTarget, Path, dict[str, str]],
    ) -> Path: ...

    def transition_environment_matches(
        self,
        active: DeploymentConfig,
        candidate: DeploymentConfig,
        prepared: tuple[ComposeTarget, Path, dict[str, str]],
    ) -> bool: ...

    def mark_active(
        self,
        previous: DeploymentConfig,
        config: DeploymentConfig,
        prepared: tuple[ComposeTarget, Path, dict[str, str]],
    ) -> None: ...

    def active_publication_matches(
        self,
        config: DeploymentConfig,
        prepared: tuple[ComposeTarget, Path, dict[str, str]],
    ) -> bool: ...


class JobQuiescence(Protocol):
    def reconcile_pause(self, unfinished_operation_ids: tuple[str, ...]) -> object | None: ...

    def stop_accepting(self, operation_id: str) -> object: ...

    def pause_owned(self, operation_id: str) -> bool: ...

    def wait_for_idle(
        self,
        *,
        cancellation: CancellationToken,
        heartbeat: HeartbeatSink,
    ) -> None: ...

    def resume_accepting(self, operation_id: str) -> bool: ...


class EnvironmentStorage(Protocol):
    def target_identity(self, config: DeploymentConfig) -> str: ...

    def candidate_environment(
        self,
        config: DeploymentConfig,
        environment: Mapping[str, str],
    ) -> dict[str, str]: ...

    def provision_credentials(self, config: DeploymentConfig) -> Mapping[str, object]: ...

    def provisioning_matches(self, config: DeploymentConfig) -> bool: ...

    def inventory(self, config: DeploymentConfig) -> StorageInventory: ...

    def probe_write(
        self,
        config: DeploymentConfig,
        environment: Mapping[str, str],
    ) -> None: ...

    def install_credentials(self, config: DeploymentConfig, path: Path) -> None: ...

    def credentials_match(self, config: DeploymentConfig, path: Path) -> bool: ...

    def credential_evidence(
        self,
        config: DeploymentConfig,
        path: Path,
    ) -> Mapping[str, object]: ...


class EnvironmentRuntime(Protocol):
    def prepare_target_images(
        self,
        config: DeploymentConfig,
        target: ComposeTarget,
        environment: Mapping[str, str],
    ) -> None: ...

    def validate_target(
        self,
        config: DeploymentConfig,
        target: ComposeTarget,
        environment: Mapping[str, str],
        *,
        managed_target: ComposeTarget,
    ) -> None: ...

    def capture_recovery(
        self,
        previous_target: ComposeTarget,
        candidate_target: ComposeTarget,
        *,
        operation_id: str,
    ) -> Mapping[str, object]: ...

    def recovery_available(self, operation_id: str) -> bool: ...

    def capture_manifest(
        self,
        config: DeploymentConfig,
        target: ComposeTarget,
        *,
        operation_id: str,
        stage: Literal["before", "after"],
    ) -> Mapping[str, object]: ...

    def load_manifest(
        self,
        operation_id: str,
        stage: Literal["before", "after"],
    ) -> Mapping[str, object] | None: ...

    def validate_preservation_baseline(self, manifest: Mapping[str, object]) -> None: ...

    def apply_artifacts(self, config: DeploymentConfig, *, operation_id: str) -> None: ...

    def artifacts_match(self, config: DeploymentConfig) -> bool: ...

    def activate(
        self,
        previous: DeploymentConfig,
        config: DeploymentConfig,
        previous_target: ComposeTarget,
        target: ComposeTarget,
        runtime_snapshot: Mapping[str, object],
    ) -> None: ...

    def healthy(self, config: DeploymentConfig, target: ComposeTarget) -> bool: ...

    def runtime_release(self, config: DeploymentConfig, target: ComposeTarget) -> str: ...

    def compare_environment_manifests(
        self,
        before: Mapping[str, object],
        after: Mapping[str, object],
    ) -> None: ...

    def restore_recovery(
        self,
        target: ComposeTarget,
        *,
        operation_id: str,
        snapshot: Mapping[str, object],
    ) -> bool: ...


class EnvironmentService:
    """Own the environment/storage transition policy and its durable gates."""

    def __init__(
        self,
        *,
        configuration: EnvironmentConfiguration,
        jobs: JobQuiescence,
        backup: BackupService,
        storage: EnvironmentStorage,
        runtime: EnvironmentRuntime,
        lifecycle_state: LifecycleState,
        release_provider: Callable[[], str],
    ) -> None:
        self.configuration = configuration
        self.jobs = jobs
        self.backup = backup
        self.storage = storage
        self.runtime = runtime
        self.lifecycle_state = lifecycle_state
        self.release_provider = release_provider

    def show(self, request: EnvironmentShowRequest) -> EnvironmentResult:
        del request
        active = self.configuration.read_active()
        target = self.configuration.derive_target(active)
        release = self._release()
        return EnvironmentResult(
            active=active,
            active_environment=active.environment,
            source_release=release,
            target_release=release,
            target_storage_identity=self.storage.target_identity(active),
            retained_resources=_retained_resources(active, target),
        )

    def plan_switch(self, request: EnvironmentSwitchRequest) -> dict[str, object]:
        active = self.configuration.read_active()
        candidate = _candidate(active, request)
        target = self.configuration.derive_target(candidate)
        release = self._release()
        source_identity = self.storage.target_identity(active)
        target_identity = self.storage.target_identity(candidate)
        inventory = self.storage.inventory(active) if target_identity != source_identity else None
        return {
            "transition": f"{active.environment} -> {candidate.environment}",
            "release": f"{release} (unchanged)",
            "development_tools": list(candidate.development_tools),
            "long_running_services": list(target.runtime_services),
            "on_demand_services": [
                service
                for service in target.required_services
                if service not in target.runtime_services
            ],
            "storage_decision": request.file_storage,
            "target_storage_identity": target_identity,
            "source_storage_inventory": (
                inventory.model_dump(mode="json") if inventory is not None else None
            ),
            "artifact_decision_required": (
                _storage_decision_required(inventory) if inventory is not None else False
            ),
            "backup_gate": "required before config/runtime mutation",
            "preserve": [
                "Compose project and durable volume identities",
                "PostgreSQL data, canonical memory, users, and secret references",
                "old storage configuration, bundled volume, and artifact rows",
            ],
            "rollback": "exact previous configuration and runtime",
        }

    def switch(
        self,
        request: EnvironmentSwitchRequest,
        *,
        progress_sink: ProgressSink | None = None,
        technical_sink: TechnicalSink | None = None,
        cancellation: CancellationToken | None = None,
    ) -> EnvironmentResult:
        active = self.configuration.read_active()
        active_target = self.configuration.derive_target(active)
        existing = self._resumable_journal()
        self.jobs.reconcile_pause((existing.operation_id,) if existing is not None else ())
        if request.target == active.environment and existing is not None:
            try:
                return self._recover_same_environment(
                    journal=existing,
                    active=active,
                    active_target=active_target,
                )
            finally:
                self.jobs.resume_accepting(existing.operation_id)
        try:
            source_release = self.runtime.runtime_release(active, active_target)
        except (OSError, RuntimeError, ValueError) as exc:
            raise LifecycleError.dependency(
                code="environment_release_unavailable",
                cause="The active runtime release identity could not be inspected.",
                remediation="Repair the active runtime before switching environment.",
                step="inspect",
            ) from exc
        if not source_release:
            raise LifecycleError.dependency(
                code="environment_release_unavailable",
                cause="The active runtime release identity is unavailable.",
                remediation="Repair the active runtime before switching environment.",
                step="inspect",
            )
        if request.target == active.environment:
            return EnvironmentResult(
                status="already_active",
                active=active,
                active_environment=active.environment,
                source_release=source_release,
                target_release=source_release,
                target_storage_identity=self.storage.target_identity(active),
                retained_resources=_retained_resources(active, active_target),
            )
        candidate = _candidate(active, request)
        source_storage_identity = self.storage.target_identity(active)
        target_identity = self.storage.target_identity(candidate)
        storage_changed = target_identity != source_storage_identity
        environment = self.storage.candidate_environment(candidate, {}) if storage_changed else {}
        prepared = self.configuration.prepare_transition_environment(
            active,
            candidate,
            environment,
        )
        target = prepared[0]
        environment = prepared[2]
        if storage_changed and request.artifact_decision is None:
            preflight_inventory = self.storage.inventory(active)
            if _storage_decision_required(preflight_inventory):
                raise _artifact_decision_required(preflight_inventory)
        operation_id = existing.operation_id if existing is not None else uuid4().hex
        context = _EnvironmentContext(
            operation_id=operation_id,
            request=request,
            previous=active,
            candidate=candidate,
            previous_target=active_target,
            target=target,
            prepared=prepared,
            source_release=source_release,
            target_storage_identity=target_identity,
            source_storage_identity=source_storage_identity,
            storage_changed=storage_changed,
            probe_target_storage=(storage_changed or candidate.file_storage == "external_s3"),
            durable_completed_steps=(existing.completed_steps if existing is not None else ()),
            durable_step_outputs=(existing.step_outputs if existing is not None else {}),
        )
        if existing is not None:
            self._load_resume_evidence(existing, context)
        gateway = _EnvironmentOperationGateway(service=self, context=context)
        definition = LifecycleOperationDefinition(
            operation_id=operation_id,
            kind=LifecycleOperationKind.ENVIRONMENT_SWITCH,
            steps=_environment_steps(
                storage_changed,
                context.probe_target_storage,
                candidate.environment == "production" and candidate.file_storage == "bundled",
            ),
            parameters={
                "source_environment": active.environment,
                "target_environment": candidate.environment,
                "source_release": source_release,
                "source_storage_identity": source_storage_identity,
                "target_storage_identity": target_identity,
                "artifact_decision": request.artifact_decision,
            },
            previous_config={"configuration": active.model_dump(mode="json")},
            recovery_command=(f"./flow-steward environment switch {active.environment} --confirm"),
        )
        operation = LifecycleOperationRunner(
            gateway=gateway,
            state=self.lifecycle_state,
        ).execute(
            definition,
            progress_sink=progress_sink,
            technical_sink=technical_sink,
            cancellation=cancellation,
        )
        return EnvironmentResult(
            active=candidate,
            active_environment=candidate.environment,
            source_environment=active.environment,
            source_release=source_release,
            target_release=gateway.target_release,
            operation_id=operation.operation_id,
            journal_path=operation.journal_path,
            log_path=operation.log_path,
            backup_path=gateway.backup_result.path if gateway.backup_result else None,
            storage_inventory=gateway.storage_inventory,
            artifact_decision=request.artifact_decision,
            target_storage_identity=target_identity,
            preservation_verified=gateway.preservation_verified,
            retained_resources=_retained_resources(active, target),
        )

    def _recover_same_environment(
        self,
        *,
        journal: LifecycleOperationJournal,
        active: DeploymentConfig,
        active_target: ComposeTarget,
    ) -> EnvironmentResult:
        source_environment = str(journal.parameters.get("source_environment") or "")
        target_environment = str(journal.parameters.get("target_environment") or "")
        source_release = str(journal.parameters.get("source_release") or "")
        if target_environment == active.environment and source_release:
            return self._resume_active_target_forward(
                journal=journal,
                active=active,
                active_target=active_target,
                source_environment=source_environment,
                source_release=source_release,
            )
        if source_environment != active.environment or not source_release:
            raise _environment_recovery_error(
                operation_id=journal.operation_id,
                cause="The unfinished environment switch does not match the active source.",
            )
        try:
            snapshot = self.lifecycle_state.load_previous_config(journal.operation_id)
            before = self.runtime.load_manifest(journal.operation_id, "before")
        except (OSError, ValueError) as exc:
            raise _environment_recovery_error(
                operation_id=journal.operation_id,
                cause="Environment rollback evidence is unreadable.",
            ) from exc
        runtime_snapshot = snapshot.get("runtime") if isinstance(snapshot, dict) else None
        if not isinstance(runtime_snapshot, dict) or before is None:
            raise _environment_recovery_error(
                operation_id=journal.operation_id,
                cause="Exact rollback evidence is unavailable.",
            )
        try:
            restored = self.runtime.restore_recovery(
                active_target,
                operation_id=journal.operation_id,
                snapshot=runtime_snapshot,
            )
            if not restored or not self.runtime.healthy(active, active_target):
                raise RuntimeError("previous runtime recovery was not verified")
            release = self.runtime.runtime_release(active, active_target)
            if release != source_release:
                raise RuntimeError("recovered runtime release differs from source")
            recovered = self.runtime.capture_manifest(
                active,
                active_target,
                operation_id=journal.operation_id,
                stage="after",
            )
            self.runtime.compare_environment_manifests(before, recovered)
        except (OSError, RuntimeError, ValueError) as exc:
            raise _environment_recovery_error(
                operation_id=journal.operation_id,
                cause="The exact previous runtime could not be reconciled.",
            ) from exc
        completed = self._completed_recovery_journal(journal)
        self.lifecycle_state.save_journal(completed)
        backup = self.backup.find_verified(journal.operation_id)
        return EnvironmentResult(
            status="recovered",
            active=active,
            active_environment=active.environment,
            source_environment=active.environment,
            source_release=source_release,
            target_release=source_release,
            operation_id=journal.operation_id,
            journal_path=str(self.lifecycle_state.journal_path(journal.operation_id)),
            log_path=journal.log_path,
            backup_path=backup.path if backup is not None else None,
            target_storage_identity=self.storage.target_identity(active),
            preservation_verified=True,
            runtime_restored=True,
            retained_resources=_retained_resources(active, active_target),
        )

    def _resume_active_target_forward(
        self,
        *,
        journal: LifecycleOperationJournal,
        active: DeploymentConfig,
        active_target: ComposeTarget,
        source_environment: str,
        source_release: str,
    ) -> EnvironmentResult:
        reached_publish = (
            journal.current_step in {"mark_active", "resume_jobs"}
            or "mark_active" in journal.completed_steps
        )
        if not reached_publish:
            raise _environment_recovery_error(
                operation_id=journal.operation_id,
                cause="The active target does not match the unfinished journal boundary.",
            )
        try:
            before = self.runtime.load_manifest(journal.operation_id, "before")
            if before is None or not self.runtime.healthy(active, active_target):
                raise RuntimeError("active target recovery evidence is unavailable")
            release = self.runtime.runtime_release(active, active_target)
            if release != source_release:
                raise RuntimeError("active target release differs from source")
            after = self.runtime.load_manifest(journal.operation_id, "after")
            if after is None:
                after = self.runtime.capture_manifest(
                    active,
                    active_target,
                    operation_id=journal.operation_id,
                    stage="after",
                )
            self.runtime.compare_environment_manifests(before, after)
            self.jobs.resume_accepting(journal.operation_id)
        except (OSError, RuntimeError, ValueError) as exc:
            raise _environment_recovery_error(
                operation_id=journal.operation_id,
                cause="The published target could not be safely finalized.",
            ) from exc
        self.lifecycle_state.save_journal(self._completed_recovery_journal(journal))
        backup = self.backup.find_verified(journal.operation_id)
        return EnvironmentResult(
            status="recovered",
            active=active,
            active_environment=active.environment,
            source_environment=cast(
                Literal["development", "production"] | None,
                source_environment if source_environment in {"development", "production"} else None,
            ),
            source_release=source_release,
            target_release=source_release,
            operation_id=journal.operation_id,
            journal_path=str(self.lifecycle_state.journal_path(journal.operation_id)),
            log_path=journal.log_path,
            backup_path=backup.path if backup is not None else None,
            target_storage_identity=self.storage.target_identity(active),
            preservation_verified=True,
            runtime_restored=False,
            retained_resources=_retained_resources(active, active_target),
        )

    @staticmethod
    def _completed_recovery_journal(
        journal: LifecycleOperationJournal,
    ) -> LifecycleOperationJournal:
        completed_ids = tuple(step.step_id for step in journal.steps)
        return journal.model_copy(
            update={
                "status": LifecycleOperationStatus.COMPLETED,
                "steps": tuple(
                    step.model_copy(update={"status": OperationStepStatus.SUCCEEDED})
                    for step in journal.steps
                ),
                "completed_steps": completed_ids,
                "current_step": None,
                "failed_step": None,
                "last_error_code": None,
                "retryable": False,
                "updated_at": datetime.now(UTC),
            }
        )

    def _release(self) -> str:
        release = str(self.release_provider() or "").strip()
        if not release:
            raise LifecycleError.dependency(
                code="environment_release_unavailable",
                cause="The installed Flow Steward release identity is unavailable.",
                remediation="Repair the installed release metadata before switching environment.",
                step="inspect",
            )
        return release

    def _resumable_journal(self) -> LifecycleOperationJournal | None:
        diagnosis = self.lifecycle_state.diagnose_active_lock()
        if diagnosis.status is LifecycleLockStatus.ACTIVE:
            raise LifecycleError.conflict(
                operation_id=diagnosis.operation_id,
                cause=diagnosis.detail,
            )
        try:
            journals = self.lifecycle_state.unfinished_journals()
        except (OSError, ValueError) as exc:
            raise _environment_recovery_error(
                operation_id=diagnosis.operation_id or "unknown-operation",
                cause="Lifecycle journals are unreadable and safe resume cannot be proven.",
            ) from exc
        if not journals:
            return None
        if len(journals) != 1 or journals[0].kind is not LifecycleOperationKind.ENVIRONMENT_SWITCH:
            raise _environment_recovery_error(
                operation_id=journals[0].operation_id,
                cause="An unfinished lifecycle operation requires operator reconciliation.",
            )
        return journals[0]

    def _load_resume_evidence(
        self,
        journal: LifecycleOperationJournal,
        context: _EnvironmentContext,
    ) -> None:
        try:
            snapshot = self.lifecycle_state.load_previous_config(journal.operation_id)
            manifest = self.runtime.load_manifest(journal.operation_id, "before")
        except (OSError, ValueError) as exc:
            raise _environment_recovery_error(
                operation_id=journal.operation_id,
                cause="Environment rollback evidence is unreadable.",
            ) from exc
        if snapshot is not None and isinstance(snapshot.get("runtime"), dict):
            context.runtime_snapshot = snapshot["runtime"]
        context.before_manifest = manifest
        if "capture_before" in journal.completed_steps and (
            context.runtime_snapshot is None
            or context.before_manifest is None
            or not self.runtime.recovery_available(journal.operation_id)
        ):
            raise _environment_recovery_error(
                operation_id=journal.operation_id,
                cause="The interrupted environment switch lacks exact rollback evidence.",
            )


def _candidate(
    active: DeploymentConfig,
    request: EnvironmentSwitchRequest,
) -> DeploymentConfig:
    values = active.model_dump(mode="json")
    values["environment"] = request.target
    values["development_tools"] = (
        list(_COMPLETE_DEVELOPMENT_TOOLS) if request.target == "development" else []
    )
    if request.file_storage == "bundled":
        values["file_storage"] = "bundled"
    elif request.file_storage == "external_s3":
        values.update(
            {
                "file_storage": "external_s3",
                "s3_endpoint": request.s3_endpoint,
                "s3_bucket": request.s3_bucket,
                "s3_region": request.s3_region,
                "s3_credentials_path": request.s3_credentials_file,
                "allow_insecure_s3_http": request.allow_insecure_s3_http,
            }
        )
    return DeploymentConfig.model_validate(values)


def _environment_steps(
    storage_changed: bool,
    probe_target_storage: bool,
    provision_bundled_credentials: bool,
) -> tuple[LifecycleOperationStep, ...]:
    steps = [
        ("prepare_target_images", "Prepare target images while the source remains online"),
        ("quiesce_jobs", "Stop accepting new jobs and wait for active jobs"),
        ("capture_before", "Snapshot exact configuration, runtime, and preservation state"),
        (
            "validate_preservation_baseline",
            "Validate required preservation evidence before mutation",
        ),
        ("create_backup", "Create and verify protected PostgreSQL backup"),
    ]
    if provision_bundled_credentials:
        steps.append(
            (
                "provision_storage_credentials",
                "Provision the owner-only bundled storage identity",
            )
        )
    steps.append(("render_target", "Render target configuration without marking it active"))
    if storage_changed or provision_bundled_credentials:
        steps.append(("install_storage_credentials", "Install exact target storage credentials"))
    steps.append(("validate_target", "Render and validate the target Compose configuration"))
    if storage_changed:
        steps.extend(
            (
                ("inventory_storage", "Inventory source artifact storage"),
                ("require_storage_decision", "Require explicit artifact availability decision"),
            )
        )
    if probe_target_storage:
        steps.append(("probe_target_storage", "Probe target storage connectivity and write access"))
    steps.extend(
        (
            ("apply_runtime_artifacts", "Install target runtime artifacts"),
            ("activate_target", "Start target runtime at the installed release"),
            ("verify_health", "Verify canonical target runtime health and release"),
            ("capture_after", "Capture post-transition preservation state"),
            ("verify_preservation", "Compare exact preservation manifests"),
            ("mark_active", "Atomically publish the verified target environment revision"),
            ("resume_jobs", "Resume new job admission"),
        )
    )
    return tuple(
        LifecycleOperationStep(
            step_id=step_id,
            label=label,
            safe_boundary=step_id
            not in {
                "provision_storage_credentials",
                "render_target",
                "install_storage_credentials",
                "validate_target",
                "inventory_storage",
                "require_storage_decision",
                "probe_target_storage",
                "apply_runtime_artifacts",
                "activate_target",
                "verify_health",
                "capture_after",
                "verify_preservation",
                "mark_active",
            },
        )
        for step_id, label in steps
    )


def _retained_resources(
    previous: DeploymentConfig,
    target: ComposeTarget,
) -> tuple[str, ...]:
    retained = ["artifact_rows"]
    if previous.file_storage == "bundled":
        retained.append(f"bundled_volume:{target.project_name}:seaweedfs")
    return tuple(retained)


def _environment_recovery_error(*, operation_id: str, cause: str) -> LifecycleError:
    failure = LifecycleError.recovery_required(
        code="environment_recovery_required",
        cause=cause,
        remediation="Inspect the lifecycle journal and reconcile the previous runtime.",
        step="recovery",
        recovery_command="flow-steward environment show",
    )
    failure.operation_id = operation_id
    return failure


__all__ = [
    "EnvironmentResult",
    "EnvironmentService",
    "EnvironmentShowRequest",
    "EnvironmentSwitchRequest",
    "StorageInventory",
]
