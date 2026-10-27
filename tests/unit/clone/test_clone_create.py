from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.application.clone.contracts import CloneDataClass
from core.application.clone.service import (
    CloneCreatePlan,
    CloneCreateRequest,
    CloneService,
    clone_create_definition,
)
from core.application.lifecycle.contracts import (
    DeploymentConfig,
    LifecycleOperationJournal,
    LifecycleOperationKind,
)
from core.application.lifecycle.errors import LifecycleError
from core.application.maintenance.contracts import MaintenanceStatusResult
from core.application.release.contracts import ReleaseIdentity, ReleaseImageIdentity
from core.application.release.images import required_product_roles
from core.infrastructure.lifecycle.state_store import LifecycleStateStore

_NOW = datetime(2026, 9, 26, 9, 8, 7, tzinfo=UTC)
_COMMIT = "a" * 40
_INDEX = "sha256:" + "1" * 64
_MANIFEST = "sha256:" + "2" * 64
_CONFIG = "sha256:" + "3" * 64


def _release(profile: str = "scalable") -> ReleaseIdentity:
    registry = "ghcr.io/example"
    platform = "linux/arm64"
    return ReleaseIdentity(
        version="1.2.3",
        tag="v1.2.3",
        commit_sha=_COMMIT,
        registry=registry,
        profile=profile,
        platform=platform,
        images=tuple(
            ReleaseImageIdentity(
                role=role.role,
                registry_ref=f"{registry}/{role.repository}@{_MANIFEST}",
                index_digest=_INDEX,
                manifest_digest=_MANIFEST,
                config_digest=_CONFIG,
                revision=_COMMIT,
                platform=platform,
            )
            for role in required_product_roles(profile)
        ),
    )


class _Maintenance:
    def __init__(self, status: str = "enabled", owner_kind: str = "manual") -> None:
        self.value = MaintenanceStatusResult(status=status, owner_kind=owner_kind)

    def status(self) -> MaintenanceStatusResult:
        return self.value


class _Configuration:
    def __init__(self, config: DeploymentConfig | None) -> None:
        self.config = config

    def read_active(self) -> DeploymentConfig | None:
        return self.config


class _ReleaseState:
    def __init__(self, release: ReleaseIdentity | None) -> None:
        self.release = release

    def load_installed_release(self) -> ReleaseIdentity | None:
        return self.release


class _Verifier:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls: list[ReleaseIdentity] = []

    def verify(self, release: ReleaseIdentity) -> ReleaseIdentity:
        self.calls.append(release)
        if self.failure is not None:
            raise self.failure
        return release


class _Runtime:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.plan: CloneCreatePlan | None = None
        self.published = False
        self.cleaned = False

    def preflight(self, plan: CloneCreatePlan) -> None:
        self.calls.append("preflight")
        self.plan = plan

    def capture_source(self, plan: CloneCreatePlan) -> None:
        self.calls.append("capture_source")
        self.plan = plan

    def assemble_archive(self, plan: CloneCreatePlan, *, passphrase_source) -> None:
        assert passphrase_source() == b"clone passphrase"
        self.calls.append("assemble_archive")

    def verify_archive(self, plan: CloneCreatePlan, *, passphrase_source) -> None:
        assert passphrase_source() == b"clone passphrase"
        self.calls.append("verify_archive")

    def publish_archive(self, plan: CloneCreatePlan) -> None:
        self.calls.append("publish_archive")
        self.published = True

    def postcondition_holds(self, plan: CloneCreatePlan, step_id: str) -> bool:
        return step_id == "publish_archive" and self.published

    def artifact_size(self, plan: CloneCreatePlan) -> int:
        return 123

    def archive_digest(self, plan: CloneCreatePlan) -> str:
        return "f" * 64

    def cleanup(
        self,
        plan: CloneCreatePlan,
        *,
        preserve_recovery: bool = False,
        preserve_resume: bool = False,
    ) -> None:
        assert preserve_recovery is False
        assert preserve_resume is False
        self.calls.append("cleanup")
        self.cleaned = True


def _service(
    tmp_path: Path,
    *,
    maintenance: _Maintenance | None = None,
    config: DeploymentConfig | None = None,
    release: ReleaseIdentity | None = None,
    verifier: _Verifier | None = None,
    runtime: _Runtime | None = None,
    resolve_output_directory=None,
    installation_ready=None,
) -> tuple[CloneService, _Runtime, _Verifier, LifecycleStateStore]:
    selected_runtime = runtime or _Runtime()
    selected_verifier = verifier or _Verifier()
    state = LifecycleStateStore(tmp_path / "lifecycle")
    service = CloneService(
        maintenance=maintenance or _Maintenance(),
        lifecycle_state=state,
        configuration=_Configuration(config or DeploymentConfig(file_storage="local_fs")),
        release_state=_ReleaseState(release or _release()),
        release_verifier=selected_verifier,
        runtime=selected_runtime,
        source_installation_id=lambda: "source-installation",
        now=lambda: _NOW,
        resolve_output_directory=resolve_output_directory,
        installation_ready=installation_ready,
    )
    return service, selected_runtime, selected_verifier, state


def test_clone_create_rejects_unfinished_installer_before_runtime_preflight(
    tmp_path: Path,
) -> None:
    def reject(_operation_id: str | None) -> None:
        raise LifecycleError.stable_failure(
            code="clone_installer_operation_unfinished",
            cause="Installer state is unfinished.",
            remediation="Resume the installer.",
            step="preflight",
        )

    service, runtime, verifier, _state = _service(
        tmp_path,
        installation_ready=reject,
    )

    request = CloneCreateRequest(output_directory=str(tmp_path / "output"))
    with pytest.raises(LifecycleError, match="Installer state"):
        service.create(
            request,
            passphrase_source=lambda: b"clone passphrase",
        )

    assert runtime.calls == []
    assert verifier.calls == []


def test_clone_create_resolves_documented_relative_backup_directory(
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "configured-backups"
    service, runtime, _verifier, _state = _service(
        tmp_path,
        resolve_output_directory=lambda raw: backup_root if raw == "backups" else Path(raw),
    )

    result = service.create(
        CloneCreateRequest(output_directory="backups", operation_id="clone-relative"),
        passphrase_source=lambda: b"clone passphrase",
    )

    assert runtime.plan is not None
    assert Path(runtime.plan.output_path).parent == backup_root
    assert Path(result.path).parent == backup_root


def test_clone_create_preflights_then_runs_one_lifecycle_operation(tmp_path: Path) -> None:
    service, runtime, verifier, _state = _service(tmp_path)

    result = service.create(
        CloneCreateRequest(output_directory=str(tmp_path / "output"), operation_id="clone-op"),
        passphrase_source=lambda: b"clone passphrase",
    )

    assert verifier.calls == [_release()]
    assert runtime.calls == [
        "preflight",
        "capture_source",
        "assemble_archive",
        "verify_archive",
        "publish_archive",
        "cleanup",
    ]
    assert result.operation_id == "clone-op"
    assert result.path.endswith("flow-steward-clone-20260926T090807Z-scalable-1.2.3.fsclone")
    assert result.size_bytes == 123
    assert result.sha256 == "f" * 64
    assert result.release == _release()
    assert "passphrase" not in result.model_dump_json()


@pytest.mark.parametrize(
    ("status", "owner"),
    (("disabled", None), ("draining", "manual"), ("enabled", "release")),
)
def test_clone_create_requires_fully_enabled_manual_maintenance_before_mutation(
    tmp_path: Path,
    status: str,
    owner: str | None,
) -> None:
    service, runtime, _verifier, _state = _service(
        tmp_path,
        maintenance=_Maintenance(status=status, owner_kind=owner),
    )

    request = CloneCreateRequest(output_directory=str(tmp_path / "output"))
    with pytest.raises(LifecycleError, match="maintenance"):
        service.create(
            request,
            passphrase_source=lambda: b"clone passphrase",
        )

    assert runtime.calls == []


def test_clone_create_refuses_an_unrelated_unfinished_operation(tmp_path: Path) -> None:
    service, runtime, _verifier, state = _service(tmp_path)
    state.save_journal(
        LifecycleOperationJournal(
            operation_id="other-operation",
            kind=LifecycleOperationKind.BACKUP_CREATE,
            recovery_command="flow-steward backup create",
        )
    )

    request = CloneCreateRequest(output_directory=str(tmp_path / "output"))
    with pytest.raises(LifecycleError) as raised:
        service.create(
            request,
            passphrase_source=lambda: b"clone passphrase",
        )

    assert raised.value.exit_code.name == "RECOVERY_REQUIRED"
    assert runtime.calls == []


def test_clone_create_rejects_unverified_or_local_release_before_runtime_changes(
    tmp_path: Path,
) -> None:
    verifier = _Verifier(failure=RuntimeError("local image is not official"))
    service, runtime, _verifier, _state = _service(tmp_path, verifier=verifier)

    request = CloneCreateRequest(output_directory=str(tmp_path / "output"))
    with pytest.raises(LifecycleError, match="official"):
        service.create(
            request,
            passphrase_source=lambda: b"clone passphrase",
        )

    assert runtime.calls == []


def test_clone_create_excludes_external_s3_objects_from_capture_plan(tmp_path: Path) -> None:
    config = DeploymentConfig(file_storage="external_s3")
    service, runtime, _verifier, _state = _service(tmp_path, config=config)

    service.create(
        CloneCreateRequest(output_directory=str(tmp_path / "output")),
        passphrase_source=lambda: b"clone passphrase",
    )

    assert runtime.plan is not None
    classes = {item.data_class for item in runtime.plan.source_plan.items}
    assert CloneDataClass.LOCAL_ARTIFACTS not in classes
    assert CloneDataClass.BUNDLED_STORAGE not in classes


def test_clone_create_definition_is_resumable_and_secret_free(tmp_path: Path) -> None:
    service, runtime, _verifier, _state = _service(tmp_path)
    service.create(
        CloneCreateRequest(output_directory=str(tmp_path / "output"), operation_id="clone-op"),
        passphrase_source=lambda: b"clone passphrase",
    )
    assert runtime.plan is not None

    definition = clone_create_definition(runtime.plan)

    assert definition.kind is LifecycleOperationKind.CLONE_CREATE
    assert tuple(step.step_id for step in definition.steps) == (
        "capture_source",
        "assemble_archive",
        "verify_archive",
        "publish_archive",
    )
    assert "passphrase" not in definition.model_dump_json()
