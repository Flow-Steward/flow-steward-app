"""Application orchestration for consistent installation Clone creation."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from core.application.clone.contracts import CloneManifest
from core.application.clone.manifest import (
    CloneSourcePlan,
    build_source_allowlist,
    validate_clone_manifest_completeness,
)
from core.application.clone.operation import (
    CloneRestoreConfirmation,
    CloneRestoreOperationGateway,
    CloneRestoreResult,
    CloneRestoreRuntimePort,
    clone_restore_definition,
)
from core.application.clone.planning import (
    CloneReplacementPlan,
    CloneTargetShell,
    PreparedCloneRestore,
)
from core.application.lifecycle.contracts import (
    CancellationToken,
    DeploymentConfig,
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
from core.application.maintenance.contracts import MaintenanceStatusResult
from core.application.release.catalog import ReleaseError
from core.application.release.contracts import ReleaseIdentity
from core.application.release.service import release_platform_for_machine

PassphraseSource = Callable[[], str | bytes]

_OPERATION_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"
_STEPS = (
    LifecycleOperationStep(step_id="capture_source", label="Capture source installation state"),
    LifecycleOperationStep(step_id="assemble_archive", label="Assemble encrypted Clone archive"),
    LifecycleOperationStep(step_id="verify_archive", label="Verify encrypted Clone archive"),
    LifecycleOperationStep(step_id="publish_archive", label="Publish Clone archive atomically"),
)
_RECOVERY_COMMAND = "flow-steward clone create"


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CloneCreateRequest(_ClosedModel):
    output_directory: str = Field(min_length=1, max_length=4096)
    operation_id: str | None = Field(
        default=None,
        pattern=_OPERATION_ID_PATTERN,
    )


class CloneRestoreRequest(_ClosedModel):
    archive_path: str = Field(min_length=1, max_length=4096)
    operation_id: str | None = Field(
        default=None,
        pattern=_OPERATION_ID_PATTERN,
    )


class CloneCreatePlan(_ClosedModel):
    operation_id: str = Field(pattern=_OPERATION_ID_PATTERN)
    created_at: datetime
    output_path: str
    source_installation_id: str
    config: DeploymentConfig
    release: ReleaseIdentity
    source_plan: CloneSourcePlan


class CloneCreateResult(_ClosedModel):
    operation_id: str
    path: str
    size_bytes: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    release: ReleaseIdentity
    log_path: str


class CloneMaintenancePort(Protocol):
    def status(self) -> MaintenanceStatusResult: ...


class CloneConfigurationPort(Protocol):
    def read_active(self) -> DeploymentConfig | None: ...


class CloneReleaseStatePort(Protocol):
    def load_installed_release(self) -> ReleaseIdentity | None: ...


class CloneReleaseVerifierPort(Protocol):
    def verify(self, release: ReleaseIdentity) -> ReleaseIdentity: ...


class CloneExactReleasePort(Protocol):
    def for_exact_identity(self, identity: ReleaseIdentity) -> ReleaseIdentity: ...


class CloneVerifiedRestorePort(Protocol):
    manifest: CloneManifest
    configuration: DeploymentConfig | None
    root: Path
    artifact_size: int
    artifact_sha256: str

    def cleanup(self) -> None: ...


class CloneRestoreArchivePort(Protocol):
    def artifact_size(self, path: str) -> int: ...

    def artifact_sha256(self, path: str) -> str: ...

    def free_bytes(self) -> int: ...

    def stage_verified(
        self,
        path: str,
        *,
        passphrase_source: PassphraseSource,
    ) -> CloneVerifiedRestorePort: ...

    def discard_staged(self, staged_root: str) -> None: ...


class CloneCreateRuntimePort(Protocol):
    def preflight(self, plan: CloneCreatePlan) -> None: ...

    def capture_source(self, plan: CloneCreatePlan) -> None: ...

    def assemble_archive(
        self,
        plan: CloneCreatePlan,
        *,
        passphrase_source: PassphraseSource,
    ) -> None: ...

    def verify_archive(
        self,
        plan: CloneCreatePlan,
        *,
        passphrase_source: PassphraseSource,
    ) -> None: ...

    def publish_archive(self, plan: CloneCreatePlan) -> None: ...

    def postcondition_holds(self, plan: CloneCreatePlan, step_id: str) -> bool: ...

    def artifact_size(self, plan: CloneCreatePlan) -> int: ...

    def archive_digest(self, plan: CloneCreatePlan) -> str: ...

    def cleanup(
        self,
        plan: CloneCreatePlan,
        *,
        preserve_recovery: bool = False,
        preserve_resume: bool = False,
    ) -> None: ...


def clone_create_definition(plan: CloneCreatePlan) -> LifecycleOperationDefinition:
    """Return the durable, secret-free Clone create operation definition."""
    return LifecycleOperationDefinition(
        operation_id=plan.operation_id,
        kind=LifecycleOperationKind.CLONE_CREATE,
        steps=_STEPS,
        parameters={"plan": plan.model_dump(mode="json")},
        recovery_command=_RECOVERY_COMMAND,
    )


class CloneService:
    """Preflight and journal one complete Clone create operation."""

    def __init__(
        self,
        *,
        maintenance: CloneMaintenancePort,
        lifecycle_state: LifecycleState,
        configuration: CloneConfigurationPort,
        release_state: CloneReleaseStatePort | None,
        release_verifier: CloneReleaseVerifierPort | None,
        runtime: CloneCreateRuntimePort | None,
        source_installation_id: Callable[[], str],
        now: Callable[[], datetime] | None = None,
        restore_archive: CloneRestoreArchivePort | None = None,
        exact_release: CloneExactReleasePort | None = None,
        target_machine: Callable[[], str] | None = None,
        target_shell: Callable[[DeploymentConfig], CloneTargetShell] | None = None,
        restore_runtime: CloneRestoreRuntimePort | None = None,
        resolve_output_directory: Callable[[str], Path] | None = None,
        resolve_archive_path: Callable[[str], Path] | None = None,
        installation_ready: Callable[[str | None], None] | None = None,
    ) -> None:
        self._maintenance = maintenance
        self._lifecycle_state = lifecycle_state
        self._configuration = configuration
        self._release_state = release_state
        self._release_verifier = release_verifier
        self._runtime = runtime
        self._source_installation_id = source_installation_id
        self._now = now or (lambda: datetime.now(UTC))
        self._restore_archive = restore_archive
        self._exact_release = exact_release
        self._target_machine = target_machine
        self._target_shell = target_shell
        self._restore_runtime = restore_runtime
        self._resolve_output_directory = resolve_output_directory or Path
        self._resolve_archive_path = resolve_archive_path or Path
        self._installation_ready = installation_ready or (lambda _operation_id: None)

    def create(
        self,
        request: CloneCreateRequest,
        *,
        passphrase_source: PassphraseSource,
        progress_sink: ProgressSink | None = None,
        technical_sink: TechnicalSink | None = None,
        cancellation: CancellationToken | None = None,
    ) -> CloneCreateResult:
        self._require_manual_maintenance(operation="creation")
        self._installation_ready(request.operation_id)
        runtime = self._runtime
        if runtime is None:
            raise RuntimeError("Clone create runtime is not configured")
        existing = self._resumable_journal(request.operation_id)
        if existing is not None:
            raw_plan = existing.parameters.get("plan")
            plan = CloneCreatePlan.model_validate(raw_plan)
        else:
            plan = self._prepare_plan(request)
        runtime.preflight(plan)
        gateway = _CloneCreateOperationGateway(
            runtime=runtime,
            plan=plan,
            passphrase_source=passphrase_source,
        )
        try:
            operation = LifecycleOperationRunner(
                gateway=gateway,
                state=self._lifecycle_state,
            ).execute(
                clone_create_definition(plan),
                progress_sink=progress_sink,
                technical_sink=technical_sink,
                cancellation=cancellation,
            )
        except LifecycleError as exc:
            preserve_recovery = exc.exit_code.name == "RECOVERY_REQUIRED"
            runtime.cleanup(
                plan,
                preserve_recovery=preserve_recovery,
                preserve_resume=True,
            )
            raise
        runtime.cleanup(plan)
        return CloneCreateResult(
            operation_id=operation.operation_id,
            path=plan.output_path,
            size_bytes=runtime.artifact_size(plan),
            sha256=runtime.archive_digest(plan),
            release=plan.release,
            log_path=operation.log_path,
        )

    def _prepare_plan(self, request: CloneCreateRequest) -> CloneCreatePlan:
        config = self._configuration.read_active()
        if config is None:
            raise LifecycleError.stable_failure(
                code="clone_installation_required",
                cause="Clone creation requires an installed active configuration.",
                remediation="Complete or repair the installation before creating a Clone archive.",
                step="preflight",
            )
        release_state = self._release_state
        release_verifier = self._release_verifier
        if release_state is None or release_verifier is None:
            raise RuntimeError("Clone create release dependencies are not configured")
        installed = release_state.load_installed_release()
        if installed is None:
            raise LifecycleError.stable_failure(
                code="clone_release_required",
                cause="Clone creation requires a complete installed release identity.",
                remediation="Repair the installed release record and retry.",
                step="preflight",
            )
        try:
            verified = release_verifier.verify(installed)
        except Exception as exc:
            raise LifecycleError.stable_failure(
                code="clone_official_release_required",
                cause="Clone creation requires the installed official release and image set.",
                remediation="Install or repair an official digest-pinned release before retrying.",
                step="preflight",
            ) from exc
        if verified != installed:
            raise LifecycleError.stable_failure(
                code="clone_release_identity_mismatch",
                cause="Official release verification changed the installed release identity.",
                remediation="Repair the installed release record before retrying.",
                step="preflight",
            )
        created_at = self._now().astimezone(UTC)
        operation_id = request.operation_id or f"clone-{uuid4().hex}"
        filename = (
            f"flow-steward-clone-{created_at:%Y%m%dT%H%M%SZ}-"
            f"{config.deployment_profile}-{installed.version}.fsclone"
        )
        output_directory = self._resolve_output_directory(request.output_directory)
        return CloneCreatePlan(
            operation_id=operation_id,
            created_at=created_at,
            output_path=str(output_directory / filename),
            source_installation_id=self._source_installation_id(),
            config=config,
            release=installed,
            source_plan=build_source_allowlist(config, installed),
        )

    def _require_manual_maintenance(self, *, operation: str) -> None:
        maintenance = self._maintenance.status()
        if maintenance.status != "enabled" or maintenance.owner_kind != "manual":
            raise LifecycleError.stable_failure(
                code="clone_manual_maintenance_required",
                cause=f"Clone {operation} requires fully enabled manual maintenance.",
                remediation="Run flow-steward maintenance enable, wait for enabled status, and retry.",
                step="preflight",
            )

    def prepare_restore(
        self,
        request: CloneRestoreRequest,
        *,
        passphrase_source: PassphraseSource,
    ) -> PreparedCloneRestore:
        """Authenticate and fully preflight a Clone without mutating the target."""
        self._require_manual_maintenance(operation="restore")
        self._installation_ready(request.operation_id)
        self._require_no_unfinished_operation(request.operation_id)
        archive = self._restore_archive
        exact_release = self._exact_release
        target_machine = self._target_machine
        target_shell = self._target_shell
        if any(
            dependency is None
            for dependency in (archive, exact_release, target_machine, target_shell)
        ):
            raise RuntimeError("Clone restore preflight dependencies are not configured")
        assert archive is not None
        assert exact_release is not None
        assert target_machine is not None
        assert target_shell is not None

        archive_path = str(self._resolve_archive_path(request.archive_path))
        try:
            verified = archive.stage_verified(
                archive_path,
                passphrase_source=passphrase_source,
            )
        except LifecycleError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            raise LifecycleError.stable_failure(
                code="clone_restore_archive_invalid",
                cause="The Clone archive could not be authenticated and staged.",
                remediation="Provide the matching passphrase and an intact Clone archive.",
                step="restore_preflight",
            ) from exc

        try:
            manifest = CloneManifest.model_validate(verified.manifest)
            validate_clone_manifest_completeness(manifest)
            if verified.configuration is None:
                raise ValueError("Clone source configuration evidence is missing")
            staged_root = str(verified.root)
            archive_sha256 = verified.artifact_sha256
            if verified.artifact_size <= 0 or not archive_sha256:
                raise LifecycleError.stable_failure(
                    code="clone_restore_archive_identity_missing",
                    cause="The staged Clone archive has no authenticated source identity.",
                    remediation="Retry with an intact Clone archive.",
                    step="restore_preflight",
                )
            if archive.free_bytes() < manifest.aggregate_size:
                raise LifecycleError.stable_failure(
                    code="clone_restore_staging_space_insufficient",
                    cause="Clone staging space is insufficient for the authenticated payload.",
                    remediation="Free space on the private staging filesystem and retry.",
                    step="restore_preflight",
                )
            active = self._configuration.read_active()
            if active is None:
                raise LifecycleError.stable_failure(
                    code="clone_restore_target_required",
                    cause="Clone restore requires a preinstalled target configuration.",
                    remediation="Install the target with the intended deployment profile first.",
                    step="restore_preflight",
                )
            if active.deployment_profile != manifest.deployment_profile:
                raise LifecycleError.stable_failure(
                    code="clone_restore_profile_mismatch",
                    cause="Clone source and target deployment profile do not match.",
                    remediation="Restore onto a target installed with the same profile.",
                    step="restore_preflight",
                )
            if active.data_placement != manifest.data_placement:
                raise LifecycleError.stable_failure(
                    code="clone_restore_data_placement_mismatch",
                    cause="Clone source and target data placement do not match.",
                    remediation=(
                        "Restore onto a target installed with the same data placement. "
                        "Clone does not convert host-directory and volume archive layouts."
                    ),
                    step="restore_preflight",
                )
            platform = release_platform_for_machine(target_machine())
            if manifest.release.platform != platform:
                raise LifecycleError.stable_failure(
                    code="clone_restore_platform_mismatch",
                    cause="Clone source and target Linux platform do not match.",
                    remediation="Restore onto a target with the same normalized architecture.",
                    step="restore_preflight",
                )
            try:
                resolved = exact_release.for_exact_identity(manifest.release)
            except (ReleaseError, ValueError) as exc:
                raise LifecycleError.dependency(
                    code="clone_restore_exact_release_unavailable",
                    cause="The archived exact release is not currently available.",
                    remediation=(
                        "Publish or restore access to the archived exact release, then repeat "
                        "Clone restore preflight."
                    ),
                    step="release_policy",
                ) from exc
            shell = target_shell(active)
            replaced = tuple(sorted({entry.data_class.value for entry in manifest.entries}))
            plan = CloneReplacementPlan.build(
                source_installation_id=manifest.source_installation_id,
                source_release_version=manifest.release.version,
                source_release=manifest.release,
                archive_sha256=archive_sha256,
                target_shell=shell,
                target_platform=platform,
                replaced_data_classes=replaced,
                external_effect_categories=manifest.external_effect_categories,
            )
            return PreparedCloneRestore(
                archive_path=archive_path,
                operation_id=(request.operation_id or f"clone-restore-{plan.plan_digest[:24]}"),
                staged_root=staged_root,
                manifest=manifest,
                source_configuration=verified.configuration,
                exact_release=resolved,
                plan=plan,
            )
        except BaseException:
            verified.cleanup()
            raise

    def discard_restore(self, prepared: PreparedCloneRestore) -> None:
        """Discard authenticated staging after a declined or invalid confirmation."""
        archive = self._restore_archive
        if archive is None:
            raise RuntimeError("Clone restore archive dependency is not configured")
        archive.discard_staged(prepared.staged_root)

    @staticmethod
    def review_restore(prepared: PreparedCloneRestore) -> dict[str, object]:
        plan = prepared.plan
        target_config = plan.retained_shell.configuration
        return {
            "source_installation_id": plan.source_installation_id,
            "source_release_version": plan.source_release_version,
            "target_installation_id": plan.target_installation_id,
            "target_profile": plan.target_profile,
            "target_platform": plan.target_platform,
            "public_host": target_config.public_host,
            "retained_shell": {
                "environment": target_config.environment,
                "ports": {
                    "public": target_config.public_port,
                    "runtime_http": target_config.runtime_http_port,
                    "redirect_http": target_config.redirect_http_port,
                    "reverse_proxy_backend": target_config.reverse_proxy_backend_port,
                    "reverse_proxy_mcp": target_config.reverse_proxy_mcp_port,
                },
                "ingress_mode": target_config.ingress_mode,
                "tls_mode": target_config.tls_mode,
                "networks": {
                    "proxy": target_config.proxy_network,
                    "traefik": target_config.traefik_network,
                },
                "filesystem_roots": plan.retained_shell.filesystem_roots,
                "host_uid": plan.retained_shell.host_uid,
                "host_gid": plan.retained_shell.host_gid,
                "postgres_roles": plan.retained_shell.postgres_roles,
                "postgres_identity_fingerprint": (
                    plan.retained_shell.postgres_identity_fingerprint
                ),
            },
            "replaced_data_classes": plan.replaced_data_classes,
            "external_effect_categories": plan.external_effect_categories,
            "plan_digest": plan.plan_digest,
            "rollback_recommendation": (
                "Before confirmation, create a separate rollback point of this populated "
                "target with `flow-steward clone create`; Clone does not back up the target "
                "automatically. A database backup protects only PostgreSQL. Also keep the "
                "source in maintenance until the restored target is validated."
            ),
        }

    def _require_no_unfinished_operation(self, requested_operation_id: str | None) -> None:
        diagnosis = self._lifecycle_state.diagnose_active_lock()
        if diagnosis.status is LifecycleLockStatus.ACTIVE:
            raise LifecycleError.conflict(
                operation_id=diagnosis.operation_id,
                cause=diagnosis.detail,
            )
        journals = self._lifecycle_state.unfinished_journals()
        if (
            len(journals) == 1
            and requested_operation_id is not None
            and journals[0].kind is LifecycleOperationKind.CLONE_RESTORE
            and journals[0].operation_id == requested_operation_id
        ):
            return
        if journals:
            journal = journals[0]
            failure = LifecycleError.recovery_required(
                code="clone_restore_unfinished_operation",
                cause="An unfinished lifecycle mutation must be reconciled before Clone restore.",
                remediation=f"Resume or recover operation {journal.operation_id}.",
                step=journal.failed_step or journal.current_step or "recovery",
                recovery_command=journal.recovery_command,
            )
            failure.operation_id = journal.operation_id
            failure.log_path = journal.log_path
            raise failure

    def restore(
        self,
        prepared: PreparedCloneRestore,
        *,
        confirmation: CloneRestoreConfirmation,
        progress_sink: ProgressSink | None = None,
        technical_sink: TechnicalSink | None = None,
        cancellation: CancellationToken | None = None,
    ) -> CloneRestoreResult:
        """Execute one confirmed, target-bound destructive replacement."""
        runtime = self._restore_runtime
        if runtime is None:
            raise RuntimeError("Clone restore runtime is not configured")
        try:
            self._require_manual_maintenance(operation="restore")
            expected = CloneRestoreConfirmation.from_plan(prepared.plan)
            if confirmation != expected:
                raise LifecycleError.stable_failure(
                    code="clone_restore_confirmation_mismatch",
                    cause="Clone restore confirmation does not match the reviewed target plan.",
                    remediation="Review the current restore plan and confirm its exact target tuple.",
                    step="restore_confirmation",
                )
            if runtime.current_target_shell() != prepared.plan.retained_shell:
                raise LifecycleError.stable_failure(
                    code="clone_restore_target_changed",
                    cause="The target deployment shell changed after Clone restore review.",
                    remediation="Discard staging, repeat preflight, and review the new target plan.",
                    step="restore_confirmation",
                )
            operation = LifecycleOperationRunner(
                gateway=CloneRestoreOperationGateway(runtime=runtime, prepared=prepared),
                state=self._lifecycle_state,
            ).execute(
                clone_restore_definition(prepared),
                progress_sink=progress_sink,
                technical_sink=technical_sink,
                cancellation=cancellation,
            )
        except BaseException:
            runtime.cleanup(prepared)
            raise
        runtime.cleanup(prepared)
        return CloneRestoreResult(
            operation_id=operation.operation_id,
            target_installation_id=prepared.plan.target_installation_id,
            release_version=prepared.exact_release.version,
            plan_digest=prepared.plan.plan_digest,
            log_path=operation.log_path,
        )

    def _resumable_journal(
        self,
        requested_operation_id: str | None,
    ) -> LifecycleOperationJournal | None:
        diagnosis = self._lifecycle_state.diagnose_active_lock()
        if diagnosis.status is LifecycleLockStatus.ACTIVE:
            raise LifecycleError.conflict(
                operation_id=diagnosis.operation_id,
                cause=diagnosis.detail,
            )
        journals = self._lifecycle_state.unfinished_journals()
        if not journals:
            return None
        journal = journals[0]
        if (
            len(journals) != 1
            or journal.kind is not LifecycleOperationKind.CLONE_CREATE
            or (
                requested_operation_id is not None
                and requested_operation_id != journal.operation_id
            )
        ):
            failure = LifecycleError.recovery_required(
                code="clone_operation_recovery_required",
                cause="An unfinished lifecycle mutation must be reconciled before Clone creation.",
                remediation=f"Resume or recover operation {journal.operation_id}.",
                step=journal.failed_step or journal.current_step or "recovery",
                recovery_command=journal.recovery_command,
            )
            failure.operation_id = journal.operation_id
            failure.log_path = journal.log_path
            raise failure
        return journal


class _CloneCreateOperationGateway:
    def __init__(
        self,
        *,
        runtime: CloneCreateRuntimePort,
        plan: CloneCreatePlan,
        passphrase_source: PassphraseSource,
    ) -> None:
        self.runtime = runtime
        self.plan = plan
        self.passphrase_source = passphrase_source

    def postcondition_holds(
        self,
        definition: LifecycleOperationDefinition,
        step: LifecycleOperationStep,
    ) -> bool:
        del definition
        return self.runtime.postcondition_holds(self.plan, step.step_id)

    def execute_step(
        self,
        definition: LifecycleOperationDefinition,
        step: LifecycleOperationStep,
        *,
        output_sink: TechnicalSink,
        heartbeat: HeartbeatSink,
        cancellation: CancellationToken,
    ) -> None:
        del definition, heartbeat, cancellation
        if step.step_id == "capture_source":
            output_sink("Capturing allowlisted source installation state.")
            self.runtime.capture_source(self.plan)
        elif step.step_id == "assemble_archive":
            output_sink("Assembling one encrypted Clone archive.")
            self.runtime.assemble_archive(
                self.plan,
                passphrase_source=self.passphrase_source,
            )
        elif step.step_id == "verify_archive":
            output_sink("Authenticating and validating the complete Clone archive.")
            self.runtime.verify_archive(
                self.plan,
                passphrase_source=self.passphrase_source,
            )
        elif step.step_id == "publish_archive":
            output_sink("Publishing the verified Clone archive atomically.")
            self.runtime.publish_archive(self.plan)
        else:  # pragma: no cover - closed definition invariant
            raise ValueError(f"Unsupported Clone create step: {step.step_id}")


__all__ = [
    "CloneCreatePlan",
    "CloneCreateRequest",
    "CloneCreateResult",
    "CloneRestoreRequest",
    "CloneRestoreResult",
    "CloneService",
    "PreparedCloneRestore",
    "clone_create_definition",
]
