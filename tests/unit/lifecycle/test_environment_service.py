from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from core.application.lifecycle.backup_service import BackupResult, BackupVerificationStatus
from core.application.lifecycle.configuration_service import ConfigurationService
from core.application.lifecycle.contracts import (
    CancellationToken,
    ComposeTarget,
    DeploymentConfig,
    LifecycleOperationJournal,
)
from core.application.lifecycle.environment_service import (
    EnvironmentService,
    EnvironmentShowRequest,
    EnvironmentSwitchRequest,
    StorageInventory,
)
from core.application.lifecycle.errors import LifecycleError
from core.application.lifecycle.stack_service import StackGatewayError
from core.application.management.command_specs.contracts import OperationStepStatus
from core.infrastructure.lifecycle.configuration_store import LifecycleConfigurationStore
from core.infrastructure.lifecycle.state_store import LifecycleStateStore
from core.infrastructure.lifecycle.storage_gateway import LifecycleStorageGateway


def _target(config: DeploymentConfig) -> ComposeTarget:
    required = ["pg-fs", "fs-app", "fs-worker", "control-plane"]
    profiles = ["cli"]
    if config.file_storage == "bundled":
        required.append("seaweedfs")
        profiles.insert(0, "storage-seaweedfs")
    required.append("fs-cli")
    runtime = list(required)
    if config.environment == "development":
        profiles.extend(("development", "test", "audit", "quality"))
        required.extend(
            (
                "fs-frontend",
                "mailpit",
                "fs-test-runner",
                "fs-audit",
                "pg-audit",
                "rabbitmq-audit",
                "sonarqube",
                "sonar-scanner",
            )
        )
        runtime.extend(("fs-frontend", "fs-test-runner", "fs-audit", "pg-audit", "rabbitmq-audit"))
    return ComposeTarget(
        stack_environment=config.environment,
        file_storage=config.file_storage,
        env_file=".env" if config.environment == "development" else ".env.prod",
        compose_files=(
            "infra/docker/docker-compose.yaml"
            if config.environment == "development"
            else "infra/docker/docker-compose.prod.yaml",
        ),
        compose_profiles=tuple(profiles),
        required_services=tuple(required),
        runtime_services=tuple(runtime),
        project_name="installed-project",
    )


@dataclass
class _Configuration:
    active: DeploymentConfig
    calls: list[str]
    staged: bool = False

    def read_active(self) -> DeploymentConfig:
        self.calls.append("inspect")
        return self.active

    def derive_target(self, config: DeploymentConfig) -> ComposeTarget:
        self.calls.append(f"derive:{config.environment}")
        return _target(config)

    def prepare_transition_environment(
        self,
        active: DeploymentConfig,
        config: DeploymentConfig,
        target_overlay: dict[str, str] | None = None,
    ) -> tuple[ComposeTarget, Path, dict[str, str]]:
        del active
        self.calls.append("prepare")
        environment = {
            "ARTIFACT_S3_ENDPOINT_URL": config.s3_endpoint,
            "ARTIFACT_S3_BUCKET": config.s3_bucket,
            "ARTIFACT_S3_REGION": config.s3_region,
            "ARTIFACT_S3_USE_SSL": (
                "true" if config.s3_endpoint.startswith("https://") else "false"
            ),
        }
        environment.update(target_overlay or {})
        return _target(config), Path(_target(config).env_file), environment

    def stage_transition_environment(
        self,
        active: DeploymentConfig,
        config: DeploymentConfig,
        prepared: tuple[ComposeTarget, Path, dict[str, str]],
    ) -> Path:
        del active, config, prepared
        self.calls.append("render")
        self.staged = True
        return Path(".env.prod")

    def transition_environment_matches(
        self,
        active: DeploymentConfig,
        config: DeploymentConfig,
        prepared: tuple[ComposeTarget, Path, dict[str, str]],
    ) -> bool:
        del active, config, prepared
        return self.staged

    def mark_active(
        self,
        previous: DeploymentConfig,
        config: DeploymentConfig,
        prepared: tuple[ComposeTarget, Path, dict[str, str]],
    ) -> None:
        del previous, prepared
        self.calls.append("mark_active")
        self.active = config

    def active_publication_matches(
        self,
        config: DeploymentConfig,
        prepared: tuple[ComposeTarget, Path, dict[str, str]],
    ) -> bool:
        del prepared
        return self.active == config


@dataclass
class _Jobs:
    calls: list[str]
    owner_operation_id: str | None = None

    def reconcile_pause(self, unfinished_operation_ids: tuple[str, ...]) -> object | None:
        if len(unfinished_operation_ids) > 1:
            raise LifecycleError.recovery_required(
                code="environment_admission_pause_ambiguous",
                cause="ambiguous",
                remediation="reconcile",
                step="recovery",
                recovery_command="./flow-steward environment show",
            )
        if self.owner_operation_id and self.owner_operation_id not in unfinished_operation_ids:
            self.owner_operation_id = None
        return self.owner_operation_id

    def stop_accepting(self, operation_id: str) -> object:
        self.calls.append("stop_accepting")
        if self.owner_operation_id not in {None, operation_id}:
            raise RuntimeError("different owner")
        self.owner_operation_id = operation_id
        return object()

    def pause_owned(self, operation_id: str) -> bool:
        return self.owner_operation_id == operation_id

    def wait_for_idle(self, *, cancellation: object, heartbeat: object) -> None:
        del cancellation, heartbeat
        self.calls.append("wait_for_idle")

    def resume_accepting(self, operation_id: str) -> bool:
        if self.owner_operation_id != operation_id:
            return False
        self.calls.append("resume_accepting")
        self.owner_operation_id = None
        return True


@dataclass
class _Backup:
    calls: list[str]
    fail: bool = False
    results: dict[str, BackupResult] = field(default_factory=dict)

    def create(self, request: object, **_kwargs: object) -> BackupResult:
        operation_id = str(getattr(request, "operation_id", "") or "")
        if operation_id in self.results:
            return self.results[operation_id]
        self.calls.append("backup")
        if self.fail:
            raise LifecycleError.dependency(
                code="backup_failed",
                cause="Backup verification failed.",
                remediation="Repair backup configuration.",
                step="backup",
            )
        result = BackupResult(
            path="backups/verified.dump.enc",
            size_bytes=12,
            created_at="2026-07-25T00:00:00Z",
            verification_status=BackupVerificationStatus.VERIFIED,
            verified_at="2026-07-25T00:00:01Z",
        )
        if operation_id:
            self.results[operation_id] = result
        return result

    def find_verified(self, operation_id: str) -> BackupResult | None:
        return self.results.get(operation_id)


@dataclass
class _Storage:
    calls: list[str]
    credentials_installed: bool = False
    credentials_provisioned: bool = False
    inventory_result: StorageInventory = field(
        default_factory=lambda: StorageInventory(
            backend="bundled",
            approximate_count=0,
            approximate_size_bytes=0,
            source_identity="volume:installed-project_seaweedfs",
            completeness="complete",
        )
    )

    def target_identity(self, config: DeploymentConfig) -> str:
        return (
            "s3:"
            f"{config.s3_endpoint.lower().rstrip('/')}|{config.s3_bucket}|"
            f"{config.s3_region.lower()}|{config.s3_endpoint.lower().startswith('https://')}"
            if config.file_storage == "external_s3"
            else "volume:installed-project_seaweedfs"
        )

    def candidate_environment(
        self,
        config: DeploymentConfig,
        environment: dict[str, str],
    ) -> dict[str, str]:
        del config
        self.calls.append("credentials")
        return dict(environment)

    def inventory(self, config: DeploymentConfig) -> StorageInventory:
        del config
        self.calls.append("inventory")
        return self.inventory_result

    def probe_write(self, config: DeploymentConfig, environment: dict[str, str]) -> None:
        del config, environment
        self.calls.append("probe")

    def install_credentials(self, config: DeploymentConfig, path: Path) -> None:
        del config, path
        self.calls.append("install_credentials")
        self.credentials_installed = True

    def credentials_match(self, config: DeploymentConfig, path: Path) -> bool:
        del config, path
        return self.credentials_installed

    def credential_evidence(self, config: DeploymentConfig, path: Path) -> dict[str, object]:
        del config, path
        return {
            "present_keys": ["ARTIFACT_S3_ACCESS_KEY", "ARTIFACT_S3_SECRET_KEY"],
            "sha256": "nonsecret-credential-proof",
        }

    def provision_credentials(self, config: DeploymentConfig) -> dict[str, object]:
        del config
        self.calls.append("provision_credentials")
        self.credentials_provisioned = True
        return {"identity": "flow-steward-core", "present": True, "sha256": "proof"}

    def provisioning_matches(self, config: DeploymentConfig) -> bool:
        return (
            self.credentials_provisioned
            if config.environment == "production" and config.file_storage == "bundled"
            else True
        )


@dataclass
class _Runtime:
    calls: list[str]
    fail_target_health: bool = False
    fail_restore: bool = False
    release: str = "4.8.2"
    target_release: str | None = None
    release_reads: int = 0
    manifests: dict[str, dict[str, object]] = field(default_factory=dict)
    artifacts_applied: bool = False
    target_active: bool = False
    base_failure_step: str = ""
    invalid_preservation_baseline: bool = False
    current_environment: str = "development"
    configuration: _Configuration | None = None
    activated_previous_targets: list[ComposeTarget] = field(default_factory=list)
    activated_targets: list[ComposeTarget] = field(default_factory=list)
    restored_targets: list[ComposeTarget] = field(default_factory=list)

    def prepare_target_images(
        self,
        config: DeploymentConfig,
        target: ComposeTarget,
        environment: dict[str, str],
    ) -> None:
        del config, target, environment
        self.calls.append("prepare_images")
        if self.base_failure_step == "prepare_images":
            raise RuntimeError("target image preparation failed")

    def validate_target(
        self,
        config: DeploymentConfig,
        target: ComposeTarget,
        environment: dict[str, str],
        *,
        managed_target: ComposeTarget,
    ) -> None:
        del config, target, environment, managed_target
        self.calls.append("validate")
        if self.base_failure_step == "validate":
            raise SystemExit("crash during validate")

    def capture_recovery(
        self,
        previous_target: ComposeTarget,
        candidate_target: ComposeTarget,
        *,
        operation_id: str,
    ) -> dict[str, object]:
        del previous_target, candidate_target, operation_id
        self.calls.append("snapshot")
        return {"runtime": "exact"}

    def recovery_available(self, operation_id: str) -> bool:
        del operation_id
        return True

    def capture_manifest(
        self,
        config: DeploymentConfig,
        target: ComposeTarget,
        *,
        operation_id: str,
        stage: str,
    ) -> dict[str, object]:
        del target, operation_id
        self.calls.append(f"manifest:{stage}")
        manifest = {
            "release": self.release,
            "environment": config.environment,
            "database": "unchanged",
        }
        self.manifests[stage] = manifest
        return manifest

    def load_manifest(self, operation_id: str, stage: str) -> dict[str, object] | None:
        del operation_id
        return self.manifests.get(stage)

    def validate_preservation_baseline(self, manifest: dict[str, object]) -> None:
        del manifest
        self.calls.append("validate_baseline")
        if self.invalid_preservation_baseline:
            raise ValueError("required preservation evidence is unknown")

    def apply_artifacts(self, config: DeploymentConfig, *, operation_id: str) -> None:
        del config, operation_id
        self.calls.append("artifacts")
        self.artifacts_applied = True

    def artifacts_match(self, config: DeploymentConfig) -> bool:
        del config
        return self.artifacts_applied

    def activate(
        self,
        previous: DeploymentConfig,
        config: DeploymentConfig,
        previous_target: ComposeTarget,
        target: ComposeTarget,
        runtime_snapshot: dict[str, object],
    ) -> None:
        del previous, runtime_snapshot
        self.calls.append("activate")
        self.activated_previous_targets.append(previous_target)
        self.activated_targets.append(target)
        self.target_active = True
        self.current_environment = config.environment

    def healthy(self, config: DeploymentConfig, target: ComposeTarget) -> bool:
        del target
        self.calls.append(f"health:{config.environment}")
        if config.environment != self.current_environment:
            return False
        return (
            not self.fail_target_health
            if len([x for x in self.calls if x.startswith("health:")]) == 1
            else not self.fail_restore
        )

    def runtime_release(self, config: DeploymentConfig, target: ComposeTarget) -> str:
        del config, target
        self.release_reads += 1
        if self.release_reads > 1 and self.target_release is not None:
            return self.target_release
        return self.release

    def compare_environment_manifests(
        self,
        before: dict[str, object],
        after: dict[str, object],
    ) -> None:
        self.calls.append("compare")
        assert before["release"] == after["release"] == self.release
        assert before["database"] == after["database"] == "unchanged"

    def restore_recovery(
        self,
        target: ComposeTarget,
        *,
        operation_id: str,
        snapshot: dict[str, object],
    ) -> bool:
        del operation_id, snapshot
        self.calls.append("restore")
        self.restored_targets.append(target)
        self.target_active = False
        self.current_environment = target.stack_environment
        if self.configuration is not None:
            self.configuration.active = self.configuration.active.model_copy(
                update={"environment": target.stack_environment}
            )
        return not self.fail_restore


class _CrashAfterEffectStateStore(LifecycleStateStore):
    def __init__(self, project_root: Path, step_id: str) -> None:
        super().__init__(project_root)
        self.step_id = step_id
        self.crashed = False

    def save_journal(self, journal: LifecycleOperationJournal) -> None:
        matching = next(
            (step for step in journal.steps if step.step_id == self.step_id),
            None,
        )
        if (
            not self.crashed
            and matching is not None
            and matching.status is OperationStepStatus.SUCCEEDED
        ):
            self.crashed = True
            raise SystemExit(f"crash after {self.step_id}")
        super().save_journal(journal)


class _CrashAfterJournalStateStore(LifecycleStateStore):
    def __init__(self, project_root: Path, step_id: str) -> None:
        super().__init__(project_root)
        self.step_id = step_id
        self.crashed = False

    def save_journal(self, journal: LifecycleOperationJournal) -> None:
        matching = next(
            (step for step in journal.steps if step.step_id == self.step_id),
            None,
        )
        super().save_journal(journal)
        if (
            not self.crashed
            and matching is not None
            and matching.status is OperationStepStatus.SUCCEEDED
        ):
            self.crashed = True
            raise SystemExit(f"crash after journal {self.step_id}")


def _service(
    tmp_path: Path,
    *,
    active: DeploymentConfig | None = None,
    backup_fail: bool = False,
    fail_target_health: bool = False,
    fail_restore: bool = False,
    inventory: StorageInventory | None = None,
    lifecycle_state: LifecycleStateStore | None = None,
    base_failure_step: str = "",
) -> tuple[EnvironmentService, list[str], _Runtime]:
    calls: list[str] = []
    active_config = active or DeploymentConfig(environment="development")
    configuration = _Configuration(
        active=active_config,
        calls=calls,
    )
    runtime = _Runtime(
        calls=calls,
        fail_target_health=fail_target_health,
        fail_restore=fail_restore,
        base_failure_step=base_failure_step,
        current_environment=active_config.environment,
        configuration=configuration,
    )
    service = EnvironmentService(
        configuration=configuration,
        jobs=_Jobs(calls),
        backup=_Backup(calls, fail=backup_fail),
        storage=_Storage(
            calls,
            inventory_result=inventory
            or StorageInventory(
                backend="bundled",
                approximate_count=0,
                approximate_size_bytes=0,
                source_identity="volume:installed-project_seaweedfs",
                completeness="complete",
            ),
        ),
        runtime=runtime,
        lifecycle_state=lifecycle_state or LifecycleStateStore(tmp_path),
        release_provider=lambda: runtime.release,
    )
    return service, calls, runtime


def test_show_and_same_target_are_read_only_noops_before_lock_or_journal(tmp_path: Path) -> None:
    service, calls, _runtime = _service(tmp_path)

    shown = service.show(EnvironmentShowRequest())
    result = service.switch(EnvironmentSwitchRequest(target="development"))

    assert shown.active_environment == "development"
    assert result.status == "already_active"
    assert result.operation_id is None
    assert calls == ["inspect", "derive:development", "inspect", "derive:development"]
    assert not (tmp_path / ".flow-steward").exists()


@pytest.mark.parametrize(
    ("source", "target"),
    [("development", "production"), ("production", "development")],
)
def test_switch_orders_gates_and_preserves_release_both_directions(
    tmp_path: Path,
    source: str,
    target: str,
) -> None:
    service, calls, runtime = _service(
        tmp_path,
        active=DeploymentConfig(environment=source),
    )

    result = service.switch(EnvironmentSwitchRequest(target=target))

    assert result.status == "active"
    assert result.source_release == result.target_release == runtime.release
    ordered = [
        "inspect",
        f"derive:{source}",
        "prepare",
        "prepare_images",
        "stop_accepting",
        "wait_for_idle",
        "snapshot",
        "manifest:before",
        "validate_baseline",
        "backup",
        "render",
        "validate",
        "artifacts",
        "activate",
        f"health:{target}",
        "manifest:after",
        "compare",
        "mark_active",
        "resume_accepting",
    ]
    if target == "production":
        render_index = ordered.index("render")
        ordered[render_index:render_index] = [
            "provision_credentials",
            "credentials",
            "prepare",
        ]
        ordered.insert(ordered.index("validate"), "install_credentials")
    assert calls == ordered
    assert calls.index("backup") < calls.index("render") < calls.index("activate")


def test_switch_rejects_incomplete_preservation_before_mutation(tmp_path: Path) -> None:
    service, calls, runtime = _service(tmp_path)
    runtime.invalid_preservation_baseline = True

    _raises_input_599_1 = EnvironmentSwitchRequest(target="production")
    with pytest.raises(LifecycleError) as raised:
        service.switch(_raises_input_599_1)

    assert raised.value.code == "environment_validate_preservation_baseline_failed"
    assert "validate_baseline" in calls
    assert "backup" not in calls
    assert "provision_credentials" not in calls
    assert "render" not in calls
    assert "activate" not in calls
    assert "restore" not in calls
    assert calls[-1] == "resume_accepting"


def test_target_image_preparation_failure_leaves_source_running_without_rollback(
    tmp_path: Path,
) -> None:
    service, calls, _runtime = _service(tmp_path, base_failure_step="prepare_images")

    _raises_input_617_1 = EnvironmentSwitchRequest(target="production")
    with pytest.raises(LifecycleError) as raised:
        service.switch(_raises_input_617_1)

    assert raised.value.code == "environment_prepare_target_images_failed"
    assert "prepare_images" in calls
    assert "stop_accepting" not in calls
    assert "backup" not in calls
    assert "restore" not in calls


def test_target_image_preparation_reports_redacted_docker_adapter_detail(
    tmp_path: Path,
) -> None:
    service, calls, runtime = _service(tmp_path)

    def fail_prepare(*_args: object, **_kwargs: object) -> None:
        calls.append("prepare_images")
        raise StackGatewayError(
            action="target_image_pull",
            detail="Docker target image pull failed for an unavailable immutable image.",
        )

    runtime.prepare_target_images = fail_prepare  # type: ignore[method-assign]

    _raises_input_641_1 = EnvironmentSwitchRequest(target="production")
    with pytest.raises(LifecycleError) as raised:
        service.switch(_raises_input_641_1)

    assert raised.value.code == "environment_prepare_target_images_failed"
    assert raised.value.cause == (
        "Docker target image pull failed for an unavailable immutable image."
    )
    assert "stop_accepting" not in calls


class _RenderedTargetRuntime(_Runtime):
    def __init__(self, *args: object, project_root: Path, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.project_root = project_root

    def validate_target(
        self,
        config: DeploymentConfig,
        target: ComposeTarget,
        environment: dict[str, str],
        *,
        managed_target: ComposeTarget,
    ) -> None:
        target_path = self.project_root / target.env_file
        if not target_path.is_file():
            raise ValueError("target environment was not rendered before Compose validation")
        super().validate_target(
            config,
            target,
            environment,
            managed_target=managed_target,
        )


def _clean_real_switch_service(
    tmp_path: Path,
    *,
    source: str,
    file_storage: str = "bundled",
) -> tuple[EnvironmentService, list[str], LifecycleStorageGateway]:
    calls: list[str] = []
    config_values: dict[str, object] = {
        "environment": source,
        "file_storage": file_storage,
    }
    if file_storage == "external_s3":
        config_values.update(
            {
                "s3_endpoint": "https://objects.example.test",
                "s3_bucket": "artifacts",
                "s3_region": "eu-test-1",
                "s3_credentials_path": str(tmp_path / "removed-installer-input.json"),
            }
        )
    active = DeploymentConfig.model_validate(config_values)
    store = LifecycleConfigurationStore(tmp_path)
    store.state_root.mkdir(parents=True, exist_ok=True)
    store.write_active(active)
    store.compose_env_path.write_text(
        "FS_COMPOSE_PROJECT_NAME=installed-project\n",
        encoding="utf-8",
    )
    source_path = store.env_path(active)
    source_path.write_text(
        "\n".join(
            (
                "FS_COMPOSE_PROJECT_NAME=installed-project",
                "FS_SECRET_KEY=durable-source-secret",
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
    source_path.chmod(0o600)
    secrets = iter(("bundled-access", "bundled-secret"))
    storage = LifecycleStorageGateway(
        project_root=tmp_path,
        compose_project_provider=lambda _config: "installed-project",
        secret_factory=lambda: next(secrets),
    )
    if source == "production":
        storage.provision_credentials(active)
        calls.clear()
    runtime = _RenderedTargetRuntime(
        calls=calls,
        current_environment=source,
        project_root=tmp_path,
    )
    service = EnvironmentService(
        configuration=ConfigurationService(store=store),
        jobs=_Jobs(calls),
        backup=_Backup(calls),
        storage=storage,
        runtime=runtime,
        lifecycle_state=LifecycleStateStore(tmp_path),
        release_provider=lambda: runtime.release,
    )
    return service, calls, storage


@pytest.mark.parametrize(
    ("source", "target", "missing_name"),
    (("development", "production", ".env.prod"), ("production", "development", ".env")),
)
def test_clean_bundled_switch_renders_absent_opposite_environment_before_compose(
    tmp_path: Path,
    source: str,
    target: str,
    missing_name: str,
) -> None:
    service, calls, _storage = _clean_real_switch_service(tmp_path, source=source)
    opposite = tmp_path / missing_name
    assert not opposite.exists()
    if source == "development":
        assert not (tmp_path / ".flow-steward/runtime/seaweedfs-s3.json").exists()

    result = service.switch(EnvironmentSwitchRequest(target=target))

    assert result.active_environment == target
    assert opposite.is_file()
    assert calls.index("backup") < calls.index("validate")
    assert (tmp_path / ".flow-steward/runtime/seaweedfs-s3.json").is_file()


@pytest.mark.parametrize(
    ("source", "target", "missing_name"),
    (("development", "production", ".env.prod"), ("production", "development", ".env")),
)
def test_clean_switch_preview_derives_target_before_opposite_environment_exists(
    tmp_path: Path,
    source: str,
    target: str,
    missing_name: str,
) -> None:
    service, _calls, _storage = _clean_real_switch_service(tmp_path, source=source)
    opposite = tmp_path / missing_name
    assert not opposite.exists()

    preview = service.plan_switch(EnvironmentSwitchRequest(target=target))

    assert "fs-cli" in preview["long_running_services"]
    assert not opposite.exists()


def test_external_environment_only_preview_uses_installed_credentials(
    tmp_path: Path,
) -> None:
    service, _calls, _storage = _clean_real_switch_service(
        tmp_path,
        source="production",
        file_storage="external_s3",
    )

    preview = service.plan_switch(EnvironmentSwitchRequest(target="development"))

    assert preview["storage_decision"] == "keep"
    assert preview["target_storage_identity"].startswith("s3:")


def test_external_environment_only_switch_does_not_reopen_installer_credential_input(
    tmp_path: Path,
) -> None:
    active = DeploymentConfig(
        environment="production",
        file_storage="external_s3",
        s3_endpoint="https://objects.example.test",
        s3_bucket="artifacts",
        s3_region="eu-test-1",
        s3_credentials_path="/removed/installer-input.json",
    )
    service, calls, _runtime = _service(tmp_path, active=active)

    result = service.switch(EnvironmentSwitchRequest(target="development"))

    assert result.active.file_storage == "external_s3"
    assert "install_credentials" not in calls


def test_cancellation_immediately_after_storage_probe_restores_exact_source_once(
    tmp_path: Path,
) -> None:
    service, calls, _runtime = _service(tmp_path)
    token = CancellationToken()
    storage = service.storage
    original_probe = storage.probe_write

    def cancel_after_probe(config: DeploymentConfig, environment: dict[str, str]) -> None:
        original_probe(config, environment)
        token.request()

    storage.probe_write = cancel_after_probe  # type: ignore[method-assign]

    _raises_input_843_1 = EnvironmentSwitchRequest(
        target="production",
        file_storage="external_s3",
        s3_endpoint="https://objects.example.test",
        s3_bucket="artifacts",
        s3_region="eu-test-1",
        s3_credentials_file="candidate.json",
    )
    with pytest.raises(LifecycleError) as raised:
        service.switch(_raises_input_843_1, cancellation=token)

    assert raised.value.code == "lifecycle_cancelled"
    assert calls.count("restore") == 1
    assert service.jobs.owner_operation_id is None


def test_storage_credential_step_journals_only_nonsecret_evidence(tmp_path: Path) -> None:
    service, _calls, _runtime = _service(tmp_path)

    result = service.switch(EnvironmentSwitchRequest(target="production"))

    assert result.operation_id is not None
    journal = service.lifecycle_state.load_journal(result.operation_id)
    assert journal is not None
    evidence = journal.step_outputs["install_storage_credentials"]
    assert evidence == {
        "present_keys": ["ARTIFACT_S3_ACCESS_KEY", "ARTIFACT_S3_SECRET_KEY"],
        "sha256": "nonsecret-credential-proof",
    }
    assert "credential-value" not in journal.model_dump_json()


def test_storage_change_requires_decision_before_any_mutation(
    tmp_path: Path,
) -> None:
    unknown = StorageInventory(
        backend="bundled",
        approximate_count=None,
        approximate_size_bytes=None,
        source_identity="volume:installed-project_seaweedfs",
        completeness="unknown",
    )
    service, calls, _runtime = _service(tmp_path, inventory=unknown)

    _raises_input_889_1 = EnvironmentSwitchRequest(
        target="production",
        file_storage="external_s3",
        s3_endpoint="https://objects.example.test",
        s3_bucket="artifacts",
        s3_region="eu-test-1",
        s3_credentials_file="/safe/candidate.json",
    )
    with pytest.raises(LifecycleError) as raised:
        service.switch(_raises_input_889_1)

    assert raised.value.code == "environment_artifact_decision_required"
    assert "inventory" in calls
    assert "stop_accepting" not in calls
    assert "backup" not in calls
    assert "render" not in calls
    assert "activate" not in calls
    assert not (tmp_path / ".flow-steward").exists()


def test_external_same_identity_skips_inventory_decision_but_still_probes_target(
    tmp_path: Path,
) -> None:
    active = DeploymentConfig(
        environment="development",
        file_storage="external_s3",
        s3_endpoint="https://OBJECTS.example.test/",
        s3_bucket="artifacts",
        s3_region="EU-TEST-1",
        s3_credentials_path="/safe/source.json",
    )
    service, calls, _runtime = _service(tmp_path, active=active)

    result = service.switch(
        EnvironmentSwitchRequest(
            target="production",
            file_storage="external_s3",
            s3_endpoint="https://objects.example.test",
            s3_bucket="artifacts",
            s3_region="eu-test-1",
            s3_credentials_file="/safe/candidate.json",
        )
    )

    assert result.active.file_storage == "external_s3"
    assert "inventory" not in calls
    assert "probe" in calls


def test_external_identity_change_requires_inventory_decision(tmp_path: Path) -> None:
    active = DeploymentConfig(
        environment="development",
        file_storage="external_s3",
        s3_endpoint="https://objects.example.test",
        s3_bucket="source-artifacts",
        s3_region="eu-test-1",
        s3_credentials_path="/safe/source.json",
    )
    service, calls, _runtime = _service(
        tmp_path,
        active=active,
        inventory=StorageInventory(
            backend="external_s3",
            approximate_count=2,
            approximate_size_bytes=64,
            source_identity="source-identity",
            completeness="complete",
        ),
    )

    _raises_input_960_1 = EnvironmentSwitchRequest(
        target="production",
        file_storage="external_s3",
        s3_endpoint="https://objects.example.test",
        s3_bucket="target-artifacts",
        s3_region="eu-test-1",
        s3_credentials_file="/safe/candidate.json",
    )
    with pytest.raises(LifecycleError) as raised:
        service.switch(_raises_input_960_1)

    assert raised.value.code == "environment_artifact_decision_required"
    assert calls.count("inventory") == 1
    assert "stop_accepting" not in calls


def test_accepting_new_backend_never_copies_or_deletes_and_retains_old_snapshot(
    tmp_path: Path,
) -> None:
    service, calls, _runtime = _service(
        tmp_path,
        inventory=StorageInventory(
            backend="bundled",
            approximate_count=17,
            approximate_size_bytes=4096,
            source_identity="volume:installed-project_seaweedfs",
            completeness="complete",
        ),
    )

    result = service.switch(
        EnvironmentSwitchRequest(
            target="production",
            file_storage="external_s3",
            s3_endpoint="https://objects.example.test",
            s3_bucket="artifacts",
            s3_region="eu-test-1",
            s3_credentials_file="/safe/candidate.json",
            artifact_decision="accept_unavailable",
        )
    )

    assert result.storage_inventory is not None
    assert result.storage_inventory.approximate_count == 17
    assert result.artifact_decision == "accept_unavailable"
    assert "inventory" in calls
    assert "probe" in calls
    assert not any("copy" in call or "delete" in call for call in calls)
    journal = next(
        path
        for path in (tmp_path / ".flow-steward/operations").glob("*.json")
        if ".previous-config." not in path.name
    )
    text = journal.read_text(encoding="utf-8")
    assert "accept_unavailable" in text
    assert "objects.example.test" in text
    assert "candidate.json" not in text


def test_external_to_bundled_accepts_explicit_artifact_decision(tmp_path: Path) -> None:
    active = DeploymentConfig(
        environment="production",
        file_storage="external_s3",
        s3_endpoint="https://objects.example.test",
        s3_bucket="artifacts",
        s3_region="eu-test-1",
        s3_credentials_path="/safe/source.json",
    )
    service, _calls, _runtime = _service(
        tmp_path,
        active=active,
        inventory=StorageInventory(
            backend="external_s3",
            approximate_count=3,
            approximate_size_bytes=128,
            source_identity="s3:objects.example.test/artifacts",
            completeness="complete",
        ),
    )

    result = service.switch(
        EnvironmentSwitchRequest(
            target="development",
            file_storage="bundled",
            artifact_decision="accept_unavailable",
        )
    )

    assert result.active.file_storage == "bundled"
    assert result.artifact_decision == "accept_unavailable"


def test_backup_failure_precedes_first_config_or_runtime_mutation_and_resumes_jobs(
    tmp_path: Path,
) -> None:
    service, calls, _runtime = _service(tmp_path, backup_fail=True)

    _raises_input_1058_1 = EnvironmentSwitchRequest(target="production")
    with pytest.raises(LifecycleError) as raised:
        service.switch(_raises_input_1058_1)

    assert raised.value.code == "backup_failed"
    assert "render" not in calls
    assert "activate" not in calls
    assert calls[-1] == "resume_accepting"


def test_failed_target_restores_exact_previous_runtime_and_reports_stable_failure(
    tmp_path: Path,
) -> None:
    service, calls, _runtime = _service(tmp_path, fail_target_health=True)

    _raises_input_1072_1 = EnvironmentSwitchRequest(target="production")
    with pytest.raises(LifecycleError) as raised:
        service.switch(_raises_input_1072_1)

    assert raised.value.code == "environment_switch_failed"
    assert raised.value.exit_code == 8
    assert calls[-5:] == [
        "restore",
        "health:development",
        "manifest:after",
        "compare",
        "resume_accepting",
    ]
    assert "mark_active" not in calls
    assert service.configuration.read_active().environment == "development"


def test_failed_target_requires_recovery_when_previous_health_cannot_be_verified(
    tmp_path: Path,
) -> None:
    service, calls, _runtime = _service(
        tmp_path,
        fail_target_health=True,
        fail_restore=True,
    )

    _raises_input_1097_1 = EnvironmentSwitchRequest(target="production")
    with pytest.raises(LifecycleError) as raised:
        service.switch(_raises_input_1097_1)

    assert raised.value.code == "environment_recovery_required"
    assert raised.value.exit_code == 9
    assert service.jobs.owner_operation_id == raised.value.operation_id
    assert calls[-1] != "resume_accepting"


def test_recovery_required_preserves_redacted_original_adapter_failure(
    tmp_path: Path,
) -> None:
    service, calls, runtime = _service(tmp_path, fail_restore=True)

    def fail_validate(*_args: object, **_kwargs: object) -> None:
        calls.append("validate")
        raise StackGatewayError(
            action="configuration",
            detail="Docker target configuration rejected a candidate mount.",
        )

    runtime.validate_target = fail_validate  # type: ignore[method-assign]

    _raises_input_1120_1 = EnvironmentSwitchRequest(target="production")
    with pytest.raises(LifecycleError) as raised:
        service.switch(_raises_input_1120_1)

    assert raised.value.code == "environment_recovery_required"
    assert "Docker target configuration rejected a candidate mount." in raised.value.cause
    assert calls[-1] != "resume_accepting"


def test_baseexception_after_target_render_restores_before_clearing_owner_pause(
    tmp_path: Path,
) -> None:
    service, calls, _runtime = _service(tmp_path, base_failure_step="validate")

    _raises_input_1133_1 = EnvironmentSwitchRequest(target="production")
    with pytest.raises(LifecycleError) as raised:
        service.switch(_raises_input_1133_1)

    assert raised.value.code == "lifecycle_forced_interrupt"
    assert calls.count("restore") == 1
    assert service.jobs.owner_operation_id is None


def test_same_current_environment_recovers_sole_unfinished_operation(tmp_path: Path) -> None:
    service, calls, runtime = _service(
        tmp_path,
        fail_target_health=True,
        fail_restore=True,
    )
    _raises_input_1147_1 = EnvironmentSwitchRequest(target="production")
    with pytest.raises(LifecycleError) as first:
        service.switch(_raises_input_1147_1)
    assert first.value.code == "environment_recovery_required"
    runtime.fail_target_health = False
    runtime.fail_restore = False

    result = service.switch(EnvironmentSwitchRequest(target="development"))

    assert result.status == "recovered"
    assert result.active_environment == "development"
    assert service.jobs.owner_operation_id is None
    journal = service.lifecycle_state.load_journal(str(first.value.operation_id))
    assert journal is not None
    assert journal.status.value == "completed"
    assert journal.recovery_command == ("./flow-steward environment switch development --confirm")
    assert calls.count("backup") == 1
    assert calls.count("restore") == 2


@pytest.mark.parametrize(
    ("step_id", "effect_calls"),
    [
        ("capture_before", ("snapshot", "manifest:before")),
        ("create_backup", ("backup",)),
        ("render_target", ("render",)),
        ("install_storage_credentials", ("install_credentials",)),
        ("apply_runtime_artifacts", ("artifacts",)),
        ("activate_target", ("activate",)),
    ],
)
def test_crash_after_effect_before_step_journal_does_not_repeat_effect(
    tmp_path: Path,
    step_id: str,
    effect_calls: tuple[str, ...],
) -> None:
    state = _CrashAfterEffectStateStore(tmp_path, step_id)
    service, calls, _runtime = _service(tmp_path, lifecycle_state=state)
    request = EnvironmentSwitchRequest(target="production")

    with pytest.raises(SystemExit, match=f"crash after {step_id}"):
        service.switch(request)

    result = service.switch(request)

    assert result.status == "active"
    for effect in effect_calls:
        assert calls.count(effect) == 1


@pytest.mark.parametrize("step_id", ("mark_active", "resume_jobs"))
@pytest.mark.parametrize("journal_persisted", (False, True), ids=("after-effect", "after-journal"))
def test_active_target_crash_resumes_forward_without_repeating_mutation(
    tmp_path: Path,
    step_id: str,
    journal_persisted: bool,
) -> None:
    state_type = _CrashAfterJournalStateStore if journal_persisted else _CrashAfterEffectStateStore
    state = state_type(tmp_path, step_id)
    service, calls, _runtime = _service(tmp_path, lifecycle_state=state)
    request = EnvironmentSwitchRequest(target="production")

    with pytest.raises(SystemExit):
        service.switch(request)

    result = service.switch(request)

    assert result.status == "recovered"
    assert result.active_environment == "production"
    assert service.jobs.owner_operation_id is None
    assert calls.count("mark_active") == 1
    assert calls.count("resume_accepting") == 1


def test_target_runtime_release_must_equal_source_runtime_release(tmp_path: Path) -> None:
    service, calls, runtime = _service(tmp_path)
    runtime.target_release = "4.9.0"

    _raises_input_1224_1 = EnvironmentSwitchRequest(target="production")
    with pytest.raises(LifecycleError) as raised:
        service.switch(_raises_input_1224_1)

    assert raised.value.code == "environment_switch_failed"
    assert "restore" in calls
    assert "mark_active" not in calls
    assert service.configuration.read_active().environment == "development"


def test_development_preset_is_complete_and_production_forces_tools_off(tmp_path: Path) -> None:
    service, _calls, _runtime = _service(
        tmp_path,
        active=DeploymentConfig(
            environment="development",
            development_tools=("test_runner", "audit_tools", "sonarqube"),
        ),
    )

    result = service.switch(EnvironmentSwitchRequest(target="production"))

    assert result.active.development_tools == ()
    assert any("seaweedfs" in resource for resource in result.retained_resources)


def test_development_preview_names_running_and_on_demand_preset(tmp_path: Path) -> None:
    service, _calls, _runtime = _service(
        tmp_path,
        active=DeploymentConfig(environment="production"),
    )

    preview = service.plan_switch(EnvironmentSwitchRequest(target="development"))

    assert preview["development_tools"] == ["test_runner", "audit_tools", "sonarqube"]
    assert {
        "fs-cli",
        "fs-frontend",
        "fs-test-runner",
        "fs-audit",
        "pg-audit",
        "rabbitmq-audit",
    } <= set(preview["long_running_services"])
    assert set(preview["on_demand_services"]) == {
        "mailpit",
        "sonarqube",
        "sonar-scanner",
    }


@pytest.mark.parametrize(
    ("source", "target", "expected_tools"),
    [
        ("production", "development", ("test_runner", "audit_tools", "sonarqube")),
        ("development", "production", ()),
    ],
)
def test_switch_preserves_cli_and_writes_normalized_target(
    tmp_path: Path,
    source: str,
    target: str,
    expected_tools: tuple[str, ...],
) -> None:
    service, _calls, runtime = _service(
        tmp_path,
        active=DeploymentConfig(environment=source),
    )

    result = service.switch(EnvironmentSwitchRequest(target=target))
    source_target = service.configuration.derive_target(DeploymentConfig(environment=source))
    derived = service.configuration.derive_target(result.active)

    assert result.active.development_tools == expected_tools
    assert "fs-cli" in derived.runtime_services
    assert runtime.activated_previous_targets == [source_target]
    assert runtime.activated_targets == [derived]
    if target == "development":
        assert {"fs-frontend", "fs-test-runner", "fs-audit"} <= set(derived.runtime_services)
    else:
        assert {"fs-frontend", "mailpit", "sonarqube"}.isdisjoint(derived.required_services)


def test_failed_target_restores_exact_normalized_source_configuration(tmp_path: Path) -> None:
    source = DeploymentConfig(
        environment="development",
        development_tools=("test_runner", "audit_tools", "sonarqube"),
    )
    service, calls, runtime = _service(
        tmp_path,
        active=source,
        fail_target_health=True,
    )

    _raises_input_1315_1 = EnvironmentSwitchRequest(target="production")
    with pytest.raises(LifecycleError):
        service.switch(_raises_input_1315_1)

    assert "restore" in calls
    assert runtime.restored_targets == [_target(source)]
    assert service.configuration.read_active() == source
