"""Dockerless service composition for Compact maintenance commands."""

from __future__ import annotations

import json
from functools import cached_property
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import urlsplit

from psycopg import ProgrammingError
from psycopg.conninfo import conninfo_to_dict

from core.application.lifecycle.backup_service import BackupService
from core.application.lifecycle.errors import LifecycleError
from core.application.maintenance.compact_client import CompactMaintenanceClient
from core.application.maintenance.contracts import MaintenanceState
from core.application.maintenance.ports import MaintenanceServicePort
from core.application.maintenance.service import MaintenanceService
from core.application.readiness import CoreReadinessService
from core.entrypoints.cli.lifecycle_runtime import (
    _REPO_ROOT as _LIFECYCLE_ROOT,
)
from core.entrypoints.cli.lifecycle_runtime import (
    LifecycleCLIRuntime,
    _app_health_checks,
    _runtime_health_payload,
)
from core.entrypoints.cli.maintenance_adapters import (
    _MaintenanceActiveWorkAdapter,
    _MaintenanceUnfinishedOperationsAdapter,
)
from core.infrastructure.config.installer_config import installer_state_root
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.lifecycle.backup_gateway import FilesystemBackupGateway, ProcessRunner
from core.infrastructure.lifecycle.job_quiescence_gateway import (
    LifecycleJobQuiescenceGateway,
    PostgresActiveJobsReader,
)
from core.infrastructure.lifecycle.maintenance_gateway import FilesystemMaintenanceGateway
from core.infrastructure.lifecycle.state_store import LifecycleStateStore

_DATA_ROOT = Path("/data")
_STATE_PROJECT_ROOT = _DATA_ROOT / "state"
_STATE_ROOT = _STATE_PROJECT_ROOT / ".flow-steward"
# AC17-R162: the lock ./install.sh holds, in the installation's host state that the
# compose file mounts here read-only (fs-cli, FS_CONFIGURATION_STATE_ROOT).
_INSTALLATION_LOCK = Path("/run/flow-steward-install-state/operations/active.lock")


def _runtime_http_port() -> int:
    raw = str(_runtime_env.get("FS_RUNTIME_HTTP_PORT") or "8080")
    if not raw.isascii() or not raw.isdecimal():
        raise RuntimeError("Compact runtime HTTP port is invalid")
    port = int(raw)
    if not 1 <= port <= 65_535:
        raise RuntimeError("Compact runtime HTTP port is invalid")
    return port


def _compact_health_payload() -> dict[str, object]:
    configured = str(
        _runtime_env.get("FS_COMPACT_HEALTH_URL") or "http://127.0.0.1:9100/health/details"
    ).strip()
    target = urlsplit(configured)
    try:
        port = target.port
    except ValueError as exc:
        raise RuntimeError("Compact health URL is invalid") from exc
    host = target.hostname
    local_health = host == "127.0.0.1" and port == 9100 and target.path == "/health/details"
    sidecar_health = (
        host == "compact-runtime"
        and port == _runtime_http_port()
        and target.path == "/health/ready"
    )
    if (
        host is None
        or target.scheme != "http"
        or not (local_health or sidecar_health)
        or target.username is not None
        or target.password is not None
        or target.query
        or target.fragment
    ):
        raise RuntimeError("Compact health URL is invalid")
    connection = HTTPConnection(host, port, timeout=5)
    try:
        connection.request("GET", target.path)
        response = connection.getresponse()
        raw = response.read(1024 * 1024 + 1)
        if response.status not in {200, 503} or len(raw) > 1024 * 1024:
            raise RuntimeError(f"Compact health returned HTTP {response.status}")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError("Compact health returned an invalid payload")
        return payload
    finally:
        connection.close()


def _readiness_payload() -> dict[str, object]:
    try:
        payload = _compact_health_payload()
    except (OSError, RuntimeError, UnicodeError, json.JSONDecodeError) as exc:
        return {
            "checks": [
                {
                    "id": "compact_runtime",
                    "category": "core",
                    "title": "Compact runtime",
                    "status": "fail",
                    "detail": f"Compact runtime health probe failed ({type(exc).__name__}).",
                    "remediation": "Inspect the Compact container health and role logs.",
                }
            ]
        }
    ready = payload.get("ready") is True
    checks: list[dict[str, str]] = [
        {
            "id": "compact_runtime",
            "category": "core",
            "title": "Compact runtime",
            "status": "pass" if ready else "fail",
            "detail": (
                "All Compact roles and dependencies are ready."
                if ready
                else "At least one Compact role, worker, or dependency is not ready."
            ),
            "remediation": "Inspect /health/details inside the Compact container.",
        }
    ]
    if ready and not str(_runtime_env.get("FS_COMPACT_HEALTH_URL") or "").strip():
        # AC17-R164: the aggregate is built from role liveness, so the application's own
        # readiness -- a stale graph catalogue among it -- never reached ./install.sh on
        # Compact, which then reported ready over a broken graph search. Inside the
        # runtime (the installer's probe runs there) the application answers locally, as
        # Scalable's installer asks fs-app.
        checks.extend(_compact_application_checks())
    return {"checks": checks}


def _compact_application_checks() -> list[dict[str, str]]:
    try:
        payload = _runtime_health_payload("127.0.0.1", 8099)
    except (OSError, ValueError) as exc:
        return [
            {
                "id": "api_ready",
                "category": "core",
                "title": "Application readiness",
                "status": "fail",
                "detail": f"The application readiness probe failed ({type(exc).__name__}).",
                "remediation": "Inspect /health/ready inside the Compact container.",
            }
        ]
    return _app_health_checks(payload, allow_lifecycle_mutation=False)


class _CompactMaintenanceHealth:
    def mandatory_health_issues(self) -> tuple[str, ...]:
        try:
            return () if _compact_health_payload().get("ready") is True else ("runtime not ready",)
        except (OSError, RuntimeError, UnicodeError, json.JSONDecodeError) as exc:
            return (f"Compact health probe failed ({type(exc).__name__})",)


class _CompactReadinessReconciler:
    def reconcile(self) -> None:
        return None


def _native_backup_gateway(*, runner: ProcessRunner | None = None) -> FilesystemBackupGateway:
    raw_dsn = str(_runtime_env.get("FS_DB_DSN") or _runtime_env.get("DATABASE_URL") or "").strip()
    try:
        connection = conninfo_to_dict(raw_dsn)
        database_port = int(connection.get("port") or 5432)
    except (ProgrammingError, TypeError, ValueError) as exc:
        raise RuntimeError("Compact backup requires a complete FS_DB_DSN") from exc
    database_host = str(connection.get("host") or "")
    database_name = str(connection.get("dbname") or "")
    if not database_host or not database_name:
        raise RuntimeError("Compact backup requires a complete FS_DB_DSN")
    return FilesystemBackupGateway.for_native_database(
        project_root=_DATA_ROOT,
        database_host=database_host,
        database_port=database_port,
        database_user=str(connection.get("user") or ""),
        database_name=database_name,
        database_password=str(connection.get("password") or ""),
        staging_root=installer_state_root(_LIFECYCLE_ROOT) / "clone/database-staging",
        logs_root=installer_state_root(_LIFECYCLE_ROOT) / "clone/database-logs",
        runner=runner,
        # P2: everything else in the DSN -- sslmode, sslrootcert, client certificates --
        # was dropped here, so a backup of a database that requires verified TLS ran
        # with libpq's default and no certificate check, or not at all.
        database_parameters={str(key): str(value) for key, value in connection.items()},
    )


class CompactPlatformRuntime(LifecycleCLIRuntime):
    """Replace Docker-dependent lifecycle adapters inside the Compact image."""

    def _lifecycle_state(self) -> LifecycleStateStore:
        if str(_runtime_env.get("FS_CONFIGURATION_STATE_ROOT") or "").strip():
            # The management CLI already locks the host lifecycle file; the
            # mounted runtime alias names that same inode, not a second lock.
            return super()._lifecycle_state()
        return LifecycleStateStore(_LIFECYCLE_ROOT, installation_lock_path=_INSTALLATION_LOCK)

    def _clone_compact_data_root(self) -> Path:
        return _DATA_ROOT

    def _clone_state_root(self) -> Path:
        return _STATE_ROOT

    def _clone_workspace_root(self) -> Path:
        return installer_state_root(_LIFECYCLE_ROOT) / "clone/workspace"

    @cached_property
    def core_readiness_service(self) -> CoreReadinessService:
        return CoreReadinessService(probe=_readiness_payload)

    def _build_backup_service(self) -> BackupService:
        if str(_runtime_env.get("FS_CONFIGURATION_STATE_ROOT") or "").strip():
            # Host management uses its installed Compose target and owner-only
            # host artifacts; embedded/provider commands retain the private volume.
            return super()._build_backup_service()
        return BackupService(gateway=_native_backup_gateway())

    @cached_property
    def maintenance_service(self) -> MaintenanceServicePort:
        if str(_runtime_env.get("FS_CONFIGURATION_STATE_ROOT") or "").strip():
            config = self.configuration_service.read_active()
            if config is None:
                raise RuntimeError("Installed Compact configuration is unavailable")
            target = self.configuration_service.derive_target(config)
            return CompactMaintenanceClient(
                lambda operation: self.docker_lifecycle_gateway.compact_maintenance_operation(
                    target, operation
                ),
                mutation_state=LifecycleStateStore(
                    _LIFECYCLE_ROOT,
                    installation_lock_path=installer_state_root(_LIFECYCLE_ROOT) / "install.lock",
                ),
            )
        lifecycle_state = LifecycleStateStore(
            _STATE_PROJECT_ROOT, installation_lock_path=_INSTALLATION_LOCK
        )
        dsn = str(_runtime_env.get("FS_DB_DSN") or _runtime_env.get("DATABASE_URL") or "").strip()
        if not dsn:
            raise RuntimeError("Compact maintenance requires FS_DB_DSN")
        quiescence = LifecycleJobQuiescenceGateway(
            project_root=_STATE_PROJECT_ROOT,
            active_jobs=PostgresActiveJobsReader(dsn),
        )
        return MaintenanceService(
            state=FilesystemMaintenanceGateway(
                project_root=_STATE_PROJECT_ROOT,
                runtime_root=_STATE_ROOT / "runtime",
                state_model=MaintenanceState,
                lifecycle_error=LifecycleError,
                marker_mode=0o640,
            ),
            active_work=_MaintenanceActiveWorkAdapter(quiescence),
            unfinished_operations=_MaintenanceUnfinishedOperationsAdapter(lifecycle_state),
            health=_CompactMaintenanceHealth(),
            readiness_reconciler=_CompactReadinessReconciler(),
        )


__all__ = ["CompactPlatformRuntime"]
