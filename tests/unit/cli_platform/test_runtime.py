"""Production composition regressions for the standalone CLI runtime."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.application.auth.user_admin.notifications import notify_signup_non_blocking
from core.application.clone.contracts import CloneDataClass
from core.application.clone.service import CloneService
from core.application.extensions.install import ExtensionInstallStore
from core.application.extensions.query_service import (
    ExtensionQueryService as CanonicalExtensionQueryService,
)
from core.application.extensions.runtime_store import ExtensionRuntimeStore
from core.application.installer import InstallConfig
from core.application.lifecycle.contracts import CancellationToken, ComposeTarget
from core.application.lifecycle.errors import CandidateReadinessProbePendingError, LifecycleError
from core.application.management.command_services_approval import ApprovalCommandService
from core.application.management.scope import ScopeContext
from core.application.release.contracts import ReleaseIdentity, ReleaseImageIdentity
from core.application.release.images import required_product_roles
from core.application.tooling.db import restore_parity_check
from core.application.tooling.db.models import DatabaseRestoreParityCaptureRequest
from core.application.tooling.db.service import DatabaseToolingService
from core.entrypoints.cli import lifecycle_runtime as cli_runtime
from core.entrypoints.cli.maintenance_adapters import _MaintenanceHealthAdapter
from core.entrypoints.cli.platform.operation_log import OperationLog
from core.entrypoints.cli.runtime import CLIRuntime
from core.infrastructure.db import job_activity_store
from core.infrastructure.lifecycle.configuration_store import LifecycleConfigurationStore
from core.infrastructure.security.secret_codec import encrypt_secret


def _clone_release(profile: str) -> ReleaseIdentity:
    digest = "sha256:" + "a" * 64
    commit = "b" * 40
    registry = "ghcr.io/example"
    return ReleaseIdentity(
        version="1.2.3",
        tag="v1.2.3",
        commit_sha=commit,
        registry=registry,
        profile=profile,
        platform="linux/amd64",
        images=tuple(
            ReleaseImageIdentity(
                role=role.role,
                registry_ref=f"{registry}/{role.repository}@{digest}",
                index_digest=digest,
                manifest_digest=digest,
                config_digest=digest,
                revision=commit,
                platform="linux/amd64",
            )
            for role in required_product_roles(profile)
        ),
    )


def test_installed_runtime_environment_reads_managed_file_without_exporting_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LifecycleConfigurationStore(tmp_path)
    store.write_active(InstallConfig(environment="development"))
    (tmp_path / ".env").write_text(
        "FS_DB_DSN=postgresql://managed-file\nFS_SECRET_KEY=file-only-secret\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cli_runtime, "_REPO_ROOT", tmp_path)
    monkeypatch.delenv("FS_DB_DSN", raising=False)
    monkeypatch.delenv("FS_SECRET_KEY", raising=False)

    environment = cli_runtime._installed_runtime_environment()

    assert environment["FS_DB_DSN"] == "postgresql://managed-file"
    assert environment["FS_SECRET_KEY"] == "file-only-secret"
    assert "FS_DB_DSN" not in cli_runtime._runtime_env
    assert "FS_SECRET_KEY" not in cli_runtime._runtime_env


def test_operation_log_uses_installed_project_root_outside_container_workdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "installed"
    unrelated_workdir = tmp_path / "image"
    project_root.mkdir()
    unrelated_workdir.mkdir()
    monkeypatch.chdir(unrelated_workdir)
    monkeypatch.setenv("FS_DOCKER_HOST_PROJECT_ROOT", str(tmp_path / "unmounted-host"))
    monkeypatch.setenv(
        "FS_CLI_OPERATION_LOG_ROOT",
        str(project_root / ".flow-steward/logs"),
    )

    log = OperationLog("maintenance-enable")
    log.close()

    assert log.path.parent == project_root / ".flow-steward/logs"


def test_backup_gateway_uses_installed_dedicated_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = ComposeTarget(
        stack_environment="production",
        file_storage="local_fs",
        env_file=".env.prod",
        compose_files=("infra/docker/docker-compose.prod.yaml",),
    )
    configuration = SimpleNamespace(
        read_active=lambda: InstallConfig(environment="production"),
        derive_target=lambda _config: target,
    )
    monkeypatch.setattr(
        cli_runtime,
        "_installed_runtime_environment",
        lambda: {"FS_BACKUP_ENCRYPTION_KEY": "installed-backup-key"},
    )
    monkeypatch.setattr(cli_runtime, "_REPO_ROOT", tmp_path)
    runtime = CLIRuntime()
    runtime.__dict__["configuration_service"] = configuration

    service = runtime._build_backup_service()

    assert service._gateway._key_source() == "installed-backup-key"


@pytest.mark.parametrize(
    ("runtime_type", "profile", "data_root_name"),
    [
        (CLIRuntime, "scalable", "installed"),
        ("compact", "compact", "compact-data"),
    ],
)
def test_installed_cli_runtime_composes_production_clone_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_type: object,
    profile: str,
    data_root_name: str,
) -> None:
    from core.entrypoints.cli import compact_platform_runtime
    from core.entrypoints.cli.compact_platform_runtime import CompactPlatformRuntime

    data_root = tmp_path / data_root_name
    for name in ("artifacts", "extensions", "state", "backups"):
        (data_root / name).mkdir(parents=True, exist_ok=True)
    (data_root / ".flow-steward").mkdir(mode=0o700)
    monkeypatch.setattr(cli_runtime, "_REPO_ROOT", data_root)
    if profile == "compact":
        native_state = data_root / "state/.flow-steward"
        native_state.mkdir(mode=0o700)
        monkeypatch.setattr(compact_platform_runtime, "_DATA_ROOT", data_root)
        monkeypatch.setattr(compact_platform_runtime, "_STATE_PROJECT_ROOT", data_root / "state")
        monkeypatch.setattr(compact_platform_runtime, "_STATE_ROOT", native_state)
        monkeypatch.setattr(compact_platform_runtime, "_LIFECYCLE_ROOT", data_root)
        selected_type = CompactPlatformRuntime
    else:
        selected_type = runtime_type

    config = InstallConfig(
        environment="production",
        deployment_profile=profile,
        data_placement="compact_volume" if profile == "compact" else "host_directories",
        file_storage="local_fs",
    )
    target = ComposeTarget(
        stack_environment="production",
        file_storage="local_fs",
        deployment_profile=profile,
        env_file=".env.prod",
        compose_files=("compose.yaml",),
        required_services=("pg-fs", "rabbitmq-fs", "fs-cli"),
        runtime_services=("pg-fs", "rabbitmq-fs"),
        project_name="installed-stack",
    )
    written_configs: list[InstallConfig] = []
    configuration = SimpleNamespace(
        read_active=lambda required=True: config,
        derive_target=lambda _config: target,
        store=SimpleNamespace(env_path=lambda _config: data_root / ".env.prod"),
        write_active_configuration=lambda selected: written_configs.append(selected) or target,
    )
    release = _clone_release(profile)
    release_state = SimpleNamespace(load_installed_release=lambda: release)
    release_service = SimpleNamespace(
        state=release_state,
        selector=SimpleNamespace(),
        activator=SimpleNamespace(activate=lambda *_args, **_kwargs: None),
        require_official_installation=lambda: release,
    )
    runtime = selected_type()
    runtime.__dict__.update(
        configuration_service=configuration,
        maintenance_service=SimpleNamespace(status=lambda: None),
        backup_service=MagicMock(),
        docker_lifecycle_gateway=MagicMock(),
        release_service=release_service,
        lifecycle_mutation_readiness_service=MagicMock(),
    )
    monkeypatch.setattr(
        cli_runtime,
        "_installed_runtime_environment",
        lambda: {
            "ARTIFACT_LOCAL_ROOT_DIR": str(data_root / "artifacts"),
            "FS_DOCKER_HOST_PROJECT_ROOT": str(data_root),
            "FS_EXTENSIONS_DIR": str(data_root / "extensions"),
            "FS_HOST_UID": "1001",
            "FS_HOST_GID": "1002",
            "POSTGRES_USER": "target-role",
            "FS_BACKUP_ENCRYPTION_KEY": "target-backup-key",
            "FS_SECRET_KEY": "source-secret",  # pragma: allowlist secret - synthetic Clone fixture
        },
    )

    service = runtime.clone_service

    assert isinstance(service, CloneService)
    assert service._runtime is not None
    assert service._runtime._capture_database_parity is not None
    assert "FS_BACKUP_ENCRYPTION_KEY" not in service._runtime._source_secrets
    assert service._restore_archive is not None
    assert service._restore_runtime is not None
    assert set(service._runtime._source_roots) == {
        CloneDataClass.LOCAL_ARTIFACTS,
        CloneDataClass.EXTENSIONS,
        CloneDataClass.DURABLE_STATE,
    }
    if profile == "scalable":
        shell = service._target_shell(config)
        assert shell.filesystem_roots == {
            "local_artifacts": str(data_root / "logs" / "artifacts"),
            "extensions": str(data_root / "extensions"),
            "durable_state": str(data_root / ".flow-steward"),
        }
    assert set(service._restore_runtime._phase_handlers) == {
        "persist_plan",
        "stop_target",
        "replace_filesystems",
        "restore_database",
        "restore_service_state",
        "restore_identity",
        "regenerate_configuration",
        "activate_release",
        "start_maintenance",
        "verify_restored_target",
    }
    restore_effects = service._restore_runtime._phase_handlers["stop_target"].__self__
    assert "pg-fs" not in restore_effects._service_names
    assert "rabbitmq-fs" in restore_effects._service_names
    assert set(service._restore_runtime._phase_postconditions) == {
        "persist_plan",
        "stop_target",
        "replace_filesystems",
        "restore_database",
        "restore_service_state",
        "restore_identity",
        "regenerate_configuration",
        "activate_release",
        "start_maintenance",
        "verify_restored_target",
    }

    staged = tmp_path / f"{profile}-staged"
    (staged / "evidence").mkdir(parents=True)
    source_config = config.model_copy(
        update={
            "file_storage": "external_s3",
            "s3_endpoint": "https://source-storage.example.test",
            "s3_bucket": "source-bucket",
            "public_host": "source.example.test",
        }
    )
    (staged / "evidence/configuration.json").write_text(
        source_config.model_dump_json(),
        encoding="utf-8",
    )
    retained_shell = service._target_shell(config)
    service._restore_runtime._phase_handlers["regenerate_configuration"](
        SimpleNamespace(
            staged_root=str(staged),
            source_configuration=source_config,
            plan=SimpleNamespace(retained_shell=retained_shell),
        )
    )

    assert written_configs[-1].file_storage == "external_s3"
    assert written_configs[-1].s3_endpoint == "https://source-storage.example.test"
    assert written_configs[-1].public_host == config.public_host

    unavailable_release = release.model_copy(update={"version": "9.9.9"})
    activation = service._restore_runtime._phase_handlers["activate_release"]
    prepared = SimpleNamespace(exact_release=unavailable_release)
    with pytest.raises(LifecycleError, match="not activated"):
        activation(prepared)


def test_preservation_decrypts_with_installed_encryption_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_key = "installed-encryption-key"
    master_key = hashlib.sha256(raw_key.encode()).digest()
    ciphertext = encrypt_secret("preserved-control", master_key=master_key)
    monkeypatch.setattr(
        cli_runtime,
        "_installed_runtime_environment",
        lambda: {
            "ENCRYPTION_KEY": raw_key,
            "FS_DB_DSN": "postgresql://managed-file",
        },
    )

    repository = CLIRuntime().preservation_service._repository

    assert repository._decrypt_control(ciphertext) == "preserved-control"


def test_restore_parity_artifacts_resolve_from_installed_project_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[Path, str]] = []

    def capture(**kwargs):
        output = kwargs["output"]
        observed.append((output, kwargs["dsn"]))
        return {"output": str(output), "snapshot": {}}

    monkeypatch.setattr(restore_parity_check, "capture_restore_parity_metadata", capture)

    result = DatabaseToolingService(
        project_root=tmp_path,
        dsn_provider=lambda: "postgresql://installed-database",
    ).restore_parity_capture(
        DatabaseRestoreParityCaptureRequest(output="backups/restore/baseline.json")
    )

    expected = tmp_path / "backups/restore/baseline.json"
    assert observed == [(expected, "postgresql://installed-database")]
    assert result.artifacts == [str(expected)]


def test_cli_database_dsn_uses_installer_owned_environment_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli_runtime,
        "_installed_runtime_environment",
        lambda: {"FS_DB_DSN": "postgresql://managed-file"},
    )

    assert CLIRuntime()._configured_platform_dsn() == "postgresql://managed-file"


def test_cli_database_dsn_uses_compact_one_shot_environment_without_managed_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_runtime, "_installed_runtime_environment", dict)
    monkeypatch.setenv("FS_DB_DSN", "postgresql://compact-one-shot")
    monkeypatch.delenv("FS_OBSERVABILITY_DSN", raising=False)

    assert CLIRuntime()._configured_platform_dsn() == "postgresql://compact-one-shot"


def test_cli_job_command_service_uses_project_store_for_restart_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_store = MagicMock(spec=[])
    project_store = MagicMock()
    project_store.get_project_run_config.return_value = {"project_name": "Demo"}
    monkeypatch.setattr(CLIRuntime, "get_job_store", lambda _self: job_store)
    monkeypatch.setattr(CLIRuntime, "get_project_store", lambda _self: project_store)

    service = CLIRuntime().job_command_service

    assert service._project_run_config(job_store, "project-1") == {"project_name": "Demo"}
    project_store.get_project_run_config.assert_called_once_with("project-1")


def test_cli_signup_notifier_propagates_explicit_failure_to_warning_policy(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from core.application.extensions import system_email

    monkeypatch.setattr(
        system_email,
        "send_signup_notification_email",
        lambda **_kwargs: {"ok": False, "reason": "private-delivery-detail"},
    )
    notifier = CLIRuntime().user_command_service._signup_notifier

    with caplog.at_level(logging.WARNING):
        notify_signup_non_blocking(
            notifier,
            user_id="u_cli",
            recipient="private-recipient@example.com",
            display_name="Private Recipient",
            locale="en",
        )

    assert "Signup notification delivery failed for user_id=u_cli" in caplog.text
    assert "private-recipient@example.com" not in caplog.text
    assert "private-delivery-detail" not in caplog.text


def test_cli_approval_composition_constructs_current_production_service() -> None:
    service = CLIRuntime().approval_command_service

    assert isinstance(service, ApprovalCommandService)


def test_cli_identity_store_enforces_runtime_schema_contract_before_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.infrastructure.runtime import entrypoint_db_adapters

    fake_logger = object()
    calls: list[str] = []
    monkeypatch.setattr(
        cli_runtime,
        "_installed_runtime_environment",
        lambda: {"FS_OBSERVABILITY_DSN": "postgresql://example"},
    )
    monkeypatch.setattr(
        entrypoint_db_adapters,
        "create_identity_store",
        lambda _dsn: calls.append("identity") or fake_logger,
    )
    monkeypatch.setattr(
        entrypoint_db_adapters,
        "enforce_runtime_schema",
        lambda dsn, *, component: calls.append(f"schema:{component}:{dsn}"),
    )

    assert CLIRuntime().identity_store is fake_logger
    assert calls == ["schema:cli:postgresql://example", "identity"]


def test_cli_reporting_service_uses_reporting_store_with_dashboard_datasets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.infrastructure.runtime import entrypoint_db_adapters

    class ReportingStore:
        def list_dashboard_job_facts(self, **_kwargs):
            return []

        def get_project(self, _project_id):
            return None

    reporting_store = ReportingStore()
    monkeypatch.setattr(
        cli_runtime,
        "_installed_runtime_environment",
        lambda: {"FS_OBSERVABILITY_DSN": "postgresql://example"},
    )
    monkeypatch.setattr(
        entrypoint_db_adapters,
        "enforce_runtime_schema",
        lambda _dsn, *, component: None,
    )
    monkeypatch.setattr(
        entrypoint_db_adapters,
        "create_reporting_store",
        lambda _dsn: reporting_store,
        raising=False,
    )

    runtime = CLIRuntime()

    assert runtime.reporting_store is reporting_store
    assert runtime.reporting_service._get_logger_only() is reporting_store


def test_cli_database_upgrade_service_remains_available_before_schema_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.infrastructure.runtime import entrypoint_db_adapters

    monkeypatch.setattr(
        entrypoint_db_adapters,
        "enforce_runtime_schema",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("schema gate called")),
    )

    assert CLIRuntime().database_tooling_service is not None


def test_lifecycle_idle_observation_remains_available_before_schema_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.infrastructure.db import psycopg_runtime

    class Connection:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    connection = Connection()
    observations: list[object] = []
    monkeypatch.setattr(
        cli_runtime,
        "_installed_runtime_environment",
        lambda: {"FS_OBSERVABILITY_DSN": "postgresql://legacy"},
    )
    monkeypatch.setattr(psycopg_runtime, "connect", lambda _dsn: connection)
    monkeypatch.setattr(
        job_activity_store,
        "get_active_jobs",
        lambda *, conn: observations.append(conn) or [],
    )
    gateway = CLIRuntime().lifecycle_job_quiescence_gateway
    gateway._sleep = lambda _seconds: None

    gateway.wait_for_idle(
        cancellation=CancellationToken(),
        heartbeat=lambda _detail: None,
    )

    assert observations == [connection, connection]
    assert connection.close_calls == 2


def test_stack_runtime_uses_mutation_aware_readiness_and_real_storage_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli_runtime,
        "_installed_runtime_environment",
        lambda: {"FS_DB_DSN": "postgresql://managed-file"},
    )
    monkeypatch.setattr(
        cli_runtime,
        "_maintenance_health_payload",
        lambda: {
            "ready": False,
            "checks": {
                "postgres": {"ok": True},
                "lifecycle_recovery": {
                    "ok": False,
                    "state": "mutation_in_progress",
                },
            },
        },
    )
    runtime = CLIRuntime()
    runtime.__dict__["configuration_service"] = SimpleNamespace(
        read_active=lambda: InstallConfig(deployment_profile="scalable"),
    )

    service = runtime.stack_service
    report = service.readiness_service.check()

    assert service.readiness_service is runtime.lifecycle_mutation_readiness_service
    assert service.readiness_service is not runtime.core_readiness_service
    assert report.status == "ready"
    assert report.core_failed == 0
    assert {finding.check_id for finding in report.findings} == {"postgres"}
    assert service.storage_connectivity_provider.__self__ is runtime
    assert service.docker_gateway is runtime.docker_lifecycle_gateway


@pytest.mark.parametrize("deployment_profile", ("scalable", "compact"))
def test_stack_runtime_readiness_uses_installed_profile_binding(
    monkeypatch: pytest.MonkeyPatch,
    deployment_profile: str,
) -> None:
    config = InstallConfig(
        environment="production",
        deployment_profile=deployment_profile,
    )
    target = object()
    configuration = SimpleNamespace(
        read_active=lambda: config,
        derive_target=lambda selected: (
            target if selected is config else pytest.fail("wrong config")
        ),
    )
    aggregate_calls: list[object] = []
    app_probe = MagicMock(
        return_value={"ready": True, "checks": {"security_posture": {"ok": True}}}
    )
    monkeypatch.setattr(
        cli_runtime,
        "_candidate_health_payload",
        lambda selected: (
            aggregate_calls.append(selected)
            or (
                {"ready": True}
                if deployment_profile == "compact"
                else {"ready": True, "checks": {"postgres": {"ok": True}}}
            )
        ),
    )
    runtime = CLIRuntime()
    runtime.__dict__["configuration_service"] = configuration
    runtime.__dict__["docker_lifecycle_gateway"] = SimpleNamespace(compact_app_readiness=app_probe)

    report = runtime.lifecycle_mutation_readiness_service.check()

    assert report.status == "ready"
    assert aggregate_calls == [config]
    if deployment_profile == "compact":
        app_probe.assert_called_once_with(target)
        assert {finding.check_id for finding in report.findings} == {
            "compact_runtime",
            "security_posture",
        }
    else:
        app_probe.assert_not_called()
        assert {finding.check_id for finding in report.findings} == {"postgres"}


def test_stack_runtime_compact_readiness_keeps_mandatory_app_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = InstallConfig(environment="production", deployment_profile="compact")
    target = object()
    configuration = SimpleNamespace(
        read_active=lambda: config,
        derive_target=lambda _selected: target,
    )
    monkeypatch.setattr(cli_runtime, "_candidate_health_payload", lambda _config: {"ready": True})
    app_probe = MagicMock(
        return_value={
            "ready": False,
            "checks": {
                "security_posture": {
                    "ok": False,
                    "error": "Security posture is stale.",
                }
            },
        }
    )
    runtime = CLIRuntime()
    runtime.__dict__["configuration_service"] = configuration
    runtime.__dict__["docker_lifecycle_gateway"] = SimpleNamespace(compact_app_readiness=app_probe)

    report = runtime.lifecycle_mutation_readiness_service.check()

    assert report.status == "blocked"
    assert report.core_failed == 1
    assert {finding.check_id for finding in report.findings} == {
        "compact_runtime",
        "security_posture",
    }
    app_probe.assert_called_once_with(target)


def test_configuration_runtime_projects_candidate_mounts_to_docker_host_root(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli_runtime,
        "_installed_runtime_environment",
        lambda: {"FS_DB_DSN": "postgresql://managed-file"},
    )
    monkeypatch.setitem(
        cli_runtime._runtime_env,
        "FS_DOCKER_HOST_PROJECT_ROOT",
        str(tmp_path),
    )

    service = CLIRuntime().configuration_service

    assert service.docker_host_project_root == tmp_path.resolve()


def test_explicit_config_readiness_uses_raw_stack_probe_without_phase_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = InstallConfig(public_port=18443)
    configuration_service = object()
    calls: list[tuple[object, bool, bool, bool]] = []

    class Probe:
        def __call__(self) -> dict[str, object]:
            raise AssertionError("explicit readiness must not use phase-aware Doctor")

        def check_config(
            self,
            value: object,
            *,
            allow_initial_auth_bootstrap: bool = False,
            allow_lifecycle_mutation: bool = False,
            allow_release_transition: bool = False,
        ) -> dict[str, object]:
            calls.append(
                (
                    value,
                    allow_initial_auth_bootstrap,
                    allow_lifecycle_mutation,
                    allow_release_transition,
                )
            )
            return {"checks": [{"id": "database", "category": "core", "status": "pass"}]}

    monkeypatch.setattr(
        cli_runtime,
        "_stack_readiness_probe",
        lambda service: (
            Probe() if service is configuration_service else pytest.fail("wrong service")
        ),
    )
    runtime = CLIRuntime()
    runtime.__dict__["configuration_service"] = configuration_service

    payload = runtime.explicit_config_readiness(config)

    assert calls == [(config, True, True, False)]
    assert payload == {"checks": [{"id": "database", "category": "core", "status": "pass"}]}


def test_configuration_candidate_readiness_uses_app_health_during_lifecycle_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli_runtime,
        "_installed_runtime_environment",
        lambda: {"FS_DB_DSN": "postgresql://managed-file"},
    )

    class Probe:
        def __call__(self) -> dict[str, object]:
            raise AssertionError("candidate readiness must not use installer Doctor")

        def check_config(self, *_args, **_kwargs) -> dict[str, object]:
            raise AssertionError("candidate readiness must not use installer Doctor")

    monkeypatch.setattr(cli_runtime, "_stack_readiness_probe", lambda _service: Probe())
    monkeypatch.setattr(
        cli_runtime,
        "_candidate_health_payload",
        lambda _config: {
            "ready": False,
            "checks": {
                "postgres": {"ok": True},
                "worker_manager": {
                    "ok": True,
                    "id": "worker_manager",
                    "category": "core",
                    "title": "Worker availability",
                    "status": "pass",
                },
                "lifecycle_recovery": {
                    "ok": False,
                    "state": "recovery_required",
                },
                "stuck_jobs": {
                    "ok": False,
                    "id": "stuck_jobs",
                    "category": "core",
                    "title": "Stuck jobs",
                    "status": "fail",
                    "persisted_count": 1,
                    "detection_count": 0,
                    "max_allowed": 0,
                    "error": "",
                },
            },
        },
    )
    runtime = CLIRuntime()
    gateway = runtime.configuration_service.gateway

    report = gateway._candidate_readiness(InstallConfig())

    assert report.status == "ready"
    assert report.core_failed == 0
    assert {finding.check_id for finding in report.findings} == {
        "postgres",
        "worker_manager",
    }


def test_configuration_compact_candidate_readiness_uses_in_container_app_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli_runtime,
        "_installed_runtime_environment",
        lambda: {"FS_DB_DSN": "postgresql://managed-file"},
    )
    monkeypatch.setattr(
        cli_runtime,
        "_candidate_health_payload",
        lambda _config: {"ready": True},
    )
    runtime = CLIRuntime()
    app_probe = MagicMock(
        return_value={
            "ready": False,
            "checks": {
                "security_posture": {"ok": False},
                "workflow_knowledge_sync": {"ok": False},
            },
        }
    )
    monkeypatch.setattr(runtime.docker_lifecycle_gateway, "compact_app_readiness", app_probe)
    service = runtime.configuration_service
    target = ComposeTarget(
        stack_environment="production",
        file_storage="local_fs",
        deployment_profile="compact",
        env_file=".env.prod",
        compose_files=("infra/docker/docker-compose.compact.yaml",),
    )
    monkeypatch.setattr(service, "derive_target", lambda _config: target)
    gateway = service.gateway
    config = InstallConfig(environment="production", deployment_profile="compact")

    report = gateway._candidate_readiness(config)

    assert report.status == "blocked"
    assert {finding.check_id for finding in report.findings} == {
        "compact_runtime",
        "security_posture",
        "workflow_knowledge_sync",
    }
    app_probe.assert_called_once_with(target)


def test_compact_candidate_readiness_uses_compact_runtime_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, int, str]] = []

    class Response:
        status = 200

        @staticmethod
        def read(_limit: int) -> bytes:
            return b'{"ready":true}'

    class Connection:
        def __init__(self, host: str, port: int, timeout: int) -> None:
            assert timeout == 5
            calls.append((host, port, ""))

        def request(self, method: str, path: str) -> None:
            assert method == "GET"
            calls[-1] = (*calls[-1][:2], path)

        @staticmethod
        def getresponse() -> Response:
            return Response()

        @staticmethod
        def close() -> None:
            return None

    monkeypatch.setattr(cli_runtime, "HTTPConnection", Connection)

    payload = cli_runtime._candidate_health_payload(
        InstallConfig(
            environment="production",
            deployment_profile="compact",
            runtime_http_port=8080,
        )
    )

    assert payload == {"ready": True}
    assert calls == [("compact-runtime", 8080, "/health/ready")]


def test_compact_candidate_readiness_marks_startup_route_404_as_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        status = 404

        @staticmethod
        def read(_limit: int) -> bytes:
            return b"404 page not found"

    class Connection:
        def __init__(self, _host: str, _port: int, timeout: int) -> None:
            assert timeout == 5

        @staticmethod
        def request(_method: str, _path: str) -> None:
            return None

        @staticmethod
        def getresponse() -> Response:
            return Response()

        @staticmethod
        def close() -> None:
            return None

    monkeypatch.setattr(cli_runtime, "HTTPConnection", Connection)

    config = InstallConfig(
        environment="production",
        deployment_profile="compact",
        runtime_http_port=8080,
    )

    with pytest.raises(CandidateReadinessProbePendingError, match="HTTP 404"):
        cli_runtime._candidate_health_payload(config)


@pytest.mark.parametrize(
    ("ready", "status"),
    ((True, "pass"), (False, "fail")),
)
def test_compact_candidate_readiness_adapts_aggregate_health_contract(
    monkeypatch: pytest.MonkeyPatch,
    ready: bool,
    status: str,
) -> None:
    monkeypatch.setattr(
        cli_runtime,
        "_candidate_health_payload",
        lambda _config: {"ready": ready},
    )

    payload = cli_runtime._app_runtime_readiness_payload(
        allow_lifecycle_mutation=True,
        config=InstallConfig(
            environment="production",
            deployment_profile="compact",
            runtime_http_port=8080,
        ),
    )

    assert payload == {
        "checks": [
            {
                "id": "compact_runtime",
                "category": "core",
                "title": "Compact runtime aggregate readiness",
                "status": status,
                "detail": (
                    "Compact runtime reports ready."
                    if ready
                    else "Compact runtime is still starting or unhealthy."
                ),
                "remediation": "",
            }
        ]
    }


def test_compact_candidate_readiness_combines_aggregate_and_app_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli_runtime,
        "_candidate_health_payload",
        lambda _config: {"ready": True},
    )

    payload = cli_runtime._app_runtime_readiness_payload(
        allow_lifecycle_mutation=True,
        config=InstallConfig(
            environment="production",
            deployment_profile="compact",
            runtime_http_port=8080,
        ),
        compact_app_probe=lambda: {
            "ready": False,
            "checks": {
                "security_posture": {"ok": False, "error": "Security posture is stale."},
                "workflow_knowledge_sync": {
                    "ok": False,
                    "error": "Workflow knowledge is stale.",
                },
                "stuck_jobs": {
                    "ok": False,
                    "persisted_count": 1,
                    "detection_count": 0,
                    "max_allowed": 0,
                    "error": "",
                },
            },
        },
    )

    assert [check["id"] for check in payload["checks"]] == [
        "compact_runtime",
        "security_posture",
        "workflow_knowledge_sync",
    ]
    assert payload["checks"][0]["status"] == "pass"
    assert payload["checks"][1]["status"] == "fail"
    assert payload["checks"][2]["status"] == "fail"


def test_compact_candidate_readiness_waits_for_aggregate_before_app_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli_runtime,
        "_candidate_health_payload",
        lambda _config: {"ready": False},
    )
    app_probe = MagicMock(side_effect=AssertionError("app probe must wait for the runtime"))

    payload = cli_runtime._app_runtime_readiness_payload(
        allow_lifecycle_mutation=True,
        config=InstallConfig(
            environment="production",
            deployment_profile="compact",
            runtime_http_port=8080,
        ),
        compact_app_probe=app_probe,
    )

    assert payload["checks"][0]["id"] == "compact_runtime"
    assert payload["checks"][0]["status"] == "fail"
    app_probe.assert_not_called()


def test_maintenance_health_uses_app_readiness_not_installer_doctor() -> None:
    issues = _MaintenanceHealthAdapter(
        lambda: {"ready": True, "checks": {"postgres": {"ok": True}}}
    ).mandatory_health_issues()

    assert issues == ()


def test_maintenance_health_fails_closed_with_app_check_detail() -> None:
    issues = _MaintenanceHealthAdapter(
        lambda: {
            "ready": False,
            "checks": {"rabbitmq": {"ok": False, "detail": "broker unavailable"}},
        }
    ).mandatory_health_issues()

    assert issues == ("broker unavailable",)


def test_maintenance_health_allows_isolated_stuck_jobs_before_resume() -> None:
    issues = _MaintenanceHealthAdapter(
        lambda: {
            "ready": False,
            "checks": {
                "postgres": {"ok": True},
                "stuck_jobs": {
                    "ok": False,
                    "detail": "1 persisted stuck job",
                    "persisted_count": 1,
                    "detection_count": 0,
                    "max_allowed": 0,
                    "error": "",
                },
            },
        },
        ignored_check_ids=frozenset({"stuck_jobs"}),
    ).mandatory_health_issues()

    assert issues == ()


@pytest.mark.parametrize(
    "stuck_check",
    [
        {
            "ok": False,
            "detail": "watchdog scan unavailable",
            "persisted_count": 0,
            "detection_count": 0,
            "max_allowed": 0,
            "error": "watchdog scan unavailable",
        },
        {
            "ok": False,
            "detail": "1 additional watchdog detection",
            "persisted_count": 0,
            "detection_count": 1,
            "max_allowed": 0,
            "error": "",
        },
        {"ok": False, "detail": "malformed watchdog result"},
    ],
)
def test_maintenance_health_keeps_unverified_stuck_job_failures(stuck_check) -> None:
    issues = _MaintenanceHealthAdapter(
        lambda: {"ready": False, "checks": {"stuck_jobs": stuck_check}},
        ignored_check_ids=frozenset({"stuck_jobs"}),
    ).mandatory_health_issues()

    assert issues == (stuck_check["detail"],)


@pytest.mark.parametrize(
    "stuck_check",
    [
        {
            "ok": False,
            "status": "fail",
            "persisted_count": 0,
            "detection_count": 0,
            "max_allowed": 0,
            "error": "watchdog scan unavailable",
        },
        {
            "ok": False,
            "status": "fail",
            "persisted_count": 0,
            "detection_count": 1,
            "max_allowed": 0,
            "error": "",
        },
        {"ok": False, "status": "fail"},
    ],
)
def test_lifecycle_readiness_keeps_unverified_stuck_job_failures(
    monkeypatch: pytest.MonkeyPatch,
    stuck_check,
) -> None:
    monkeypatch.setattr(
        cli_runtime,
        "_maintenance_health_payload",
        lambda: {"ready": False, "checks": {"stuck_jobs": stuck_check}},
    )

    payload = cli_runtime._app_runtime_readiness_payload(allow_lifecycle_mutation=True)

    assert payload["checks"] == [
        {
            "id": "stuck_jobs",
            "category": "core",
            "title": "stuck_jobs",
            "status": "fail",
            "detail": str(stuck_check.get("error") or ""),
            "remediation": "",
        }
    ]


def test_stack_recovery_gate_ignores_its_own_operation_and_allowed_persisted_stuck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The audited AC17-R24 deadlock state must not block its own recovery."""
    monkeypatch.setattr(
        cli_runtime,
        "_installed_runtime_environment",
        lambda: {"FS_DB_DSN": "postgresql://managed-file"},
    )
    monkeypatch.setattr(
        cli_runtime,
        "_maintenance_health_payload",
        lambda: {
            "ready": False,
            "checks": {
                "postgres": {"ok": True},
                "lifecycle_recovery": {
                    "ok": False,
                    "state": "recovery_required",
                    "unfinished_count": 1,
                    "recovery_required_count": 1,
                },
                "stuck_jobs": {
                    "ok": False,
                    "status": "fail",
                    "detail": "1 persisted stuck job(s); 0 additional watchdog detection(s).",
                    "persisted_count": 1,
                    "detection_count": 0,
                    "max_allowed": 0,
                    "error": "",
                },
            },
        },
    )
    runtime = CLIRuntime()
    runtime.__dict__["configuration_service"] = SimpleNamespace(
        read_active=lambda: InstallConfig(deployment_profile="scalable"),
    )

    report = runtime.stack_service.readiness_service.check()

    assert report.status == "ready"
    assert report.core_failed == 0
    assert {finding.check_id for finding in report.findings} == {"postgres"}


def test_stack_recovery_gate_still_blocks_on_any_other_core_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli_runtime,
        "_installed_runtime_environment",
        lambda: {"FS_DB_DSN": "postgresql://managed-file"},
    )
    monkeypatch.setattr(
        cli_runtime,
        "_maintenance_health_payload",
        lambda: {
            "ready": False,
            "checks": {
                "postgres": {"ok": False, "error": "database unreachable"},
                "lifecycle_recovery": {"ok": False, "state": "recovery_required"},
                "stuck_jobs": {
                    "ok": False,
                    "status": "fail",
                    "persisted_count": 1,
                    "detection_count": 0,
                    "max_allowed": 0,
                    "error": "",
                },
            },
        },
    )
    runtime = CLIRuntime()
    runtime.__dict__["configuration_service"] = SimpleNamespace(
        read_active=lambda: InstallConfig(deployment_profile="scalable"),
    )

    report = runtime.stack_service.readiness_service.check()

    assert report.status == "blocked"
    assert {finding.check_id for finding in report.findings} == {"postgres"}


def test_maintenance_reconciler_syncs_canonical_scope_and_records_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = object()

    class _Configuration:
        def read_active(self):
            return object()

        def derive_target(self, _config):
            return target

    class _Docker:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object]] = []

        def reconcile_security_posture(self, selected_target) -> None:
            self.calls.append(("security", selected_target))

        def reconcile_workflow_knowledge(self, selected_target) -> None:
            self.calls.append(("workflow", selected_target))

    docker = _Docker()
    payloads = iter(({"ready": False}, {"ready": True}))
    cli_runtime._MaintenanceReadinessReconciler(
        configuration=_Configuration(),
        docker=docker,
        readiness_probe=lambda _config: next(payloads),
    ).reconcile()

    assert docker.calls == [("security", target), ("workflow", target)]


def test_maintenance_reconciler_skips_healthy_derived_checks_for_persisted_stuck_job() -> None:
    target = object()

    class _Configuration:
        def read_active(self):
            return object()

        def derive_target(self, _config):
            return target

    class _Docker:
        def reconcile_security_posture(self, _target) -> None:
            pytest.fail("healthy security posture must not be reconciled")

        def reconcile_workflow_knowledge(self, _target) -> None:
            pytest.fail("healthy workflow knowledge must not be reconciled")

    probes = 0

    def readiness_probe(_config):
        nonlocal probes
        probes += 1
        return {
            "ready": False,
            "checks": {
                "security_posture": {"ok": True},
                "workflow_knowledge_sync": {"ok": True},
                "stuck_jobs": {
                    "ok": False,
                    "persisted_count": 1,
                    "detection_count": 0,
                    "max_allowed": 0,
                    "error": "",
                },
            },
        }

    cli_runtime._MaintenanceReadinessReconciler(
        configuration=_Configuration(),
        docker=_Docker(),
        readiness_probe=readiness_probe,
    ).reconcile()

    assert probes == 1


@pytest.mark.parametrize(
    ("security_ok", "workflow_ok", "expected_calls"),
    [
        (False, True, ["security"]),
        (True, False, ["workflow"]),
        (False, False, ["security", "workflow"]),
    ],
)
def test_maintenance_reconciler_selects_only_unhealthy_derived_checks(
    security_ok: bool,
    workflow_ok: bool,
    expected_calls: list[str],
) -> None:
    target = object()

    class _Configuration:
        def read_active(self):
            return object()

        def derive_target(self, _config):
            return target

    class _Docker:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def reconcile_security_posture(self, selected_target) -> None:
            assert selected_target is target
            self.calls.append("security")

        def reconcile_workflow_knowledge(self, selected_target) -> None:
            assert selected_target is target
            self.calls.append("workflow")

    docker = _Docker()
    payloads = iter(
        (
            {
                "ready": False,
                "checks": {
                    "security_posture": {"ok": security_ok},
                    "workflow_knowledge_sync": {"ok": workflow_ok},
                },
            },
            {"ready": True},
        )
    )
    cli_runtime._MaintenanceReadinessReconciler(
        configuration=_Configuration(),
        docker=docker,
        readiness_probe=lambda _config: next(payloads),
    ).reconcile()

    assert docker.calls == expected_calls


def test_maintenance_reconciler_does_not_repeat_pending_derived_reconciliation() -> None:
    target = object()

    class _Configuration:
        def read_active(self):
            return object()

        def derive_target(self, _config):
            return target

    class _Docker:
        def __init__(self) -> None:
            self.workflow_calls = 0

        def reconcile_security_posture(self, _target) -> None:
            pytest.fail("healthy security posture must not be reconciled")

        def reconcile_workflow_knowledge(self, selected_target) -> None:
            assert selected_target is target
            self.workflow_calls += 1

    unhealthy = {
        "ready": False,
        "checks": {
            "security_posture": {"ok": True},
            "workflow_knowledge_sync": {"ok": False},
        },
    }
    payloads = iter((unhealthy, unhealthy, {"ready": True}))
    docker = _Docker()
    cli_runtime._MaintenanceReadinessReconciler(
        configuration=_Configuration(),
        docker=docker,
        readiness_probe=lambda _config: next(payloads),
        sleep=lambda _seconds: None,
    ).reconcile()

    assert docker.workflow_calls == 1


def test_maintenance_reconciler_waits_for_async_sync_completion() -> None:
    target = object()
    config = object()

    class _Configuration:
        def read_active(self):
            return config

        def derive_target(self, _config):
            return target

    class _Docker:
        def reconcile_security_posture(self, _target) -> None:
            return None

        def reconcile_workflow_knowledge(self, _target) -> None:
            return None

    payloads = iter(({"ready": False}, {"ready": False}, {"ready": True}))
    sleeps: list[float] = []

    cli_runtime._MaintenanceReadinessReconciler(
        configuration=_Configuration(),
        docker=_Docker(),
        readiness_probe=lambda selected: next(payloads) if selected is config else {"ready": False},
        sleep=sleeps.append,
        timeout_seconds=5,
        poll_interval_seconds=0.25,
    ).reconcile()

    assert sleeps == [0.25]


def test_maintenance_reconciler_waits_through_transient_startup_refusals() -> None:
    target = object()
    config = object()

    class _Configuration:
        def read_active(self):
            return config

        def derive_target(self, _config):
            return target

    class _Docker:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def reconcile_security_posture(self, _target) -> None:
            self.calls.append("security")

        def reconcile_workflow_knowledge(self, _target) -> None:
            self.calls.append("workflow")

    attempts = 0

    def readiness_probe(selected):
        nonlocal attempts
        assert selected is config
        attempts += 1
        if attempts <= 17:
            raise ConnectionRefusedError("runtime is still binding")
        return {"ready": attempts >= 19}

    sleeps: list[float] = []
    docker = _Docker()
    cli_runtime._MaintenanceReadinessReconciler(
        configuration=_Configuration(),
        docker=docker,
        readiness_probe=readiness_probe,
        sleep=sleeps.append,
        timeout_seconds=15,
        poll_interval_seconds=1,
    ).reconcile(timeout_seconds=20)

    assert attempts == 19
    assert sleeps == [1.0] * 17
    assert docker.calls == ["security", "workflow"]


def test_maintenance_reconciler_bounds_and_retains_permanent_transport_failure() -> None:
    config = object()

    class _Configuration:
        def read_active(self):
            return config

        def derive_target(self, _config):
            return object()

    class _Docker:
        def reconcile_security_posture(self, _target) -> None:
            pytest.fail("reconciliation must wait for the first real response")

        def reconcile_workflow_knowledge(self, _target) -> None:
            pytest.fail("reconciliation must wait for the first real response")

    attempts = 0

    def readiness_probe(_selected):
        nonlocal attempts
        attempts += 1
        raise ConnectionRefusedError(f"refused attempt {attempts}")

    sleeps: list[float] = []
    reconciler = cli_runtime._MaintenanceReadinessReconciler(
        configuration=_Configuration(),
        docker=_Docker(),
        readiness_probe=readiness_probe,
        sleep=sleeps.append,
        timeout_seconds=30,
        poll_interval_seconds=0.5,
    )

    with pytest.raises(ConnectionRefusedError, match="refused attempt 5"):
        reconciler.reconcile(timeout_seconds=2)

    assert attempts == 5
    assert sleeps == [0.5] * 4


def test_maintenance_reconciler_bounds_and_retains_pending_compact_route() -> None:
    config = object()

    class _Configuration:
        def read_active(self):
            return config

        def derive_target(self, _config):
            return object()

    class _Docker:
        def reconcile_security_posture(self, _target) -> None:
            pytest.fail("reconciliation must wait for the first real response")

        def reconcile_workflow_knowledge(self, _target) -> None:
            pytest.fail("reconciliation must wait for the first real response")

    attempts = 0

    def readiness_probe(_selected):
        nonlocal attempts
        attempts += 1
        raise CandidateReadinessProbePendingError("health endpoint returned HTTP 404")

    sleeps: list[float] = []
    reconciler = cli_runtime._MaintenanceReadinessReconciler(
        configuration=_Configuration(),
        docker=_Docker(),
        readiness_probe=readiness_probe,
        sleep=sleeps.append,
        timeout_seconds=30,
        poll_interval_seconds=0.5,
    )

    with pytest.raises(CandidateReadinessProbePendingError, match="HTTP 404"):
        reconciler.reconcile(timeout_seconds=1)

    assert attempts == 3
    assert sleeps == [0.5, 0.5]


def test_maintenance_reconciler_does_not_invalidate_current_readiness() -> None:
    target = object()
    config = object()

    class _Configuration:
        def read_active(self):
            return config

        def derive_target(self, _config):
            return target

    class _Docker:
        def __init__(self) -> None:
            self.targets: list[object] = []

        def reconcile_security_posture(self, selected_target) -> None:
            self.targets.append(selected_target)

        def reconcile_workflow_knowledge(self, selected_target) -> None:
            self.targets.append(selected_target)

    docker = _Docker()
    cli_runtime._MaintenanceReadinessReconciler(
        configuration=_Configuration(),
        docker=docker,
        readiness_probe=lambda _config: {"ready": True},
    ).reconcile()

    assert docker.targets == []


@pytest.mark.parametrize(("maintenance_state", "expected_calls"), [(None, 0), (object(), 1)])
def test_stack_readiness_reconciliation_runs_only_while_maintenance_is_active(
    monkeypatch: pytest.MonkeyPatch,
    maintenance_state,
    expected_calls: int,
) -> None:
    class _StateGateway:
        def __init__(self, **_kwargs) -> None:
            return None

        def read(self):
            return maintenance_state

    class _Reconciler:
        calls = 0
        timeout_seconds: float | None = None

        def reconcile(self, *, timeout_seconds: float | None = None) -> None:
            self.calls += 1
            self.timeout_seconds = timeout_seconds

    monkeypatch.setattr(cli_runtime, "FilesystemMaintenanceGateway", _StateGateway)
    runtime = CLIRuntime()
    reconciler = _Reconciler()
    runtime.__dict__["maintenance_readiness_reconciler"] = reconciler

    runtime._reconcile_stack_readiness_if_maintenance(60)

    assert reconciler.calls == expected_calls
    assert reconciler.timeout_seconds == (60 if maintenance_state is not None else None)


def test_environment_runtime_composes_real_lifecycle_ports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli_runtime,
        "_installed_runtime_environment",
        lambda: {"FS_DB_DSN": "postgresql://managed-file"},
    )
    runtime = CLIRuntime()

    service = runtime.environment_service

    assert service.configuration is runtime.configuration_service
    assert service.runtime is runtime.configuration_service.gateway
    assert service.lifecycle_state.state_root == cli_runtime._REPO_ROOT / ".flow-steward"
    assert service.storage._project_root == cli_runtime._REPO_ROOT
    assert service.jobs._project_root == cli_runtime._REPO_ROOT
    assert service.storage._bundled_credentials_path == (
        cli_runtime._REPO_ROOT / ".flow-steward/runtime/seaweedfs-s3.json"
    )
    assert service.storage._bundled_probe_prepare is not None


def test_cli_and_api_share_canonical_extension_query_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.application.extensions import service_support
    from core.infrastructure.extension_runtime import extension_runtime_helpers

    install_store = ExtensionInstallStore()
    runtime_store = ExtensionRuntimeStore()
    install_store.install(
        extension_id="acme.data-only",
        version="1.0.0",
        account_id="acc_default",
    )
    monkeypatch.setattr(
        service_support,
        "get_default_extension_install_store",
        lambda **_kwargs: install_store,
    )
    monkeypatch.setattr(
        service_support,
        "get_default_extension_runtime_store",
        lambda **_kwargs: runtime_store,
    )
    monkeypatch.setattr(
        extension_runtime_helpers,
        "list_extension_runtime_records",
        lambda *, account_id: [],
    )

    runtime = CLIRuntime()
    monkeypatch.setattr(runtime, "get_identity_store", lambda: None)
    monkeypatch.setattr(runtime, "get_project_store", lambda: None)
    cli_service = runtime.extension_query_service
    api_service = CanonicalExtensionQueryService(
        get_logger=lambda: None,
        get_install_store=lambda: install_store,
        get_runtime_store=lambda: runtime_store,
    )
    scope = ScopeContext(account_id="acc_default", actor_id="u_cli")

    assert isinstance(cli_service, CanonicalExtensionQueryService)
    assert (
        [row.extension_id for row in cli_service.list_extensions(scope=scope).extensions]
        == [row.extension_id for row in api_service.list_extensions(scope=scope).extensions]
        == ["acme.data-only"]
    )


def test_cli_runtime_detail_uses_scoped_runtime_manifest_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.application.extensions import service_support
    from core.infrastructure.extension_runtime import extension_runtime_helpers

    install_store = ExtensionInstallStore()
    runtime_store = ExtensionRuntimeStore()
    release = runtime_store.create_release(
        {
            "release_id": "pr_cli_runtime",
            "extension_id": "acme.cli-runtime",
            "release_version": "1.0.0",
            "descriptor": {
                "extension_id": "acme.cli-runtime",
                "runtime": {"contract_version": "extension_host_v2"},
            },
        }
    )
    install_store.install(
        extension_id=release.extension_id,
        version=release.release_version,
        account_id="acc_cli",
        release_id=release.release_id,
    )
    requested_accounts: list[str | None] = []

    def resolve(extension_id: str, **kwargs):
        requested_accounts.append(kwargs.get("account_id"))
        return ({"extension_key": extension_id}, None)

    monkeypatch.setattr(
        service_support,
        "get_default_extension_install_store",
        lambda **_kwargs: install_store,
    )
    monkeypatch.setattr(
        service_support,
        "get_default_extension_runtime_store",
        lambda **_kwargs: runtime_store,
    )
    monkeypatch.setattr(
        extension_runtime_helpers,
        "resolve_extension_runtime_record",
        resolve,
    )
    monkeypatch.setattr(
        extension_runtime_helpers,
        "build_extension_compiled_manifest",
        lambda _extension: {
            "extension_id": "acme.cli-runtime",
            "ui_manifest": {"version": "1", "pages": [{"id": "cli"}]},
        },
    )

    runtime = CLIRuntime()
    monkeypatch.setattr(runtime, "get_identity_store", lambda: None)
    monkeypatch.setattr(runtime, "get_project_store", lambda: None)
    detail = runtime.extension_query_service.get_extension(
        "acme.cli-runtime",
        scope=ScopeContext(account_id="acc_cli", actor_id="u_cli"),
    )

    assert detail.compiled_manifest["ui_manifest"]["pages"] == [{"id": "cli"}]
    assert requested_accounts == ["acc_cli"]
