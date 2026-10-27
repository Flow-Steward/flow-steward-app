"""Lazy first-party service composition for the schema-driven CLI."""

from __future__ import annotations

import hashlib
import json
import math
import platform as host_platform
import sys
import time
from collections.abc import Callable, Mapping
from functools import cached_property
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from typing import Any, cast

from core.application.auth.secrets import decrypt_secret
from core.application.clone.contracts import CloneDataClass
from core.application.installer import classify_installation
from core.application.installer.redaction import redact_installer_text
from core.application.lifecycle.backup_command_service import BackupCommandService
from core.application.lifecycle.backup_service import BackupService
from core.application.lifecycle.configuration_service import ConfigurationService
from core.application.lifecycle.contracts import (
    ComposeTarget,
    DeploymentConfig,
    LifecycleLockStatus,
)
from core.application.lifecycle.environment_service import EnvironmentService, StorageInventory
from core.application.lifecycle.errors import CandidateReadinessProbePendingError, LifecycleError
from core.application.lifecycle.preservation_service import PreservationService
from core.application.lifecycle.stack_service import StackService
from core.application.maintenance.contracts import MaintenanceState
from core.application.maintenance.ports import MaintenanceServicePort
from core.application.maintenance.service import MaintenanceService
from core.application.management.command_specs.registry import complete_operator_command
from core.application.readiness import CoreReadinessService
from core.application.readiness.contracts import ReadinessPrerequisiteMissing
from core.application.release.activation import ReleaseActivator
from core.application.release.catalog import GitHubReleaseDiscovery
from core.application.release.service import (
    ReleasePlatform,
    ReleaseService,
    ReleaseTransitionBinding,
    release_platform_for_machine,
)
from core.application.tooling.ops.deployment_doctor import run_doctor
from core.application.tooling.ops.deployment_doctor_common import DoctorOptions
from core.entrypoints.cli.maintenance_adapters import (
    _is_persisted_stuck_jobs_only,
    _MaintenanceActiveWorkAdapter,
    _MaintenanceHealthAdapter,
    _MaintenanceUnfinishedOperationsAdapter,
)
from core.infrastructure.config.connection_strings import resolve_postgres_dsn
from core.infrastructure.config.installer_config import installer_state_root
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.config.version import get_app_version
from core.infrastructure.installer.gateway import DockerInstallerGateway
from core.infrastructure.installer.readiness_probe import StackReadinessProbe
from core.infrastructure.installer.registry_image_resolver import OfficialReleaseResolver
from core.infrastructure.installer.release_handoff import InstallerReleaseHandoff
from core.infrastructure.installer.state_store import InstallerStateStore
from core.infrastructure.lifecycle.backup_gateway import FilesystemBackupGateway
from core.infrastructure.lifecycle.configuration_gateway import ConfigurationLifecycleGateway
from core.infrastructure.lifecycle.configuration_store import LifecycleConfigurationStore
from core.infrastructure.lifecycle.docker_gateway import (
    DockerLifecycleGateway,
    DockerPreservationGateway,
)
from core.infrastructure.lifecycle.job_quiescence_gateway import (
    LifecycleJobQuiescenceGateway,
    PostgresActiveJobsReader,
)
from core.infrastructure.lifecycle.maintenance_gateway import FilesystemMaintenanceGateway
from core.infrastructure.lifecycle.preservation_repository import (
    PostgresPreservationRepository,
    PreservationManifestStore,
)
from core.infrastructure.lifecycle.state_store import LifecycleStateStore
from core.infrastructure.lifecycle.storage_gateway import LifecycleStorageGateway
from core.infrastructure.release.github import GitHubJSONFetcher
from core.infrastructure.release.github_credentials import GitHubReleaseCredentialLoader
from core.infrastructure.release.installed_origin import InstalledReleaseOriginReader

_IMAGE_ROOT = Path(__file__).resolve().parents[3]
_REPO_ROOT = Path(str(_runtime_env.get("FS_DOCKER_HOST_PROJECT_ROOT") or _IMAGE_ROOT)).resolve()
_LINT_SCRIPT_PATH = _REPO_ROOT / "core/entrypoints/cli/scripts/lint.sh"
_FLOW_STEWARD_STATE_NAME = ".flow-steward"


_RUNTIME_MAINTENANCE_ROOT = Path("/run/flow-steward-runtime")
_HOST_DIAGNOSTIC_FIELDS = frozenset({"id", "category", "title", "status", "detail", "remediation"})
_MAX_HOST_DIAGNOSTICS_BYTES = 64 * 1024
_PROBE_BODY_EXCERPT = 200


def _runtime_health_payload(
    host: str,
    port: int,
    *,
    startup_route_pending: bool = False,
    timeout_seconds: float = 5,
) -> dict[str, Any]:
    if timeout_seconds <= 0:
        raise TimeoutError("Readiness probe timeout budget exhausted")
    connection = HTTPConnection(host, port, timeout=min(5, timeout_seconds))
    try:
        connection.request("GET", "/health/ready")
        response = connection.getresponse()
        raw = response.read(1024 * 1024 + 1)
        if startup_route_pending and response.status == 404:
            raise CandidateReadinessProbePendingError("health endpoint returned HTTP 404")
        if response.status not in {200, 503}:
            # AC17-R152: what the endpoint said, bounded and redacted, so a failed apply
            # records more than a status code once its candidate has been rolled back.
            body = redact_installer_text(raw[:_PROBE_BODY_EXCERPT].decode("utf-8", "replace"))
            body = " ".join(body.split())
            raise ValueError(
                f"health endpoint returned HTTP {response.status}" + (f": {body}" if body else "")
            )
        if len(raw) > 1024 * 1024:
            raise ValueError("health response exceeded the size limit")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("health response must be an object")
        return payload
    finally:
        connection.close()


def _maintenance_health_payload() -> dict[str, Any]:
    return _runtime_health_payload("fs-app", 8099)


def _candidate_health_payload(
    config: DeploymentConfig, *, timeout_seconds: float | None = None
) -> dict[str, Any]:
    if config.deployment_profile == "compact":
        if timeout_seconds is None:
            return _runtime_health_payload(
                "compact-runtime",
                config.runtime_http_port,
                startup_route_pending=True,
            )
        return _runtime_health_payload(
            "compact-runtime",
            config.runtime_http_port,
            startup_route_pending=True,
            timeout_seconds=timeout_seconds,
        )
    if timeout_seconds is not None:
        return _runtime_health_payload("fs-app", 8099, timeout_seconds=timeout_seconds)
    return _maintenance_health_payload()


def _app_health_checks(
    payload: Mapping[str, Any],
    *,
    allow_lifecycle_mutation: bool,
) -> list[dict[str, str]]:
    raw_checks = payload.get("checks")
    if not isinstance(raw_checks, Mapping):
        return [
            {
                "id": "api_ready",
                "category": "core",
                "title": "Application readiness",
                "status": "fail",
                "detail": "App readiness response did not contain checks.",
                "remediation": "Inspect the application readiness endpoint.",
            }
        ]
    checks: list[dict[str, str]] = []
    for check_id, value in raw_checks.items():
        if allow_lifecycle_mutation and check_id == "lifecycle_recovery":
            continue
        if allow_lifecycle_mutation and _is_persisted_stuck_jobs_only(check_id, value):
            continue
        if not isinstance(value, Mapping):
            checks.append(
                {
                    "id": str(check_id),
                    "category": "core",
                    "status": "fail",
                    "detail": "App readiness check returned an invalid result.",
                }
            )
            continue
        raw_status = str(value.get("status") or "").strip().lower()
        status = "pass" if value.get("ok") is True else "fail"
        if raw_status in {"pass", "warn", "fail"}:
            status = raw_status
        checks.append(
            {
                "id": str(value.get("id") or check_id),
                "category": str(value.get("category") or "core"),
                "title": str(value.get("title") or check_id),
                "status": status,
                "detail": str(value.get("detail") or value.get("error") or ""),
                "remediation": str(value.get("remediation") or ""),
            }
        )
    return checks


def _app_runtime_readiness_payload(
    *,
    allow_lifecycle_mutation: bool,
    config: DeploymentConfig | None = None,
    compact_app_probe: Callable[[], Mapping[str, Any]] | None = None,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    if timeout_seconds is None:
        payload = (
            _candidate_health_payload(config)
            if config is not None
            else _maintenance_health_payload()
        )
    elif config is not None:
        payload = _candidate_health_payload(config, timeout_seconds=timeout_seconds)
    else:
        payload = _runtime_health_payload("fs-app", 8099, timeout_seconds=timeout_seconds)
    raw_checks = payload.get("checks")
    if (
        config is not None
        and config.deployment_profile == "compact"
        and raw_checks is None
        and isinstance(payload.get("ready"), bool)
    ):
        ready = bool(payload["ready"])
        checks = [
            {
                "id": "compact_runtime",
                "category": "core",
                "title": "Compact runtime aggregate readiness",
                "status": "pass" if ready else "fail",
                "detail": (
                    "Compact runtime reports ready."
                    if ready
                    else "Compact runtime is still starting or unhealthy."
                ),
                "remediation": "",
            }
        ]
        if ready and compact_app_probe is not None:
            checks.extend(
                _app_health_checks(
                    compact_app_probe(),
                    allow_lifecycle_mutation=allow_lifecycle_mutation,
                )
            )
        return {"checks": checks}
    return {
        "checks": _app_health_checks(
            payload,
            allow_lifecycle_mutation=allow_lifecycle_mutation,
        )
    }


class _MaintenanceReadinessReconciler:
    def __init__(
        self,
        *,
        configuration: Any,
        docker: DockerLifecycleGateway,
        readiness_probe: Callable[[DeploymentConfig], Mapping[str, Any]] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        timeout_seconds: float = 15,
        poll_interval_seconds: float = 0.5,
    ) -> None:
        self._configuration = configuration
        self._docker = docker
        self._readiness_probe = readiness_probe or _candidate_health_payload
        self._sleep = sleep
        self._timeout_seconds = max(float(timeout_seconds), 0.0)
        self._poll_interval_seconds = max(float(poll_interval_seconds), 0.01)

    def reconcile(self, *, timeout_seconds: float | None = None) -> None:
        config = self._configuration.read_active()
        if config is None:
            raise RuntimeError("Installed configuration is unavailable")
        target = self._configuration.derive_target(config)
        timeout = (
            self._timeout_seconds if timeout_seconds is None else max(float(timeout_seconds), 0.0)
        )
        attempts = max(
            1,
            math.ceil(timeout / self._poll_interval_seconds) + 1,
        )
        reconciled: set[str] = set()
        last_transport_error: (
            OSError | HTTPException | CandidateReadinessProbePendingError | None
        ) = None
        for attempt in range(attempts):
            try:
                payload = self._readiness_probe(config)
            except (OSError, HTTPException, CandidateReadinessProbePendingError) as exc:
                last_transport_error = exc
            else:
                last_transport_error = None
                if payload.get("ready") is True:
                    return
                raw_checks = payload.get("checks")
                if isinstance(raw_checks, Mapping):
                    unhealthy = {
                        check_id
                        for check_id in ("security_posture", "workflow_knowledge_sync")
                        if check_id in raw_checks
                        and (
                            not isinstance(raw_checks[check_id], Mapping)
                            or raw_checks[check_id].get("ok") is not True
                        )
                    }
                    if not unhealthy:
                        return
                else:
                    unhealthy = {"security_posture", "workflow_knowledge_sync"}
                requested = False
                if "security_posture" in unhealthy and "security_posture" not in reconciled:
                    self._docker.reconcile_security_posture(target)
                    reconciled.add("security_posture")
                    requested = True
                if (
                    "workflow_knowledge_sync" in unhealthy
                    and "workflow_knowledge_sync" not in reconciled
                ):
                    self._docker.reconcile_workflow_knowledge(target)
                    reconciled.add("workflow_knowledge_sync")
                    requested = True
                if requested:
                    continue
            if attempt + 1 < attempts:
                self._sleep(self._poll_interval_seconds)
        if last_transport_error is not None:
            raise last_transport_error


def _docker_host_project_root() -> Path:
    raw = str(_runtime_env.get("FS_DOCKER_HOST_PROJECT_ROOT") or "").strip()
    if not raw:
        return _REPO_ROOT
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise RuntimeError("FS_DOCKER_HOST_PROJECT_ROOT must be an absolute path")
    return candidate.resolve()


def _launcher_host_diagnostics() -> tuple[dict[str, object], ...] | None:
    arguments = tuple(sys.argv[1:])
    if arguments.count("doctor") != 1 or any(
        argument
        not in {
            "doctor",
            "--json",
            "--plain",
            "--tui",
            "--no-color",
            "--verbose",
            "--non-interactive",
        }
        for argument in arguments
    ):
        return None
    if sys.stdin.isatty():
        return None
    payload = sys.stdin.read(_MAX_HOST_DIAGNOSTICS_BYTES + 1)
    if not payload:
        return None
    try:
        if len(payload.encode("utf-8")) > _MAX_HOST_DIAGNOSTICS_BYTES:
            raise ValueError("host diagnostic payload is too large")
        decoded = json.loads(payload)
        if not isinstance(decoded, list) or not 1 <= len(decoded) <= 32:
            raise ValueError("host diagnostic payload must be a bounded list")
        findings: list[dict[str, object]] = []
        for finding in decoded:
            if not isinstance(finding, dict) or set(finding) != _HOST_DIAGNOSTIC_FIELDS:
                raise ValueError("host diagnostic finding fields are invalid")
            if finding["status"] not in {"pass", "warn", "fail"}:
                raise ValueError("host diagnostic status is invalid")
            if any(
                not isinstance(finding[field], str) or len(finding[field]) > 4096
                for field in _HOST_DIAGNOSTIC_FIELDS
            ):
                raise ValueError("host diagnostic values are invalid")
            findings.append(dict(finding))
        return tuple(findings)
    except ValueError:
        return (
            {
                "id": "host_diagnostics",
                "category": "core",
                "title": "Host diagnostics",
                "status": "fail",
                "detail": "Launcher host diagnostics could not be transferred safely.",
                "remediation": "Rerun ./flow-steward doctor from the installation host.",
            },
        )


def _stack_readiness_probe(configuration_service: ConfigurationService) -> StackReadinessProbe:
    state_root = installer_state_root(_REPO_ROOT)
    state = InstallerStateStore(state_root)
    gateway = DockerInstallerGateway(repo_root=_REPO_ROOT, state_store=state)
    launcher_host_diagnostics = _launcher_host_diagnostics()
    return StackReadinessProbe(
        configuration_service=configuration_service,
        doctor=run_doctor,
        doctor_options_factory=DoctorOptions,
        repo_root=_REPO_ROOT,
        host_diagnostics=(
            (lambda: launcher_host_diagnostics) if launcher_host_diagnostics is not None else None
        ),
        installation_classification=lambda: classify_installation(gateway.inspect()),
        installation_journal=state.load_journal,
        installer_state_root=state_root,
        active_lifecycle_operation=lambda: _active_lifecycle_operation(
            LifecycleStateStore(_REPO_ROOT)
        ),
        complete_command=complete_operator_command,
    )


def _active_lifecycle_operation(state: LifecycleStateStore) -> str | None:
    """The operation holding the lifecycle lock, if one is alive (W1)."""
    lock = state.diagnose_active_lock()
    return lock.operation_id if lock.status is LifecycleLockStatus.ACTIVE else None


class _DeferredBackupService:
    """Resolve installed Compose identity only when a backup is requested."""

    def __init__(self, factory: Callable[[], BackupService]) -> None:
        self._factory = factory

    def create(self, request: object, **operation_sinks: object) -> object:
        return self._factory().create(cast(Any, request), **operation_sinks)

    def list(self, request: object) -> object:
        return self._factory().list(cast(Any, request))

    def verify(self, request: object, **operation_sinks: object) -> object:
        return self._factory().verify(cast(Any, request), **operation_sinks)

    def find_verified(self, operation_id: str) -> object | None:
        return self._factory().find_verified(operation_id)

    def clone_dump(self, operation_id: str) -> object:
        return self._factory().clone_dump(operation_id)

    def restore_clone_dump(self, source: Path, *, recovery_target: str) -> None:
        self._factory().restore_clone_dump(source, recovery_target=recovery_target)


def _release_platform() -> ReleasePlatform:
    return release_platform_for_machine(host_platform.machine())


def _lifecycle_configuration_store() -> LifecycleConfigurationStore:
    configured_state_root = str(_runtime_env.get("FS_CONFIGURATION_STATE_ROOT") or "").strip()
    return LifecycleConfigurationStore(
        _REPO_ROOT,
        state_root=(Path(configured_state_root) if configured_state_root else None),
    )


def _installed_runtime_environment() -> dict[str, str]:
    """Read the installer-owned active environment without exporting it."""
    store = _lifecycle_configuration_store()
    active = store.read_active()
    if active is None:
        return {}
    return store.read_durable_environment(active)


def _runtime_dsn() -> str:
    environment = _installed_runtime_environment()
    dsn = str(environment.get("FS_OBSERVABILITY_DSN") or "").strip()
    if not dsn:
        dsn = resolve_postgres_dsn(environment).strip()
    if not dsn:
        dsn = resolve_postgres_dsn(_runtime_env).strip()
    if not dsn:
        raise RuntimeError("No configured database connection is available")
    return dsn


def _installed_required_secret(name: str) -> str:
    value = str(_installed_runtime_environment().get(name) or "").strip()
    if not value:
        raise RuntimeError(f"{name} is required in the installed environment")
    return value


def _decrypt_installed_control(ciphertext: str) -> str:
    raw_key = _installed_required_secret("ENCRYPTION_KEY")
    master_key = hashlib.sha256(raw_key.encode()).digest()
    return decrypt_secret(ciphertext, master_key=master_key)


class LifecycleCLIRuntime:
    """Assemble only installed-stack lifecycle services for retained fs-cli."""

    def _lifecycle_state(self) -> LifecycleStateStore:
        """The lifecycle lock and journals; Compact adds the installer's host lock."""
        return LifecycleStateStore(
            _REPO_ROOT,
            installation_lock_path=installer_state_root(_REPO_ROOT) / "install.lock",
        )

    def _clone_compact_data_root(self) -> Path:
        return Path("/data")

    def _clone_state_root(self) -> Path:
        return _REPO_ROOT / _FLOW_STEWARD_STATE_NAME

    def _clone_workspace_root(self) -> Path:
        return self._clone_state_root() / "clone"

    def _clone_data_roots(
        self,
        config: DeploymentConfig,
        _environment: Mapping[str, str],
    ) -> dict[CloneDataClass, Path]:
        if config.data_placement == "compact_volume":
            data_root = self._clone_compact_data_root()
            return {
                CloneDataClass.LOCAL_ARTIFACTS: data_root / "artifacts",
                CloneDataClass.EXTENSIONS: data_root / "extensions",
                CloneDataClass.DURABLE_STATE: data_root / "state",
            }
        if config.deployment_profile == "compact":
            data_root = self._clone_compact_data_root()
            return {
                CloneDataClass.LOCAL_ARTIFACTS: data_root / "artifacts",
                CloneDataClass.EXTENSIONS: data_root / "extensions",
                CloneDataClass.DURABLE_STATE: self._clone_state_root(),
            }
        return {
            CloneDataClass.LOCAL_ARTIFACTS: Path("/app/logs/artifacts"),
            CloneDataClass.EXTENSIONS: Path("/app/extensions"),
            CloneDataClass.DURABLE_STATE: self._clone_state_root(),
        }

    def _clone_shell_filesystem_roots(
        self,
        config: DeploymentConfig,
        environment: Mapping[str, str],
    ) -> dict[str, str]:
        """Return target-host identities for review/digest, never container I/O paths."""
        if config.data_placement == "compact_volume":
            return {
                CloneDataClass.LOCAL_ARTIFACTS.value: "volume:fs-compact-data/artifacts",
                CloneDataClass.EXTENSIONS.value: "volume:fs-compact-data/extensions",
                CloneDataClass.DURABLE_STATE.value: "volume:fs-compact-data/state",
            }
        # Preserve Compose's long-standing host defaults for installations created
        # before these identities were persisted explicitly.  The launcher supplies
        # the host project root, so these are host paths rather than container paths.
        host_project_root = Path(
            str(environment.get("FS_DOCKER_HOST_PROJECT_ROOT") or _REPO_ROOT)
        ).resolve()
        compose_root = host_project_root / "infra/docker"

        def host_path(name: str, fallback: Path) -> str:
            selected = Path(str(environment.get(name) or fallback)).expanduser()
            if not selected.is_absolute():
                selected = compose_root / selected
            return str(selected.resolve())

        extensions = host_path("FS_EXTENSIONS_DIR", host_project_root / "extensions")
        logs = host_path("FS_LOGS_DIR", host_project_root / "logs")
        state = host_path("FS_INSTALLER_STATE_DIR", host_project_root / _FLOW_STEWARD_STATE_NAME)
        if config.deployment_profile == "compact":
            # Mixed placement retains runtime state in the Compact data volume,
            # independently of the host installation's journal and configuration.
            state = "volume:fs-compact-data/state/.flow-steward"
        return {
            CloneDataClass.LOCAL_ARTIFACTS.value: str(Path(logs) / "artifacts"),
            CloneDataClass.EXTENSIONS.value: extensions,
            CloneDataClass.DURABLE_STATE.value: state,
        }

    def _clone_output_root(
        self,
        config: DeploymentConfig,
        _environment: Mapping[str, str],
    ) -> Path:
        if config.deployment_profile == "compact" or config.data_placement == "compact_volume":
            return installer_state_root(_REPO_ROOT) / "clone/outbox"
        return Path("/app/backups")

    def _clone_input_root(
        self,
        config: DeploymentConfig,
        _environment: Mapping[str, str],
    ) -> Path:
        if config.deployment_profile == "compact" or config.data_placement == "compact_volume":
            return installer_state_root(_REPO_ROOT) / "clone/inbox"
        return Path("/app/backups")

    @cached_property
    def clone_service(self) -> Any:
        from core.entrypoints.cli.clone_composition import build_installed_clone_service

        return build_installed_clone_service(
            self,
            repo_root=_REPO_ROOT,
            environment_loader=_installed_runtime_environment,
            platform_machine=host_platform.machine,
        )

    @cached_property
    def database_tooling_service(self) -> Any:
        from core.application.tooling.db.service import DatabaseToolingService

        return DatabaseToolingService(
            project_root=_REPO_ROOT,
            dsn_provider=self._configured_platform_dsn,
        )

    def explicit_config_readiness(
        self,
        config: DeploymentConfig,
        *,
        allow_release_transition: bool = False,
    ) -> dict[str, Any]:
        """Run raw explicit-config readiness inside the retained CLI runtime."""
        payload = _stack_readiness_probe(self.configuration_service).check_config(
            config,
            allow_initial_auth_bootstrap=True,
            allow_lifecycle_mutation=True,
            allow_release_transition=allow_release_transition,
        )
        return {"checks": payload.get("checks")}

    @cached_property
    def configuration_service(self) -> ConfigurationService:
        service = ConfigurationService(
            store=_lifecycle_configuration_store(),
            lifecycle_state=self._lifecycle_state(),
            docker_host_project_root=_docker_host_project_root(),
        )
        readiness_probe = _stack_readiness_probe(service)
        readiness = CoreReadinessService(probe=readiness_probe)
        self.__dict__.setdefault("core_readiness_service", readiness)
        service.gateway = ConfigurationLifecycleGateway(
            project_root=_REPO_ROOT,
            docker=self.docker_lifecycle_gateway,
            preservation=self.preservation_service,
            manifest_store=PreservationManifestStore(_REPO_ROOT),
            readiness_service=readiness,
            release_provider=get_app_version,
            candidate_readiness=lambda config: CoreReadinessService(
                probe=lambda: _app_runtime_readiness_payload(
                    allow_lifecycle_mutation=True,
                    config=config,
                    compact_app_probe=lambda: self.docker_lifecycle_gateway.compact_app_readiness(
                        service.derive_target(config)
                    ),
                )
            ).check(),
        )
        return service

    @cached_property
    def docker_lifecycle_gateway(self) -> DockerLifecycleGateway:
        return DockerLifecycleGateway(
            project_root=_REPO_ROOT,
            docker_host_project_root=_docker_host_project_root(),
        )

    @cached_property
    def stack_service(self) -> StackService:
        return StackService(
            configuration_service=self.configuration_service,
            docker_gateway=self.docker_lifecycle_gateway,
            lifecycle_state=self._lifecycle_state(),
            preservation_service=self.preservation_service,
            manifest_store=PreservationManifestStore(_REPO_ROOT),
            release_provider=get_app_version,
            readiness_service=self.lifecycle_mutation_readiness_service,
            readiness_reconciler=self._reconcile_stack_readiness_if_maintenance,
            storage_connectivity_provider=self._stack_storage_connectivity,
        )

    @cached_property
    def backup_service(self) -> BackupService:
        """Return the lock-free internal backup port used by composite operations."""
        return cast(BackupService, _DeferredBackupService(self._build_backup_service))

    @cached_property
    def backup_command_service(self) -> BackupCommandService:
        """Return the lifecycle-coordinated public backup command surface."""
        return BackupCommandService(
            backup=self.backup_service,
            lifecycle_state=self._lifecycle_state(),
        )

    def _build_backup_service(self) -> BackupService:
        active = self.configuration_service.read_active()
        if active is None:  # pragma: no cover - required read invariant
            raise RuntimeError("Installed configuration is unavailable.")
        target = self.configuration_service.derive_target(active)
        return BackupService(
            gateway=FilesystemBackupGateway.for_compose_target(
                project_root=_REPO_ROOT,
                docker_host_project_root=_docker_host_project_root(),
                target=target,
                key_source=lambda: _installed_required_secret("FS_BACKUP_ENCRYPTION_KEY"),
            )
        )

    @cached_property
    def release_service(self) -> ReleaseService:
        active = self.configuration_service.read_active(required=False)
        profile = active.deployment_profile if active is not None else "scalable"
        durable_environment = (
            self.configuration_service.store.read_durable_environment(active)
            if active is not None
            else {}
        )
        release_environment = dict(_runtime_env)
        release_environment.update(durable_environment)
        process_release_repository = str(
            _runtime_env.get("FS_RELEASE_REPOSITORY") or "Flow-Steward/flow-steward-app"
        )
        release_repository = str(
            durable_environment.get("FS_RELEASE_REPOSITORY") or process_release_repository
        )
        state = InstallerStateStore(
            installer_state_root(_REPO_ROOT),
            lifecycle_state_root=_REPO_ROOT / _FLOW_STEWARD_STATE_NAME,
        )
        fetcher = GitHubJSONFetcher(GitHubReleaseCredentialLoader(state.root).load)
        discovery = GitHubReleaseDiscovery(
            repository=release_repository,
            fetch_json=fetcher.fetch,
        )
        source_provenance = DockerInstallerGateway(
            repo_root=_REPO_ROOT,
            state_store=state,
            configuration_service=self.configuration_service,
            environment=release_environment,
        )
        origin_reader = InstalledReleaseOriginReader(
            state=state,
            configuration=self.configuration_service.store,
            source_provenance=source_provenance,
            platform=_release_platform,
        )

        def revalidate_transition(binding: ReleaseTransitionBinding) -> None:
            if origin_reader.load() != binding.installed_origin:
                raise LifecycleError.stable_failure(
                    code="release_upgrade_state_changed",
                    cause="Installed source origin changed after the upgrade review.",
                    remediation="Run release upgrade again and review the new plan.",
                    step="release_policy",
                )

        return ReleaseService(
            discovery=discovery,
            state=state,
            resolver=OfficialReleaseResolver(),
            maintenance=self.maintenance_service,
            activator=ReleaseActivator(
                InstallerReleaseHandoff(
                    repo_root=_REPO_ROOT,
                    state=state,
                    source_repository=release_repository,
                    source_registries=str(_runtime_env.get("FS_RELEASE_REGISTRIES") or ""),
                    transition_revalidator=revalidate_transition,
                )
            ),
            profile=profile,
            platform=_release_platform(),
            origin=origin_reader,
        )

    @cached_property
    def maintenance_service(self) -> MaintenanceServicePort:
        return MaintenanceService(
            state=FilesystemMaintenanceGateway(
                project_root=_REPO_ROOT,
                runtime_root=_RUNTIME_MAINTENANCE_ROOT,
                state_model=MaintenanceState,
                lifecycle_error=LifecycleError,
            ),
            active_work=_MaintenanceActiveWorkAdapter(self.lifecycle_job_quiescence_gateway),
            unfinished_operations=_MaintenanceUnfinishedOperationsAdapter(self._lifecycle_state()),
            health=_MaintenanceHealthAdapter(
                _maintenance_health_payload,
                ignored_check_ids=frozenset({"stuck_jobs"}),
            ),
            readiness_reconciler=self.maintenance_readiness_reconciler,
        )

    @cached_property
    def maintenance_readiness_reconciler(self) -> _MaintenanceReadinessReconciler:
        return _MaintenanceReadinessReconciler(
            configuration=self.configuration_service,
            docker=self.docker_lifecycle_gateway,
        )

    def _reconcile_stack_readiness_if_maintenance(self, timeout_seconds: float) -> None:
        state = FilesystemMaintenanceGateway(
            project_root=_REPO_ROOT,
            runtime_root=_RUNTIME_MAINTENANCE_ROOT,
            state_model=MaintenanceState,
            lifecycle_error=LifecycleError,
        ).read()
        if state is not None:
            self.maintenance_readiness_reconciler.reconcile(timeout_seconds=timeout_seconds)

    @cached_property
    def lifecycle_storage_gateway(self) -> LifecycleStorageGateway:
        return LifecycleStorageGateway(
            project_root=_REPO_ROOT,
            compose_project_provider=lambda config: (
                self.configuration_service.derive_target(config).project_name
            ),
            bundled_probe_prepare=lambda config: (
                self.docker_lifecycle_gateway.prepare_bundled_storage_probe(
                    self.configuration_service.derive_target(config)
                )
            ),
            bundled_write_probe=lambda config: self.docker_lifecycle_gateway.probe_bundled_storage(
                self.configuration_service.derive_target(config)
            ),
            bundled_inventory=lambda config: StorageInventory.model_validate(
                {
                    "backend": "bundled",
                    "source_identity": (
                        "volume:"
                        + self.configuration_service.derive_target(config).project_name
                        + "_seaweedfs-data"
                    ),
                    **self.docker_lifecycle_gateway.bundled_storage_inventory(
                        self.configuration_service.derive_target(config)
                    ),
                }
            ),
        )

    @cached_property
    def lifecycle_job_quiescence_gateway(self) -> LifecycleJobQuiescenceGateway:
        return LifecycleJobQuiescenceGateway(
            project_root=_REPO_ROOT,
            active_jobs=PostgresActiveJobsReader(self._configured_platform_dsn()),
        )

    @cached_property
    def environment_service(self) -> EnvironmentService:
        gateway = self.configuration_service.gateway
        if gateway is None:  # pragma: no cover - composition invariant
            raise RuntimeError("Environment lifecycle gateway is unavailable.")
        return EnvironmentService(
            configuration=cast(Any, self.configuration_service),
            jobs=self.lifecycle_job_quiescence_gateway,
            backup=self.backup_service,
            storage=self.lifecycle_storage_gateway,
            runtime=cast(Any, gateway),
            lifecycle_state=self._lifecycle_state(),
            release_provider=get_app_version,
        )

    @cached_property
    def preservation_service(self) -> PreservationService:
        repository = PostgresPreservationRepository.from_dsn(
            dsn=self._configured_platform_dsn(),
            decrypt_control=_decrypt_installed_control,
        )
        return PreservationService(
            repository=cast(Any, repository),
            docker_gateway=cast(Any, DockerPreservationGateway(project_root=_REPO_ROOT)),
        )

    def _stack_storage_connectivity(
        self,
        config: DeploymentConfig,
        target: ComposeTarget,
    ) -> bool:
        del config
        return self.docker_lifecycle_gateway.storage_connectivity(target)

    @cached_property
    def core_readiness_service(self) -> CoreReadinessService:
        return CoreReadinessService(probe=_stack_readiness_probe(self.configuration_service))

    @cached_property
    def lifecycle_mutation_readiness_service(self) -> CoreReadinessService:
        return CoreReadinessService(probe=self._lifecycle_mutation_readiness_payload)

    def _lifecycle_mutation_readiness_payload(self) -> dict[str, Any]:
        config = self.configuration_service.read_active()
        if config is None:  # pragma: no cover - required installed-runtime invariant
            raise RuntimeError("Installed configuration is unavailable.")
        return _app_runtime_readiness_payload(
            allow_lifecycle_mutation=True,
            config=config,
            compact_app_probe=lambda: self.docker_lifecycle_gateway.compact_app_readiness(
                self.configuration_service.derive_target(config)
            ),
        )

    def _configured_platform_dsn(self) -> str:
        environment = _installed_runtime_environment()
        dsn = str(environment.get("FS_OBSERVABILITY_DSN") or "").strip()
        if not dsn:
            dsn = resolve_postgres_dsn(environment).strip()
        if not dsn:
            dsn = resolve_postgres_dsn(_runtime_env).strip()
        if not dsn:
            raise ReadinessPrerequisiteMissing(
                "No configured database connection is available on this host.",
                remediation=(
                    "Run the installer on this host, or set FS_OBSERVABILITY_DSN to "
                    "the installed stack's database before running lifecycle commands."
                ),
            )
        return dsn


__all__ = ["LifecycleCLIRuntime"]
