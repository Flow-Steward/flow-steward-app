"""Typed database tooling operations shared by CLI and internal wrappers."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from core.application.tooling.db.models import (
    DatabaseBootstrapSeedRequest,
    DatabaseGenerateMigrationRequest,
    DatabaseOperationResult,
    DatabaseQueryDiagnosticsRequest,
    DatabaseRestoreParityCaptureRequest,
    DatabaseRestoreParityVerifyRequest,
    DatabaseStatusRequest,
    DatabaseTuneEnvRequest,
    DatabaseUpgradeRequest,
)


class DatabaseToolingService:
    """Application-facing service for typed database tooling commands."""

    def __init__(
        self,
        *,
        project_root: Path | None = None,
        dsn_provider: Callable[[], str] | None = None,
    ) -> None:
        self._project_root = Path(project_root or Path.cwd()).resolve()
        self._dsn_provider = dsn_provider

    def _artifact_path(self, value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else self._project_root / path

    def _dsn(self, value: str) -> str | None:
        explicit = _dsn(value)
        if explicit is not None or self._dsn_provider is None:
            return explicit
        return _dsn(self._dsn_provider())

    def status(self, request: DatabaseStatusRequest) -> DatabaseOperationResult:
        from core.application.tooling.db import upgrade_db

        status = upgrade_db.inspect_database_upgrade(dsn=self._dsn(request.dsn))
        needs_upgrade = bool(status.get("needs_upgrade"))
        return DatabaseOperationResult(
            operation="database.status",
            status="needs_upgrade" if needs_upgrade else "ok",
            changed=False,
            message="database upgrade is pending" if needs_upgrade else "database is current",
            details=dict(status),
        )

    def upgrade(self, request: DatabaseUpgradeRequest) -> DatabaseOperationResult:
        from core.application.tooling.db import upgrade_db

        summary = upgrade_db.upgrade_database(
            dsn=self._dsn(request.dsn),
            author=request.author,
            maintenance_confirmed=request.maintenance_confirmed,
        )
        return DatabaseOperationResult(
            operation="database.upgrade",
            status="completed",
            changed=True,
            message="database upgrade completed",
            details=dict(summary),
        )

    def bootstrap_seed(self, request: DatabaseBootstrapSeedRequest) -> DatabaseOperationResult:
        from core.application.tooling.db import bootstrap_seed

        summary = bootstrap_seed.apply_bootstrap_seed(
            dsn=self._dsn(request.dsn),
            author=request.author,
        )
        return DatabaseOperationResult(
            operation="database.bootstrap-seed",
            status="completed",
            changed=True,
            message="canonical seed data applied",
            details=dict(summary),
        )

    def generate_migration(
        self, request: DatabaseGenerateMigrationRequest
    ) -> DatabaseOperationResult:
        from core.application.tooling.db import generate_migration

        payload = generate_migration.generate_migration_file(
            name=request.name,
            root=Path(request.root),
        )
        return DatabaseOperationResult(
            operation="database.generate-migration",
            status="created",
            changed=True,
            message="migration file created",
            details=payload,
            artifacts=[str(payload["path"])],
        )

    def query_diagnostics(
        self, request: DatabaseQueryDiagnosticsRequest
    ) -> DatabaseOperationResult:
        from core.application.tooling.db import pg_query_diagnostics

        result = pg_query_diagnostics.collect_query_diagnostics(
            dsn=self._dsn(request.dsn) or "",
            limit=request.limit,
            sort_by=request.sort_by,
            min_rows=request.min_rows,
            max_query_len=request.max_query_len,
        )
        return DatabaseOperationResult(
            operation="database.query-diagnostics",
            status=str(result.get("status") or "completed"),
            changed=False,
            message="query diagnostics collected",
            details=dict(result),
        )

    def tune_env(self, request: DatabaseTuneEnvRequest) -> DatabaseOperationResult:
        from core.application.tooling.db import postgres_tune_env

        result = postgres_tune_env.generate_tuned_env_profile(
            memory_gb=request.memory_gb,
            max_connections=request.max_connections,
            storage=request.storage,
        )
        return DatabaseOperationResult(
            operation="database.tune-env",
            status="completed",
            changed=False,
            message="postgres env profile generated",
            details=result,
        )

    def restore_parity_capture(
        self, request: DatabaseRestoreParityCaptureRequest
    ) -> DatabaseOperationResult:
        from core.application.tooling.db import restore_parity_check

        result = restore_parity_check.capture_restore_parity_metadata(
            dsn=self._dsn(request.dsn) or "",
            label=request.label,
            output=self._artifact_path(request.output),
        )
        return DatabaseOperationResult(
            operation="database.restore-parity-capture",
            status="captured",
            changed=True,
            message="restore parity metadata captured",
            details=result,
            artifacts=[str(result["output"])],
        )

    def restore_parity_verify(
        self, request: DatabaseRestoreParityVerifyRequest
    ) -> DatabaseOperationResult:
        from core.application.tooling.db import restore_parity_check

        result = restore_parity_check.verify_restore_parity_metadata(
            dsn=self._dsn(request.dsn) or "",
            label=request.label,
            baseline=self._artifact_path(request.baseline),
            output=self._artifact_path(request.output) if request.output else None,
        )
        passed = bool(result.get("passed"))
        output = str(result.get("output") or "")
        return DatabaseOperationResult(
            operation="database.restore-parity-verify",
            status="passed" if passed else "failed",
            changed=bool(output),
            message="restore parity metadata verified" if passed else "restore parity mismatch",
            details=result,
            artifacts=[output] if output else [],
        )


def _dsn(value: str) -> str | None:
    return str(value or "").strip() or None
