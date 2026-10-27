from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.application.clone.contracts import CloneDataClass, CloneEntry, CloneManifest
from core.application.clone.operation import (
    CloneRestoreConfirmation,
    clone_restore_definition,
)
from core.application.clone.planning import (
    CloneReplacementPlan,
    CloneTargetShell,
    PreparedCloneRestore,
)
from core.application.clone.service import CloneService
from core.application.lifecycle.contracts import DeploymentConfig, LifecycleOperationKind
from core.application.lifecycle.errors import LifecycleError
from core.application.maintenance.contracts import MaintenanceStatusResult
from core.application.release.contracts import ReleaseIdentity, ReleaseImageIdentity
from core.application.release.images import required_product_roles
from core.infrastructure.lifecycle.state_store import LifecycleStateStore

_DIGEST = "sha256:" + "1" * 64
_COMMIT = "a" * 40


def _release() -> ReleaseIdentity:
    return ReleaseIdentity(
        version="1.2.3",
        tag="v1.2.3",
        commit_sha=_COMMIT,
        registry="ghcr.io/example",
        profile="scalable",
        platform="linux/amd64",
        images=tuple(
            ReleaseImageIdentity(
                role=role.role,
                registry_ref=f"ghcr.io/example/{role.repository}@{_DIGEST}",
                index_digest=_DIGEST,
                manifest_digest=_DIGEST,
                config_digest=_DIGEST,
                revision=_COMMIT,
                platform="linux/amd64",
            )
            for role in required_product_roles("scalable")
        ),
    )


def _shell(host: str = "target.example.test") -> CloneTargetShell:
    return CloneTargetShell.from_config(
        installation_id="target-installation",
        compose_project="flow-steward-target",
        config=DeploymentConfig(public_host=host),
        host_uid=1000,
        host_gid=1000,
        filesystem_roots={"extensions": "/srv/extensions"},
        postgres_roles=("flow_steward",),
        postgres_identity_fingerprint="f" * 64,
    )


def _prepared(tmp_path: Path) -> PreparedCloneRestore:
    release = _release()
    entry = CloneEntry(
        data_class=CloneDataClass.POSTGRESQL,
        path="postgresql/database.dump",
        entry_type="file",
        size=5,
        mode=0o600,
        sha256="f" * 64,
    )
    manifest = CloneManifest(
        created_at=datetime(2026, 9, 26, tzinfo=UTC),
        release=release,
        deployment_profile="scalable",
        data_placement="host_directories",
        file_storage="local_fs",
        source_installation_id="source-installation",
        entries=(entry,),
        entry_count=1,
        aggregate_size=5,
    )
    shell = _shell()
    plan = CloneReplacementPlan.build(
        source_installation_id="source-installation",
        source_release_version="1.2.3",
        source_release=release,
        archive_sha256="e" * 64,
        target_shell=shell,
        target_platform="linux/amd64",
        replaced_data_classes=("postgresql",),
        external_effect_categories=(),
    )
    return PreparedCloneRestore(
        archive_path=str(tmp_path / "clone.fsclone"),
        operation_id="clone-restore-test",
        staged_root=str(tmp_path / "staged"),
        manifest=manifest,
        source_configuration=DeploymentConfig(
            deployment_profile=manifest.deployment_profile,
            data_placement=manifest.data_placement,
            file_storage=manifest.file_storage,
        ),
        exact_release=release,
        plan=plan,
    )


class _Maintenance:
    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled

    def status(self) -> MaintenanceStatusResult:
        return MaintenanceStatusResult(
            status="enabled" if self.enabled else "disabled",
            owner_kind="manual" if self.enabled else None,
        )


class _Config:
    def read_active(self) -> DeploymentConfig:
        return DeploymentConfig(public_host="target.example.test")


class _RestoreRuntime:
    def __init__(
        self,
        shell: CloneTargetShell,
        *,
        fail_once: str | None = None,
        interrupt_once: str | None = None,
    ) -> None:
        self.shell = shell
        self.fail_once = fail_once
        self.interrupt_once = interrupt_once
        self.calls: list[str] = []
        self.completed: set[str] = set()

    def current_target_shell(self) -> CloneTargetShell:
        return self.shell

    def execute_phase(self, prepared: PreparedCloneRestore, phase: str) -> None:
        self.calls.append(phase)
        if self.interrupt_once == phase:
            self.interrupt_once = None
            raise KeyboardInterrupt
        if self.fail_once == phase:
            self.fail_once = None
            raise LifecycleError.dependency(
                code="clone_restore_phase_pending",
                cause="Injected retryable restore failure.",
                remediation="Resume the same Clone restore operation.",
                step=phase,
            )
        self.completed.add(phase)

    def postcondition_holds(self, prepared: PreparedCloneRestore, phase: str) -> bool:
        return phase in self.completed

    def cleanup(self, prepared: PreparedCloneRestore) -> None:
        self.calls.append("cleanup")


def _service(
    tmp_path: Path,
    runtime: _RestoreRuntime,
    *,
    maintenance: _Maintenance | None = None,
) -> CloneService:
    return CloneService(
        maintenance=maintenance or _Maintenance(),
        lifecycle_state=LifecycleStateStore(tmp_path / "lifecycle"),
        configuration=_Config(),
        release_state=None,
        release_verifier=None,
        runtime=None,
        source_installation_id=lambda: "unused",
        restore_runtime=runtime,
    )


def test_restore_requires_target_bound_confirmation_before_mutation(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path)
    runtime = _RestoreRuntime(_shell())
    service = _service(tmp_path, runtime)
    confirmation = CloneRestoreConfirmation(
        target_installation_id="other",
        target_profile="scalable",
        public_host="target.example.test",
        plan_digest=prepared.plan.plan_digest,
    )

    with pytest.raises(LifecycleError, match="confirmation"):
        service.restore(prepared, confirmation=confirmation)

    assert runtime.calls == ["cleanup"]


def test_restore_rejects_target_shell_drift_before_mutation(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path)
    runtime = _RestoreRuntime(_shell("changed.example.test"))
    service = _service(tmp_path, runtime)
    confirmation = CloneRestoreConfirmation.from_plan(prepared.plan)

    with pytest.raises(LifecycleError, match="changed"):
        service.restore(prepared, confirmation=confirmation)

    assert runtime.calls == ["cleanup"]


def test_restore_cleans_staging_when_maintenance_changes_after_preview(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path)
    runtime = _RestoreRuntime(_shell())
    maintenance = _Maintenance(enabled=False)
    service = _service(tmp_path, runtime, maintenance=maintenance)
    confirmation = CloneRestoreConfirmation.from_plan(prepared.plan)

    with pytest.raises(LifecycleError, match="maintenance"):
        service.restore(prepared, confirmation=confirmation)

    assert runtime.calls == ["cleanup"]


def test_restore_runs_one_resumable_lifecycle_definition(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path)
    runtime = _RestoreRuntime(_shell())
    service = _service(tmp_path, runtime)

    result = service.restore(
        prepared,
        confirmation=CloneRestoreConfirmation.from_plan(prepared.plan),
    )

    definition = clone_restore_definition(prepared)
    assert definition.kind is LifecycleOperationKind.CLONE_RESTORE
    assert runtime.calls == [*(step.step_id for step in definition.steps), "cleanup"]
    assert result.status == "restored"
    assert result.target_installation_id == "target-installation"
    assert result.maintenance_retained is True
    assert "passphrase" not in result.model_dump_json()


def test_restore_resume_skips_proven_phases(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path)
    runtime = _RestoreRuntime(_shell(), fail_once="restore_database")
    service = _service(tmp_path, runtime)
    confirmation = CloneRestoreConfirmation.from_plan(prepared.plan)

    with pytest.raises(LifecycleError):
        service.restore(prepared, confirmation=confirmation)

    service.restore(prepared, confirmation=confirmation)

    assert runtime.calls.count("persist_plan") == 1
    assert runtime.calls.count("stop_target") == 1
    assert runtime.calls.count("replace_filesystems") == 1
    assert runtime.calls.count("restore_database") == 2


def test_restore_failure_cleans_plaintext_before_new_service_instance_resumes(
    tmp_path: Path,
) -> None:
    class _DeletingRestoreRuntime(_RestoreRuntime):
        def cleanup(self, prepared: PreparedCloneRestore) -> None:
            super().cleanup(prepared)
            shutil.rmtree(prepared.staged_root)

    first_prepared = _prepared(tmp_path)
    first_staging = Path(first_prepared.staged_root)
    first_staging.mkdir()
    (first_staging / "source-secret.json").write_text("private", encoding="utf-8")
    runtime = _DeletingRestoreRuntime(_shell(), fail_once="restore_database")
    confirmation = CloneRestoreConfirmation.from_plan(first_prepared.plan)
    service = _service(tmp_path, runtime)

    with pytest.raises(LifecycleError):
        service.restore(first_prepared, confirmation=confirmation)

    assert runtime.calls[-1] == "cleanup"
    assert not first_staging.exists()
    resumed = first_prepared.model_copy(
        update={"staged_root": str(tmp_path / "fresh-authenticated-staging")}
    )
    resumed_staging = Path(resumed.staged_root)
    resumed_staging.mkdir()
    (resumed_staging / "source-secret.json").write_text("private", encoding="utf-8")
    _service(tmp_path, runtime).restore(resumed, confirmation=confirmation)

    assert not resumed_staging.exists()
    assert runtime.calls.count("cleanup") == 2
    assert runtime.calls.count("persist_plan") == 1
    assert runtime.calls.count("restore_database") == 2


def test_restore_command_resumes_recovery_required_after_interrupt(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path)
    prepared = prepared.model_copy(update={"archive_path": str(tmp_path / "clone archive.fsclone")})
    runtime = _RestoreRuntime(_shell(), interrupt_once="restore_database")
    service = _service(tmp_path, runtime)
    confirmation = CloneRestoreConfirmation.from_plan(prepared.plan)

    with pytest.raises(LifecycleError) as raised:
        service.restore(prepared, confirmation=confirmation)

    definition = clone_restore_definition(prepared)
    assert definition.allow_recovery_resume is True
    assert "--passphrase-stdin" not in definition.recovery_command
    assert "'" in definition.recovery_command
    assert raised.value.recovery_command == definition.recovery_command

    service.restore(prepared, confirmation=confirmation)

    assert runtime.calls.count("persist_plan") == 1
    assert runtime.calls.count("restore_database") == 2
