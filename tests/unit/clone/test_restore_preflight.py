from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.application.clone.contracts import CloneDataClass, CloneEntry, CloneManifest
from core.application.clone.planning import (
    CloneTargetShell,
    merge_source_configuration,
    merge_source_configuration_with_shell,
)
from core.application.clone.service import CloneRestoreRequest, CloneService
from core.application.lifecycle.contracts import (
    DeploymentConfig,
    LifecycleOperationJournal,
    LifecycleOperationKind,
)
from core.application.lifecycle.errors import LifecycleError
from core.application.maintenance.contracts import MaintenanceStatusResult
from core.application.release.catalog import ReleaseNotFoundError
from core.application.release.contracts import ReleaseIdentity, ReleaseImageIdentity
from core.application.release.images import required_product_roles
from core.infrastructure.lifecycle.state_store import LifecycleStateStore
from core.infrastructure.release.github import GitHubHTTPError

_NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
_COMMIT = "a" * 40
_DIGEST = "sha256:" + "1" * 64


def _release(*, profile: str = "scalable", platform: str = "linux/amd64") -> ReleaseIdentity:
    return ReleaseIdentity(
        version="1.2.3",
        tag="v1.2.3",
        commit_sha=_COMMIT,
        registry="ghcr.io/example",
        profile=profile,
        platform=platform,
        images=tuple(
            ReleaseImageIdentity(
                role=role.role,
                registry_ref=f"ghcr.io/example/{role.repository}@{_DIGEST}",
                index_digest=_DIGEST,
                manifest_digest=_DIGEST,
                config_digest=_DIGEST,
                revision=_COMMIT,
                platform=platform,
            )
            for role in required_product_roles(profile)
        ),
    )


def _manifest(*, profile: str = "scalable", platform: str = "linux/amd64") -> CloneManifest:
    definitions = (
        (CloneDataClass.LOCAL_ARTIFACTS, "artifacts", "directory", 0, None),
        (CloneDataClass.EXTENSIONS, "extensions", "directory", 0, None),
        (CloneDataClass.POSTGRESQL, "postgresql/database.dump", "file", 10, "a" * 64),
        (CloneDataClass.RABBITMQ, "rabbitmq", "directory", 0, None),
        (CloneDataClass.RELEASE_EVIDENCE, "evidence/configuration.json", "file", 1, "b" * 64),
        (CloneDataClass.RELEASE_EVIDENCE, "evidence/database-parity.json", "file", 1, "c" * 64),
        (CloneDataClass.RELEASE_EVIDENCE, "evidence/preservation.json", "file", 1, "f" * 64),
        (CloneDataClass.RELEASE_EVIDENCE, "evidence/release.json", "file", 1, "d" * 64),
        (CloneDataClass.SOURCE_SECRETS, "secrets/source.json", "file", 1, "e" * 64),
        (CloneDataClass.DURABLE_STATE, "state", "directory", 0, None),
    )
    entries = tuple(
        sorted(
            (
                CloneEntry(
                    data_class=data_class,
                    path=path,
                    entry_type=entry_type,
                    size=size,
                    mode=0o700 if entry_type == "directory" else 0o600,
                    sha256=digest,
                )
                for data_class, path, entry_type, size, digest in definitions
            ),
            key=lambda entry: entry.path,
        )
    )
    return CloneManifest(
        created_at=_NOW,
        release=_release(profile=profile, platform=platform),
        deployment_profile=profile,
        data_placement="host_directories" if profile == "scalable" else "compact_volume",
        file_storage="local_fs",
        source_installation_id="source-installation",
        entries=entries,
        entry_count=len(entries),
        aggregate_size=sum(entry.size for entry in entries),
        external_effect_categories=("webhooks", "schedules"),
        database_parity_evidence={"snapshot_sha256": "c" * 64},
        capture_evidence={"postgresql_format": "custom", "maintenance": "manual"},
    )


class _Verified:
    def __init__(self, root: Path, manifest: CloneManifest) -> None:
        self.root = root
        self.manifest = manifest
        self.configuration = DeploymentConfig(
            environment=(
                "production"
                if manifest.deployment_profile == "compact"
                or manifest.data_placement == "compact_volume"
                else "development"
            ),
            deployment_profile=manifest.deployment_profile,
            data_placement=manifest.data_placement,
            file_storage=manifest.file_storage,
        )
        self.artifact_size = 1
        self.artifact_sha256 = "d" * 64
        self.cleaned = False

    def cleanup(self) -> None:
        self.cleaned = True


class _Archive:
    def __init__(self, verified: _Verified, *, free_bytes: int = 10_000) -> None:
        self.verified = verified
        self.free_bytes_value = free_bytes
        self.calls: list[str] = []

    def stage_verified(self, path: str, *, passphrase_source) -> _Verified:
        self.calls.append(f"stage:{path}")
        assert passphrase_source() == b"restore passphrase"
        return self.verified

    def artifact_size(self, path: str) -> int:
        self.calls.append(f"size:{path}")
        return 1

    def artifact_sha256(self, path: str) -> str:
        self.calls.append(f"sha256:{path}")
        return "e" * 64

    def free_bytes(self) -> int:
        self.calls.append("free")
        return self.free_bytes_value

    def discard_staged(self, staged_root: str) -> None:
        self.calls.append(f"discard:{staged_root}")
        self.verified.cleanup()


class _Maintenance:
    def __init__(self, *, status: str = "enabled", owner: str | None = "manual") -> None:
        self.value = MaintenanceStatusResult(status=status, owner_kind=owner)

    def status(self) -> MaintenanceStatusResult:
        return self.value


class _Config:
    def __init__(self, value: DeploymentConfig) -> None:
        self.value = value

    def read_active(self) -> DeploymentConfig:
        return self.value


class _ExactRelease:
    def __init__(
        self,
        *,
        mismatch: bool = False,
        unavailable: bool = False,
        registry_invalid: bool = False,
        http_error: bool = False,
    ) -> None:
        self.mismatch = mismatch
        self.unavailable = unavailable
        self.registry_invalid = registry_invalid
        self.http_error = http_error
        self.calls: list[ReleaseIdentity] = []

    def for_exact_identity(self, identity: ReleaseIdentity) -> ReleaseIdentity:
        self.calls.append(identity)
        if self.unavailable:
            raise ReleaseNotFoundError("release v1.2.3 was not found")
        if self.registry_invalid:
            raise ValueError("registry release identity is incomplete")
        if self.http_error:
            raise GitHubHTTPError("GitHub Release endpoint returned HTTP 404")
        if self.mismatch:
            raise LifecycleError.stable_failure(
                code="release_exact_identity_mismatch",
                cause="The exact release differs from the Clone manifest.",
                remediation="Make the archived exact release available before restoring.",
                step="release_policy",
            )
        return identity


def _service(
    tmp_path: Path,
    *,
    manifest: CloneManifest | None = None,
    config: DeploymentConfig | None = None,
    maintenance: _Maintenance | None = None,
    free_bytes: int = 10_000,
    machine: str = "x86_64",
    mismatch: bool = False,
    release_unavailable: bool = False,
    release_registry_invalid: bool = False,
    release_http_error: bool = False,
    resolve_archive_path=None,
    installation_ready=None,
) -> tuple[CloneService, _Verified, _Archive, LifecycleStateStore]:
    verified = _Verified(tmp_path / "staged", manifest or _manifest())
    archive = _Archive(verified, free_bytes=free_bytes)
    state = LifecycleStateStore(tmp_path / "lifecycle")
    target = config or DeploymentConfig(
        deployment_profile="scalable",
        file_storage="external_s3",
        public_host="target.example.test",
    )
    service = CloneService(
        maintenance=maintenance or _Maintenance(),
        lifecycle_state=state,
        configuration=_Config(target),
        release_state=None,
        release_verifier=None,
        runtime=None,
        source_installation_id=lambda: "unused",
        restore_archive=archive,
        exact_release=_ExactRelease(
            mismatch=mismatch,
            unavailable=release_unavailable,
            registry_invalid=release_registry_invalid,
            http_error=release_http_error,
        ),
        target_machine=lambda: machine,
        resolve_archive_path=resolve_archive_path,
        target_shell=lambda active: CloneTargetShell.from_config(
            installation_id="target-installation",
            compose_project="flow-steward-target",
            config=active,
            host_uid=1000,
            host_gid=1000,
            filesystem_roots={"extensions": "/srv/flow-steward/extensions"},
            postgres_roles=("flow_steward",),
            postgres_identity_fingerprint="f" * 64,
        ),
        installation_ready=installation_ready,
    )
    return service, verified, archive, state


def test_restore_rejects_unfinished_installer_before_archive_staging(tmp_path: Path) -> None:
    def reject(_operation_id: str | None) -> None:
        raise LifecycleError.stable_failure(
            code="clone_installer_operation_unfinished",
            cause="Installer state is unfinished.",
            remediation="Resume the installer.",
            step="preflight",
        )

    service, verified, archive, _state = _service(
        tmp_path,
        installation_ready=reject,
    )
    request = CloneRestoreRequest(archive_path=str(tmp_path / "clone.fsclone"))

    with pytest.raises(LifecycleError, match="Installer state"):
        service.prepare_restore(
            request,
            passphrase_source=lambda: b"restore passphrase",
        )

    assert archive.calls == []
    assert verified.cleaned is False


def test_restore_preflight_resolves_documented_relative_backup_path(tmp_path: Path) -> None:
    selected = tmp_path / "configured-backups/source.fsclone"
    service, _verified, archive, _state = _service(
        tmp_path,
        resolve_archive_path=lambda raw: selected if raw == "backups/source.fsclone" else Path(raw),
    )

    prepared = service.prepare_restore(
        CloneRestoreRequest(archive_path="backups/source.fsclone"),
        passphrase_source=lambda: b"restore passphrase",
    )

    assert archive.calls[0] == f"stage:{selected}"
    assert prepared.archive_path == str(selected)


def test_restore_configuration_imports_source_state_but_retains_target_shell() -> None:
    source = DeploymentConfig(
        environment="production",
        deployment_profile="scalable",
        file_storage="external_s3",
        s3_endpoint="https://source-storage.example.test",
        s3_bucket="source-bucket",
        public_host="source.example.test",
        public_port=9443,
    )
    target = DeploymentConfig(
        environment="development",
        deployment_profile="scalable",
        file_storage="local_fs",
        public_host="target.example.test",
        public_port=8443,
        runtime_http_port=18080,
    )

    restored = merge_source_configuration(source=source, target=target)

    assert restored.file_storage == "external_s3"
    assert restored.s3_endpoint == "https://source-storage.example.test"
    assert restored.s3_bucket == "source-bucket"
    assert restored.environment == "development"
    assert restored.public_host == "target.example.test"
    assert restored.public_port == 8443
    assert restored.runtime_http_port == 18080


def test_restore_configuration_uses_preflight_shell_without_rereading_target_state() -> None:
    source = DeploymentConfig(
        environment="production",
        deployment_profile="scalable",
        file_storage="external_s3",
        s3_endpoint="https://source-storage.example.test",
        public_host="source.example.test",
        public_port=9443,
        proxy_network="source-proxy",
    )
    target = DeploymentConfig(
        environment="development",
        deployment_profile="scalable",
        public_host="target.example.test",
        public_port=8443,
        runtime_http_port=18080,
        redirect_http_port=18081,
        ingress_mode="external_proxy",
        tls_mode="reverse_proxy",
        reverse_proxy_backend_host="127.0.0.1",
        reverse_proxy_backend_port=18082,
        reverse_proxy_mcp_port=18083,
        proxy_placement="host",
        proxy_kind="nginx",
        proxy_trusted_ips=("10.0.0.0/8",),
        traefik_network="target-traefik",
        traefik_https_entrypoint="target-websecure",
        traefik_http_entrypoint="target-web",
        traefik_cert_resolver="target-resolver",
        proxy_network="target-proxy",
        acme_contact="target@example.test",
        acme_directory_url="https://acme.target/directory",
        certificate_path="/target/tls/cert.pem",
        private_key_path="/target/tls/key.pem",
        extension_installer_origin="http://target.example.test:8099",
    )
    shell = CloneTargetShell.from_config(
        installation_id="target-installation",
        compose_project="target-project",
        config=target,
        host_uid=1200,
        host_gid=1300,
        filesystem_roots={"extensions": "/srv/target/extensions"},
        postgres_roles=("target-role",),
        postgres_identity_fingerprint="f" * 64,
    )

    restored = merge_source_configuration_with_shell(source=source, target_shell=shell)

    assert restored.file_storage == "external_s3"
    assert restored.s3_endpoint == "https://source-storage.example.test"
    for field in (
        "environment",
        "deployment_profile",
        "data_placement",
        "public_host",
        "public_port",
        "runtime_http_port",
        "redirect_http_port",
        "ingress_mode",
        "tls_mode",
        "reverse_proxy_backend_host",
        "reverse_proxy_backend_port",
        "reverse_proxy_mcp_port",
        "proxy_placement",
        "proxy_kind",
        "proxy_trusted_ips",
        "traefik_network",
        "traefik_https_entrypoint",
        "traefik_http_entrypoint",
        "traefik_cert_resolver",
        "proxy_network",
        "acme_contact",
        "acme_directory_url",
        "certificate_path",
        "private_key_path",
        "extension_installer_origin",
    ):
        assert getattr(restored, field) == getattr(target, field)


def test_target_shell_binds_every_retained_configuration_and_postgres_identity() -> None:
    config = DeploymentConfig(
        environment="development",
        deployment_profile="scalable",
        extension_installer_origin="http://target.example.test:8099",
    )

    shell = CloneTargetShell.from_config(
        installation_id="target-installation",
        compose_project="target-project",
        config=config,
        host_uid=1000,
        host_gid=1001,
        filesystem_roots={"extensions": "/srv/extensions"},
        postgres_roles=("target-role",),
        postgres_identity_fingerprint="f" * 64,
    )

    assert shell.configuration.environment == "development"
    assert shell.configuration.extension_installer_origin == "http://target.example.test:8099"
    assert shell.postgres_identity_fingerprint == "f" * 64


def test_restore_preflight_authenticates_then_builds_immutable_target_bound_plan(
    tmp_path: Path,
) -> None:
    service, verified, archive, _state = _service(tmp_path)

    prepared = service.prepare_restore(
        CloneRestoreRequest(archive_path=str(tmp_path / "clone.fsclone")),
        passphrase_source=lambda: b"restore passphrase",
    )

    assert archive.calls == [
        f"stage:{tmp_path / 'clone.fsclone'}",
        "free",
    ]
    assert prepared.staged_root == str(verified.root)
    assert prepared.plan.target_installation_id == "target-installation"
    assert prepared.plan.target_profile == "scalable"
    assert prepared.plan.target_platform == "linux/amd64"
    assert prepared.plan.replaced_data_classes == (
        "durable_state",
        "extensions",
        "local_artifacts",
        "postgresql",
        "rabbitmq",
        "release_evidence",
        "source_secrets",
    )
    assert prepared.plan.retained_shell.configuration.public_host == "target.example.test"
    assert len(prepared.plan.plan_digest) == 64
    assert "passphrase" not in prepared.model_dump_json()

    review = service.review_restore(prepared)
    assert review["target_installation_id"] == "target-installation"
    assert review["public_host"] == "target.example.test"
    retained_shell = review["retained_shell"]
    assert isinstance(retained_shell, dict)
    assert retained_shell["ports"] == {
        "public": 8099,
        "runtime_http": 8080,
        "redirect_http": 8080,
        "reverse_proxy_backend": 18099,
        "reverse_proxy_mcp": 18100,
    }
    assert retained_shell["filesystem_roots"] == {"extensions": "/srv/flow-steward/extensions"}
    assert retained_shell["host_uid"] == 1000
    assert retained_shell["host_gid"] == 1000
    assert retained_shell["postgres_roles"] == ("flow_steward",)
    assert review["external_effect_categories"] == ("schedules", "webhooks")
    assert review["plan_digest"] == prepared.plan.plan_digest
    recommendation = str(review["rollback_recommendation"])
    assert "flow-steward clone create" in recommendation
    assert "does not back up the target automatically" in recommendation


def test_restore_preflight_uses_identity_of_the_staged_authenticated_descriptor(
    tmp_path: Path,
) -> None:
    service, verified, archive, _state = _service(tmp_path)

    prepared = service.prepare_restore(
        CloneRestoreRequest(archive_path=str(tmp_path / "clone.fsclone")),
        passphrase_source=lambda: b"restore passphrase",
    )

    assert prepared.plan.archive_sha256 == verified.artifact_sha256
    assert not any(call.startswith("sha256:") for call in archive.calls)


@pytest.mark.parametrize("failure", ("not_found", "registry_invalid", "github_http"))
def test_restore_preflight_maps_exact_release_failures_to_dependency_without_mutation(
    tmp_path: Path,
    failure: str,
) -> None:
    service, verified, _archive, state = _service(
        tmp_path,
        release_unavailable=failure == "not_found",
        release_registry_invalid=failure == "registry_invalid",
        release_http_error=failure == "github_http",
    )
    request = CloneRestoreRequest(archive_path=str(tmp_path / "clone.fsclone"))

    with pytest.raises(LifecycleError) as raised:
        service.prepare_restore(
            request,
            passphrase_source=lambda: b"restore passphrase",
        )

    assert raised.value.code == "clone_restore_exact_release_unavailable"
    assert raised.value.retryable is True
    assert raised.value.step == "release_policy"
    assert state.unfinished_journals() == ()
    assert verified.cleaned is True


@pytest.mark.parametrize(
    ("manifest", "config", "machine", "match"),
    (
        (_manifest(profile="compact"), DeploymentConfig(), "x86_64", "profile"),
        (_manifest(platform="linux/arm64"), DeploymentConfig(), "x86_64", "platform"),
    ),
)
def test_restore_preflight_rejects_profile_or_platform_mismatch_and_cleans_staging(
    tmp_path: Path,
    manifest: CloneManifest,
    config: DeploymentConfig,
    machine: str,
    match: str,
) -> None:
    service, verified, _archive, _state = _service(
        tmp_path,
        manifest=manifest,
        config=config,
        machine=machine,
    )
    request = CloneRestoreRequest(archive_path=str(tmp_path / "clone.fsclone"))

    with pytest.raises(LifecycleError, match=match):
        service.prepare_restore(
            request,
            passphrase_source=lambda: b"restore passphrase",
        )

    assert verified.cleaned is True


@pytest.mark.parametrize("profile", ("compact", "scalable"))
@pytest.mark.parametrize("source_placement", ("host_directories", "compact_volume"))
def test_restore_preflight_rejects_cross_placement_before_release_resolution(
    tmp_path: Path, profile: str, source_placement: str
) -> None:
    manifest = _manifest(profile=profile).model_copy(update={"data_placement": source_placement})
    target_placement = (
        "compact_volume" if source_placement == "host_directories" else "host_directories"
    )
    service, verified, _archive, state = _service(
        tmp_path,
        manifest=manifest,
        config=DeploymentConfig(
            environment="production",
            deployment_profile=profile,
            data_placement=target_placement,
        ),
        release_unavailable=True,
    )

    request = CloneRestoreRequest(archive_path=str(tmp_path / "clone.fsclone"))
    with pytest.raises(LifecycleError) as raised:
        service.prepare_restore(request, passphrase_source=lambda: b"restore passphrase")

    assert raised.value.code == "clone_restore_data_placement_mismatch"
    assert verified.cleaned is True
    assert state.unfinished_journals() == ()


def test_restore_preflight_rejects_non_manual_maintenance_before_staging(tmp_path: Path) -> None:
    service, verified, archive, _state = _service(
        tmp_path,
        maintenance=_Maintenance(owner="release"),
    )
    request = CloneRestoreRequest(archive_path=str(tmp_path / "clone.fsclone"))

    with pytest.raises(LifecycleError, match="Clone restore requires"):
        service.prepare_restore(
            request,
            passphrase_source=lambda: b"restore passphrase",
        )

    assert archive.calls == []
    assert verified.cleaned is False


def test_restore_preflight_rejects_unfinished_operation_without_staging(tmp_path: Path) -> None:
    service, _verified, archive, state = _service(tmp_path)
    state.save_journal(
        LifecycleOperationJournal(
            operation_id="unfinished",
            kind=LifecycleOperationKind.BACKUP_CREATE,
            recovery_command="flow-steward backup create",
        )
    )
    request = CloneRestoreRequest(archive_path=str(tmp_path / "clone.fsclone"))

    with pytest.raises(LifecycleError):
        service.prepare_restore(
            request,
            passphrase_source=lambda: b"restore passphrase",
        )

    assert archive.calls == []


def test_restore_preflight_rehydrates_matching_unfinished_restore(tmp_path: Path) -> None:
    service, _verified, _archive, state = _service(tmp_path)
    state.save_journal(
        LifecycleOperationJournal(
            operation_id="clone-restore-resume",
            kind=LifecycleOperationKind.CLONE_RESTORE,
            recovery_command=(
                "flow-steward clone restore /private/source.fsclone "
                "--operation-id clone-restore-resume --passphrase-stdin"
            ),
        )
    )

    prepared = service.prepare_restore(
        CloneRestoreRequest(
            archive_path=str(tmp_path / "clone.fsclone"),
            operation_id="clone-restore-resume",
        ),
        passphrase_source=lambda: b"restore passphrase",
    )

    assert prepared.operation_id == "clone-restore-resume"
    assert prepared.plan.archive_sha256 == "d" * 64


def test_restore_preflight_rejects_exact_release_or_space_failure_and_cleans(
    tmp_path: Path,
) -> None:
    service, verified, _archive, _state = _service(tmp_path, mismatch=True)
    request = CloneRestoreRequest(archive_path=str(tmp_path / "clone.fsclone"))

    with pytest.raises(LifecycleError, match="exact"):
        service.prepare_restore(
            request,
            passphrase_source=lambda: b"restore passphrase",
        )
    assert verified.cleaned is True

    service, verified, _archive, _state = _service(tmp_path / "space", free_bytes=9)
    with pytest.raises(LifecycleError, match="space"):
        service.prepare_restore(
            request,
            passphrase_source=lambda: b"restore passphrase",
        )
    assert verified.cleaned is True
