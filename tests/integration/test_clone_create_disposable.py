from __future__ import annotations

import hashlib
import json
import os
import stat
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.application.clone.contracts import CloneDataClass, CloneEntry
from core.application.clone.service import CloneCreateRequest, CloneService
from core.application.lifecycle.contracts import DeploymentConfig
from core.application.lifecycle.errors import LifecycleError
from core.application.maintenance.contracts import MaintenanceStatusResult
from core.application.release.contracts import ReleaseIdentity, ReleaseImageIdentity
from core.application.release.images import required_product_roles
from core.infrastructure.clone.archive_gateway import CloneArchiveGateway
from core.infrastructure.clone.runtime_gateway import CloneRuntimeGateway
from core.infrastructure.clone.volume_metadata import inventory_volume_metadata
from core.infrastructure.lifecycle.state_store import LifecycleStateStore

_PASSPHRASE = b"disposable clone passphrase"
_NOW = datetime(2026, 9, 26, 9, 8, 7, tzinfo=UTC)
_COMMIT = "b" * 40
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
    def status(self) -> MaintenanceStatusResult:
        return MaintenanceStatusResult(status="enabled", owner_kind="manual")


class _Configuration:
    def __init__(self, storage: str = "local_fs", *, profile: str = "scalable") -> None:
        self.value = DeploymentConfig(
            deployment_profile=profile,
            environment="production" if profile == "compact" else "development",
            data_placement="host_directories",
            file_storage=storage,
            s3_endpoint=("https://source-storage.example.test" if storage == "external_s3" else ""),
            s3_bucket="source-bucket",
        )

    def read_active(self) -> DeploymentConfig:
        return self.value


class _ReleaseState:
    def __init__(self, profile: str = "scalable") -> None:
        self.profile = profile

    def load_installed_release(self) -> ReleaseIdentity:
        return _release(self.profile)


class _Verifier:
    def verify(self, release: ReleaseIdentity) -> ReleaseIdentity:
        return release


class _Backup:
    def __init__(self, root: Path, *, failure: Exception | None = None) -> None:
        self.root = root
        self.failure = failure
        self.calls = 0

    @contextmanager
    def clone_dump(self, operation_id: str):
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        self.root.mkdir(parents=True, exist_ok=True)
        dump = self.root / f"{operation_id}.dump"
        dump.write_bytes(b"PGDMP cloned database")
        dump.chmod(0o600)
        try:
            yield dump
        finally:
            dump.unlink(missing_ok=True)


class _Services:
    def __init__(
        self,
        states: dict[str, bool],
        *,
        fail_start: bool = False,
    ) -> None:
        self.states = dict(states)
        self.fail_start = fail_start
        self.events: list[tuple[str, tuple[str, ...]]] = []

    def service_states(self, names: tuple[str, ...]) -> dict[str, bool]:
        return {name: self.states.get(name, False) for name in names}

    def stop(self, names: tuple[str, ...]) -> None:
        self.events.append(("stop", names))
        for name in names:
            self.states[name] = False

    def start(self, names: tuple[str, ...]) -> None:
        self.events.append(("start", names))
        if self.fail_start:
            raise RuntimeError("service restoration failed")
        for name in names:
            self.states[name] = True


class _Volumes:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls: list[str] = []

    def capture(self, name: str, destination: Path) -> tuple[CloneEntry, ...]:
        self.calls.append(name)
        if self.failure is not None:
            raise self.failure
        destination.mkdir(parents=True, exist_ok=True)
        destination.chmod(0o700)
        payload = destination / "state.bin"
        payload.write_bytes(f"{name} durable state".encode())
        payload.chmod(0o600)
        data_class = (
            CloneDataClass.RABBITMQ if name == "rabbitmq" else CloneDataClass.BUNDLED_STORAGE
        )
        return (
            CloneEntry(
                data_class=data_class,
                path=destination.name,
                entry_type="directory",
                size=0,
                mode=0o700,
                uid=os.getuid(),
                gid=os.getgid(),
            ),
            CloneEntry(
                data_class=data_class,
                path=f"{destination.name}/state.bin",
                entry_type="file",
                size=payload.stat().st_size,
                mode=0o600,
                uid=os.getuid(),
                gid=os.getgid(),
                sha256=hashlib.sha256(payload.read_bytes()).hexdigest(),
            ),
        )


class _MixedVolumes(_Volumes):
    def validate_compact_filesystems(self, items, *, durable_state_only: bool = False) -> None:
        assert items == ((CloneDataClass.DURABLE_STATE, "state"),)
        assert durable_state_only is True

    def capture_compact_filesystems(
        self, destination: Path, items, *, durable_state_only: bool = False
    ) -> tuple[CloneEntry, ...]:
        self.validate_compact_filesystems(items, durable_state_only=durable_state_only)
        root = destination / "state"
        root.mkdir(mode=0o750)
        (root / "product.json").write_text("volume-product")
        (root / "product.json").chmod(0o640)
        return tuple(
            entry.model_copy(update={"uid": 10007, "gid": 10102})
            for entry in inventory_volume_metadata(
                root, archive_root="state", data_class=CloneDataClass.DURABLE_STATE
            )
        )


class _FailOnceAfterPublishRuntime(CloneRuntimeGateway):
    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.failed_after_publish = False

    def publish_archive(self, plan) -> None:
        super().publish_archive(plan)
        if not self.failed_after_publish:
            self.failed_after_publish = True
            raise LifecycleError.dependency(
                code="clone_publish_acknowledgement_failed",
                cause="Publication completed before acknowledgement failed.",
                remediation="Resume the same Clone operation.",
                step="publish_archive",
            )


def _source_roots(tmp_path: Path) -> dict:
    roots = {}
    for name in ("artifacts", "extensions", "state"):
        root = tmp_path / "sources" / name
        root.mkdir(parents=True)
        payload = root / "data.txt"
        payload.write_text(f"{name} data", encoding="utf-8")
        payload.chmod(0o640)
        roots[name] = root
    return roots


def _service(
    tmp_path: Path,
    *,
    storage: str = "local_fs",
    backup_failure: Exception | None = None,
    volume_failure: Exception | None = None,
    fail_start: bool = False,
    fail_once_after_publish: bool = False,
    initial_states: dict[str, bool] | None = None,
    source_secrets: dict[str, str] | None = None,
    staging_inside_durable_state: bool = False,
    mixed_compact: bool = False,
) -> tuple[CloneService, _Services, _Volumes, CloneRuntimeGateway, _Backup]:
    backup = _Backup(tmp_path / "backup-tmp", failure=backup_failure)
    services = _Services(
        initial_states or {"rabbitmq-fs": True, "seaweedfs": True},
        fail_start=fail_start,
    )
    volumes = (
        _MixedVolumes(failure=volume_failure) if mixed_compact else _Volumes(failure=volume_failure)
    )
    runtime_type = _FailOnceAfterPublishRuntime if fail_once_after_publish else CloneRuntimeGateway
    source_roots = _source_roots(tmp_path)
    if mixed_compact:
        source_roots["state"] = tmp_path / "host-inaccessible-volume-state"
    runtime = runtime_type(
        staging_root=(
            source_roots["state"] / "clone"
            if staging_inside_durable_state
            else tmp_path / "clone-staging"
        ),
        output_root=tmp_path / "output",
        backup=backup,
        services=services,
        volumes=volumes,
        source_roots=source_roots,
        archive=CloneArchiveGateway(),
        source_secrets=source_secrets,
        database_parity_evidence={"schema_fingerprint": "schema-1", "row_count": 42},
        capture_database_parity=lambda output: output.write_text(
            json.dumps({"snapshot": "source"}),
            encoding="utf-8",
        ),
        capture_preservation=lambda plan, output: output.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "operation_id": plan.operation_id,
                    "stage": "before",
                    "database_reachable": True,
                    "fields": {"source": "captured"},
                    "captured_at": _NOW.isoformat(),
                }
            ),
            encoding="utf-8",
        ),
        external_effect_categories=("schedules", "webhooks"),
    )
    service = CloneService(
        maintenance=_Maintenance(),
        lifecycle_state=LifecycleStateStore(tmp_path / "lifecycle"),
        configuration=_Configuration(storage, profile="compact" if mixed_compact else "scalable"),
        release_state=_ReleaseState("compact" if mixed_compact else "scalable"),
        release_verifier=_Verifier(),
        runtime=runtime,
        source_installation_id=lambda: "source-installation",
        now=lambda: _NOW,
    )
    return service, services, volumes, runtime, backup


def test_mixed_compact_clone_keeps_host_trees_and_authenticated_volume_state(
    tmp_path: Path,
) -> None:
    service, _services, _volumes, _runtime, _backup = _service(tmp_path, mixed_compact=True)
    result = service.create(
        CloneCreateRequest(output_directory=str(tmp_path / "output"), operation_id="mixed-compact"),
        passphrase_source=lambda: _PASSPHRASE,
    )
    with Path(result.path).open("rb") as source:
        verified = CloneArchiveGateway().stage_verified(
            source,
            staging_parent=tmp_path / "verified-mixed",
            passphrase_source=lambda: _PASSPHRASE,
        )
    try:
        assert (verified.root / "state/product.json").read_text() == "volume-product"
        assert (verified.root / "extensions/data.txt").read_text() == "extensions data"
        assert (verified.root / "artifacts/data.txt").read_text() == "artifacts data"
        state_entries = [
            entry for entry in verified.manifest.entries if entry.path.startswith("state")
        ]
        assert all((entry.uid, entry.gid) == (10007, 10102) for entry in state_entries)
        assert verified.manifest.data_placement == "host_directories"
    finally:
        verified.cleanup()


def test_clone_create_excludes_its_own_staging_from_durable_state(tmp_path: Path) -> None:
    service, _services, _volumes, runtime, _backup = _service(
        tmp_path,
        staging_inside_durable_state=True,
    )
    durable = runtime._source_roots[CloneDataClass.DURABLE_STATE]
    for excluded in ("operations", "runtime"):
        path = durable / excluded
        path.mkdir()
        (path / "must-not-clone.json").write_text("{}", encoding="utf-8")

    result = service.create(
        CloneCreateRequest(output_directory=str(tmp_path / "output"), operation_id="nested"),
        passphrase_source=lambda: _PASSPHRASE,
    )

    with Path(result.path).open("rb") as source:
        verified = CloneArchiveGateway().stage_verified(
            source,
            staging_parent=tmp_path / "verified-nested",
            passphrase_source=lambda: _PASSPHRASE,
        )
    try:
        assert all(
            all(
                entry.path != f"state/{excluded}"
                and not entry.path.startswith(f"state/{excluded}/")
                for excluded in ("clone", "operations", "runtime")
            )
            for entry in verified.manifest.entries
        )
    finally:
        verified.cleanup()


@pytest.mark.parametrize("local_directory", ("secret-input", "secret-output"))
def test_clone_create_excludes_deployment_local_credentials_from_real_archive(
    tmp_path: Path, local_directory: str
) -> None:
    service, services, _volumes, runtime, _backup = _service(
        tmp_path, source_secrets={"ENCRYPTION_KEY": "synthetic-source-owned-key"}
    )
    durable = runtime._source_roots[CloneDataClass.DURABLE_STATE]
    credential = durable / local_directory / "synthetic-operator-credential"
    credential.parent.mkdir(mode=0o700)
    credential.write_text("synthetic-local-only", encoding="utf-8")
    credential.chmod(0o600)

    result = service.create(
        CloneCreateRequest(output_directory=str(tmp_path / "output")),
        passphrase_source=lambda: _PASSPHRASE,
    )
    with Path(result.path).open("rb") as source:
        verified = CloneArchiveGateway().stage_verified(
            source,
            staging_parent=tmp_path / "verified-local-credentials",
            passphrase_source=lambda: _PASSPHRASE,
        )
    try:
        assert (verified.root / "state/data.txt").read_text() == "state data"
        assert json.loads((verified.root / "secrets/source.json").read_text()) == {
            "ENCRYPTION_KEY": "synthetic-source-owned-key"
        }
        assert services.states == {"rabbitmq-fs": True, "seaweedfs": True}
        assert not (verified.root / "state" / local_directory).exists()
        assert all(
            entry.path != f"state/{local_directory}"
            and not entry.path.startswith(f"state/{local_directory}/")
            for entry in verified.manifest.entries
        )
        assert credential.read_text() == "synthetic-local-only"
    finally:
        verified.cleanup()


def test_disposable_clone_create_publishes_one_archive_and_removes_plaintext(
    tmp_path: Path,
) -> None:
    service, services, volumes, runtime, backup = _service(tmp_path)

    result = service.create(
        CloneCreateRequest(output_directory=str(tmp_path / "output"), operation_id="clone-op"),
        passphrase_source=lambda: _PASSPHRASE,
    )

    archive = Path(result.path)
    assert archive.is_file()
    assert archive.stat().st_mode & 0o777 == 0o600
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == result.sha256
    assert list(archive.parent.glob("*.fsclone")) == [archive]
    assert not list(archive.parent.glob(".*.partial"))
    assert not list((tmp_path / "clone-staging").glob("clone-op/source"))
    assert backup.calls == 1  # lock-free lower-level dump, not the public backup command
    assert volumes.calls == ["rabbitmq"]
    assert services.states == {"rabbitmq-fs": True, "seaweedfs": True}
    assert services.events == [
        ("stop", ("rabbitmq-fs",)),
        ("start", ("rabbitmq-fs",)),
    ]
    assert runtime.recovery_state_path("clone-op").exists() is False


def test_clone_create_encrypts_source_secrets_and_records_restore_evidence(
    tmp_path: Path,
) -> None:
    service, _services, _volumes, _runtime, _backup = _service(
        tmp_path,
        storage="external_s3",
        source_secrets={"ENCRYPTION_KEY": "source-only-secret"},
    )

    result = service.create(
        CloneCreateRequest(output_directory=str(tmp_path / "output")),
        passphrase_source=lambda: _PASSPHRASE,
    )
    with Path(result.path).open("rb") as source:
        verified = CloneArchiveGateway().stage_verified(
            source,
            staging_parent=tmp_path / "verified",
            passphrase_source=lambda: _PASSPHRASE,
        )
    try:
        manifest = verified.manifest
        for staged_path in (verified.root, *verified.root.rglob("*")):
            metadata = staged_path.lstat()
            assert (metadata.st_uid, metadata.st_gid) == (os.geteuid(), os.getegid())
            if stat.S_ISDIR(metadata.st_mode):
                assert stat.S_IMODE(metadata.st_mode) == 0o700
            elif stat.S_ISREG(metadata.st_mode):
                assert stat.S_IMODE(metadata.st_mode) & ~0o600 == 0
        assert manifest.imported_secret_names == ("ENCRYPTION_KEY",)
        assert manifest.source_secret_fingerprints == {
            "ENCRYPTION_KEY": hashlib.sha256(b"source-only-secret").hexdigest()
        }
        assert manifest.external_effect_categories == ("schedules", "webhooks")
        assert manifest.database_parity_evidence["row_count"] == 42
        assert (
            manifest.database_parity_evidence["snapshot_sha256"]
            == hashlib.sha256(json.dumps({"snapshot": "source"}).encode()).hexdigest()
        )
        assert json.loads((verified.root / "evidence/database-parity.json").read_text()) == {
            "snapshot": "source"
        }
        preservation = json.loads(
            (verified.root / "evidence/preservation.json").read_text(encoding="utf-8")
        )
        assert preservation["stage"] == "before"
        assert preservation["fields"] == {"source": "captured"}
        source_config = json.loads(
            (verified.root / "evidence/configuration.json").read_text(encoding="utf-8")
        )
        assert source_config["file_storage"] == "external_s3"
        assert source_config["s3_endpoint"] == "https://source-storage.example.test"
        assert source_config["s3_bucket"] == "source-bucket"
        assert json.loads((verified.root / "secrets/source.json").read_text()) == {
            "ENCRYPTION_KEY": "source-only-secret"
        }
        assert "source-only-secret" not in result.model_dump_json()
    finally:
        verified.cleanup()


@pytest.mark.parametrize(
    ("storage", "expected_volumes", "stopped"),
    (
        ("local_fs", ["rabbitmq"], ("rabbitmq-fs",)),
        ("bundled", ["rabbitmq", "seaweedfs"], ("rabbitmq-fs", "seaweedfs")),
        ("external_s3", ["rabbitmq"], ("rabbitmq-fs",)),
    ),
)
def test_capture_stops_only_required_services_and_never_copies_external_s3(
    tmp_path: Path,
    storage: str,
    expected_volumes: list[str],
    stopped: tuple[str, ...],
) -> None:
    service, services, volumes, _runtime, _backup = _service(tmp_path, storage=storage)

    service.create(
        CloneCreateRequest(output_directory=str(tmp_path / "output")),
        passphrase_source=lambda: _PASSPHRASE,
    )

    assert volumes.calls == expected_volumes
    assert services.events[0] == ("stop", stopped)
    assert services.events[-1] == ("start", stopped)
    assert "external_s3" not in volumes.calls


def test_reused_runtime_does_not_retain_prior_bundled_volume_metadata(
    tmp_path: Path,
) -> None:
    service, _services, _volumes, _runtime, _backup = _service(
        tmp_path,
        storage="bundled",
    )

    first = service.create(
        CloneCreateRequest(output_directory=str(tmp_path / "output"), operation_id="bundled"),
        passphrase_source=lambda: _PASSPHRASE,
    )
    Path(first.path).unlink()
    service._configuration.value = DeploymentConfig(file_storage="local_fs")

    second = service.create(
        CloneCreateRequest(output_directory=str(tmp_path / "output"), operation_id="local"),
        passphrase_source=lambda: _PASSPHRASE,
    )
    with Path(second.path).open("rb") as source:
        verified = CloneArchiveGateway().stage_verified(
            source,
            staging_parent=tmp_path / "verified-reused-runtime",
            passphrase_source=lambda: _PASSPHRASE,
        )
    try:
        assert all(
            entry.data_class is not CloneDataClass.BUNDLED_STORAGE
            for entry in verified.manifest.entries
        )
    finally:
        verified.cleanup()


def test_archive_assembly_after_runtime_restart_recovers_trusted_volume_metadata(
    tmp_path: Path,
) -> None:
    service, services, volumes, runtime, backup = _service(tmp_path)
    request = CloneCreateRequest(
        output_directory=str(tmp_path / "output"),
        operation_id="restart-after-capture",
    )
    plan = service._prepare_plan(request)
    runtime.preflight(plan)
    runtime.capture_source(plan)
    manifest_path = runtime._manifest_path(plan.operation_id)
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in raw["entries"]:
        if entry["path"] == "rabbitmq" or entry["path"].startswith("rabbitmq/"):
            entry["uid"] = 12345
            entry["gid"] = 12346
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")
    restarted = CloneRuntimeGateway(
        staging_root=runtime._staging_root,
        output_root=runtime._output_root,
        backup=backup,
        services=services,
        volumes=volumes,
        source_roots=runtime._source_roots,
        archive=CloneArchiveGateway(),
    )

    restarted.assemble_archive(plan, passphrase_source=lambda: _PASSPHRASE)
    restarted.verify_archive(plan, passphrase_source=lambda: _PASSPHRASE)

    assert (runtime._workspace(plan.operation_id) / "verified.sha256").is_file()


@pytest.mark.parametrize("failure_site", ["backup", "volume"])
def test_capture_failure_restores_the_exact_prior_service_state(
    tmp_path: Path,
    failure_site: str,
) -> None:
    initial = {"rabbitmq-fs": True, "seaweedfs": False}
    service, services, _volumes, _runtime, _backup = _service(
        tmp_path,
        backup_failure=RuntimeError("dump failed") if failure_site == "backup" else None,
        volume_failure=RuntimeError("volume failed") if failure_site == "volume" else None,
        initial_states=initial,
    )

    request = CloneCreateRequest(output_directory=str(tmp_path / "output"))
    with pytest.raises(LifecycleError):
        service.create(
            request,
            passphrase_source=lambda: _PASSPHRASE,
        )

    assert services.states == initial
    assert not list((tmp_path / "output").glob("*.fsclone"))


def test_capture_and_restoration_failure_preserve_original_and_recovery_detail(
    tmp_path: Path,
) -> None:
    service, services, _volumes, runtime, _backup = _service(
        tmp_path,
        volume_failure=RuntimeError("original capture failure"),
        fail_start=True,
    )

    request = CloneCreateRequest(output_directory=str(tmp_path / "output"), operation_id="clone-op")
    with pytest.raises(LifecycleError) as raised:
        service.create(
            request,
            passphrase_source=lambda: _PASSPHRASE,
        )

    assert raised.value.exit_code.name == "RECOVERY_REQUIRED"
    assert raised.value.details["capture_failure"] == "RuntimeError"
    assert raised.value.details["recovery_failure"] == "RuntimeError"
    assert isinstance(raised.value.__cause__, RuntimeError)
    assert services.states["rabbitmq-fs"] is False
    assert runtime.recovery_state_path("clone-op").is_file()


def test_clone_create_resumes_from_a_verified_published_archive(tmp_path: Path) -> None:
    service, services, volumes, runtime, backup = _service(
        tmp_path,
        fail_once_after_publish=True,
    )
    request = CloneCreateRequest(
        output_directory=str(tmp_path / "output"),
        operation_id="clone-op",
    )

    with pytest.raises(LifecycleError, match="acknowledgement"):
        service.create(request, passphrase_source=lambda: _PASSPHRASE)

    archive = next((tmp_path / "output").glob("*.fsclone"))
    assert archive.is_file()
    assert runtime.recovery_state_path("clone-op").exists() is False
    assert (tmp_path / "clone-staging" / "clone-op" / "published.sha256").is_file()

    result = service.create(request, passphrase_source=lambda: _PASSPHRASE)

    assert Path(result.path) == archive
    assert backup.calls == 1
    assert volumes.calls == ["rabbitmq"]
    assert services.events == [
        ("stop", ("rabbitmq-fs",)),
        ("start", ("rabbitmq-fs",)),
    ]
    assert not (tmp_path / "clone-staging" / "clone-op").exists()
