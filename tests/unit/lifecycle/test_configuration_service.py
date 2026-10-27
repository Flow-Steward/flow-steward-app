from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from pydantic import ValidationError

from core.application.installer.contracts import InstallConfig
from core.application.lifecycle.configuration_service import (
    ConfigApplyRequest,
    ConfigSetRequest,
    ConfigShowRequest,
    ConfigurationService,
    ConfigValidateRequest,
)
from core.application.lifecycle.contracts import (
    ComposeTarget,
    ConfigurationPatch,
    ConfigurationValidationResult,
    DeploymentConfig,
    LifecycleOperationStatus,
)
from core.application.lifecycle.errors import (
    CandidateReadinessProbePendingError,
    LifecycleError,
)
from core.application.lifecycle.stack_service import (
    StackContainerRollbackState,
    StackContainerState,
    StackGatewayError,
    StackRollbackSnapshot,
    StackRuntimeState,
)
from core.application.readiness import (
    ReadinessArea,
    ReadinessFinding,
    ReadinessReport,
    ReadinessStatus,
)
from core.application.tooling.ops.deployment_doctor_common import DoctorOptions
from core.infrastructure.installer.gateway import DockerInstallerGateway
from core.infrastructure.installer.process import ProcessOutcome
from core.infrastructure.installer.state_store import InstallerStateStore
from core.infrastructure.lifecycle.compact_runtime_scripts import (
    _COMPACT_APP_READINESS_SCRIPT,
    _COMPACT_RUNTIME_STATE_EXPORT_SCRIPT,
    _COMPACT_RUNTIME_STATE_STAGE_SCRIPT,
)
from core.infrastructure.lifecycle.configuration_files import _reverse_proxy_override
from core.infrastructure.lifecycle.configuration_gateway import ConfigurationLifecycleGateway
from core.infrastructure.lifecycle.configuration_store import LifecycleConfigurationStore
from core.infrastructure.lifecycle.docker_gateway import DockerLifecycleGateway
from core.infrastructure.lifecycle.preservation_repository import PreservationManifestStore
from core.infrastructure.lifecycle.state_store import LifecycleStateStore


class FakeConfigurationGateway:
    def __init__(
        self,
        *,
        fail_target_health: bool = False,
        fail_restore_health: bool = False,
        validation_error: str = "",
        project_root: Path | None = None,
    ):
        self.calls: list[str] = []
        self.fail_target_health = fail_target_health
        self.fail_restore_health = fail_restore_health
        self.validation_error = validation_error
        self.project_root = project_root
        self._activation_count = 0
        self.manifests: dict[tuple[str, str], dict[str, object]] = {}
        self.recovery_snapshots: dict[str, StackRollbackSnapshot] = {}
        self.recovery_configurations: dict[str, tuple[bytes, int]] = {}
        self.images_preserved = True
        self.activation_source_environments: list[dict[str, str]] = []

    def validate_target(
        self,
        _config,
        _target,
        _environment=None,
        managed_target=None,
    ) -> None:
        del managed_target
        self.calls.append("validate")
        if self.validation_error:
            raise ValueError(self.validation_error)

    def capture_manifest(
        self,
        config,
        _target,
        *,
        operation_id: str,
        stage: str,
    ) -> dict[str, str]:
        self.calls.append(f"manifest:{config.public_host}")
        manifest = {"public_host": config.public_host}
        self.manifests[(operation_id, stage)] = manifest
        return manifest

    def load_manifest(self, operation_id: str, stage: str) -> dict[str, object] | None:
        return self.manifests.get((operation_id, stage))

    def validate_preservation_baseline(self, _manifest: Mapping[str, object]) -> None:
        return None

    def capture_recovery(
        self,
        previous_target,
        candidate_target,
        *,
        operation_id: str,
    ) -> dict[str, object]:
        self.calls.append("recovery:capture")
        managed = tuple(
            dict.fromkeys((*previous_target.required_services, *candidate_target.required_services))
        )
        snapshot = StackRollbackSnapshot(
            operation_id=operation_id,
            project_name=previous_target.project_name,
            selected_services=managed,
            services=tuple(
                StackContainerRollbackState(
                    service=name,
                    topology="running",
                    container_id=f"id-{name}",
                    image_id=f"sha256:{index:064x}",
                )
                for index, name in enumerate(managed, start=1)
            ),
        )
        self.recovery_snapshots[operation_id] = snapshot
        if self.project_root is not None:
            active_path = self.project_root / ".flow-steward/stack.toml"
            self.recovery_configurations[operation_id] = (
                active_path.read_bytes(),
                active_path.stat().st_mode & 0o777,
            )
        return snapshot.model_dump(mode="json")

    def recovery_available(self, operation_id: str) -> bool:
        return operation_id in self.recovery_snapshots

    def apply_artifacts(self, _config, *, operation_id: str) -> None:
        self.calls.append(f"artifacts:{operation_id}")

    def restore_recovery(
        self,
        _target,
        *,
        operation_id: str,
        snapshot,
        output_sink=None,
    ) -> bool:
        del output_sink
        self.calls.append("recovery:restore")
        self._activation_count += 1
        if StackRollbackSnapshot.model_validate(snapshot) != self.recovery_snapshots.get(
            operation_id
        ):
            return False
        if self.project_root is not None:
            active_path = self.project_root / ".flow-steward/stack.toml"
            contents, mode = self.recovery_configurations[operation_id]
            active_path.write_bytes(contents)
            active_path.chmod(mode)
        self.images_preserved = True
        return True

    def runtime_images_match(self, _target, _snapshot) -> bool:
        self.calls.append("runtime_images_match")
        return self.images_preserved

    def runtime_image_drift(self, _target, _snapshot) -> tuple[str, ...]:
        return () if self.images_preserved else ("traefik was sha256:1111, now sha256:2222",)

    def activate(
        self,
        _previous,
        config,
        _previous_target,
        _target,
        _runtime_snapshot,
        *,
        source_environment=None,
    ) -> None:
        self._activation_count += 1
        self.activation_source_environments.append(dict(source_environment or {}))
        self.calls.append(f"activate:{config.public_host}")

    def healthy(self, config, _target) -> bool:
        self.calls.append(f"health:{config.public_host}")
        if self._activation_count == 1:
            return not self.fail_target_health
        return not self.fail_restore_health

    def candidate_diagnostics(self, config, _target, output_sink) -> None:
        self.calls.append(f"diagnostics:{config.public_host}")
        output_sink("candidate says: PermissionError: [Errno 13] /app/logs/platform.log")

    def recovery_healthy(self, config, _target, _snapshot) -> bool:
        self.calls.append(f"recovery_health:{config.public_host}")
        return not self.fail_restore_health

    def compare_manifests(self, before, after, *, profile_transition: bool = False) -> None:
        del profile_transition
        self.calls.append(f"compare:{before['public_host']}:{after['public_host']}")


class InstallerProcess:
    def run(self, command, **_kwargs) -> ProcessOutcome:
        normalized = tuple(command)
        stdout = "a" * 40 + "\n" if normalized[-2:] == ("rev-parse", "HEAD^{commit}") else ""
        return ProcessOutcome(command=normalized, returncode=0, stdout=stdout)


def _service(
    tmp_path: Path,
    *,
    gateway: FakeConfigurationGateway | None = None,
    installed_identity: str | None = "flow-steward",
    docker_host_project_root: Path | None = None,
) -> ConfigurationService:
    store = LifecycleConfigurationStore(tmp_path)
    if installed_identity is not None:
        store.compose_env_path.parent.mkdir(parents=True, exist_ok=True)
        store.compose_env_path.write_text(
            f"FS_COMPOSE_PROJECT_NAME={installed_identity}\n",
            encoding="utf-8",
        )
    return ConfigurationService(
        store=store,
        gateway=gateway,
        lifecycle_state=LifecycleStateStore(tmp_path),
        docker_host_project_root=docker_host_project_root,
    )


def _write_lifecycle_secret_environment(
    path: Path,
    *,
    project_identity: str | None = None,
) -> tuple[str, ...]:
    secret_lines = (
        'FS_SECRET_KEY="keep fs secret"',
        'ENCRYPTION_KEY="keep encryption key"',
        'FS_BACKUP_ENCRYPTION_KEY="keep backup key"',
        'POSTGRES_PASSWORD="keep postgres password"',
        'RABBITMQ_PASS="keep rabbit password"',
        'FS_METRICS_TOKEN="keep metrics token"',
        'SECURITY_PII_SALT="keep pii salt"',
        'ARTIFACT_S3_ACCESS_KEY="keep artifact access key"',
        'ARTIFACT_S3_SECRET_KEY="keep artifact secret key"',
    )
    identity_lines = (
        () if project_identity is None else (f'FS_COMPOSE_PROJECT_NAME="{project_identity}"',)
    )
    path.write_text(
        "\n".join(
            (
                "# BEGIN FLOW STEWARD INSTALLER MANAGED",
                *secret_lines,
                *identity_lines,
                "# END FLOW STEWARD INSTALLER MANAGED",
                "",
            )
        ),
        encoding="utf-8",
    )
    return secret_lines


def test_prepare_environment_resolves_tls_mounts_to_absolute_project_paths(
    tmp_path: Path,
) -> None:
    _write_lifecycle_secret_environment(tmp_path / ".env.prod")
    service = _service(tmp_path)
    config = DeploymentConfig(
        environment="production",
        deployment_profile="compact",
        file_storage="local_fs",
        ingress_mode="bundled_traefik",
        tls_mode="local",
    )

    target, _path, environment = service.prepare_environment(config)
    compose_environment = service.compose_environment(config, target)

    assert environment["FS_TLS_CERTIFICATE_PATH"] == str((tmp_path / ".ssl/cert.pem").resolve())
    assert environment["FS_TLS_PRIVATE_KEY_PATH"] == str((tmp_path / ".ssl/key.pem").resolve())
    assert compose_environment["FS_TLS_CERTIFICATE_PATH"] == environment["FS_TLS_CERTIFICATE_PATH"]
    assert compose_environment["FS_TLS_PRIVATE_KEY_PATH"] == environment["FS_TLS_PRIVATE_KEY_PATH"]


@pytest.mark.parametrize("profile", ("compact", "scalable"))
@pytest.mark.parametrize(
    ("host", "port", "expected"),
    (
        ("flow.example.test", 443, "https://flow.example.test"),
        ("Flow.Example.TEST", 443, "https://flow.example.test"),
        ("flow.example.test", 8443, "https://flow.example.test:8443"),
        ("2001:db8::1", 443, "https://[2001:db8::1]"),
        ("2001:0DB8:0000:0000:0000:0000:0000:0001", 443, "https://[2001:db8::1]"),
    ),
)
def test_config_apply_environment_uses_the_canonical_installer_public_origin(
    tmp_path: Path, profile: str, host: str, port: int, expected: str
) -> None:
    _write_lifecycle_secret_environment(tmp_path / ".env.prod")
    service = _service(tmp_path)
    config = DeploymentConfig(
        environment="production",
        deployment_profile=profile,
        file_storage="local_fs",
        public_host=host,
        public_port=port,
    )

    _target, _path, environment = service.prepare_environment(config)

    assert environment["FS_PUBLIC_BASE_URL"] == expected
    assert environment["FS_AUTH_TRUSTED_ORIGINS"] == expected


def _write_certificate_pair(
    cert_path: Path,
    key_path: Path,
    *,
    host: str,
    key: rsa.RSAPrivateKey | None = None,
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
) -> rsa.RSAPrivateKey:
    private_key = key or rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC)
    try:
        san_name: x509.GeneralName = x509.IPAddress(ip_address(host))
    except ValueError:
        san_name = x509.DNSName(host)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(valid_from or now - timedelta(minutes=5))
        .not_valid_after(valid_until or now + timedelta(days=30))
        .add_extension(x509.SubjectAlternativeName([san_name]), critical=False)
        .sign(private_key, hashes.SHA256())
    )
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return private_key


def _installer_provisioned_environment(
    tmp_path: Path,
    config: InstallConfig,
) -> tuple[ConfigurationService, Path]:
    (tmp_path / "VERSION").write_text("0.2.0\n", encoding="utf-8")
    (tmp_path / "infra/docker").mkdir(parents=True)
    (tmp_path / "infra/docker/docker-compose.yaml").write_text("services: {}\n", encoding="utf-8")
    (tmp_path / "infra/docker/docker-compose.prod.yaml").write_text(
        "services: {}\n", encoding="utf-8"
    )
    if config.file_storage == "external_s3":
        credentials = tmp_path / "installer-s3-credentials.json"
        credentials.write_text(
            json.dumps({"access_key": "installed access", "secret_key": "installed secret"}),
            encoding="utf-8",
        )
        credentials.chmod(0o600)
        config = config.model_copy(update={"s3_credentials_path": str(credentials)})
    state = InstallerStateStore(tmp_path / ".flow-steward")
    gateway = DockerInstallerGateway(
        repo_root=tmp_path,
        state_store=state,
        process=InstallerProcess(),
        disk_free_bytes=lambda _path: 10 * 1024**3,
        port_available=lambda _host, _port, _protocol: True,
        architecture=lambda: "arm64",
        docker_cli_available=lambda: True,
        secret_factory=lambda: "installed-secret",
        sleep=lambda _seconds: None,
    )
    log = state.open_log("lifecycle-compatibility")
    gateway.ensure_configuration(config, log=log)
    gateway.ensure_secrets(config, log=log)
    log.close()
    return ConfigurationService(store=LifecycleConfigurationStore(tmp_path)), (
        tmp_path / (".env" if config.environment == "development" else ".env.prod")
    )


def test_install_config_inherits_closed_canonical_deployment_contract() -> None:
    config = InstallConfig(environment="development", file_storage="bundled")

    assert isinstance(config, DeploymentConfig)
    assert config.model_dump(mode="json")["environment"] == "development"
    assert config.model_dump(mode="json")["file_storage"] == "bundled"
    with pytest.raises(ValidationError):
        InstallConfig(environment="local_development")
    with pytest.raises(ValidationError):
        InstallConfig(file_storage="bundled_seaweedfs")
    with pytest.raises(ValidationError):
        DeploymentConfig(unknown_setting=True)


def test_install_config_preserves_prior_acceptance_of_empty_public_host() -> None:
    config = InstallConfig(public_host="")

    assert config.public_host == ""


def test_plan_apply_empty_state_reports_nothing_to_apply(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.store.write_active(DeploymentConfig())

    _raises_input_364_1 = ConfigApplyRequest()
    with pytest.raises(LifecycleError) as raised:
        service.plan_apply(_raises_input_364_1)

    assert raised.value.code == "nothing_to_apply"
    assert raised.value.exit_code.value == 8


def test_store_normalizes_legacy_values_only_when_reading_existing_state(
    tmp_path: Path,
) -> None:
    state = tmp_path / ".flow-steward"
    state.mkdir()
    (state / "stack.toml").write_text(
        "\n".join(
            (
                'environment = "local_development"',
                'file_storage = "bundled_seaweedfs"',
                "test_runner = true",
                "audit_tools = false",
                "quality_tools = true",
                "",
            )
        ),
        encoding="utf-8",
    )
    store = LifecycleConfigurationStore(tmp_path)

    config = store.read_active()
    assert config is not None
    assert config.environment == "development"
    assert config.file_storage == "bundled"
    assert config.development_tools == ("test_runner", "sonarqube")

    store.write_active(config)

    text = (state / "stack.toml").read_text(encoding="utf-8")
    assert 'environment = "development"' in text
    assert 'file_storage = "bundled"' in text
    assert "local_development" not in text
    assert "bundled_seaweedfs" not in text


def test_store_rejects_corrupt_journal_when_partial_legacy_stack_needs_it(
    tmp_path: Path,
) -> None:
    state = tmp_path / ".flow-steward"
    state.mkdir()
    (state / "stack.toml").write_text(
        'environment = "production"\nfile_storage = "bundled"\n',
        encoding="utf-8",
    )
    (state / "install-state.json").write_text("{not-json", encoding="utf-8")
    service = _service(tmp_path)

    with pytest.raises(LifecycleError) as raised:
        service.read_active()

    assert raised.value.code == "configuration_invalid"


def test_store_rejects_journal_without_config_when_partial_legacy_stack_needs_it(
    tmp_path: Path,
) -> None:
    state = tmp_path / ".flow-steward"
    state.mkdir()
    (state / "stack.toml").write_text(
        'environment = "production"\nfile_storage = "bundled"\n',
        encoding="utf-8",
    )
    (state / "install-state.json").write_text(
        json.dumps({"status": "completed"}),
        encoding="utf-8",
    )
    service = _service(tmp_path)

    with pytest.raises(LifecycleError) as raised:
        service.read_active()

    assert raised.value.code == "configuration_invalid"


def test_store_rejects_empty_legacy_journal_config_before_defaulting(
    tmp_path: Path,
) -> None:
    state = tmp_path / ".flow-steward"
    state.mkdir()
    (state / "stack.toml").write_text(
        'environment = "production"\nfile_storage = "bundled"\n',
        encoding="utf-8",
    )
    (state / "install-state.json").write_text(
        json.dumps({"config": {}}),
        encoding="utf-8",
    )
    service = _service(tmp_path)

    with pytest.raises(LifecycleError) as raised:
        service.read_active()

    assert raised.value.code == "configuration_invalid"


def test_store_rejects_partial_legacy_journal_config_before_merging(
    tmp_path: Path,
) -> None:
    state = tmp_path / ".flow-steward"
    state.mkdir()
    (state / "stack.toml").write_text(
        'environment = "production"\nfile_storage = "bundled"\n',
        encoding="utf-8",
    )
    (state / "install-state.json").write_text(
        json.dumps({"config": {"public_host": "partial.example.test"}}),
        encoding="utf-8",
    )
    service = _service(tmp_path)

    with pytest.raises(LifecycleError) as raised:
        service.read_active()

    assert raised.value.code == "configuration_invalid"


def test_store_rejects_invalid_complete_legacy_journal_config_before_merging(
    tmp_path: Path,
) -> None:
    state = tmp_path / ".flow-steward"
    state.mkdir()
    (state / "stack.toml").write_text(
        'environment = "production"\nfile_storage = "bundled"\n',
        encoding="utf-8",
    )
    serialized = InstallConfig().model_dump(mode="json")
    serialized["environment"] = "not-a-phase3-environment"
    (state / "install-state.json").write_text(
        json.dumps({"config": serialized}),
        encoding="utf-8",
    )
    service = _service(tmp_path)

    with pytest.raises(LifecycleError) as raised:
        service.read_active()

    assert raised.value.code == "configuration_invalid"


def test_store_reads_complete_legacy_journal_config_then_normalizes_stack_boundary(
    tmp_path: Path,
) -> None:
    state = tmp_path / ".flow-steward"
    state.mkdir()
    (state / "stack.toml").write_text(
        "\n".join(
            (
                'environment = "local_development"',
                'file_storage = "bundled_seaweedfs"',
                "test_runner = true",
                "",
            )
        ),
        encoding="utf-8",
    )
    serialized = InstallConfig(
        environment="production",
        file_storage="bundled",
        public_host="journal.example.test",
    ).model_dump(mode="json")
    (state / "install-state.json").write_text(
        json.dumps({"config": serialized}),
        encoding="utf-8",
    )
    service = _service(tmp_path)

    config = service.read_active()

    assert config is not None
    assert config.environment == "development"
    assert config.file_storage == "bundled"
    assert config.development_tools == ("test_runner",)
    assert config.public_host == "journal.example.test"


def test_store_ignores_corrupt_stale_journal_for_complete_canonical_stack(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    expected = DeploymentConfig(environment="production", public_host="flow.example.test")
    service.store.write_active(expected)
    service.store.install_journal_path.write_text("{not-json", encoding="utf-8")

    assert service.read_active() == expected


def test_set_stages_one_typed_secret_free_patch_without_runtime_mutation(
    tmp_path: Path,
) -> None:
    gateway = FakeConfigurationGateway()
    service = _service(tmp_path, gateway=gateway)
    service.store.write_active(DeploymentConfig())
    _write_lifecycle_secret_environment(tmp_path / ".env")

    pending = service.set(
        ConfigurationPatch(public_host="flow.example.test", public_port=443)
    ).pending

    assert pending.public_host == "flow.example.test"
    assert pending.public_port == 443
    assert gateway.calls == []
    payload = json.loads(service.store.pending_path.read_text(encoding="utf-8"))
    assert payload == {"public_host": "flow.example.test", "public_port": 443}
    assert service.store.read_active() == DeploymentConfig()
    with pytest.raises(ValidationError):
        ConfigurationPatch()
    with pytest.raises(ValidationError):
        ConfigurationPatch(environment="production")
    with pytest.raises(ValidationError):
        ConfigurationPatch(FS_SECRET_KEY="must-not-be-written")


def test_set_can_stage_path_patch_while_apply_owns_filesystem_validation(
    tmp_path: Path,
) -> None:
    gateway = FakeConfigurationGateway()
    service = _service(tmp_path, gateway=gateway)
    service.store.write_active(DeploymentConfig())
    patch = ConfigurationPatch(
        tls_mode="provided",
        certificate_path="missing/site.crt",
        private_key_path="missing/site.key",
    )

    assert service.set(patch).pending == patch
    assert gateway.calls == []

    _raises_input_598_1 = ConfigApplyRequest()
    with pytest.raises(LifecycleError) as raised:
        service.apply(_raises_input_598_1)

    assert raised.value.code == "configuration_invalid"
    assert gateway.calls == []


def test_config_apply_rejects_scoped_ipv6_public_host(tmp_path: Path) -> None:
    gateway = FakeConfigurationGateway()
    service = _service(tmp_path, gateway=gateway)
    service.store.write_active(DeploymentConfig())
    service.set(ConfigurationPatch(public_host="fe80::1%eth0", public_port=443))
    request = ConfigApplyRequest()

    with pytest.raises(LifecycleError) as raised:
        service.apply(request)

    assert raised.value.code == "configuration_invalid"
    assert "public_host" in raised.value.cause
    assert gateway.calls == []


def test_validate_invokes_non_mutating_full_target_validation(tmp_path: Path) -> None:
    gateway = FakeConfigurationGateway()
    service = _service(tmp_path, gateway=gateway)
    service.store.write_active(DeploymentConfig())
    _write_lifecycle_secret_environment(tmp_path / ".env")

    result = service.validate()

    assert result.valid is True
    assert result.issues == ()
    assert gateway.calls == ["validate"]
    assert service.store.read_active() == DeploymentConfig()
    assert service.store.read_pending() is None


def test_validate_aggregates_local_and_full_target_failures(tmp_path: Path) -> None:
    gateway = FakeConfigurationGateway(validation_error="compose target could not render")
    service = _service(tmp_path, gateway=gateway)
    service.store.write_active(
        DeploymentConfig(
            tls_mode="provided",
            certificate_path="missing/site.crt",
            private_key_path="missing/site.key",
        )
    )
    _write_lifecycle_secret_environment(tmp_path / ".env")

    result = service.validate()

    assert result.valid is False
    assert result.issues == (
        "active: provided TLS certificate path is unavailable: missing/site.crt",
        "active: provided TLS private key path is unavailable: missing/site.key",
        "active: compose target could not render",
    )
    assert gateway.calls == ["validate"]


def test_lifecycle_validation_rejects_empty_public_host_at_service_boundary(
    tmp_path: Path,
) -> None:
    gateway = FakeConfigurationGateway()
    service = _service(tmp_path, gateway=gateway)
    service.store.write_active(DeploymentConfig(public_host=""))
    _write_lifecycle_secret_environment(tmp_path / ".env")

    result = service.validate()

    assert result.valid is False
    assert "active: public_host must not be empty" in result.issues
    assert gateway.calls == ["validate"]


@pytest.mark.parametrize(
    ("environment", "profile", "placement", "storage", "expected_overlays"),
    (
        (
            "development",
            "scalable",
            "host_directories",
            "bundled",
            ("infra/docker/scalable/cli-host-directories.yaml",),
        ),
        (
            "development",
            "scalable",
            "host_directories",
            "local_fs",
            (
                "infra/docker/scalable/cli-host-directories.yaml",
                "infra/docker/scalable/cli-host-directories-artifacts.yaml",
            ),
        ),
        (
            "production",
            "scalable",
            "host_directories",
            "external_s3",
            ("infra/docker/scalable/cli-host-directories.yaml",),
        ),
        ("production", "scalable", "compact_volume", "local_fs", ()),
        ("production", "compact", "host_directories", "local_fs", ()),
        ("production", "compact", "compact_volume", "bundled", ()),
    ),
)
def test_cli_host_bindings_are_selected_only_for_scalable_host_placement(
    tmp_path: Path,
    environment: str,
    profile: str,
    placement: str,
    storage: str,
    expected_overlays: tuple[str, ...],
) -> None:
    config = DeploymentConfig(
        environment=environment,
        deployment_profile=profile,
        data_placement=placement,
        file_storage=storage,
    )
    target = _service(tmp_path).derive_target(config)
    selected = tuple(path for path in target.compose_files if "/cli-host-directories" in path)
    assert selected == expected_overlays


def test_target_compose_is_derived_from_canonical_configuration(tmp_path: Path) -> None:
    service = _service(tmp_path)
    config = DeploymentConfig(
        environment="development",
        file_storage="bundled",
        development_tools=("test_runner", "audit_tools", "sonarqube"),
    )

    target = service.derive_target(config)

    assert target.env_file == ".env"
    assert target.compose_files == (
        "infra/docker/docker-compose.yaml",
        "infra/docker/scalable/cli-host-directories.yaml",
    )
    assert target.compose_profiles == (
        "storage-seaweedfs",
        "cli",
        "development",
        "test",
        "audit",
        "quality",
    )
    assert target.required_services == (
        "traefik",
        "rabbitmq-fs",
        "pg-fs",
        "fs-mcp",
        "fs-app",
        "fs-extension-installer",
        "fs-worker",
        "control-plane",
        "seaweedfs",
        "fs-cli",
        "fs-frontend",
        "mailpit",
        "fs-test-runner",
        "fs-audit",
        "pg-audit",
        "rabbitmq-audit",
        "sonarqube",
        "sonar-scanner",
    )
    assert target.runtime_services == (
        "traefik",
        "rabbitmq-fs",
        "pg-fs",
        "fs-mcp",
        "fs-app",
        "fs-extension-installer",
        "fs-worker",
        "control-plane",
        "seaweedfs",
        "fs-cli",
        "fs-frontend",
        "fs-test-runner",
        "fs-audit",
        "pg-audit",
        "rabbitmq-audit",
    )
    assert target.stack_environment == "development"
    assert target.file_storage == "bundled"


@pytest.mark.parametrize("environment", ("development", "production"))
@pytest.mark.parametrize("file_storage", ("bundled", "external_s3"))
@pytest.mark.parametrize(
    "development_tools",
    (
        (),
        ("test_runner",),
        ("audit_tools",),
        ("sonarqube",),
        ("test_runner", "audit_tools"),
        ("test_runner", "sonarqube"),
        ("audit_tools", "sonarqube"),
        ("test_runner", "audit_tools", "sonarqube"),
    ),
)
def test_derived_lifecycle_targets_retain_warm_cli_runtime(
    tmp_path: Path,
    environment: str,
    file_storage: str,
    development_tools: tuple[str, ...],
) -> None:
    service = _service(tmp_path)
    config = DeploymentConfig(
        environment=environment,
        file_storage=file_storage,
        development_tools=development_tools,
    )

    target = service.derive_target(config)

    assert "cli" in target.compose_profiles
    assert "fs-cli" in target.required_services
    assert "fs-cli" in target.runtime_services
    assert "fs-cli" not in target.rebuild_services


def test_development_target_normalizes_legacy_partial_tools(tmp_path: Path) -> None:
    service = _service(tmp_path)
    target = service.derive_target(
        DeploymentConfig(
            environment="development",
            file_storage="external_s3",
            development_tools=("test_runner",),
        )
    )

    assert target.compose_profiles == ("cli", "development", "test", "audit", "quality")
    assert {
        "fs-frontend",
        "mailpit",
        "fs-test-runner",
        "fs-audit",
        "pg-audit",
        "rabbitmq-audit",
        "sonarqube",
        "sonar-scanner",
    } <= set(target.required_services)
    assert {
        "fs-frontend",
        "fs-test-runner",
        "fs-audit",
        "pg-audit",
        "rabbitmq-audit",
    } <= set(target.runtime_services)
    assert {"mailpit", "sonarqube", "sonar-scanner"}.isdisjoint(target.runtime_services)


def test_production_external_s3_target_is_minimal_and_retains_cli(tmp_path: Path) -> None:
    service = _service(tmp_path)
    target = service.derive_target(
        DeploymentConfig(
            environment="production",
            file_storage="external_s3",
            development_tools=("test_runner", "audit_tools", "sonarqube"),
        )
    )

    expected = (
        "traefik",
        "rabbitmq-fs",
        "pg-fs",
        "fs-mcp",
        "fs-app",
        "fs-extension-installer",
        "fs-worker",
        "control-plane",
        "fs-cli",
    )
    assert target.compose_profiles == ("bundled-ingress", "cli")
    assert target.required_services == expected
    assert target.runtime_services == expected


def test_reverse_proxy_target_keeps_compose_definition_but_omits_traefik_runtime(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    config = DeploymentConfig(
        tls_mode="reverse_proxy",
        public_host="flow.example.test",
    )

    target = service.derive_target(config)

    assert ".flow-steward/installer.compose.override.yaml" in target.compose_files
    assert "traefik" not in target.required_services
    assert "traefik" not in target.runtime_services


def test_environment_generation_preserves_secrets_and_writes_canonical_markers(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    env_path = tmp_path / ".env"
    secret_lines = _write_lifecycle_secret_environment(env_path)
    env_path.write_text(
        "OPENAI_API_KEY=sk-existing\n" + env_path.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    config = DeploymentConfig(environment="development", file_storage="bundled")

    service.write_environment(config)
    service.write_environment(config)

    text = env_path.read_text(encoding="utf-8")
    assert text.count("# BEGIN FLOW STEWARD INSTALLER MANAGED") == 1
    assert "OPENAI_API_KEY=sk-existing" in text
    assert all(line in text for line in secret_lines)
    assert "ENVIRONMENT=development" in text
    assert "FS_STACK_ENVIRONMENT=development" in text
    assert "local_development" not in text
    assert "ARTIFACT_S3_ENDPOINT_URL=http://seaweedfs:8333" in text


def test_production_transition_environment_includes_retained_cli_candidate_mounts(
    tmp_path: Path,
) -> None:
    container_root = tmp_path / "container-root"
    host_root = tmp_path / "host-root"
    container_root.mkdir()
    host_root.mkdir()
    service = _service(container_root, docker_host_project_root=host_root)
    active = DeploymentConfig(environment="development", file_storage="bundled")
    _write_lifecycle_secret_environment(container_root / ".env")
    candidate = DeploymentConfig(environment="production", file_storage="external_s3")

    _target, path, environment = service.prepare_transition_environment(active, candidate)

    assert path == container_root / ".env.prod"
    assert environment["FS_CANDIDATE_STATE_DIR"] == str((host_root / ".flow-steward").resolve())
    assert environment["FS_CANDIDATE_PROD_ENV"] == str((host_root / ".env.prod").resolve())


def test_development_transition_preparation_retains_installed_image_pins(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path, installed_identity="customer_project")
    active = DeploymentConfig(environment="production", file_storage="external_s3")
    active_path = tmp_path / ".env.prod"
    _write_lifecycle_secret_environment(active_path, project_identity="customer_project")
    image_pins = {
        "FS_APP_IMAGE": "registry.example/fs@sha256:" + ("a" * 64),
        "MCP_IMAGE": "registry.example/mcp@sha256:" + ("b" * 64),
        "FS_CLI_IMAGE": "registry.example/cli@sha256:" + ("c" * 64),
        "TRAEFIK_IMAGE": "traefik@sha256:" + ("d" * 64),
        "POSTGRES_IMAGE": "registry.example/postgres@sha256:" + ("e" * 64),
        "RABBITMQ_IMAGE": "rabbitmq@sha256:" + ("f" * 64),
    }
    active_path.write_text(
        active_path.read_text(encoding="utf-8")
        + "".join(f"{name}={value}\n" for name, value in image_pins.items()),
        encoding="utf-8",
    )
    candidate = active.model_copy(update={"environment": "development"})

    _target, _path, environment = service.prepare_transition_environment(active, candidate)

    assert {name: environment.get(name) for name in image_pins} == image_pins


def test_monitoring_expectations_follow_deployment_owner(tmp_path: Path) -> None:
    service = _service(tmp_path, installed_identity="customer_project")
    active = DeploymentConfig(
        environment="production",
        deployment_profile="scalable",
        file_storage="local_fs",
        worker_replicas=3,
    )
    path = tmp_path / ".env.prod"
    _write_lifecycle_secret_environment(path, project_identity="customer_project")
    _target, _path, values = service.prepare_transition_environment(active, active)
    assert json.loads(values["FS_MONITORING_EXPECTATIONS"])["worker"] == 3
    candidate = active.model_copy(update={"deployment_profile": "compact", "worker_processes": 4})
    _target, _path, values = service.prepare_transition_environment(active, candidate)
    assert json.loads(values["FS_MONITORING_EXPECTATIONS"])["worker"] == 4


def test_profile_transition_retains_compact_image_provenance(tmp_path: Path) -> None:
    service = _service(tmp_path, installed_identity="customer_project")
    active = DeploymentConfig(
        environment="production",
        deployment_profile="scalable",
        file_storage="local_fs",
    )
    active_path = tmp_path / ".env.prod"
    _write_lifecycle_secret_environment(active_path, project_identity="customer_project")
    compact_provenance = {
        "FS_COMPACT_IMAGE": "sha256:" + ("a" * 64),
        "FS_COMPACT_IMAGE_DIGEST": "sha256:" + ("b" * 64),
        "FS_COMPACT_IMAGE_REGISTRY_REF": "registry.example/compact@sha256:" + ("c" * 64),
        "FS_COMPACT_IMAGE_INDEX_DIGEST": "sha256:" + ("c" * 64),
        "FS_COMPACT_IMAGE_MANIFEST_DIGEST": "sha256:" + ("d" * 64),
        "FS_COMPACT_IMAGE_CONFIG_DIGEST": "sha256:" + ("e" * 64),
        "FS_COMPACT_IMAGE_REVISION": "f" * 40,
        "FS_COMPACT_IMAGE_ROLE": "compact-runtime",
        "FS_COMPACT_IMAGE_PLATFORM": "linux/arm64",
    }
    active_path.write_text(
        active_path.read_text(encoding="utf-8")
        + "".join(f"{name}={value}\n" for name, value in compact_provenance.items()),
        encoding="utf-8",
    )
    candidate = active.model_copy(update={"deployment_profile": "compact"})

    _target, _path, environment = service.prepare_transition_environment(active, candidate)

    assert {name: environment.get(name) for name in compact_provenance} == compact_provenance


def test_profile_transition_preserves_installed_public_binding_in_managed_block(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path, installed_identity="customer_project")
    active = DeploymentConfig(
        environment="production",
        deployment_profile="scalable",
        file_storage="local_fs",
        public_host="127.0.0.1",
        public_port=19543,
    )
    source_path = tmp_path / ".env.prod"
    _write_lifecycle_secret_environment(source_path, project_identity="customer_project")
    source_path.write_text(
        source_path.read_text(encoding="utf-8")
        + "# BEGIN FLOW STEWARD INSTALLER MANAGED\n"
        + "HTTP_PORT=19540\n"
        + "APP_PORT=19543\n"
        + "FS_PUBLIC_BIND_HOST=127.0.0.1\n"
        + "# END FLOW STEWARD INSTALLER MANAGED\n",
        encoding="utf-8",
    )
    candidate = active.model_copy(update={"deployment_profile": "compact"})

    prepared = service.prepare_transition_environment(active, candidate)
    service.stage_transition_environment(active, candidate, prepared)
    rendered = service.store.read_env_values(source_path)

    assert prepared[2]["HTTP_PORT"] == "19540"
    assert prepared[2]["FS_PUBLIC_BIND_HOST"] == "127.0.0.1"
    assert prepared[2]["FS_RUNTIME_HTTP_PORT"] == "8080"
    assert rendered["HTTP_PORT"] == "19540"
    assert rendered["FS_PUBLIC_BIND_HOST"] == "127.0.0.1"


def test_profile_transition_renders_installed_compact_traefik_public_port(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path, installed_identity="customer_project")
    active = DeploymentConfig(
        environment="production",
        deployment_profile="scalable",
        file_storage="local_fs",
        public_host="127.0.0.1",
        public_port=19543,
    )
    source_path = tmp_path / ".env.prod"
    _write_lifecycle_secret_environment(source_path, project_identity="customer_project")
    source_path.write_text(
        source_path.read_text(encoding="utf-8") + "HTTP_PORT=19540\n",
        encoding="utf-8",
    )
    candidate = active.model_copy(update={"deployment_profile": "compact"})

    _target, _path, environment = service.prepare_transition_environment(active, candidate)
    compose = Path("infra/docker/compact/ingress-bundled_traefik.yaml").read_text(encoding="utf-8")
    published = "${FS_PUBLIC_BIND_HOST:-0.0.0.0}:${HTTP_PORT:-8080}:${HTTP_PORT:-8080}"

    assert published in compose
    assert published.replace(
        "${FS_PUBLIC_BIND_HOST:-0.0.0.0}", environment["FS_PUBLIC_BIND_HOST"]
    ).replace("${HTTP_PORT:-8080}", environment["HTTP_PORT"]) == ("127.0.0.1:19540:19540")
    assert environment["FS_RUNTIME_HTTP_PORT"] == "8080"


def test_profile_transition_rederives_bind_for_an_explicit_public_host_change(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path, installed_identity="customer_project")
    active = DeploymentConfig(
        environment="production",
        deployment_profile="scalable",
        file_storage="local_fs",
        public_host="127.0.0.1",
    )
    source_path = tmp_path / ".env.prod"
    _write_lifecycle_secret_environment(source_path, project_identity="customer_project")
    source_path.write_text(
        source_path.read_text(encoding="utf-8")
        + "HTTP_PORT=19540\nFS_PUBLIC_BIND_HOST=127.0.0.1\n",
        encoding="utf-8",
    )
    candidate = active.model_copy(
        update={"deployment_profile": "compact", "public_host": "flow.example.test"}
    )

    _target, _path, environment = service.prepare_transition_environment(active, candidate)

    assert environment["HTTP_PORT"] == "19540"
    assert environment["FS_PUBLIC_BIND_HOST"] == "0.0.0.0"


def test_profile_transition_rejects_invalid_installed_public_http_port(tmp_path: Path) -> None:
    service = _service(tmp_path, installed_identity="customer_project")
    active = DeploymentConfig(
        environment="production",
        deployment_profile="scalable",
        file_storage="local_fs",
    )
    source_path = tmp_path / ".env.prod"
    _write_lifecycle_secret_environment(source_path, project_identity="customer_project")
    source_path.write_text(
        source_path.read_text(encoding="utf-8") + "HTTP_PORT=not-a-port\n",
        encoding="utf-8",
    )

    requested = active.model_copy(update={"deployment_profile": "compact"})

    with pytest.raises(LifecycleError) as raised:
        service.prepare_transition_environment(active, requested)

    assert raised.value.code == "configuration_public_http_port_invalid"
    assert raised.value.details == {"field": "HTTP_PORT"}


def test_environment_generation_refuses_missing_durable_secret_configuration(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)

    _raises_input_901_1 = DeploymentConfig(environment="development")
    with pytest.raises(LifecycleError) as raised:
        service.write_environment(_raises_input_901_1)

    assert raised.value.code == "configuration_durable_secrets_unavailable"
    assert "./install.sh" in raised.value.remediation
    assert not (tmp_path / ".env").exists()


def test_environment_generation_preserves_existing_durable_secret_bytes(tmp_path: Path) -> None:
    service = _service(tmp_path)
    env_path = tmp_path / ".env"
    secret_lines = _write_lifecycle_secret_environment(env_path)

    service.write_environment(DeploymentConfig(environment="development"))

    text = env_path.read_text(encoding="utf-8")
    assert all(line in text for line in secret_lines)
    assert "must-not-be-generated" not in text


def test_environment_generation_never_adds_compose_project_identity(tmp_path: Path) -> None:
    service = _service(tmp_path)
    env_path = tmp_path / ".env"
    _write_lifecycle_secret_environment(env_path)

    service.write_environment(DeploymentConfig(environment="development"))

    assert "FS_COMPOSE_PROJECT_NAME" not in env_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "target_contents",
    (
        None,
        "# BEGIN FLOW STEWARD INSTALLER MANAGED\n"
        "FS_SECRET_KEY=stale-secret\n"
        "FS_APP_IMAGE=registry.example/fs:stale-pin\n"
        "# END FLOW STEWARD INSTALLER MANAGED\n",
    ),
    ids=("target-absent", "target-stale"),
)
def test_transition_environment_uses_active_protected_values_when_target_is_absent_or_stale(
    tmp_path: Path,
    target_contents: str | None,
) -> None:
    service = _service(tmp_path, installed_identity="customer_project")
    active = DeploymentConfig(environment="development", file_storage="bundled")
    candidate = active.model_copy(update={"environment": "production"})
    active_path = tmp_path / ".env"
    active_path.write_text(
        "\n".join(
            (
                "# BEGIN FLOW STEWARD INSTALLER MANAGED",
                'FS_SECRET_KEY="active fs secret"',
                'ENCRYPTION_KEY="active encryption key"',
                'FS_BACKUP_ENCRYPTION_KEY="active backup key"',
                'POSTGRES_PASSWORD="active postgres password"',
                'RABBITMQ_PASS="active rabbit password"',
                'FS_METRICS_TOKEN="active metrics token"',
                'SECURITY_PII_SALT="active pii salt"',
                'ARTIFACT_S3_ACCESS_KEY="active artifact access"',
                'ARTIFACT_S3_SECRET_KEY="active artifact secret"',
                'FS_COMPOSE_PROJECT_NAME="customer_project"',
                'FS_APP_IMAGE="registry.example/fs:active-pin"',
                'MCP_IMAGE="registry.example/mcp:active-pin"',
                'TRAEFIK_IMAGE="registry.example/traefik:active-pin"',
                'POSTGRES_IMAGE="registry.example/postgres:active-pin"',
                'RABBITMQ_IMAGE="registry.example/rabbit:active-pin"',
                "# END FLOW STEWARD INSTALLER MANAGED",
                "",
            )
        ),
        encoding="utf-8",
    )
    if target_contents is not None:
        (tmp_path / ".env.prod").write_text(target_contents, encoding="utf-8")

    prepared = service.prepare_transition_environment(active, candidate)
    service.stage_transition_environment(active, candidate, prepared)

    target_text = (tmp_path / ".env.prod").read_text(encoding="utf-8")
    for line in (
        'FS_SECRET_KEY="active fs secret"',
        'ENCRYPTION_KEY="active encryption key"',
        'FS_BACKUP_ENCRYPTION_KEY="active backup key"',
        'POSTGRES_PASSWORD="active postgres password"',
        'RABBITMQ_PASS="active rabbit password"',
        'FS_METRICS_TOKEN="active metrics token"',
        'SECURITY_PII_SALT="active pii salt"',
        'ARTIFACT_S3_ACCESS_KEY="active artifact access"',
        'ARTIFACT_S3_SECRET_KEY="active artifact secret"',
        'FS_COMPOSE_PROJECT_NAME="customer_project"',
        'FS_APP_IMAGE="registry.example/fs:active-pin"',
        'MCP_IMAGE="registry.example/mcp:active-pin"',
        'TRAEFIK_IMAGE="registry.example/traefik:active-pin"',
        'POSTGRES_IMAGE="registry.example/postgres:active-pin"',
        'RABBITMQ_IMAGE="registry.example/rabbit:active-pin"',
    ):
        assert line in target_text
    assert "stale-secret" not in target_text
    assert "stale-pin" not in target_text
    assert "FS_STACK_ENVIRONMENT=production" in target_text
    assert service.transition_environment_matches(active, candidate, prepared) is True

    (tmp_path / ".env.prod").write_text(target_text + "# interrupted mutation\n", encoding="utf-8")

    assert service.transition_environment_matches(active, candidate, prepared) is False


def test_transition_environment_accepts_explicit_external_s3_credentials_overlay(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    active = DeploymentConfig(environment="development", file_storage="bundled")
    active_path = tmp_path / ".env"
    _write_lifecycle_secret_environment(active_path)
    active_path.write_text(
        "\n".join(
            line
            for line in active_path.read_text(encoding="utf-8").splitlines()
            if not line.startswith(("ARTIFACT_S3_ACCESS_KEY=", "ARTIFACT_S3_SECRET_KEY="))
        )
        + "\n",
        encoding="utf-8",
    )
    candidate = active.model_copy(
        update={
            "environment": "production",
            "file_storage": "external_s3",
            "s3_endpoint": "https://objects.example.test",
            "s3_credentials_path": "storage-managed.json",
        }
    )

    with pytest.raises(LifecycleError):
        service.prepare_transition_environment(active, candidate)

    prepared = service.prepare_transition_environment(
        active,
        candidate,
        {
            "ARTIFACT_S3_ACCESS_KEY": "storage-managed-access",
            "ARTIFACT_S3_SECRET_KEY": "storage-managed-secret",
        },
    )

    assert prepared[2]["ARTIFACT_S3_ACCESS_KEY"] == "storage-managed-access"
    assert prepared[2]["ARTIFACT_S3_SECRET_KEY"] == "storage-managed-secret"

    service.stage_transition_environment(active, candidate, prepared)
    target_path = tmp_path / ".env.prod"
    target_path.write_text(
        target_path.read_text(encoding="utf-8")
        + "\n# BEGIN FLOW STEWARD STORAGE CREDENTIALS\n"
        + "ARTIFACT_S3_ACCESS_KEY=storage-managed-access\n"
        + "ARTIFACT_S3_SECRET_KEY=storage-managed-secret\n"
        + "# END FLOW STEWARD STORAGE CREDENTIALS\n",
        encoding="utf-8",
    )

    assert service.transition_environment_matches(active, candidate, prepared) is True

    target_path.write_text(
        target_path.read_text(encoding="utf-8") + "UNRELATED_MUTATION=true\n",
        encoding="utf-8",
    )

    assert service.transition_environment_matches(active, candidate, prepared) is False


def test_lifecycle_preserves_authenticated_development_bundled_environment_from_installer(
    tmp_path: Path,
) -> None:
    config = InstallConfig(environment="development", file_storage="bundled")
    service, env_path = _installer_provisioned_environment(tmp_path, config)

    service.write_environment(DeploymentConfig.model_validate(config.model_dump(mode="json")))

    values = service.store.read_env_values(env_path)
    expected_secret = "installed-secret"  # pragma: allowlist secret
    assert values["ARTIFACT_S3_ACCESS_KEY"] == expected_secret
    assert values["ARTIFACT_S3_SECRET_KEY"] == expected_secret


def test_installer_development_env_stages_clean_production_transition_env(
    tmp_path: Path,
) -> None:
    from core.application.tooling.ops.deployment_doctor import _check_env_duplicate_keys

    active = DeploymentConfig(environment="development", file_storage="bundled")
    service, _active_path = _installer_provisioned_environment(
        tmp_path,
        InstallConfig(environment="development", file_storage="bundled"),
    )
    candidate = active.model_copy(
        update={
            "environment": "production",
            "file_storage": "external_s3",
            "s3_endpoint": "https://objects.example.test",
            "s3_credentials_path": "external-s3.json",
        }
    )
    prepared = service.prepare_transition_environment(
        active,
        candidate,
        {
            "ARTIFACT_S3_ACCESS_KEY": "external-access",
            "ARTIFACT_S3_SECRET_KEY": "external-secret",
        },
    )

    target_path = service.stage_transition_environment(active, candidate, prepared)

    result = _check_env_duplicate_keys(target_path)
    assert result.status == "pass"
    assert "none assigned twice" in result.detail


@pytest.mark.parametrize("environment", ("development", "production"))
def test_config_validate_hydrates_bundled_storage_identity_from_installer_identity(
    tmp_path: Path,
    environment: str,
) -> None:
    class _CredentialCheckingGateway(FakeConfigurationGateway):
        def validate_target(
            self,
            _config,
            _target,
            environment=None,
            managed_target=None,
        ) -> None:
            del managed_target
            assert environment["ARTIFACT_S3_ACCESS_KEY"] == "durable-bundled-access"
            assert (
                environment["ARTIFACT_S3_SECRET_KEY"] == "durable-bundled-secret"
            )  # pragma: allowlist secret -- test fixture

    gateway = _CredentialCheckingGateway()
    service = _service(tmp_path, gateway=gateway)
    config = DeploymentConfig(environment=environment, file_storage="bundled")
    service.store.write_active(config)
    env_path = tmp_path / (".env" if environment == "development" else ".env.prod")
    _write_lifecycle_secret_environment(env_path)
    env_path.write_text(
        "\n".join(
            line
            for line in env_path.read_text(encoding="utf-8").splitlines()
            if not line.startswith(("ARTIFACT_S3_ACCESS_KEY=", "ARTIFACT_S3_SECRET_KEY="))
        )
        + "\n",
        encoding="utf-8",
    )
    identity_path = tmp_path / ".flow-steward/runtime/seaweedfs-s3.json"
    identity_path.parent.mkdir(parents=True, exist_ok=True)
    identity_path.write_text(
        json.dumps(
            {
                "identities": [
                    {
                        "name": "flow-steward-core",
                        "credentials": [
                            {
                                "accessKey": "durable-bundled-access",
                                "secretKey": "durable-bundled-secret",
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    identity_path.chmod(0o600)

    result = service.validate()

    assert result.valid is True
    assert result.issues == ()


@pytest.mark.parametrize(
    "config",
    (
        InstallConfig(environment="production", file_storage="bundled"),
        InstallConfig(
            environment="development",
            file_storage="external_s3",
            s3_endpoint="https://objects.example.test",
            s3_credentials_path="installer-managed",
        ),
        InstallConfig(
            environment="production",
            file_storage="external_s3",
            s3_endpoint="https://objects.example.test",
            s3_credentials_path="installer-managed",
        ),
    ),
)
def test_lifecycle_rejects_missing_installer_artifact_credentials(
    tmp_path: Path,
    config: InstallConfig,
) -> None:
    service, env_path = _installer_provisioned_environment(tmp_path, config)
    env_path.write_text(
        "\n".join(
            line
            for line in env_path.read_text(encoding="utf-8").splitlines()
            if not line.startswith(("ARTIFACT_S3_ACCESS_KEY=", "ARTIFACT_S3_SECRET_KEY="))
        )
        + "\n",
        encoding="utf-8",
    )
    if config.file_storage == "bundled":
        (tmp_path / ".flow-steward/runtime/seaweedfs-s3.json").unlink()

    _raises_input_1216_1 = DeploymentConfig.model_validate(config.model_dump(mode="json"))
    with pytest.raises(LifecycleError) as raised:
        service.write_environment(_raises_input_1216_1)

    assert raised.value.code == "configuration_durable_secrets_unavailable"


@pytest.mark.parametrize(
    "config",
    (
        InstallConfig(environment="production", file_storage="bundled"),
        InstallConfig(
            environment="development",
            file_storage="external_s3",
            s3_endpoint="https://objects.example.test",
            s3_credentials_path="installer-managed",
        ),
        InstallConfig(
            environment="production",
            file_storage="external_s3",
            s3_endpoint="https://objects.example.test",
            s3_credentials_path="installer-managed",
        ),
    ),
)
def test_lifecycle_preserves_installer_artifact_credential_bytes(
    tmp_path: Path,
    config: InstallConfig,
) -> None:
    service, env_path = _installer_provisioned_environment(tmp_path, config)
    credential_lines = tuple(
        line
        for line in env_path.read_text(encoding="utf-8").splitlines()
        if line.startswith(("ARTIFACT_S3_ACCESS_KEY=", "ARTIFACT_S3_SECRET_KEY="))
    )

    service.write_environment(DeploymentConfig.model_validate(config.model_dump(mode="json")))

    text = env_path.read_text(encoding="utf-8")
    assert len(credential_lines) == 2
    assert all(line in text for line in credential_lines)


def test_lifecycle_rejects_absent_compose_identity_before_gateway_or_write(tmp_path: Path) -> None:
    gateway = FakeConfigurationGateway()
    service = _service(tmp_path, gateway=gateway, installed_identity=None)
    env_path = tmp_path / ".env"
    _write_lifecycle_secret_environment(env_path)
    active = DeploymentConfig(public_host="old.example.test")
    service.store.write_active(active)
    service.set(ConfigurationPatch(public_host="new.example.test"))
    before = env_path.read_text(encoding="utf-8")

    _raises_input_1268_1 = ConfigApplyRequest()
    with pytest.raises(LifecycleError) as raised:
        service.apply(_raises_input_1268_1)

    assert raised.value.code == "configuration_identity_invalid"
    assert env_path.read_text(encoding="utf-8") == before
    assert gateway.calls == []


@pytest.mark.parametrize("identity", ("", "customer project", "Customer", "-customer"))
def test_lifecycle_rejects_blank_or_invalid_compose_identity_before_gateway_or_write(
    tmp_path: Path,
    identity: str,
) -> None:
    gateway = FakeConfigurationGateway()
    service = _service(tmp_path, gateway=gateway, installed_identity=None)
    env_path = tmp_path / ".env"
    _write_lifecycle_secret_environment(env_path)
    service.store.compose_env_path.parent.mkdir(parents=True, exist_ok=True)
    service.store.compose_env_path.write_text(
        f"FS_COMPOSE_PROJECT_NAME={identity}\n",
        encoding="utf-8",
    )
    active = DeploymentConfig(public_host="old.example.test")
    service.store.write_active(active)
    service.set(ConfigurationPatch(public_host="new.example.test"))
    env_before = env_path.read_text(encoding="utf-8")
    compose_before = service.store.compose_env_path.read_text(encoding="utf-8")

    _raises_input_1296_1 = ConfigApplyRequest()
    with pytest.raises(LifecycleError) as raised:
        service.apply(_raises_input_1296_1)

    assert raised.value.code == "configuration_identity_invalid"
    assert env_path.read_text(encoding="utf-8") == env_before
    assert service.store.compose_env_path.read_text(encoding="utf-8") == compose_before
    assert gateway.calls == []


def test_lifecycle_accepts_repeated_valid_compose_identity(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _write_lifecycle_secret_environment(tmp_path / ".env", project_identity="customer_1")
    _write_lifecycle_secret_environment(tmp_path / ".env.prod", project_identity="customer_1")
    service.store.compose_env_path.parent.mkdir(parents=True, exist_ok=True)
    service.store.compose_env_path.write_text(
        "FS_COMPOSE_PROJECT_NAME=customer_1\n",
        encoding="utf-8",
    )

    target = service.derive_target(DeploymentConfig(environment="development"))

    assert target.project_name == "customer_1"


def test_transition_derives_compose_identity_only_from_active_durable_files(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    _write_lifecycle_secret_environment(
        tmp_path / ".env",
        project_identity="installed-project",
    )
    _write_lifecycle_secret_environment(
        tmp_path / ".env.prod",
        project_identity="stale-opposite-project",
    )
    service.store.compose_env_path.write_text(
        "FS_COMPOSE_PROJECT_NAME=installed-project\n",
        encoding="utf-8",
    )
    active = DeploymentConfig(environment="development")
    candidate = active.model_copy(update={"environment": "production"})

    target, _path, _environment = service.prepare_transition_environment(active, candidate)

    assert target.project_name == "installed-project"


def test_store_rejects_conflicting_duplicate_identity_in_one_file(tmp_path: Path) -> None:
    store = LifecycleConfigurationStore(tmp_path)
    (tmp_path / ".env").write_text(
        "FS_COMPOSE_PROJECT_NAME=customer_a\nFS_COMPOSE_PROJECT_NAME=customer_b\n",
        encoding="utf-8",
    )

    _raises_input_1351_1 = DeploymentConfig()
    with pytest.raises(ValueError, match="identities disagree"):
        store.project_name(_raises_input_1351_1)


def test_store_rejects_blank_identity_alongside_valid_identity_in_another_file(
    tmp_path: Path,
) -> None:
    store = LifecycleConfigurationStore(tmp_path)
    (tmp_path / ".env").write_text("FS_COMPOSE_PROJECT_NAME=\n", encoding="utf-8")
    (tmp_path / ".env.prod").write_text(
        "FS_COMPOSE_PROJECT_NAME=customer\n",
        encoding="utf-8",
    )

    _raises_input_1365_1 = DeploymentConfig()
    with pytest.raises(ValueError, match="identity is blank"):
        store.project_name(_raises_input_1365_1)


def test_store_rejects_conflicting_protected_identity_key_names(tmp_path: Path) -> None:
    store = LifecycleConfigurationStore(tmp_path)
    (tmp_path / ".env").write_text(
        "FS_COMPOSE_PROJECT_NAME=customer_a\nCOMPOSE_PROJECT_NAME=customer_b\n",
        encoding="utf-8",
    )

    _raises_input_1376_1 = DeploymentConfig()
    with pytest.raises(ValueError, match="identities disagree"):
        store.project_name(_raises_input_1376_1)


def test_store_rejects_invalid_compose_identity_when_fs_identity_is_valid(tmp_path: Path) -> None:
    store = LifecycleConfigurationStore(tmp_path)
    (tmp_path / ".env").write_text(
        "FS_COMPOSE_PROJECT_NAME=customer\nCOMPOSE_PROJECT_NAME=Customer\n",
        encoding="utf-8",
    )

    _raises_input_1387_1 = DeploymentConfig()
    with pytest.raises(ValueError, match="unsafe syntax"):
        store.project_name(_raises_input_1387_1)


def test_store_accepts_repeated_identical_values_for_both_identity_keys(tmp_path: Path) -> None:
    store = LifecycleConfigurationStore(tmp_path)
    (tmp_path / ".env").write_text(
        "FS_COMPOSE_PROJECT_NAME=customer\nCOMPOSE_PROJECT_NAME=customer\n",
        encoding="utf-8",
    )
    (tmp_path / ".env.prod").write_text(
        "FS_COMPOSE_PROJECT_NAME=customer\n",
        encoding="utf-8",
    )
    store.compose_env_path.parent.mkdir(parents=True, exist_ok=True)
    store.compose_env_path.write_text(
        "COMPOSE_PROJECT_NAME=customer\n",
        encoding="utf-8",
    )

    assert store.project_name(DeploymentConfig()) == "customer"


def test_configuration_write_preserves_existing_compose_project_identity_bytes(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    env_path = tmp_path / ".env"
    _write_lifecycle_secret_environment(env_path, project_identity="customer_project")
    service.store.compose_env_path.parent.mkdir(parents=True, exist_ok=True)
    service.store.compose_env_path.write_text(
        'FS_COMPOSE_PROJECT_NAME="customer_project"\nFS_STACK_ENVIRONMENT=development\n',
        encoding="utf-8",
    )

    service.write_active_configuration(DeploymentConfig(environment="development"))

    assert 'FS_COMPOSE_PROJECT_NAME="customer_project"' in env_path.read_text(encoding="utf-8")
    assert 'FS_COMPOSE_PROJECT_NAME="customer_project"' in service.store.compose_env_path.read_text(
        encoding="utf-8"
    )


def test_configuration_apply_rejects_compose_identity_mismatch_before_mutation(
    tmp_path: Path,
) -> None:
    gateway = FakeConfigurationGateway()
    service = _service(tmp_path, gateway=gateway)
    env_path = tmp_path / ".env"
    _write_lifecycle_secret_environment(env_path, project_identity="customer-a")
    service.store.compose_env_path.parent.mkdir(parents=True, exist_ok=True)
    service.store.compose_env_path.write_text(
        "FS_COMPOSE_PROJECT_NAME=customer-b\nFS_STACK_ENVIRONMENT=development\n",
        encoding="utf-8",
    )
    active = DeploymentConfig(environment="development", public_host="old.example.test")
    service.store.write_active(active)
    service.set(ConfigurationPatch(public_host="new.example.test"))
    env_before = env_path.read_text(encoding="utf-8")
    compose_before = service.store.compose_env_path.read_text(encoding="utf-8")

    _raises_input_1448_1 = ConfigApplyRequest()
    with pytest.raises(LifecycleError) as raised:
        service.apply(_raises_input_1448_1)

    assert raised.value.code == "configuration_identity_invalid"
    assert service.store.read_active() == active
    assert env_path.read_text(encoding="utf-8") == env_before
    assert service.store.compose_env_path.read_text(encoding="utf-8") == compose_before
    assert gateway.calls == []


def test_apply_owns_validation_manifest_runtime_health_and_commit(tmp_path: Path) -> None:
    gateway = FakeConfigurationGateway()
    service = _service(tmp_path, gateway=gateway)
    active = DeploymentConfig(public_host="old.example.test")
    service.store.write_active(active)
    _write_lifecycle_secret_environment(tmp_path / ".env")
    service.set(ConfigurationPatch(public_host="new.example.test"))

    result = service.apply(ConfigApplyRequest())

    assert result.active.public_host == "new.example.test"
    assert result.runtime_restored is False
    assert service.store.read_active() == result.active
    assert service.store.read_pending() is None
    assert Path(result.snapshot_path).is_file()
    calls = ["artifacts" if call.startswith("artifacts:") else call for call in gateway.calls]
    assert calls == [
        "validate",
        "recovery:capture",
        "manifest:old.example.test",
        "artifacts",
        "activate:new.example.test",
        "health:new.example.test",
        "manifest:new.example.test",
        "runtime_images_match",
        "compare:old.example.test:new.example.test",
    ]


def test_apply_preserves_source_compact_environment_for_runtime_export(tmp_path: Path) -> None:
    gateway = FakeConfigurationGateway()
    service = _service(tmp_path, gateway=gateway)
    active = DeploymentConfig(
        environment="production",
        deployment_profile="compact",
        worker_processes=3,
    )
    service.store.write_active(active)
    _write_lifecycle_secret_environment(tmp_path / ".env.prod")
    service.set(ConfigurationPatch(deployment_profile="scalable"))

    service.apply(ConfigApplyRequest())

    assert gateway.activation_source_environments[0]["FS_DEPLOYMENT_PROFILE"] == "compact"
    assert gateway.activation_source_environments[0]["FS_WORKER_PROCESSES"] == "3"


def test_apply_rejects_incomplete_preservation_before_runtime_mutation(tmp_path: Path) -> None:
    class IncompletePreservationError(ValueError):
        failed_fields = ("indexed_branch_sha_markers",)

    class IncompleteBaselineGateway(FakeConfigurationGateway):
        def validate_preservation_baseline(self, _manifest: Mapping[str, object]) -> None:
            raise IncompletePreservationError("indexed_branch_sha_markers is unknown")

    gateway = IncompleteBaselineGateway()
    service = _service(tmp_path, gateway=gateway)
    active = DeploymentConfig(public_host="old.example.test")
    service.store.write_active(active)
    _write_lifecycle_secret_environment(tmp_path / ".env")
    service.set(ConfigurationPatch(public_host="new.example.test"))

    _raises_input_1499_1 = ConfigApplyRequest()
    with pytest.raises(LifecycleError) as raised:
        service.apply(_raises_input_1499_1)

    assert raised.value.code == "configuration_preservation_capture_failed"
    assert raised.value.details == {"failed_fields": ["indexed_branch_sha_markers"]}
    assert service.store.read_active() == active
    assert service.store.read_pending() is not None
    assert not any(call.startswith("artifacts:") for call in gateway.calls)
    assert not any(call.startswith("activate:") for call in gateway.calls)


def test_apply_restores_previous_config_and_runtime_when_target_is_unhealthy(
    tmp_path: Path,
) -> None:
    gateway = FakeConfigurationGateway(fail_target_health=True, project_root=tmp_path)
    service = _service(tmp_path, gateway=gateway)
    active = DeploymentConfig(public_host="old.example.test")
    service.store.write_active(active)
    _write_lifecycle_secret_environment(tmp_path / ".env")
    service.set(ConfigurationPatch(public_host="new.example.test"))

    _raises_input_1519_1 = ConfigApplyRequest()
    with pytest.raises(LifecycleError) as raised:
        service.apply(_raises_input_1519_1)

    assert raised.value.code == "configuration_apply_failed"
    assert service.store.read_active() == active
    assert service.store.read_pending() is not None
    assert gateway.calls[-4:] == [
        "recovery:restore",
        "recovery_health:old.example.test",
        "manifest:old.example.test",
        "compare:old.example.test:old.example.test",
    ]


class _TalkativeRestoreGateway(FakeConfigurationGateway):
    """The restore's own output (Compose progress) is handed the operation log."""

    def restore_recovery(self, target, *, operation_id: str, snapshot, output_sink=None) -> bool:
        assert output_sink is not None
        output_sink("compose: Container previous-fs-app Recreated")
        return super().restore_recovery(target, operation_id=operation_id, snapshot=snapshot)


@pytest.mark.parametrize("restore_healthy", [True, False])
def test_the_rollback_says_in_the_operation_log_what_it_restored_and_how_it_went(
    tmp_path: Path, restore_healthy: bool
) -> None:
    """AC17-R167: the log held one line, "the previous runtime was restored"."""
    gateway = _TalkativeRestoreGateway(
        fail_target_health=True, fail_restore_health=not restore_healthy, project_root=tmp_path
    )
    service = _service(tmp_path, gateway=gateway)
    service.store.write_active(DeploymentConfig(public_host="old.example.test"))
    _write_lifecycle_secret_environment(tmp_path / ".env")
    service.set(ConfigurationPatch(public_host="new.example.test"))

    request = ConfigApplyRequest()
    with pytest.raises(LifecycleError) as raised:
        service.apply(request)

    assert raised.value.log_path is not None
    log = Path(raised.value.log_path).read_text(encoding="utf-8")
    assert re.search(r"Rollback: restoring [^\n]*fs-app running sha256:0{12}", log)
    assert "compose: Container previous-fs-app Recreated" in log
    if restore_healthy:
        assert "Rollback: verified; the data is as it was before the apply." in log
    else:
        assert "Rollback: the restored primary service is not running or not healthy." in log
        assert "Rollback: verified" not in log


def test_apply_reports_recovery_required_when_previous_runtime_cannot_be_restored(
    tmp_path: Path,
) -> None:
    gateway = FakeConfigurationGateway(fail_target_health=True, fail_restore_health=True)
    service = _service(tmp_path, gateway=gateway)
    service.store.write_active(DeploymentConfig(public_host="old.example.test"))
    _write_lifecycle_secret_environment(tmp_path / ".env")
    service.set(ConfigurationPatch(public_host="new.example.test"))

    _raises_input_1542_1 = ConfigApplyRequest()
    with pytest.raises(LifecycleError) as raised:
        service.apply(_raises_input_1542_1)

    assert raised.value.code == "configuration_recovery_required"
    assert raised.value.recovery_command == "flow-steward config apply"


def test_apply_recovery_required_keeps_the_redacted_runtime_failure_detail(
    tmp_path: Path,
) -> None:
    class ActivationFailureGateway(FakeConfigurationGateway):
        def activate(self, *_args, **_kwargs) -> None:
            raise StackGatewayError(
                action="configuration_apply",
                detail=("candidate failed for postgresql://runtime_user:runtime_secret@db/runtime"),
            )

    gateway = ActivationFailureGateway(fail_restore_health=True)
    service = _service(tmp_path, gateway=gateway)
    service.store.write_active(DeploymentConfig(public_host="old.example.test"))
    _write_lifecycle_secret_environment(tmp_path / ".env")
    service.set(ConfigurationPatch(public_host="new.example.test"))

    request = ConfigApplyRequest()

    with pytest.raises(LifecycleError) as raised:
        service.apply(request)

    assert raised.value.code == "configuration_recovery_required"
    assert raised.value.details == {
        "failed_action": "configuration_apply",
        "failure_detail": ("candidate failed for postgresql://runtime_user:[REDACTED]@db/runtime"),
    }


def test_apply_successful_rollback_keeps_redacted_runtime_failure_in_result_and_log(
    tmp_path: Path,
) -> None:
    class ActivationFailureGateway(FakeConfigurationGateway):
        def activate(self, *_args, **_kwargs) -> None:
            raise StackGatewayError(
                action="configuration_apply",
                detail=("candidate failed for postgresql://runtime_user:runtime_secret@db/runtime"),
            )

    service = _service(tmp_path, gateway=ActivationFailureGateway())
    service.store.write_active(DeploymentConfig(public_host="old.example.test"))
    _write_lifecycle_secret_environment(tmp_path / ".env")
    service.set(ConfigurationPatch(public_host="new.example.test"))

    request = ConfigApplyRequest()

    with pytest.raises(LifecycleError) as raised:
        service.apply(request)

    failure = raised.value
    assert failure.code == "configuration_apply_failed"
    assert failure.details == {
        "failed_action": "configuration_apply",
        "failure_detail": ("candidate failed for postgresql://runtime_user:[REDACTED]@db/runtime"),
    }
    assert "configuration_apply" in failure.cause
    assert "postgresql://runtime_user:[REDACTED]@db/runtime" in failure.cause
    assert "runtime_secret" not in failure.cause
    assert failure.log_path is not None
    operation_log = Path(failure.log_path).read_text(encoding="utf-8")
    assert "configuration_apply" in operation_log
    assert "postgresql://runtime_user:[REDACTED]@db/runtime" in operation_log
    assert "runtime_secret" not in operation_log


def test_config_apply_command_recovers_the_same_recovery_required_operation(
    tmp_path: Path,
) -> None:
    gateway = FakeConfigurationGateway(
        fail_target_health=True,
        fail_restore_health=True,
        project_root=tmp_path,
    )
    state = LifecycleStateStore(tmp_path)
    service = ConfigurationService(
        store=LifecycleConfigurationStore(tmp_path),
        gateway=gateway,
        lifecycle_state=state,
    )
    service.store.compose_env_path.parent.mkdir(parents=True, exist_ok=True)
    service.store.compose_env_path.write_text(
        "FS_COMPOSE_PROJECT_NAME=flow-steward\n",
        encoding="utf-8",
    )
    service.store.write_active(DeploymentConfig(public_host="old.example.test"))
    _write_lifecycle_secret_environment(tmp_path / ".env")
    service.set(ConfigurationPatch(public_host="new.example.test"))

    request = ConfigApplyRequest()

    with pytest.raises(LifecycleError) as raised:
        service.apply(request)

    operation_id = raised.value.operation_id
    assert operation_id is not None
    journal = state.load_journal(operation_id)
    assert journal is not None
    assert journal.status is LifecycleOperationStatus.RECOVERY_REQUIRED

    gateway.fail_restore_health = False
    recovered = service.apply(ConfigApplyRequest())

    assert recovered.operation_id == operation_id
    assert recovered.runtime_restored is True
    assert recovered.active.public_host == "old.example.test"
    assert service.store.read_pending() is not None
    completed = state.load_journal(operation_id)
    assert completed is not None
    assert completed.status is LifecycleOperationStatus.FAILED
    assert completed.last_error_code == "configuration_recovery_completed"


def test_readiness_probe_derives_doctor_input_from_configuration_service(
    tmp_path: Path,
) -> None:
    from core.infrastructure.installer.readiness_probe import StackReadinessProbe

    service = _service(tmp_path)
    service.store.write_active(DeploymentConfig(environment="development", file_storage="bundled"))
    captured = {}

    def doctor(options):
        captured["options"] = options
        return {"ok": True, "checks": []}

    payload = StackReadinessProbe(
        configuration_service=service,
        repo_root=tmp_path,
        doctor=doctor,
        doctor_options_factory=DoctorOptions,
    )()

    assert payload["ok"] is True
    options = captured["options"]
    assert options.env_file == tmp_path / ".env"
    assert options.compose_files == (
        tmp_path / "infra/docker/docker-compose.yaml",
        tmp_path / "infra/docker/scalable/cli-host-directories.yaml",
    )
    assert options.stack_environment == "local_development"
    assert "seaweedfs" in options.required_compose_services


def test_readiness_probe_reports_invalid_active_configuration_as_core_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.application.readiness import CoreReadinessService
    from core.infrastructure.installer.readiness_probe import StackReadinessProbe

    service = _service(tmp_path)
    active = DeploymentConfig(environment="development", file_storage="bundled")
    service.store.write_active(active)
    (tmp_path / ".env").write_text(
        "RABBITMQ_URL=amqp://development:secret@rabbitmq-fs:5672/\n",
        encoding="utf-8",
    )

    def validate_config(config: DeploymentConfig) -> ConfigurationValidationResult:
        assert config == active
        return ConfigurationValidationResult(
            valid=False,
            issues=("active: public_host is invalid",),
        )

    monkeypatch.setattr(
        service,
        "validate_config",
        validate_config,
    )

    payload = StackReadinessProbe(
        configuration_service=service,
        repo_root=tmp_path,
        doctor=lambda _options: {
            "ok": True,
            "checks": [
                {"id": "database", "category": "runtime", "status": "pass"},
            ],
        },
        doctor_options_factory=DoctorOptions,
    )()

    report = CoreReadinessService(probe=lambda: payload).check()

    assert report.status == "blocked"
    assert report.core_failed == 1
    assert report.process_exit_code == 4
    configuration = next(
        finding for finding in report.findings if finding.check_id == "configuration_validity"
    )
    assert configuration.detail == "active: public_host is invalid"


def test_readiness_probe_preserves_doctor_queue_failure_as_core_failure(tmp_path: Path) -> None:
    from core.application.readiness import CoreReadinessService
    from core.infrastructure.installer.readiness_probe import StackReadinessProbe

    service = _service(tmp_path)
    service.store.write_active(DeploymentConfig(environment="development", file_storage="bundled"))
    (tmp_path / ".env").write_text(
        "RABBITMQ_URL=amqp://development:secret@rabbitmq-fs:5672/\n",
        encoding="utf-8",
    )
    payload = StackReadinessProbe(
        configuration_service=service,
        repo_root=tmp_path,
        doctor=lambda _o: {
            "checks": [
                {
                    "id": "queue_worker_health",
                    "category": "runtime",
                    "status": "fail",
                    "detail": "queue worker is unavailable",
                }
            ]
        },
        doctor_options_factory=DoctorOptions,
    )()
    report = CoreReadinessService(probe=lambda: payload).check()
    assert report.status == "blocked"
    assert any(finding.check_id == "queue_worker_health" for finding in report.findings)


def test_cli_bootstrap_ignores_stale_reverse_proxy_override_after_tls_mode_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.entrypoints.cli.bootstrap_recovery import BootstrapRecoveryRuntime

    store = LifecycleConfigurationStore(tmp_path)
    config = DeploymentConfig(environment="production", tls_mode="local")
    store.write_active(config)
    (tmp_path / ".env.prod").write_text("FS_COMPOSE_PROJECT_NAME=test-project\n")
    (tmp_path / ".flow-steward/installer.compose.override.yaml").write_text("services: {}\n")
    monkeypatch.setenv("FS_DOCKER_HOST_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("FS_INSTALLER_STATE_DIR", str(store.state_root))

    recovery = BootstrapRecoveryRuntime().bootstrap_token_service
    target = recovery.gateway.installer._configuration.derive_target(config)

    assert target.env_file == ".env.prod"
    assert target.compose_files == (
        "infra/docker/docker-compose.prod.yaml",
        "infra/docker/scalable/cli-host-directories.yaml",
    )
    assert target.project_name == "test-project"


def test_show_returns_active_pending_diff_and_secret_presence_only(tmp_path: Path) -> None:
    service = _service(tmp_path)
    active = DeploymentConfig(public_host="old.example.test", public_port=8099)
    service.store.write_active(active)
    secret_lines = _write_lifecycle_secret_environment(tmp_path / ".env")
    service.set(ConfigurationPatch(public_host="new.example.test", public_port=443))

    result = service.show(ConfigShowRequest())

    assert result.active == active
    assert result.pending == ConfigurationPatch(
        public_host="new.example.test",
        public_port=443,
    )
    assert result.pending_diff == {
        "public_host": {
            "active": "old.example.test",
            "pending": "new.example.test",
        },
        "public_port": {"active": 8099, "pending": 443},
    }
    states = {item.name: item.state for item in result.protected_setting_states}
    assert states["FS_SECRET_KEY"] == "configured"
    assert states["FS_BACKUP_ENCRYPTION_KEY"] == "configured"
    serialized = result.model_dump_json()
    assert all(line.split("=", 1)[1].strip('"') not in serialized for line in secret_lines)


def test_validate_request_does_not_write_configuration_runtime_or_journal(tmp_path: Path) -> None:
    gateway = FakeConfigurationGateway()
    service = ConfigurationService(
        store=LifecycleConfigurationStore(tmp_path),
        gateway=gateway,
        lifecycle_state=LifecycleStateStore(tmp_path),
    )
    service.store.compose_env_path.parent.mkdir(parents=True, exist_ok=True)
    service.store.compose_env_path.write_text(
        "FS_COMPOSE_PROJECT_NAME=flow-steward\n",
        encoding="utf-8",
    )
    service.store.write_active(DeploymentConfig())
    _write_lifecycle_secret_environment(tmp_path / ".env")
    before = {
        path: path.read_bytes()
        for path in (
            service.store.active_path,
            service.store.compose_env_path,
            tmp_path / ".env",
        )
    }

    result = service.validate(ConfigValidateRequest())

    assert result.valid is True
    assert gateway.calls == ["validate"]
    assert {path: path.read_bytes() for path in before} == before
    assert not (tmp_path / ".flow-steward/operations").exists()


def test_validate_reports_active_and_pending_candidate_independently(tmp_path: Path) -> None:
    gateway = FakeConfigurationGateway()
    service = ConfigurationService(
        store=LifecycleConfigurationStore(tmp_path),
        gateway=gateway,
        lifecycle_state=LifecycleStateStore(tmp_path),
    )
    service.store.compose_env_path.parent.mkdir(parents=True, exist_ok=True)
    service.store.compose_env_path.write_text(
        "FS_COMPOSE_PROJECT_NAME=flow-steward\n",
        encoding="utf-8",
    )
    service.store.write_active(DeploymentConfig(public_host=""))
    _write_lifecycle_secret_environment(tmp_path / ".env")
    service.store.write_pending(ConfigurationPatch(public_host="fixed.example.test"))

    result = service.validate(ConfigValidateRequest())

    assert result.valid is False
    assert any(issue.startswith("active: public_host") for issue in result.issues)
    assert not any(issue.startswith("pending: public_host") for issue in result.issues)
    assert gateway.calls.count("validate") == 2


def test_apply_uses_shared_lock_and_rejects_second_mutation_before_gateway_calls(
    tmp_path: Path,
) -> None:
    gateway = FakeConfigurationGateway()
    state = LifecycleStateStore(tmp_path)
    service = ConfigurationService(
        store=LifecycleConfigurationStore(tmp_path),
        gateway=gateway,
        lifecycle_state=state,
    )
    service.store.compose_env_path.parent.mkdir(parents=True, exist_ok=True)
    service.store.compose_env_path.write_text(
        "FS_COMPOSE_PROJECT_NAME=flow-steward\n",
        encoding="utf-8",
    )
    service.store.write_active(DeploymentConfig(public_host="old.example.test"))
    _write_lifecycle_secret_environment(tmp_path / ".env")
    service.set(ConfigurationPatch(public_host="new.example.test"))
    gateway.calls.clear()

    _raises_input_1788_1 = ConfigApplyRequest()
    with (
        state.operation_lock("other-operation", mutating=True),
        pytest.raises(LifecycleError) as raised,
    ):
        service.apply(_raises_input_1788_1)

    assert raised.value.exit_code == 6
    assert gateway.calls == []


class SimulatedCrash(BaseException):
    pass


class CrashDuringActivePublicationStore(LifecycleConfigurationStore):
    def __init__(self, project_root: Path, crash_after: str) -> None:
        super().__init__(project_root)
        self.crash_after = crash_after
        self.crashed = False

    def _crash(self, write_name: str) -> None:
        if not self.crashed and self.crash_after == write_name:
            self.crashed = True
            raise SimulatedCrash(f"crash after {write_name}")

    def write_active(self, config: DeploymentConfig) -> None:
        super().write_active(config)
        self._crash("stack.toml")

    def write_runtime_environment(self, path: Path, managed: dict[str, str]) -> None:
        super().write_runtime_environment(path, managed)
        self._crash("target.env")

    def write_compose_environment(self, values: dict[str, str]) -> None:
        super().write_compose_environment(values)
        self._crash("compose.env")


@pytest.mark.parametrize("crash_after", ("stack.toml", "target.env", "compose.env"))
def test_environment_active_publication_is_resumable_and_not_visible_until_committed(
    tmp_path: Path,
    crash_after: str,
) -> None:
    store = CrashDuringActivePublicationStore(tmp_path, "")
    store.state_root.mkdir(parents=True, exist_ok=True)
    store.compose_env_path.write_text(
        "FS_COMPOSE_PROJECT_NAME=flow-steward\n",
        encoding="utf-8",
    )
    previous = DeploymentConfig(environment="development")
    candidate = previous.model_copy(update={"environment": "production"})
    store.write_active(previous)
    (tmp_path / ".env").write_text(
        "\n".join(
            (
                "FS_COMPOSE_PROJECT_NAME=flow-steward",
                "FS_SECRET_KEY=durable-secret",
                "ENCRYPTION_KEY=durable-encryption",
                "FS_BACKUP_ENCRYPTION_KEY=durable-backup",
                "FS_METRICS_TOKEN=durable-metrics",
                "POSTGRES_PASSWORD=durable-postgres",
                # SECURITY_PII_SALT is an installer-provisioned durable secret,
                # so a production transition is incomplete without it.
                "SECURITY_PII_SALT=durable-pii-salt",  # pragma: allowlist secret - fixture value
                "RABBITMQ_PASS=durable-rabbit",
                "ARTIFACT_S3_ACCESS_KEY=bundled-access",
                "ARTIFACT_S3_SECRET_KEY=bundled-secret",
                "",
            )
        ),
        encoding="utf-8",
    )
    service = ConfigurationService(store=store)
    prepared = service.prepare_transition_environment(previous, candidate)
    service.stage_transition_environment(previous, candidate, prepared)
    store.crash_after = crash_after

    with pytest.raises(SimulatedCrash, match=crash_after):
        service.mark_active(previous, candidate, prepared)

    assert service.read_active() == previous
    pending = store.active_publication_pending_path.read_text(encoding="utf-8")
    assert "durable-secret" not in pending

    service.mark_active(previous, candidate, prepared)

    assert service.active_publication_matches(candidate, prepared)
    assert service.read_active() == candidate
    assert store.active_revision_path.is_file()


@pytest.mark.parametrize("crash_side", ("before", "after"))
def test_active_publication_commit_has_one_atomic_pending_to_revision_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_side: str,
) -> None:
    store = LifecycleConfigurationStore(tmp_path)
    store.state_root.mkdir(parents=True, exist_ok=True)
    store.compose_env_path.write_text(
        "FS_COMPOSE_PROJECT_NAME=flow-steward\n",
        encoding="utf-8",
    )
    previous = DeploymentConfig(environment="development")
    candidate = previous.model_copy(update={"environment": "production"})
    store.write_active(previous)
    (tmp_path / ".env").write_text(
        "\n".join(
            (
                "FS_COMPOSE_PROJECT_NAME=flow-steward",
                "FS_SECRET_KEY=durable-secret",
                "ENCRYPTION_KEY=durable-encryption",
                "FS_BACKUP_ENCRYPTION_KEY=durable-backup",
                "FS_METRICS_TOKEN=durable-metrics",
                "POSTGRES_PASSWORD=durable-postgres",
                # SECURITY_PII_SALT is an installer-provisioned durable secret,
                # so a production transition is incomplete without it.
                "SECURITY_PII_SALT=durable-pii-salt",  # pragma: allowlist secret - fixture value
                "RABBITMQ_PASS=durable-rabbit",
                "ARTIFACT_S3_ACCESS_KEY=bundled-access",
                "ARTIFACT_S3_SECRET_KEY=bundled-secret",
                "",
            )
        ),
        encoding="utf-8",
    )
    service = ConfigurationService(store=store)
    prepared = service.prepare_transition_environment(previous, candidate)
    service.stage_transition_environment(previous, candidate, prepared)
    real_replace = os.replace

    def crash_at_commit(source: str | Path, destination: str | Path) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        if (
            source_path == store.active_publication_pending_path
            and destination_path == store.active_revision_path
        ):
            if crash_side == "after":
                real_replace(source, destination)
            raise SimulatedCrash(f"crash {crash_side} active publication rename")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", crash_at_commit)
    with pytest.raises(SimulatedCrash, match=crash_side):
        service.mark_active(previous, candidate, prepared)
    monkeypatch.undo()

    if crash_side == "before":
        assert store.active_publication_pending_path.is_file()
        assert service.read_active() == previous
        assert not service.active_publication_matches(candidate, prepared)
        service.mark_active(previous, candidate, prepared)
    else:
        assert not store.active_publication_pending_path.exists()

    assert service.read_active() == candidate
    assert service.active_publication_matches(candidate, prepared)

    reverse = service.prepare_transition_environment(candidate, previous)
    service.stage_transition_environment(candidate, previous, reverse)
    service.mark_active(candidate, previous, reverse)
    assert service.read_active() == previous


class CrashAfterRuntimeGateway(FakeConfigurationGateway):
    def __init__(self) -> None:
        super().__init__()
        self.crash_once = True

    def healthy(self, config, target) -> bool:
        self.calls.append(f"health:{config.public_host}")
        if self.crash_once:
            self.crash_once = False
            raise SimulatedCrash("process disappeared after runtime activation")
        return True


def test_apply_crash_resume_reuses_journal_and_does_not_repeat_runtime_change(
    tmp_path: Path,
) -> None:
    gateway = CrashAfterRuntimeGateway()
    state = LifecycleStateStore(tmp_path)
    service = ConfigurationService(
        store=LifecycleConfigurationStore(tmp_path),
        gateway=gateway,
        lifecycle_state=state,
    )
    service.store.compose_env_path.parent.mkdir(parents=True, exist_ok=True)
    service.store.compose_env_path.write_text(
        "FS_COMPOSE_PROJECT_NAME=flow-steward\n",
        encoding="utf-8",
    )
    service.store.write_active(DeploymentConfig(public_host="old.example.test"))
    _write_lifecycle_secret_environment(tmp_path / ".env")
    service.set(ConfigurationPatch(public_host="new.example.test"))

    _raises_input_1985_1 = ConfigApplyRequest()
    with pytest.raises(SimulatedCrash):
        service.apply(_raises_input_1985_1)

    unfinished = state.unfinished_journals()
    assert len(unfinished) == 1
    operation_id = unfinished[0].operation_id
    assert unfinished[0].current_step == "verify_health"
    assert "change_runtime" in unfinished[0].completed_steps

    result = service.apply(ConfigApplyRequest())

    assert result.operation_id == operation_id
    assert service.store.read_active().public_host == "new.example.test"
    assert service.store.read_pending() is None
    assert gateway.calls.count("activate:new.example.test") == 1
    assert gateway.calls.count("manifest:old.example.test") == 1
    journal = state.load_journal(operation_id)
    assert journal is not None
    assert journal.status is LifecycleOperationStatus.COMPLETED


class PreservationShapeGateway(FakeConfigurationGateway):
    def capture_manifest(
        self,
        config,
        _target,
        *,
        operation_id: str,
        stage: str,
    ) -> dict[str, object]:
        self.calls.append(f"manifest:{config.public_host}")
        manifest: dict[str, object] = {
            "public_host": config.public_host,
            "secret_ref_count": 2,
            "secret_ref_control_available": True,
        }
        self.manifests[(operation_id, stage)] = manifest
        return manifest


def test_apply_keeps_preservation_evidence_out_of_generic_config_snapshot(
    tmp_path: Path,
) -> None:
    gateway = PreservationShapeGateway()
    state = LifecycleStateStore(tmp_path)
    service = ConfigurationService(
        store=LifecycleConfigurationStore(tmp_path),
        gateway=gateway,
        lifecycle_state=state,
    )
    service.store.compose_env_path.parent.mkdir(parents=True, exist_ok=True)
    service.store.compose_env_path.write_text(
        "FS_COMPOSE_PROJECT_NAME=flow-steward\n",
        encoding="utf-8",
    )
    service.store.write_active(DeploymentConfig(public_host="old.example.test"))
    _write_lifecycle_secret_environment(tmp_path / ".env")
    service.set(ConfigurationPatch(public_host="new.example.test"))

    result = service.apply(ConfigApplyRequest())

    snapshot = state.load_previous_config(result.operation_id)
    assert snapshot is not None
    assert snapshot["configuration"] == DeploymentConfig(public_host="old.example.test").model_dump(
        mode="json"
    )
    assert isinstance(snapshot["pending_revision"], str)
    assert isinstance(snapshot["runtime"], dict)
    assert "manifest" not in snapshot
    assert gateway.load_manifest(result.operation_id, "before") is not None


def test_apply_failure_restores_exact_config_runtime_and_keeps_pending_and_journal(
    tmp_path: Path,
) -> None:
    gateway = FakeConfigurationGateway(fail_target_health=True, project_root=tmp_path)
    state = LifecycleStateStore(tmp_path)
    service = ConfigurationService(
        store=LifecycleConfigurationStore(tmp_path),
        gateway=gateway,
        lifecycle_state=state,
    )
    service.store.compose_env_path.parent.mkdir(parents=True, exist_ok=True)
    service.store.compose_env_path.write_text(
        "FS_COMPOSE_PROJECT_NAME=flow-steward\n",
        encoding="utf-8",
    )
    active = DeploymentConfig(public_host="old.example.test", public_port=8099)
    service.store.write_active(active)
    _write_lifecycle_secret_environment(tmp_path / ".env")
    service.set(ConfigurationPatch(public_host="new.example.test", public_port=443))

    _raises_input_2077_1 = ConfigApplyRequest()
    with pytest.raises(LifecycleError) as raised:
        service.apply(_raises_input_2077_1)

    assert raised.value.code == "configuration_apply_failed"
    assert service.store.read_active() == active
    assert service.store.read_pending() is not None
    journals = tuple(
        path
        for path in state.operations_dir.glob("*.json")
        if not path.name.endswith(".previous-config.json")
    )
    assert len(journals) == 1
    journal = state.load_journal(journals[0].stem)
    assert journal is not None
    assert journal.status is LifecycleOperationStatus.FAILED
    snapshot = state.load_previous_config(journal.operation_id)
    assert snapshot is not None
    assert snapshot["configuration"] == active.model_dump(mode="json")
    assert "recovery:restore" in gateway.calls
    assert "recovery_health:old.example.test" in gateway.calls
    assert "compare:old.example.test:old.example.test" in gateway.calls


def test_apply_rejects_changed_runtime_image_identity(tmp_path: Path) -> None:
    gateway = FakeConfigurationGateway(project_root=tmp_path)
    gateway.images_preserved = False
    state = LifecycleStateStore(tmp_path)
    service = ConfigurationService(
        store=LifecycleConfigurationStore(tmp_path),
        gateway=gateway,
        lifecycle_state=state,
    )
    service.store.compose_env_path.parent.mkdir(parents=True, exist_ok=True)
    service.store.compose_env_path.write_text(
        "FS_COMPOSE_PROJECT_NAME=flow-steward\n",
        encoding="utf-8",
    )
    active = DeploymentConfig(public_host="old.example.test")
    service.store.write_active(active)
    _write_lifecycle_secret_environment(tmp_path / ".env")
    service.set(ConfigurationPatch(public_host="new.example.test"))

    _raises_input_2119_1 = ConfigApplyRequest()
    with pytest.raises(LifecycleError) as raised:
        service.apply(_raises_input_2119_1)

    assert raised.value.code == "configuration_apply_failed"
    assert "runtime_images_match" in gateway.calls
    assert "recovery:restore" in gateway.calls
    # AC17-R167 (live): named -- the container, what it was, what it runs now
    assert raised.value.details["failed_action"] == "image_drift"
    assert "traefik was sha256:1111, now sha256:2222" in raised.value.cause
    assert service.store.read_active() == active
    assert service.store.read_pending() is not None


@pytest.mark.parametrize(
    ("certificate_host", "mismatched_key", "expired", "expected_issue"),
    (
        ("wrong.example.test", False, False, "SAN"),
        ("flow.example.test", True, False, "private key"),
        ("flow.example.test", False, True, "valid"),
    ),
)
def test_provided_tls_validation_parses_material_and_fails_closed(
    tmp_path: Path,
    certificate_host: str,
    mismatched_key: bool,
    expired: bool,
    expected_issue: str,
) -> None:
    cert_path = tmp_path / "operator/site.crt"
    key_path = tmp_path / "operator/site.key"
    now = datetime.now(UTC)
    _write_certificate_pair(
        cert_path,
        key_path,
        host=certificate_host,
        valid_from=(now - timedelta(days=30) if expired else None),
        valid_until=(now - timedelta(days=1) if expired else None),
    )
    if mismatched_key:
        _write_certificate_pair(
            tmp_path / "unused.crt",
            key_path,
            host=certificate_host,
        )
    service = _service(tmp_path)
    config = DeploymentConfig(
        tls_mode="provided",
        public_host="flow.example.test",
        certificate_path="operator/site.crt",
        private_key_path="operator/site.key",
    )

    result = service.validate_config(config)

    assert result.valid is False
    assert any(expected_issue.lower() in issue.lower() for issue in result.issues)


def test_provided_tls_validation_rejects_unrelated_fullchain_issuer(tmp_path: Path) -> None:
    cert_path = tmp_path / "operator/site.crt"
    key_path = tmp_path / "operator/site.key"
    _write_certificate_pair(cert_path, key_path, host="flow.example.test")
    unrelated = tmp_path / "unrelated.crt"
    _write_certificate_pair(unrelated, tmp_path / "unrelated.key", host="issuer.example.test")
    cert_path.write_bytes(cert_path.read_bytes() + unrelated.read_bytes())
    service = _service(tmp_path)

    result = service.validate_config(
        DeploymentConfig(
            tls_mode="provided",
            public_host="flow.example.test",
            certificate_path="operator/site.crt",
            private_key_path="operator/site.key",
        )
    )

    assert result.valid is False
    assert any("chain" in issue for issue in result.issues)


class _ArtifactDocker:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.validated_targets = []
        self.capture_calls = []
        self.restore_calls = []
        self.storage_validation_calls = []
        self.port_validation_calls = []
        self.apply_calls = []
        self.prepare_image_calls = []
        self.stop_calls = []
        self.project_stop_calls = []
        self.profile_state_stage_calls = []
        self.released_rollback_pins: list[str] = []
        self.transition_events = []
        self.security_posture_reconcile_calls = []
        self.workflow_knowledge_reconcile_calls = []

    def validate_target(self, target, *, environment, override_content=None) -> None:
        self.validated_targets.append((target, dict(environment), override_content))

    def prepare_target_images(
        self,
        target,
        *,
        environment,
        override_content=None,
        timeout_seconds: int,
    ) -> None:
        self.prepare_image_calls.append(
            (target, dict(environment), override_content, timeout_seconds)
        )

    def capture_rollback(
        self,
        target,
        *,
        operation_id: str,
        selected_services: tuple[str, ...],
        managed_services: tuple[str, ...],
    ) -> StackRollbackSnapshot:
        self.capture_calls.append((selected_services, managed_services))
        return StackRollbackSnapshot(
            operation_id=operation_id,
            project_name=target.project_name,
            selected_services=selected_services,
            services=tuple(
                StackContainerRollbackState(
                    service=service,
                    topology="running" if index % 2 else "stopped",
                    container_id=f"id-{service}",
                    image_id=f"sha256:{index:064x}",
                )
                for index, service in enumerate(managed_services, start=1)
            ),
        )

    def restore(self, target, **kwargs) -> bool:
        self.restore_calls.append((target, kwargs))
        return kwargs["action"] == "rebuild"

    def release_rollback_pins(self, snapshot) -> tuple[str, ...]:
        self.released_rollback_pins.append(snapshot.operation_id)
        return ()

    def storage_connectivity(self, _target) -> bool:
        return True

    def require_storage_connectivity(self, _target) -> None:
        return None

    def reconcile_workflow_knowledge(self, target) -> None:
        self.workflow_knowledge_reconcile_calls.append(target)

    def reconcile_security_posture(self, target) -> None:
        self.security_posture_reconcile_calls.append(target)

    def port_available_for_transition(
        self, target, host: str, port: int, *, protocol: str = "tcp"
    ) -> bool:
        self.port_validation_calls.append((target, host, port, protocol))
        return True

    def apply_configuration(self, target, **kwargs) -> None:
        self.transition_events.append("apply")
        self.apply_calls.append((target, kwargs))

    def stage_profile_runtime_state(
        self, previous_target, target, *, source_environment=None
    ) -> None:
        self.transition_events.append("stage_profile_runtime_state")
        self.profile_state_stage_calls.append(
            (previous_target, target, dict(source_environment or {}))
        )

    def stop(
        self,
        target,
        services,
        *,
        timeout_seconds: int,
        output_sink,
        heartbeat,
    ) -> None:
        del output_sink, heartbeat
        self.stop_calls.append((target, services, timeout_seconds))

    def stop_project_services(
        self,
        project_name,
        services,
        *,
        timeout_seconds,
        output_sink,
        heartbeat,
    ) -> None:
        del output_sink, heartbeat
        self.transition_events.append("stop_source")
        self.project_stop_calls.append((project_name, services, timeout_seconds))


def _artifact_gateway(
    tmp_path: Path,
) -> tuple[ConfigurationLifecycleGateway, _ArtifactDocker]:
    docker = _ArtifactDocker(tmp_path)
    gateway = ConfigurationLifecycleGateway(
        project_root=tmp_path,
        docker=docker,  # type: ignore[arg-type]
        preservation=SimpleNamespace(),  # type: ignore[arg-type]
        manifest_store=PreservationManifestStore(tmp_path),
        readiness_service=SimpleNamespace(check=lambda: None),
        release_provider=lambda: "test",
        storage_probe=lambda environment: (
            docker.storage_validation_calls.append(dict(environment)) or True
        ),
    )
    return gateway, docker


def _tree_inventory(root: Path) -> tuple[tuple[str, int, bytes | str | None], ...]:
    rows = []
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        stat = path.lstat()
        if path.is_symlink():
            payload: bytes | str | None = str(path.readlink())
        elif path.is_file():
            payload = path.read_bytes()
        else:
            payload = None
        rows.append((relative, stat.st_mode, payload))
    return tuple(rows)


def test_configuration_gateway_prepares_images_from_in_memory_target(
    tmp_path: Path,
) -> None:
    gateway, docker = _artifact_gateway(tmp_path)
    service = _service(tmp_path)
    config = DeploymentConfig(
        environment="development",
        tls_mode="reverse_proxy",
        public_host="flow.example.test",
    )
    target = service.derive_target(config)
    environment = {"CANDIDATE_ONLY": "value"}

    gateway.prepare_target_images(config, target, environment)

    prepared_target, prepared_environment, override, timeout = docker.prepare_image_calls[0]
    assert prepared_target == target
    assert prepared_environment == environment
    assert override == _reverse_proxy_override(config)
    assert timeout == 1800


def test_configuration_gateway_retries_transient_candidate_readiness(
    tmp_path: Path,
) -> None:
    docker = _ArtifactDocker(tmp_path)
    clock = [0.0]
    readiness_calls: list[str] = []

    def candidate_readiness(config: DeploymentConfig) -> ReadinessReport:
        readiness_calls.append(config.environment)
        ready = len(readiness_calls) >= 3
        return _candidate_readiness_report(
            () if ready else ("queue_worker_health",),
        )

    gateway = ConfigurationLifecycleGateway(
        project_root=tmp_path,
        docker=docker,  # type: ignore[arg-type]
        preservation=SimpleNamespace(),  # type: ignore[arg-type]
        manifest_store=PreservationManifestStore(tmp_path),
        readiness_service=SimpleNamespace(check=lambda: None),
        release_provider=lambda: "test",
        timeout_seconds=10,
        candidate_readiness=candidate_readiness,
        readiness_clock=lambda: clock[0],
        readiness_sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )
    config = DeploymentConfig(environment="development")
    target = _service(tmp_path).derive_target(config)

    assert gateway.healthy(config, target) is True
    assert docker.security_posture_reconcile_calls == []
    assert docker.workflow_knowledge_reconcile_calls == []
    assert readiness_calls == ["development", "development", "development"]
    assert clock[0] == 2


def test_configuration_gateway_reconciles_only_failed_derived_readiness(
    tmp_path: Path,
) -> None:
    docker = _ArtifactDocker(tmp_path)
    readiness_calls = 0

    def candidate_readiness(_config: DeploymentConfig) -> ReadinessReport:
        nonlocal readiness_calls
        readiness_calls += 1
        return _candidate_readiness_report(
            ("security_posture", "workflow_knowledge_sync") if readiness_calls == 1 else ()
        )

    gateway = ConfigurationLifecycleGateway(
        project_root=tmp_path,
        docker=docker,  # type: ignore[arg-type]
        preservation=SimpleNamespace(),  # type: ignore[arg-type]
        manifest_store=PreservationManifestStore(tmp_path),
        readiness_service=SimpleNamespace(check=lambda: None),
        release_provider=lambda: "test",
        timeout_seconds=10,
        candidate_readiness=candidate_readiness,
        readiness_clock=lambda: 0.0,
        readiness_sleep=lambda _seconds: None,
    )
    config = DeploymentConfig(environment="production")
    target = _service(tmp_path).derive_target(config)

    assert gateway.healthy(config, target) is True
    assert docker.security_posture_reconcile_calls == [target]
    assert docker.workflow_knowledge_reconcile_calls == [target]


def test_configuration_gateway_retries_all_candidate_startup_failures_together(
    tmp_path: Path,
) -> None:
    docker = _ArtifactDocker(tmp_path)
    clock = [0.0]
    readiness_calls = 0
    startup_ids = (
        "api_ready",
        "compact_runtime",
        "compose_runtime",
        "job_dispatch",
        "mcp_auth",
        "queue_worker_health",
        "rabbitmq_socket",
        "security_posture",
        "workflow_knowledge_sync",
    )

    def candidate_readiness(_config: DeploymentConfig) -> ReadinessReport:
        nonlocal readiness_calls
        readiness_calls += 1
        return _candidate_readiness_report(startup_ids if readiness_calls == 1 else ())

    gateway = ConfigurationLifecycleGateway(
        project_root=tmp_path,
        docker=docker,  # type: ignore[arg-type]
        preservation=SimpleNamespace(),  # type: ignore[arg-type]
        manifest_store=PreservationManifestStore(tmp_path),
        readiness_service=SimpleNamespace(check=lambda: None),
        release_provider=lambda: "test",
        timeout_seconds=10,
        candidate_readiness=candidate_readiness,
        readiness_clock=lambda: clock[0],
        readiness_sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )
    config = DeploymentConfig(environment="production")

    assert gateway.healthy(config, _service(tmp_path).derive_target(config)) is True
    assert readiness_calls == 2
    assert clock[0] == 1


def test_configuration_gateway_does_not_retry_startup_failure_with_env_conflict(
    tmp_path: Path,
) -> None:
    docker = _ArtifactDocker(tmp_path)
    calls = 0
    sleeps: list[float] = []

    def candidate_readiness(_config: DeploymentConfig) -> ReadinessReport:
        nonlocal calls
        calls += 1
        return _candidate_readiness_report(("api_ready", "env_duplicate_keys"))

    gateway = ConfigurationLifecycleGateway(
        project_root=tmp_path,
        docker=docker,  # type: ignore[arg-type]
        preservation=SimpleNamespace(),  # type: ignore[arg-type]
        manifest_store=PreservationManifestStore(tmp_path),
        readiness_service=SimpleNamespace(check=lambda: None),
        release_provider=lambda: "test",
        timeout_seconds=10,
        candidate_readiness=candidate_readiness,
        readiness_clock=lambda: 0.0,
        readiness_sleep=sleeps.append,
    )
    config = DeploymentConfig(environment="production")

    _raises_input_2407_1 = _service(tmp_path).derive_target(config)
    with pytest.raises(StackGatewayError):
        gateway.healthy(config, _raises_input_2407_1)
    assert calls == 1
    assert sleeps == []


def _candidate_readiness_report(
    failed_check_ids: tuple[str, ...] = (),
) -> ReadinessReport:
    return ReadinessReport(
        status="blocked" if failed_check_ids else "ready",
        findings=tuple(
            ReadinessFinding(
                check_id=check_id,
                area=ReadinessArea.CORE,
                title=check_id,
                status=ReadinessStatus.FAILED,
            )
            for check_id in failed_check_ids
        ),
        core_failed=len(failed_check_ids),
        process_exit_code=1 if failed_check_ids else 0,
    )


@pytest.mark.parametrize(
    "reports",
    [
        (_candidate_readiness_report(("database",)),),
        (_candidate_readiness_report(("queue_worker_health", "database")),),
        (SimpleNamespace(status="blocked", core_failed=1, findings=()),),
    ],
    ids=("unrelated-blocker", "mixed-blockers", "malformed-report"),
)
def test_configuration_gateway_does_not_retry_non_transient_readiness(
    tmp_path: Path,
    reports: tuple[object, ...],
) -> None:
    docker = _ArtifactDocker(tmp_path)
    calls = 0
    sleeps: list[float] = []

    def candidate_readiness(_config: DeploymentConfig) -> object:
        nonlocal calls
        calls += 1
        return reports[0]

    gateway = ConfigurationLifecycleGateway(
        project_root=tmp_path,
        docker=docker,  # type: ignore[arg-type]
        preservation=SimpleNamespace(),  # type: ignore[arg-type]
        manifest_store=PreservationManifestStore(tmp_path),
        readiness_service=SimpleNamespace(check=lambda: None),
        release_provider=lambda: "test",
        timeout_seconds=10,
        candidate_readiness=candidate_readiness,
        readiness_clock=lambda: 0.0,
        readiness_sleep=sleeps.append,
    )

    _raises_input_2468_1 = DeploymentConfig()
    _raises_input_2468_2 = _service(tmp_path).derive_target(DeploymentConfig())
    with pytest.raises(StackGatewayError) as raised:
        gateway.healthy(_raises_input_2468_1, _raises_input_2468_2)
    # AC17-R148: a malformed report used to be a bare False, which `config apply`
    # reported as "the target configuration failed" and nothing more.
    assert raised.value.action == "candidate_readiness"
    if not isinstance(reports[0], ReadinessReport):
        assert raised.value.detail == (
            "Candidate readiness returned no readiness report (SimpleNamespace)."
        )
    assert calls == 1
    assert sleeps == []


def test_configuration_gateway_preserves_safe_non_transient_readiness_ids(
    tmp_path: Path,
) -> None:
    docker = _ArtifactDocker(tmp_path)
    gateway = ConfigurationLifecycleGateway(
        project_root=tmp_path,
        docker=docker,  # type: ignore[arg-type]
        preservation=SimpleNamespace(),  # type: ignore[arg-type]
        manifest_store=PreservationManifestStore(tmp_path),
        readiness_service=SimpleNamespace(check=lambda: None),
        release_provider=lambda: "test",
        timeout_seconds=10,
        candidate_readiness=lambda _config: _candidate_readiness_report(("database", "api_health")),
        readiness_clock=lambda: 0.0,
        readiness_sleep=lambda _seconds: None,
    )

    _raises_input_2502_1 = DeploymentConfig()
    _raises_input_2502_2 = _service(tmp_path).derive_target(DeploymentConfig())
    with pytest.raises(StackGatewayError) as raised:
        gateway.healthy(_raises_input_2502_1, _raises_input_2502_2)

    assert raised.value.action == "candidate_readiness"
    assert raised.value.detail == "Candidate readiness failed: api_health, database."


def test_configuration_gateway_retries_transient_readiness_exception(tmp_path: Path) -> None:
    docker = _ArtifactDocker(tmp_path)
    clock = [0.0]
    calls = 0
    sleeps: list[float] = []

    def candidate_readiness(_config: DeploymentConfig) -> ReadinessReport:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionRefusedError("candidate app is still starting")
        return _candidate_readiness_report()

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    gateway = ConfigurationLifecycleGateway(
        project_root=tmp_path,
        docker=docker,  # type: ignore[arg-type]
        preservation=SimpleNamespace(),  # type: ignore[arg-type]
        manifest_store=PreservationManifestStore(tmp_path),
        readiness_service=SimpleNamespace(check=lambda: None),
        release_provider=lambda: "test",
        timeout_seconds=10,
        candidate_readiness=candidate_readiness,
        readiness_clock=lambda: clock[0],
        readiness_sleep=sleep,
    )

    assert gateway.healthy(DeploymentConfig(), _service(tmp_path).derive_target(DeploymentConfig()))
    assert calls == 2
    assert sleeps == [1.0]


def test_configuration_gateway_retries_pending_candidate_route(tmp_path: Path) -> None:
    docker = _ArtifactDocker(tmp_path)
    clock = [0.0]
    calls = 0
    sleeps: list[float] = []

    def candidate_readiness(_config: DeploymentConfig) -> ReadinessReport:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise CandidateReadinessProbePendingError("health endpoint returned HTTP 404")
        return _candidate_readiness_report()

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    gateway = ConfigurationLifecycleGateway(
        project_root=tmp_path,
        docker=docker,  # type: ignore[arg-type]
        preservation=SimpleNamespace(),  # type: ignore[arg-type]
        manifest_store=PreservationManifestStore(tmp_path),
        readiness_service=SimpleNamespace(check=lambda: None),
        release_provider=lambda: "test",
        timeout_seconds=10,
        candidate_readiness=candidate_readiness,
        readiness_clock=lambda: clock[0],
        readiness_sleep=sleep,
    )

    assert gateway.healthy(DeploymentConfig(), _service(tmp_path).derive_target(DeploymentConfig()))
    assert calls == 2
    assert sleeps == [1.0]


def test_configuration_gateway_preserves_pending_route_cause_at_deadline(
    tmp_path: Path,
) -> None:
    docker = _ArtifactDocker(tmp_path)
    clock = [0.0]

    def candidate_readiness(_config: DeploymentConfig) -> ReadinessReport:
        raise CandidateReadinessProbePendingError("health endpoint returned HTTP 404")

    gateway = ConfigurationLifecycleGateway(
        project_root=tmp_path,
        docker=docker,  # type: ignore[arg-type]
        preservation=SimpleNamespace(),  # type: ignore[arg-type]
        manifest_store=PreservationManifestStore(tmp_path),
        readiness_service=SimpleNamespace(check=lambda: None),
        release_provider=lambda: "test",
        timeout_seconds=2,
        candidate_readiness=candidate_readiness,
        readiness_clock=lambda: clock[0],
        readiness_sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )

    config = DeploymentConfig()
    target = _service(tmp_path).derive_target(DeploymentConfig())

    with pytest.raises(StackGatewayError) as raised:
        gateway.healthy(config, target)

    assert raised.value.action == "candidate_readiness"
    assert raised.value.detail == (
        "Candidate readiness timed out: health endpoint returned HTTP 404."
    )


def test_configuration_gateway_does_not_retry_non_transport_readiness_exception(
    tmp_path: Path,
) -> None:
    docker = _ArtifactDocker(tmp_path)
    sleeps: list[float] = []

    def candidate_readiness(_config: DeploymentConfig) -> ReadinessReport:
        raise ValueError("health endpoint returned HTTP 401")

    gateway = ConfigurationLifecycleGateway(
        project_root=tmp_path,
        docker=docker,  # type: ignore[arg-type]
        preservation=SimpleNamespace(),  # type: ignore[arg-type]
        manifest_store=PreservationManifestStore(tmp_path),
        readiness_service=SimpleNamespace(check=lambda: None),
        release_provider=lambda: "test",
        timeout_seconds=10,
        candidate_readiness=candidate_readiness,
        readiness_clock=lambda: 0.0,
        readiness_sleep=sleeps.append,
    )

    config = DeploymentConfig()
    target = _service(tmp_path).derive_target(DeploymentConfig())
    with pytest.raises(StackGatewayError) as raised:
        gateway.healthy(config, target)
    assert raised.value.action == "candidate_readiness"
    assert raised.value.detail == (
        "Candidate readiness probe failed: ValueError: health endpoint returned HTTP 401."
    )
    assert sleeps == []


@pytest.mark.parametrize(
    ("storage_error", "expected_detail"),
    [
        (
            StackGatewayError(
                action="storage_readiness", detail="local artifact root is unavailable"
            ),
            "Candidate storage probe failed: local artifact root is unavailable",
        ),
        (
            RuntimeError("storage failed"),
            "Candidate storage probe failed: RuntimeError: storage failed.",
        ),
    ],
    ids=("probe-refused", "probe-crashed"),
)
def test_configuration_gateway_does_not_retry_storage_failure(
    tmp_path: Path,
    storage_error: Exception,
    expected_detail: str,
) -> None:
    docker = _ArtifactDocker(tmp_path)
    calls = 0
    sleeps: list[float] = []

    def require_storage_connectivity(_target: ComposeTarget) -> None:
        nonlocal calls
        calls += 1
        raise storage_error

    docker.require_storage_connectivity = require_storage_connectivity  # type: ignore[method-assign]
    gateway = ConfigurationLifecycleGateway(
        project_root=tmp_path,
        docker=docker,  # type: ignore[arg-type]
        preservation=SimpleNamespace(),  # type: ignore[arg-type]
        manifest_store=PreservationManifestStore(tmp_path),
        readiness_service=SimpleNamespace(check=lambda: None),
        release_provider=lambda: "test",
        timeout_seconds=10,
        candidate_readiness=lambda _config: _candidate_readiness_report(),
        readiness_clock=lambda: 0.0,
        readiness_sleep=sleeps.append,
    )

    config = DeploymentConfig()
    target = _service(tmp_path).derive_target(DeploymentConfig())
    with pytest.raises(StackGatewayError) as raised:
        gateway.healthy(config, target)
    assert raised.value.action == "storage_readiness"
    assert raised.value.detail == expected_detail
    assert calls == 1
    assert sleeps == []


def test_configuration_recovery_accepts_exactly_restored_stopped_primary(
    tmp_path: Path,
) -> None:
    storage_calls = 0
    target = _service(tmp_path).derive_target(
        DeploymentConfig(environment="production", deployment_profile="scalable")
    )
    snapshot = StackRollbackSnapshot(
        operation_id="stopped-baseline",
        project_name=target.project_name,
        selected_services=("fs-app",),
        services=(
            StackContainerRollbackState(
                service="fs-app",
                topology="stopped",
                container_id="stopped-app",
                image_id=f"sha256:{'a' * 64}",
            ),
        ),
    )

    def storage_connectivity(_target: ComposeTarget) -> bool:
        nonlocal storage_calls
        storage_calls += 1
        return True

    docker = SimpleNamespace(
        status=lambda _target: StackRuntimeState(
            project_name=target.project_name,
            services=(
                StackContainerState(
                    service="fs-app",
                    container_id="stopped-app",
                    container_name="stopped-app",
                    state="exited",
                ),
            ),
        ),
        storage_connectivity=storage_connectivity,
    )
    gateway = ConfigurationLifecycleGateway(
        project_root=tmp_path,
        docker=docker,  # type: ignore[arg-type]
        preservation=SimpleNamespace(),  # type: ignore[arg-type]
        manifest_store=PreservationManifestStore(tmp_path),
        readiness_service=SimpleNamespace(check=lambda: None),
        release_provider=lambda: "test",
    )

    assert gateway.recovery_healthy(
        DeploymentConfig(environment="production", deployment_profile="scalable"),
        target,
        snapshot.model_dump(mode="json"),
    )
    assert storage_calls == 0


@pytest.mark.parametrize(
    ("state", "health", "storage_ready", "expected"),
    (
        ("running", "healthy", True, True),
        ("running", "unhealthy", True, False),
        ("exited", "", True, False),
        ("running", "healthy", False, False),
    ),
)
def test_configuration_recovery_keeps_health_and_storage_guards_for_running_primary(
    tmp_path: Path,
    state: str,
    health: str,
    storage_ready: bool,
    expected: bool,
) -> None:
    target = _service(tmp_path).derive_target(
        DeploymentConfig(environment="production", deployment_profile="compact")
    )
    snapshot = StackRollbackSnapshot(
        operation_id="running-baseline",
        project_name=target.project_name,
        selected_services=("compact-runtime",),
        services=(
            StackContainerRollbackState(
                service="compact-runtime",
                topology="running",
                container_id="compact-app",
                image_id=f"sha256:{'b' * 64}",
            ),
        ),
    )
    docker = SimpleNamespace(
        status=lambda _target: StackRuntimeState(
            project_name=target.project_name,
            services=(
                StackContainerState(
                    service="compact-runtime",
                    container_id="compact-app",
                    container_name="compact-app",
                    state=state,
                    health=health,
                ),
            ),
        ),
        storage_connectivity=lambda _target: storage_ready,
        require_storage_connectivity=lambda _target: None,
    )
    gateway = ConfigurationLifecycleGateway(
        project_root=tmp_path,
        docker=docker,  # type: ignore[arg-type]
        preservation=SimpleNamespace(),  # type: ignore[arg-type]
        manifest_store=PreservationManifestStore(tmp_path),
        readiness_service=SimpleNamespace(check=lambda: None),
        release_provider=lambda: "test",
        candidate_readiness=lambda _config: _candidate_readiness_report(),
    )

    assert (
        gateway.recovery_healthy(
            DeploymentConfig(environment="production", deployment_profile="compact"),
            target,
            snapshot.model_dump(mode="json"),
        )
        is expected
    )


def _running_compact_recovery(
    tmp_path: Path, reports: list[ReadinessReport], clock: list[float]
) -> tuple[ConfigurationLifecycleGateway, ComposeTarget, dict[str, object]]:
    target = _service(tmp_path).derive_target(
        DeploymentConfig(environment="production", deployment_profile="compact")
    )
    snapshot = StackRollbackSnapshot(
        operation_id="running-baseline",
        project_name=target.project_name,
        selected_services=("compact-runtime",),
        services=(
            StackContainerRollbackState(
                service="compact-runtime",
                topology="running",
                container_id="compact-app",
                image_id=f"sha256:{'b' * 64}",
            ),
        ),
    )
    docker = SimpleNamespace(
        status=lambda _target: StackRuntimeState(
            project_name=target.project_name,
            services=(
                StackContainerState(
                    service="compact-runtime",
                    container_id="compact-app",
                    container_name="compact-app",
                    state="running",
                    health="healthy",
                ),
            ),
        ),
        storage_connectivity=lambda _target: True,
        require_storage_connectivity=lambda _target: None,
    )

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    gateway = ConfigurationLifecycleGateway(
        project_root=tmp_path,
        docker=docker,  # type: ignore[arg-type]
        preservation=SimpleNamespace(),  # type: ignore[arg-type]
        manifest_store=PreservationManifestStore(tmp_path),
        readiness_service=SimpleNamespace(check=lambda: None),
        release_provider=lambda: "test",
        timeout_seconds=30,
        candidate_readiness=lambda _config: reports.pop(0) if len(reports) > 1 else reports[0],
        readiness_clock=lambda: clock[0],
        readiness_sleep=sleep,
    )
    return gateway, target, snapshot.model_dump(mode="json")


def test_a_restored_runtime_is_back_only_once_it_is_ready(tmp_path: Path) -> None:
    """AC17-R167: live, the restored Compact answered 502 then 503 for ~26 s after the
    apply had reported it restored; the operator's next command failed in that window."""
    clock = [0.0]
    starting = _candidate_readiness_report(("api_ready",))
    gateway, target, snapshot = _running_compact_recovery(
        tmp_path, [starting, starting, _candidate_readiness_report()], clock
    )

    assert gateway.recovery_healthy(
        DeploymentConfig(environment="production", deployment_profile="compact"),
        target,
        snapshot,
    )
    assert clock[0] == 2.0  # waited through the two not-yet-ready reports


def test_a_restored_runtime_that_never_gets_ready_says_why(tmp_path: Path) -> None:
    clock = [0.0]
    gateway, target, snapshot = _running_compact_recovery(
        tmp_path, [_candidate_readiness_report(("api_ready",))], clock
    )

    config = DeploymentConfig(environment="production", deployment_profile="compact")
    with pytest.raises(StackGatewayError) as raised:
        gateway.recovery_healthy(config, target, snapshot)

    assert raised.value.action == "recovery_readiness"
    assert raised.value.detail == (
        "The restored runtime did not become ready: Candidate readiness timed out: api_ready."
    )


def test_configuration_gateway_caps_transient_wait_at_deadline(tmp_path: Path) -> None:
    docker = _ArtifactDocker(tmp_path)
    clock = [0.0]
    calls = 0
    sleeps: list[float] = []

    def candidate_readiness(_config: DeploymentConfig) -> ReadinessReport:
        nonlocal calls
        calls += 1
        return _candidate_readiness_report(("queue_worker_health",))

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    gateway = ConfigurationLifecycleGateway(
        project_root=tmp_path,
        docker=docker,  # type: ignore[arg-type]
        preservation=SimpleNamespace(),  # type: ignore[arg-type]
        manifest_store=PreservationManifestStore(tmp_path),
        readiness_service=SimpleNamespace(check=lambda: None),
        release_provider=lambda: "test",
        timeout_seconds=2,
        candidate_readiness=candidate_readiness,
        readiness_clock=lambda: clock[0],
        readiness_sleep=sleep,
    )

    _raises_input_2642_1 = DeploymentConfig()
    _raises_input_2642_2 = _service(tmp_path).derive_target(DeploymentConfig())
    with pytest.raises(StackGatewayError) as raised:
        gateway.healthy(_raises_input_2642_1, _raises_input_2642_2)

    assert raised.value.action == "candidate_readiness"
    assert raised.value.detail == ("Candidate readiness timed out: queue_worker_health.")
    assert calls == 2
    assert sleeps == [1.0, 1.0]
    assert clock[0] == 2.0


def test_configuration_gateway_rejects_success_completed_at_deadline(tmp_path: Path) -> None:
    docker = _ArtifactDocker(tmp_path)
    clock = [0.0]

    def candidate_readiness(_config: DeploymentConfig) -> ReadinessReport:
        clock[0] = 2.0
        return _candidate_readiness_report()

    gateway = ConfigurationLifecycleGateway(
        project_root=tmp_path,
        docker=docker,  # type: ignore[arg-type]
        preservation=SimpleNamespace(),  # type: ignore[arg-type]
        manifest_store=PreservationManifestStore(tmp_path),
        readiness_service=SimpleNamespace(check=lambda: None),
        release_provider=lambda: "test",
        timeout_seconds=2,
        candidate_readiness=candidate_readiness,
        readiness_clock=lambda: clock[0],
        readiness_sleep=lambda _seconds: None,
    )

    config = DeploymentConfig()
    target = _service(tmp_path).derive_target(DeploymentConfig())
    with pytest.raises(StackGatewayError) as raised:
        gateway.healthy(config, target)
    assert raised.value.detail == (
        "Candidate readiness timed out: the readiness report arrived at the deadline."
    )


def test_configuration_gateway_rejects_storage_completed_at_deadline(tmp_path: Path) -> None:
    docker = _ArtifactDocker(tmp_path)
    clock = [0.0]
    storage_calls = 0
    sleeps: list[float] = []

    def require_storage_connectivity(_target: ComposeTarget) -> None:
        nonlocal storage_calls
        storage_calls += 1
        clock[0] = 2.0

    docker.require_storage_connectivity = require_storage_connectivity  # type: ignore[method-assign]
    gateway = ConfigurationLifecycleGateway(
        project_root=tmp_path,
        docker=docker,  # type: ignore[arg-type]
        preservation=SimpleNamespace(),  # type: ignore[arg-type]
        manifest_store=PreservationManifestStore(tmp_path),
        readiness_service=SimpleNamespace(check=lambda: None),
        release_provider=lambda: "test",
        timeout_seconds=2,
        candidate_readiness=lambda _config: _candidate_readiness_report(),
        readiness_clock=lambda: clock[0],
        readiness_sleep=sleeps.append,
    )

    config = DeploymentConfig()
    target = _service(tmp_path).derive_target(DeploymentConfig())
    with pytest.raises(StackGatewayError) as raised:
        gateway.healthy(config, target)
    assert raised.value.detail == (
        "Candidate readiness timed out while storage was being verified."
    )
    assert storage_calls == 1
    assert sleeps == []


def test_validate_keeps_repository_tree_byte_identical_with_in_memory_candidates(
    tmp_path: Path,
) -> None:
    source_cert = tmp_path / "operator/site.crt"
    source_key = tmp_path / "operator/site.key"
    _write_certificate_pair(source_cert, source_key, host="flow.example.test")
    original_env = b"# unmanaged\nCUSTOM=value\n"
    (tmp_path / ".env").write_bytes(original_env)
    gateway, docker = _artifact_gateway(tmp_path)
    service = _service(tmp_path)
    config = DeploymentConfig(
        tls_mode="reverse_proxy",
        public_host="flow.example.test",
        reverse_proxy_backend_port=18443,
        certificate_path="operator/site.crt",
        private_key_path="operator/site.key",
    )
    target = service.derive_target(config)
    before = _tree_inventory(tmp_path)

    gateway.validate_target(config, target, {"FS_PUBLIC_BASE_URL": "https://flow.example.test"})

    assert len(docker.validated_targets) == 1
    validated_target, environment, override = docker.validated_targets[0]
    assert validated_target == target
    assert environment == {"FS_PUBLIC_BASE_URL": "https://flow.example.test"}
    assert override == _reverse_proxy_override(config)
    assert _tree_inventory(tmp_path) == before
    assert docker.storage_validation_calls == []
    assert [call[1:] for call in docker.port_validation_calls] == [
        ("127.0.0.1", 18443, "tcp"),
    ]


def test_validate_local_checks_candidate_app_port_for_tcp_and_udp(tmp_path: Path) -> None:
    gateway, docker = _artifact_gateway(tmp_path)
    service = _service(tmp_path)
    config = DeploymentConfig(tls_mode="local", public_port=18099)
    target = service.derive_target(config)

    gateway.validate_target(config, target, {"ARTIFACT_S3_BUCKET": "artifacts"})

    assert [call[1:] for call in docker.port_validation_calls] == [
        ("0.0.0.0", 18099, "tcp"),
        ("0.0.0.0", 18099, "udp"),
    ]


def test_validate_bundled_target_defers_live_storage_probe_to_ordered_probe_step(
    tmp_path: Path,
) -> None:
    gateway, docker = _artifact_gateway(tmp_path)
    service = _service(tmp_path)
    config = DeploymentConfig(file_storage="bundled")
    target = service.derive_target(config)

    gateway.validate_target(config, target, {"ARTIFACT_S3_ENDPOINT_URL": "http://seaweedfs:8333"})

    assert docker.storage_validation_calls == []


def test_validate_rejects_symlinked_control_directory_without_outside_creation(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    outside_root = tmp_path / "outside"
    project_root.mkdir()
    outside_root.mkdir()
    (project_root / ".flow-steward").symlink_to(outside_root, target_is_directory=True)
    gateway, docker = _artifact_gateway(project_root)
    config = DeploymentConfig()
    target = ComposeTarget(
        stack_environment="development",
        file_storage="bundled",
        env_file=".env",
        compose_files=("infra/docker/docker-compose.yaml",),
        required_services=("fs-app",),
        runtime_services=("fs-app",),
        rebuild_services=("fs-app",),
        project_name="installed-project",
    )

    with pytest.raises(ValueError, match="control directory"):
        gateway.validate_target(config, target, {"FS_SECRET_KEY": "candidate-secret"})

    assert docker.validated_targets == []
    assert not any(outside_root.iterdir())


def test_validate_uses_real_compose_resolver_with_in_memory_candidate_state(
    tmp_path: Path,
) -> None:
    commands: list[tuple[str, ...]] = []
    invocations: list[dict[str, object]] = []

    class RecordingProcess:
        def run(self, command: list[str], **kwargs: object):
            commands.append(tuple(command))
            invocations.append(dict(kwargs))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    docker = DockerLifecycleGateway(
        project_root=tmp_path,
        process=RecordingProcess(),
        port_available=lambda _host, _port, _protocol: True,
    )
    gateway = ConfigurationLifecycleGateway(
        project_root=tmp_path,
        docker=docker,
        preservation=SimpleNamespace(),  # type: ignore[arg-type]
        manifest_store=PreservationManifestStore(tmp_path),
        readiness_service=SimpleNamespace(check=lambda: None),
        release_provider=lambda: "test",
        storage_probe=lambda candidate: candidate == environment,
    )
    service = _service(tmp_path)
    config = DeploymentConfig(
        file_storage="external_s3",
        s3_endpoint="https://objects.example.test",
        s3_bucket="artifacts",
        tls_mode="reverse_proxy",
    )
    target = service.derive_target(config)
    environment = {
        "ARTIFACT_S3_ENDPOINT_URL": "https://objects.example.test",
        "ARTIFACT_S3_BUCKET": "artifacts",
        "ARTIFACT_S3_REGION": "us-east-1",
        "ARTIFACT_S3_USE_SSL": "true",
        "ARTIFACT_S3_ACCESS_KEY": "candidate-access-secret",
        "ARTIFACT_S3_SECRET_KEY": "candidate-secret-value",
    }
    before = _tree_inventory(tmp_path)

    gateway.validate_target(config, target, environment, managed_target=target)

    assert len(commands) == 1
    assert commands[0][-2:] == ("config", "--quiet")
    assert commands[0][commands[0].index("--env-file") + 1] == "/dev/null"
    assert "-" in commands[0]
    assert invocations[0]["environment"] == environment
    assert invocations[0]["input_text"] == _reverse_proxy_override(config)
    assert "candidate-access-secret" not in repr(commands)
    assert "candidate-secret-value" not in repr(commands)
    assert _tree_inventory(tmp_path) == before


@pytest.mark.parametrize(
    "public_host",
    (
        "https://flow.example.test",
        "flow.example.test/path",
        "flow.example.test:443",
        "flow.example.test\nnext",
        "bad_host.example",
        "-bad.example",
    ),
)
def test_configuration_validation_rejects_non_dns_non_ip_public_host(
    tmp_path: Path,
    public_host: str,
) -> None:
    service = _service(tmp_path)

    result = service.validate_config(DeploymentConfig(public_host=public_host))

    assert result.valid is False
    assert any("public_host" in issue for issue in result.issues)


def test_apply_artifacts_copies_provided_material_and_updates_tls_transitions(
    tmp_path: Path,
) -> None:
    source_cert = tmp_path / "operator/site.crt"
    source_key = tmp_path / "operator/site.key"
    _write_certificate_pair(source_cert, source_key, host="flow.example.test")
    gateway, _docker = _artifact_gateway(tmp_path)
    provided = DeploymentConfig(
        tls_mode="provided",
        public_host="flow.example.test",
        certificate_path="operator/site.crt",
        private_key_path="operator/site.key",
    )

    gateway.apply_artifacts(provided, operation_id="provided-apply")

    runtime_cert = tmp_path / ".ssl/cert.pem"
    runtime_key = tmp_path / ".ssl/key.pem"
    assert runtime_cert.read_bytes() == source_cert.read_bytes()
    assert runtime_key.read_bytes() == source_key.read_bytes()
    assert runtime_key.stat().st_mode & 0o777 == 0o600
    reverse = provided.model_copy(
        update={"tls_mode": "reverse_proxy", "reverse_proxy_backend_port": 19001}
    )
    gateway.apply_artifacts(reverse, operation_id="reverse-apply")
    override = tmp_path / ".flow-steward/installer.compose.override.yaml"
    assert "127.0.0.1:19001:8099" in override.read_text(encoding="utf-8")
    local = provided.model_copy(update={"tls_mode": "local", "public_host": "new.example.test"})
    gateway.apply_artifacts(local, operation_id="local-apply")
    assert not override.exists()
    certificate = x509.load_pem_x509_certificate(runtime_cert.read_bytes())
    san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "new.example.test" in san.get_values_for_type(x509.DNSName)


@pytest.mark.parametrize(
    ("tls_mode", "directory"),
    (("reverse_proxy", ".flow-steward"), ("local", ".ssl")),
)
def test_apply_artifacts_rejects_symlinked_destination_without_outside_write(
    tmp_path: Path,
    tls_mode: str,
    directory: str,
) -> None:
    project_root = tmp_path / "project"
    outside_root = tmp_path / "outside"
    project_root.mkdir()
    outside_root.mkdir()
    (project_root / directory).symlink_to(outside_root, target_is_directory=True)
    gateway, _docker = _artifact_gateway(project_root)
    config = DeploymentConfig(tls_mode=tls_mode)

    with pytest.raises(ValueError, match="real directory"):
        gateway.apply_artifacts(config, operation_id="symlink-rejected")

    assert not any(outside_root.iterdir())


@pytest.mark.parametrize("previous_mode", ("local", "provided"))
def test_activate_to_reverse_proxy_reconciles_prior_union_and_stops_old_traefik(
    tmp_path: Path,
    previous_mode: str,
) -> None:
    gateway, docker = _artifact_gateway(tmp_path)
    service = _service(tmp_path)
    previous = DeploymentConfig(tls_mode=previous_mode)
    candidate = DeploymentConfig(tls_mode="reverse_proxy", reverse_proxy_backend_port=18099)
    target = service.derive_target(candidate)
    snapshot = StackRollbackSnapshot(
        operation_id="transition-to-reverse",
        project_name=target.project_name,
        selected_services=("traefik", "fs-app"),
        services=(
            StackContainerRollbackState(
                service="traefik",
                topology="running",
                container_id="traefik-id",
                image_id=f"sha256:{1:064x}",
            ),
            StackContainerRollbackState(
                service="fs-app",
                topology="running",
                container_id="app-id",
                image_id=f"sha256:{2:064x}",
            ),
        ),
    )

    gateway.activate(
        previous,
        candidate,
        service.derive_target(previous),
        target,
        snapshot.model_dump(mode="json"),
    )

    assert docker.apply_calls[0][1]["managed_services"] == ("traefik", "fs-app")
    assert docker.apply_calls[0][1]["stop_before_reconcile"] == ()


def test_activate_stops_source_only_managed_services_before_target_reconcile(
    tmp_path: Path,
) -> None:
    gateway, docker = _artifact_gateway(tmp_path)
    service = _service(tmp_path)
    previous = DeploymentConfig(environment="development", file_storage="bundled")
    candidate = DeploymentConfig(environment="production", file_storage="external_s3")
    previous_target = service.derive_target(previous)
    target = service.derive_target(candidate)
    managed = tuple(dict.fromkeys((*previous_target.required_services, *target.required_services)))
    snapshot = StackRollbackSnapshot(
        operation_id="bundled-to-external",
        project_name=target.project_name,
        selected_services=managed,
        services=tuple(
            StackContainerRollbackState(
                service=service_name,
                topology="running",
                container_id=f"id-{service_name}",
                image_id=f"sha256:{index:064x}",
            )
            for index, service_name in enumerate(managed, start=1)
        ),
    )

    gateway.activate(
        previous,
        candidate,
        previous_target,
        target,
        snapshot.model_dump(mode="json"),
    )

    assert docker.stop_calls == []
    assert docker.project_stop_calls == [
        (
            previous_target.project_name,
            (
                "seaweedfs",
                "fs-frontend",
                "mailpit",
                "fs-test-runner",
                "fs-audit",
                "pg-audit",
                "rabbitmq-audit",
                "sonarqube",
                "sonar-scanner",
            ),
            60,
        )
    ]
    assert all(
        "fs-cli" not in services for _project, services, _timeout in docker.project_stop_calls
    )
    assert docker.apply_calls[0][1]["reconcile_services"] == (
        "traefik",
        "fs-mcp",
        "fs-app",
        "fs-extension-installer",
        "fs-worker",
        "control-plane",
    )
    assert docker.apply_calls[0][1]["no_dependencies"] is True
    assert docker.apply_calls[0][1]["remove_orphans"] is False


def test_activate_stages_runtime_state_before_scalable_to_compact_stop(
    tmp_path: Path,
) -> None:
    gateway, docker = _artifact_gateway(tmp_path)
    service = _service(tmp_path)
    previous = DeploymentConfig(environment="production", deployment_profile="scalable")
    candidate = DeploymentConfig(
        environment="production",
        deployment_profile="compact",
        file_storage="local_fs",
    )
    previous_target = service.derive_target(previous)
    target = service.derive_target(candidate)
    managed = tuple(dict.fromkeys((*previous_target.required_services, *target.required_services)))
    snapshot = StackRollbackSnapshot(
        operation_id="scalable-to-compact-state",
        project_name=target.project_name,
        selected_services=managed,
        services=tuple(
            StackContainerRollbackState(
                service=service_name,
                topology="running",
                container_id=f"id-{service_name}",
                image_id=f"sha256:{index:064x}",
            )
            for index, service_name in enumerate(managed, start=1)
        ),
    )

    gateway.activate(
        previous,
        candidate,
        previous_target,
        target,
        snapshot.model_dump(mode="json"),
    )

    assert docker.profile_state_stage_calls == [(previous_target, target, {})]
    assert docker.transition_events[:3] == [
        "stage_profile_runtime_state",
        "stop_source",
        "apply",
    ]


def test_compact_runtime_state_stage_script_is_valid_python() -> None:
    compile(_COMPACT_APP_READINESS_SCRIPT, "<compact-app-readiness>", "exec")
    compile(_COMPACT_RUNTIME_STATE_STAGE_SCRIPT, "<compact-runtime-state-stage>", "exec")
    compile(_COMPACT_RUNTIME_STATE_EXPORT_SCRIPT, "<compact-runtime-state-export>", "exec")


def test_compact_app_readiness_runs_inside_candidate_runtime(tmp_path: Path) -> None:
    commands: list[tuple[str, ...]] = []

    class RecordingProcess:
        def run(self, command: list[str], **_kwargs: object):
            commands.append(tuple(command))
            return SimpleNamespace(
                returncode=0,
                stdout='{"ready":false,"checks":{"security_posture":{"ok":false}}}\n',
                stderr="",
            )

    docker = DockerLifecycleGateway(project_root=tmp_path, process=RecordingProcess())
    target = _service(tmp_path).derive_target(
        DeploymentConfig(environment="production", deployment_profile="compact")
    )

    payload = docker.compact_app_readiness(target)

    assert payload == {
        "ready": False,
        "checks": {"security_posture": {"ok": False}},
    }
    assert commands[0][-6:-1] == ("exec", "-T", "compact-runtime", "python", "-c")
    assert '_runtime_health_payload("127.0.0.1", 8099)' in commands[0][-1]


def test_compact_runtime_state_export_uses_disposable_stopped_source_probe(
    tmp_path: Path,
) -> None:
    commands: list[tuple[str, ...]] = []
    invocations: list[dict[str, object]] = []

    class RecordingProcess:
        def run(self, command: list[str], **kwargs: object):
            commands.append(tuple(command))
            invocations.append(kwargs)
            payload = json.dumps({"maintenance_state": None, "worker_token": "worker-token"})
            progress = "[+] Creating 1/1\n ✔ Container compact-runtime-run Created\n"
            separate_stderr = kwargs.get("separate_stderr") is True
            return SimpleNamespace(
                returncode=0,
                stdout=payload if separate_stderr else progress + payload,
                stderr=progress if separate_stderr else "",
            )

    (tmp_path / ".env.prod").write_text(
        "FS_WORKER_MCP_BEARER_TOKEN=old-token\n",
        encoding="utf-8",
    )
    docker = DockerLifecycleGateway(project_root=tmp_path, process=RecordingProcess())
    service = _service(tmp_path)
    previous_target = service.derive_target(
        DeploymentConfig(environment="production", deployment_profile="compact")
    )
    target = service.derive_target(
        DeploymentConfig(environment="production", deployment_profile="scalable")
    )

    source_environment = {
        "FS_DEPLOYMENT_PROFILE": "compact",
        "FS_WORKER_PROCESSES": "2",
    }
    docker.stage_profile_runtime_state(
        previous_target,
        target,
        source_environment=source_environment,
    )

    assert len(commands) == 1
    assert commands[0][-9:-1] == (
        "run",
        "--rm",
        "--no-deps",
        "-T",
        "--entrypoint",
        "python",
        "compact-runtime",
        "-c",
    )
    assert commands[0][-1] == _COMPACT_RUNTIME_STATE_EXPORT_SCRIPT
    assert "docker exec" not in " ".join(commands[0])
    assert invocations[0]["environment"] == source_environment
    assert invocations[0]["separate_stderr"] is True


def test_docker_gateway_machine_output_keeps_progress_on_stderr(tmp_path: Path) -> None:
    docker = DockerLifecycleGateway(project_root=tmp_path)
    script = (
        "import json, sys; "
        "sys.stderr.write('[+] Creating compact export probe\\n'); "
        "sys.stdout.write(json.dumps({'maintenance_state': None, "
        "'worker_token': 'worker-token'}))"
    )

    outcome = docker._run(
        (sys.executable, "-c", script),
        action="machine_readable_probe",
        timeout_seconds=10,
        separate_stderr=True,
    )

    assert json.loads(outcome.stdout) == {
        "maintenance_state": None,
        "worker_token": "worker-token",
    }
    assert outcome.stderr == "[+] Creating compact export probe\n"


def test_activate_exports_compact_runtime_state_before_scalable_start(tmp_path: Path) -> None:
    gateway, docker = _artifact_gateway(tmp_path)
    service = _service(tmp_path)
    previous = DeploymentConfig(
        environment="production",
        deployment_profile="compact",
        file_storage="local_fs",
    )
    candidate = DeploymentConfig(environment="production", deployment_profile="scalable")
    previous_target = service.derive_target(previous)
    target = service.derive_target(candidate)
    managed = tuple(dict.fromkeys((*previous_target.required_services, *target.required_services)))
    snapshot = StackRollbackSnapshot(
        operation_id="compact-to-scalable-state",
        project_name=target.project_name,
        selected_services=managed,
        services=tuple(
            StackContainerRollbackState(
                service=service_name,
                topology="running",
                container_id=f"id-{service_name}",
                image_id=f"sha256:{index:064x}",
            )
            for index, service_name in enumerate(managed, start=1)
        ),
    )

    gateway.activate(
        previous,
        candidate,
        previous_target,
        target,
        snapshot.model_dump(mode="json"),
    )

    assert docker.profile_state_stage_calls == [(previous_target, target, {})]
    assert docker.transition_events[:3] == [
        "stage_profile_runtime_state",
        "stop_source",
        "apply",
    ]


def test_activate_production_to_development_starts_only_target_specific_services(
    tmp_path: Path,
) -> None:
    gateway, docker = _artifact_gateway(tmp_path)
    service = _service(tmp_path)
    previous = DeploymentConfig(environment="production", file_storage="external_s3")
    candidate = DeploymentConfig(environment="development", file_storage="bundled")
    previous_target = service.derive_target(previous)
    target = service.derive_target(candidate)
    managed = tuple(dict.fromkeys((*previous_target.required_services, *target.required_services)))
    snapshot = StackRollbackSnapshot(
        operation_id="external-to-bundled",
        project_name=target.project_name,
        selected_services=managed,
        services=tuple(
            StackContainerRollbackState(
                service=service_name,
                topology="running",
                container_id=f"id-{service_name}",
                image_id=f"sha256:{index:064x}",
            )
            for index, service_name in enumerate(managed, start=1)
        ),
    )

    gateway.activate(
        previous,
        candidate,
        previous_target,
        target,
        snapshot.model_dump(mode="json"),
    )

    assert docker.stop_calls == []
    assert docker.apply_calls[0][1]["reconcile_services"] == (
        "traefik",
        "fs-mcp",
        "fs-app",
        "fs-extension-installer",
        "fs-worker",
        "control-plane",
        "seaweedfs",
        "fs-frontend",
        "fs-test-runner",
        "fs-audit",
        "pg-audit",
        "rabbitmq-audit",
    )
    assert docker.apply_calls[0][1]["no_dependencies"] is True
    assert docker.apply_calls[0][1]["remove_orphans"] is False


def test_activate_from_reverse_proxy_stops_old_fs_app_port_holder_before_local_up(
    tmp_path: Path,
) -> None:
    gateway, docker = _artifact_gateway(tmp_path)
    service = _service(tmp_path)
    previous = DeploymentConfig(tls_mode="reverse_proxy", reverse_proxy_backend_port=8099)
    candidate = DeploymentConfig(tls_mode="local", public_port=8099)
    target = service.derive_target(candidate)
    snapshot = StackRollbackSnapshot(
        operation_id="transition-from-reverse",
        project_name=target.project_name,
        selected_services=("traefik", "fs-app"),
        services=(
            StackContainerRollbackState(
                service="traefik",
                topology="stopped",
                container_id="traefik-id",
                image_id=f"sha256:{1:064x}",
            ),
            StackContainerRollbackState(
                service="fs-app",
                topology="running",
                container_id="app-id",
                image_id=f"sha256:{2:064x}",
            ),
        ),
    )

    gateway.activate(
        previous,
        candidate,
        service.derive_target(previous),
        target,
        snapshot.model_dump(mode="json"),
    )

    assert docker.apply_calls[0][1]["managed_services"] == ("traefik", "fs-app")
    assert docker.apply_calls[0][1]["stop_before_reconcile"] == ("fs-app",)


def test_config_recovery_restores_exact_managed_bytes_modes_existence_and_runtime(
    tmp_path: Path,
) -> None:
    gateway, docker = _artifact_gateway(tmp_path)
    service = _service(tmp_path)
    paths = {
        tmp_path / ".flow-steward/install-state.json": (
            b'{"config":{"deployment_profile":"compact"}}\n',
            0o600,
        ),
        tmp_path / ".flow-steward/stack.toml": (b"# keep comment\nvalue = 1\n", 0o640),
        tmp_path / ".flow-steward/compose.env": (
            b"# unmanaged\nFS_COMPOSE_PROJECT_NAME=flow-steward\nCUSTOM=x\n",
            0o600,
        ),
        tmp_path / ".env": (b"# comment\nCUSTOM=y\n", 0o600),
        tmp_path / ".ssl/cert.pem": (b"old-cert\n", 0o644),
        tmp_path / ".ssl/key.pem": (b"old-key\n", 0o600),
    }
    for path, (content, mode) in paths.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(mode)
    override = tmp_path / ".flow-steward/installer.compose.override.yaml"
    assert not override.exists()
    previous_target = service.derive_target(DeploymentConfig())
    candidate_target = service.derive_target(
        DeploymentConfig(tls_mode="reverse_proxy", reverse_proxy_backend_port=19002)
    )

    snapshot = gateway.capture_recovery(
        previous_target,
        candidate_target,
        operation_id="exact-config-recovery",
    )
    for path in paths:
        path.write_bytes(b"changed\n")
        path.chmod(0o666)
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text("created", encoding="utf-8")

    restored = gateway.restore_recovery(
        previous_target,
        operation_id="exact-config-recovery",
        snapshot=snapshot,
    )

    assert restored is True
    for path, (content, mode) in paths.items():
        assert path.read_bytes() == content
        assert path.stat().st_mode & 0o777 == mode
    assert not override.exists()
    assert docker.restore_calls[0][1]["snapshot"] == StackRollbackSnapshot.model_validate(snapshot)
    selected, managed = docker.capture_calls[0]
    assert set(managed) == set(previous_target.required_services).union(
        candidate_target.required_services
    ) - {"fs-cli"}
    assert set(selected) == set(previous_target.required_services) - {
        "fs-cli",
        "pg-fs",
        "rabbitmq-fs",
        "seaweedfs",
    }


def test_config_recovery_normalizes_legacy_snapshot_before_restoring_retained_services(
    tmp_path: Path,
) -> None:
    gateway, docker = _artifact_gateway(tmp_path)
    service = _service(tmp_path)
    previous_target = service.derive_target(DeploymentConfig())
    candidate_target = service.derive_target(DeploymentConfig(environment="production"))
    snapshot = gateway.capture_recovery(
        previous_target,
        candidate_target,
        operation_id="legacy-retained-recovery",
    )
    runtime = StackRollbackSnapshot.model_validate(snapshot)
    legacy = runtime.model_copy(
        update={"selected_services": tuple(item.service for item in runtime.services)}
    )

    assert gateway.restore_recovery(
        previous_target,
        operation_id="legacy-retained-recovery",
        snapshot=legacy.model_dump(mode="json"),
    )

    restored = docker.restore_calls[0][1]["snapshot"]
    assert set(restored.selected_services) == set(previous_target.required_services) - {
        "fs-cli",
        "pg-fs",
        "rabbitmq-fs",
        "seaweedfs",
    }


def test_cross_profile_recovery_selects_only_services_defined_by_source_compose(
    tmp_path: Path,
) -> None:
    gateway, docker = _artifact_gateway(tmp_path)
    service = _service(tmp_path)
    common = {
        "environment": "production",
        "file_storage": "local_fs",
        "ingress_mode": "bundled_traefik",
        "tls_mode": "local",
    }
    compact_target = service.derive_target(DeploymentConfig(deployment_profile="compact", **common))
    scalable_target = service.derive_target(
        DeploymentConfig(deployment_profile="scalable", **common)
    )

    gateway.capture_recovery(
        compact_target,
        scalable_target,
        operation_id="cross-profile-recovery",
    )

    selected, managed = docker.capture_calls[0]
    assert set(managed) == set(compact_target.required_services).union(
        scalable_target.required_services
    ) - {"fs-cli"}
    assert set(selected) == set(compact_target.required_services) - {
        "fs-cli",
        "pg-fs",
        "rabbitmq-fs",
    }


class _RevisionStore(LifecycleConfigurationStore):
    def __init__(self, project_root: Path, *, stale_on_verify: bool = False) -> None:
        super().__init__(project_root)
        self.stale_on_verify = stale_on_verify
        self.preserve_replacement_on_clear = False

    def pending_revision_matches(self, revision: str) -> bool:
        return not self.stale_on_verify and super().pending_revision_matches(revision)

    def clear_pending_if_revision(self, revision: str) -> bool:
        if self.preserve_replacement_on_clear:
            self.write_pending(ConfigurationPatch(public_port=9443))
        return super().clear_pending_if_revision(revision)


def test_config_set_uses_shared_mutation_lock(tmp_path: Path) -> None:
    state = LifecycleStateStore(tmp_path)
    service = ConfigurationService(
        store=LifecycleConfigurationStore(tmp_path),
        lifecycle_state=state,
    )
    service.store.write_active(DeploymentConfig())

    _raises_input_3245_1 = ConfigSetRequest(public_port=443)
    with (
        state.operation_lock("active-apply", mutating=True),
        pytest.raises(LifecycleError) as raised,
    ):
        service.set(_raises_input_3245_1)

    assert raised.value.code == "lifecycle_operation_active"
    assert service.store.read_pending() is None


def test_apply_rejects_changed_pending_revision_under_runner_lock_before_effects(
    tmp_path: Path,
) -> None:
    store = _RevisionStore(tmp_path, stale_on_verify=True)
    gateway = FakeConfigurationGateway()
    service = ConfigurationService(
        store=store,
        gateway=gateway,
        lifecycle_state=LifecycleStateStore(tmp_path),
    )
    store.compose_env_path.parent.mkdir(parents=True, exist_ok=True)
    store.compose_env_path.write_text("FS_COMPOSE_PROJECT_NAME=flow-steward\n", encoding="utf-8")
    store.write_active(DeploymentConfig())
    _write_lifecycle_secret_environment(tmp_path / ".env")
    store.write_pending(ConfigurationPatch(public_port=443))

    _raises_input_3271_1 = ConfigApplyRequest()
    with pytest.raises(LifecycleError) as raised:
        service.apply(_raises_input_3271_1)

    assert raised.value.code == "pending_configuration_changed"
    assert gateway.calls == []
    assert store.read_active().public_port == 8099


def test_apply_never_deletes_a_newer_pending_revision_at_commit(tmp_path: Path) -> None:
    store = _RevisionStore(tmp_path)
    gateway = FakeConfigurationGateway()
    service = ConfigurationService(
        store=store,
        gateway=gateway,
        lifecycle_state=LifecycleStateStore(tmp_path),
    )
    store.compose_env_path.parent.mkdir(parents=True, exist_ok=True)
    store.compose_env_path.write_text("FS_COMPOSE_PROJECT_NAME=flow-steward\n", encoding="utf-8")
    store.write_active(DeploymentConfig())
    _write_lifecycle_secret_environment(tmp_path / ".env")
    store.write_pending(ConfigurationPatch(public_port=443))
    store.preserve_replacement_on_clear = True

    service.apply(ConfigApplyRequest())

    assert store.read_active().public_port == 443
    assert store.read_pending() == ConfigurationPatch(public_port=9443)


def test_configuration_gateway_names_the_blocking_finding_and_its_detail(
    tmp_path: Path,
) -> None:
    """AC17-R148: the operator is told which check blocked the candidate, and why."""
    report = ReadinessReport(
        status="blocked",
        findings=(
            ReadinessFinding(
                check_id="auth_defaults",
                area=ReadinessArea.CORE,
                title="Selected-stack auth defaults are valid",
                status=ReadinessStatus.FAILED,
                detail="FS_AUTH_COOKIE_SECURE should be 1",
            ),
        ),
        core_failed=1,
        process_exit_code=4,
    )
    gateway = ConfigurationLifecycleGateway(
        project_root=tmp_path,
        docker=_ArtifactDocker(tmp_path),  # type: ignore[arg-type]
        preservation=SimpleNamespace(),  # type: ignore[arg-type]
        manifest_store=PreservationManifestStore(tmp_path),
        readiness_service=SimpleNamespace(check=lambda: None),
        release_provider=lambda: "test",
        timeout_seconds=10,
        candidate_readiness=lambda _config: report,
        readiness_clock=lambda: 0.0,
        readiness_sleep=lambda _seconds: None,
    )

    config = DeploymentConfig()
    target = _service(tmp_path).derive_target(DeploymentConfig())
    with pytest.raises(StackGatewayError) as raised:
        gateway.healthy(config, target)

    assert raised.value.action == "candidate_readiness"
    assert raised.value.detail == (
        "Candidate readiness failed: auth_defaults (FS_AUTH_COOKIE_SECURE should be 1)."
    )


def test_apply_error_carries_the_reason_the_target_was_not_healthy(tmp_path: Path) -> None:
    class BlockedCandidateGateway(FakeConfigurationGateway):
        def healthy(self, config, target) -> bool:
            if self._activation_count == 1:
                raise StackGatewayError(
                    action="storage_readiness",
                    detail="Candidate storage probe failed: local artifact root is unavailable",
                )
            return super().healthy(config, target)

    gateway = BlockedCandidateGateway(project_root=tmp_path)
    service = _service(tmp_path, gateway=gateway)
    service.store.write_active(DeploymentConfig(public_host="old.example.test"))
    _write_lifecycle_secret_environment(tmp_path / ".env")
    service.set(ConfigurationPatch(public_host="new.example.test"))

    request = ConfigApplyRequest()
    with pytest.raises(LifecycleError) as raised:
        service.apply(request)

    assert raised.value.code == "configuration_apply_failed"
    assert raised.value.cause == (
        "The target configuration failed; the previous runtime was restored. Primary failure: "
        "storage_readiness: Candidate storage probe failed: local artifact root is unavailable"
    )


def test_a_failed_candidate_leaves_its_own_log_in_the_operation_log(tmp_path: Path) -> None:
    """AC17-R152: the rollback removes the candidate, and with it the only record of why.

    The auditor found the PermissionError that decided the apply only because a watcher
    was running beside it; the operation log held one line.
    """
    gateway = FakeConfigurationGateway(fail_target_health=True, project_root=tmp_path)
    service = _service(tmp_path, gateway=gateway)
    service.store.write_active(DeploymentConfig(public_host="old.example.test"))
    _write_lifecycle_secret_environment(tmp_path / ".env")
    service.set(ConfigurationPatch(public_host="new.example.test"))

    request = ConfigApplyRequest()
    with pytest.raises(LifecycleError) as raised:
        service.apply(request)

    assert raised.value.code == "configuration_apply_failed"
    # Taken from the candidate, before the rollback replaced it.
    assert gateway.calls.index("diagnostics:new.example.test") < gateway.calls.index(
        "recovery:restore"
    )
    assert raised.value.log_path is not None
    operation_log = Path(raised.value.log_path).read_text(encoding="utf-8")
    assert "candidate says: PermissionError" in operation_log


def test_evidence_that_cannot_be_read_does_not_replace_the_failure(tmp_path: Path) -> None:
    class UnreadableLogGateway(FakeConfigurationGateway):
        def candidate_diagnostics(self, config, _target, output_sink) -> None:
            raise RuntimeError("docker logs failed")

    gateway = UnreadableLogGateway(fail_target_health=True, project_root=tmp_path)
    service = _service(tmp_path, gateway=gateway)
    service.store.write_active(DeploymentConfig(public_host="old.example.test"))
    _write_lifecycle_secret_environment(tmp_path / ".env")
    service.set(ConfigurationPatch(public_host="new.example.test"))

    request = ConfigApplyRequest()
    with pytest.raises(LifecycleError) as raised:
        service.apply(request)

    assert raised.value.code == "configuration_apply_failed"
    operation_log = Path(raised.value.log_path or "").read_text(encoding="utf-8")
    assert "Candidate log could not be read: RuntimeError" in operation_log


def test_the_candidate_log_is_read_from_the_primary_service_and_redacted(tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    def logs(target, *, service, tail, follow, output_sink) -> int:
        seen.update(service=service, tail=tail, follow=follow)
        output_sink("fs-app | connecting to postgresql://flow:hunter2secret@pg-fs/flow")
        return 1

    docker = _ArtifactDocker(tmp_path)
    docker.logs = logs  # type: ignore[attr-defined]
    gateway = ConfigurationLifecycleGateway(
        project_root=tmp_path,
        docker=docker,  # type: ignore[arg-type]
        preservation=SimpleNamespace(),  # type: ignore[arg-type]
        manifest_store=PreservationManifestStore(tmp_path),
        readiness_service=SimpleNamespace(check=lambda: None),
        release_provider=lambda: "test",
    )
    written: list[str] = []
    config = DeploymentConfig(deployment_profile="scalable")

    gateway.candidate_diagnostics(config, _service(tmp_path).derive_target(config), written.append)

    assert seen == {"service": "fs-app", "tail": 40, "follow": False}
    assert written[0].startswith("Candidate fs-app log before rollback")
    assert "hunter2secret" not in written[1]
    assert "postgresql://flow:" in written[1]


def _gateway_running_workers(tmp_path: Path, replicas: int) -> FakeConfigurationGateway:
    class RunningWorkers(FakeConfigurationGateway):
        def capture_recovery(self, previous_target, candidate_target, *, operation_id):
            snapshot = super().capture_recovery(
                previous_target, candidate_target, operation_id=operation_id
            )
            snapshot["services"] = [
                {
                    "service": "fs-worker",
                    "topology": "running",
                    "container_id": "w1",
                    "image_id": "sha256:" + "b" * 64,
                    "replicas": replicas,
                }
                if item["service"] == "fs-worker"
                else item
                for item in snapshot["services"]
            ]
            return snapshot

    return RunningWorkers(project_root=tmp_path)


@pytest.mark.parametrize(
    ("running", "patch", "refused"),
    (
        (3, ConfigurationPatch(public_host="new.example.test"), True),
        (3, ConfigurationPatch(worker_replicas=3), False),
        (1, ConfigurationPatch(public_host="new.example.test"), False),
    ),
    ids=("unrecorded-scale", "recording-it", "as-recorded"),
)
def test_apply_refuses_to_change_a_worker_count_nobody_recorded(
    tmp_path: Path, running: int, patch: ConfigurationPatch, refused: bool
) -> None:
    """Review P2: a bare `--scale` was silently undone by the next unrelated apply."""
    gateway = _gateway_running_workers(tmp_path, running)
    service = _service(tmp_path, gateway=gateway)
    service.store.write_active(DeploymentConfig(public_host="old.example.test"))
    _write_lifecycle_secret_environment(tmp_path / ".env")
    service.set(patch)

    if refused:
        request = ConfigApplyRequest()
        with pytest.raises(LifecycleError) as raised:
            service.apply(request)
        assert raised.value.code == "worker_replicas_unrecorded"
        assert "--worker-replicas 3" in raised.value.remediation
        assert not any(call.startswith("activate") for call in gateway.calls)
    else:
        service.apply(ConfigApplyRequest())
        assert any(call.startswith("activate") for call in gateway.calls)


@pytest.mark.parametrize(
    "patch",
    (
        ConfigurationPatch(proxy_trusted_ips=("10.0.0.0/8",)),
        ConfigurationPatch(worker_replicas=3),
        ConfigurationPatch(public_host="new.example.test"),
    ),
    ids=("tuple-field", "integer-field", "string-field"),
)
def test_config_show_reports_every_pending_change_as_json(tmp_path: Path, patch) -> None:
    """W3 (auditor): `config show` crashed while a proxy_trusted_ips change was pending.

    The pending side of the diff was JSON-dumped and the active side was the raw
    attribute, a tuple, which is not a JSON value -- so the operator could not see their
    own pending change until it had been applied.
    """
    service = _service(tmp_path)
    service.store.write_active(DeploymentConfig(public_host="old.example.test"))
    service.set(patch)

    shown = service.show()

    field_name = next(iter(patch.model_dump(exclude_none=True)))
    assert field_name in shown.pending_diff
    assert json.loads(shown.model_dump_json())["pending_diff"][field_name]["pending"] is not None
