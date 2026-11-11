"""Typed database tooling operations shared by CLI and internal wrappers."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.application.lifecycle.errors import LifecycleError
from core.application.management.command_specs.contracts import ExitCode
from core.application.tooling.db.models import (
    DatabaseBootstrapSeedRequest,
    DatabaseGenerateMigrationRequest,
    DatabaseOperationResult,
    DatabasePreflightRequest,
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

    def preflight(self, request: DatabasePreflightRequest) -> DatabaseOperationResult:
        from core.application.tooling.db import upgrade_db

        report = upgrade_db.preflight_database_upgrade(
            dsn=self._dsn(request.dsn),
            allow_unlabeled_sessions=request.allow_unlabeled_sessions,
        )
        blocked = bool(report["blocking"])
        return DatabaseOperationResult(
            operation="database.preflight",
            status="blocked" if blocked else "ok",
            changed=False,
            message=(
                f"database upgrade is blocked by {len(report['blocking'])} issue(s)"
                if blocked
                else "database upgrade can proceed"
            ),
            details=dict(report),
        )

    def upgrade(self, request: DatabaseUpgradeRequest) -> DatabaseOperationResult:
        from core.application.tooling.db import upgrade_db

        try:
            summary = upgrade_db.upgrade_database(
                dsn=self._dsn(request.dsn),
                author=request.author,
                maintenance_confirmed=request.maintenance_confirmed,
                allow_unlabeled_sessions=request.allow_unlabeled_sessions,
            )
        except upgrade_db.DatabaseUpgradeBlockedError as exc:
            raise database_upgrade_blocked(exc.blocking) from exc
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


_STOP_PREVIOUS_RELEASE = (
    "Stop the running release's processes; Postgres, RabbitMQ and storage keep running. "
    "Enable maintenance with ./flow-steward maintenance enable --confirm and rerun "
    "./flow-steward database upgrade --maintenance-confirmed --confirm: on Scalable it "
    "then stops fs-app, fs-worker, control-plane and fs-mcp itself and leaves them "
    "stopped until ./install.sh or the release upgrade starts the new release."
)
_BLOCKING_REMEDIATION = {
    "runtime_sessions": _STOP_PREVIOUS_RELEASE,
    "unlabeled_sessions": (
        f"{_STOP_PREVIOUS_RELEASE} Releases before 0.9.14 do not name their database "
        "sessions, so their processes are listed here. If the listed sessions are not "
        "Flow Steward processes, rerun with --allow-unlabeled-sessions."
    ),
}


def database_upgrade_blocked(blocking: list[dict[str, Any]]) -> LifecycleError:
    """The public error for an upgrade its read-only preflight refused.

    Every blocking issue becomes a finding with its own next step, so the operator
    sees what stops the upgrade without opening the log.
    """
    findings = [
        {
            "status": "blocking",
            "check_id": str(issue.get("code") or "blocked"),
            "title": str(issue.get("message") or ""),
            "detail": _issue_detail(issue),
            "remediation": _BLOCKING_REMEDIATION.get(
                str(issue.get("code") or ""),
                "Resolve this issue, then rerun the upgrade.",
            ),
        }
        for issue in blocking
    ]
    codes = list(dict.fromkeys(row["check_id"] for row in findings))
    return LifecycleError(
        code="database_upgrade_blocked",
        summary="The database upgrade preflight is blocked.",
        cause=(
            f"The database upgrade was refused before any change: {len(findings)} "
            f"blocking issue(s) ({', '.join(codes)})."
        ),
        remediation=(
            # Only sessions block: the last-listed advice covers both session codes.
            _BLOCKING_REMEDIATION[codes[-1]]
            if codes and all(code in _BLOCKING_REMEDIATION for code in codes)
            else "Resolve each blocking issue above, then rerun the upgrade; "
            "./flow-steward database preflight shows the same report without changing anything."
        ),
        retryable=True,
        step="preflight",
        exit_code=ExitCode.PRECONDITION,
        details={"findings": findings},
    )


def _issue_detail(issue: dict[str, Any]) -> str:
    details = issue.get("details")
    if not isinstance(details, dict):
        return ""
    sessions = details.get("sessions")
    if isinstance(sessions, list) and sessions:
        shown = [
            f"pid {row.get('pid')}"
            + (f" ({row['application_name']})" if row.get("application_name") else "")
            for row in sessions
            if isinstance(row, dict)
        ]
        return "Connected: " + ", ".join(shown) + "."
    listed = [
        f"{key}: {', '.join(map(str, value))}"
        for key, value in details.items()
        if isinstance(value, list) and value and all(isinstance(item, str) for item in value)
    ]
    return "; ".join(listed)


def _dsn(value: str) -> str | None:
    return str(value or "").strip() or None
