"""Database composition adapters for transport entrypoints.

Entrypoints depend on application-owned ports and these factories, never on
database implementation modules.  This module is the single composition root
that binds those ports to PostgreSQL-backed stores.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any

from core.application.lifecycle.errors import DatabaseSchemaRecoveryRequiredError
from core.application.ports.platform_store import JobStorePort, PlatformStorePort, ProjectStorePort
from core.infrastructure.db import job_thread_store, project_connection_store
from core.infrastructure.db.db_pool import transaction_connection
from core.infrastructure.db.platform_logger_common import resolve_observability_dsn
from core.infrastructure.db.platform_stores import (
    PlatformConnectionStore,
    PlatformControlScheduleStore,
    PlatformIdentityStore,
    PlatformIntegrationStore,
    PlatformJobStore,
    PlatformObservabilityStore,
    PlatformProjectStore,
    PlatformReactionStore,
    PlatformReportingStore,
    PlatformWorkerStore,
)
from core.infrastructure.db.psycopg_runtime import OperationalError, connect, extras
from core.infrastructure.db.psycopg_runtime.pool import PoolError
from core.infrastructure.db.schema_contract import (
    RuntimeSchemaContractViolation,
    enforce_runtime_schema_contract_dsn,
)
from core.infrastructure.db.system_settings_store import MAX_RETENTION_DAYS, MAX_REVISIONS_KEEP

DATABASE_OPERATION_ERRORS = (OperationalError, PoolError)


def _validated(seam: str, dependency: str, service: Any, methods: tuple[str, ...]) -> Any:
    from core.infrastructure.runtime.app_factory import validate_runtime_service_instance

    return validate_runtime_service_instance(
        seam=seam,
        dependency=dependency,
        service=service,
        required_methods=methods,
    )


def create_connection_store(dsn: str) -> PlatformConnectionStore:
    return PlatformConnectionStore(dsn)


def create_identity_store(dsn: str) -> PlatformIdentityStore:
    return PlatformIdentityStore(dsn)


def create_user_counter(dsn: str) -> Callable[[], int]:
    """Bind "does this installation have any user yet" for an entrypoint to ask.

    AC17-R117: the Compact entrypoint decides whether to mint a first-administrator
    link before any store or service exists, and the answer is one count. It belongs
    here rather than in the entrypoint because this module is where entrypoints are
    allowed to meet PostgreSQL at all.
    """

    def count() -> int:
        connection = connect(dsn)
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM users")
                row = cursor.fetchone()
                return int((row[0] if row else 0) or 0)
        finally:
            connection.close()

    return count


def create_integration_store(dsn: str) -> PlatformStorePort:
    return _validated(
        "entrypoints.extension_oauth",
        "PlatformIntegrationStore",
        PlatformIntegrationStore(dsn),
        ("get_project", "get_extension_install_for_operator"),
    )


def create_project_store(dsn: str) -> ProjectStorePort:
    return _validated(
        "entrypoints.project_scope",
        "PlatformProjectStore",
        PlatformProjectStore(dsn),
        ("get_project", "get_projects", "get_project_workflows", "list_project_connections"),
    )


def create_job_store(dsn: str) -> JobStorePort:
    return _validated(
        "entrypoints.job_scope",
        "PlatformJobStore",
        PlatformJobStore(dsn),
        ("get_job", "get_active_jobs", "update_job"),
    )


def create_reporting_store(dsn: str) -> PlatformStorePort:
    return _validated(
        "entrypoints.reporting_scope",
        "PlatformReportingStore",
        PlatformReportingStore(dsn),
        (
            "get_project",
            "list_dashboard_daily_rollups",
            "list_dashboard_job_facts",
        ),
    )


def create_event_store(dsn: str) -> PlatformStorePort:
    return _validated(
        "entrypoints.event_scope",
        "PlatformReactionStore",
        PlatformReactionStore(dsn),
        ("list_workflow_reactions", "list_job_reaction_events"),
    )


def create_observability_store(dsn: str) -> PlatformStorePort:
    return _validated(
        "entrypoints.observability_scope",
        "PlatformObservabilityStore",
        PlatformObservabilityStore(dsn),
        ("build_job_trace", "get_logs", "get_events", "get_agent_messages"),
    )


def create_worker_store(dsn: str) -> PlatformStorePort:
    return _validated(
        "worker.execution_scope",
        "PlatformWorkerStore",
        PlatformWorkerStore(dsn),
        ("get_job", "update_job", "get_project", "heartbeat_project_execution_lease"),
    )


def create_control_stores(dsn: str) -> dict[str, PlatformStorePort]:
    stores: dict[str, PlatformStorePort] = {
        "connection": PlatformConnectionStore(dsn),
        "events": PlatformReactionStore(dsn),
        "observability": PlatformObservabilityStore(dsn),
        "projects": PlatformProjectStore(dsn),
        "scheduler": PlatformControlScheduleStore(dsn),
        "worker": PlatformWorkerStore(dsn),
    }
    contracts = {
        "events": ("claim_pending_domain_event_outbox",),
        "observability": ("refresh_dashboard_rollups",),
        "projects": (
            "list_active_project_ids",
            "list_recent_registry_changelog",
            "record_security_posture_loop_attempt",
            "record_workflow_knowledge_sync_loop_attempt",
            "record_workflow_knowledge_sync_loop_success",
            "record_workflow_knowledge_sync_loop_failure",
        ),
        "scheduler": ("list_due_scheduled_jobs", "reconcile_scheduled_job_execution_locks"),
        "worker": ("get_active_jobs", "update_job"),
    }
    for name, methods in contracts.items():
        _validated(
            f"control_process.{name}",
            type(stores[name]).__name__,
            stores[name],
            methods,
        )
    return stores


def resolve_platform_dsn(explicit_dsn: object | None = None) -> str:
    return resolve_observability_dsn(explicit_dsn)


def enforce_runtime_schema(dsn: str, *, component: str) -> None:
    try:
        enforce_runtime_schema_contract_dsn(dsn, component=component)
    except RuntimeSchemaContractViolation as exc:
        raise DatabaseSchemaRecoveryRequiredError(str(exc)) from exc


def connect_database(dsn: str):
    return connect(dsn)


def database_transaction(connection: Any) -> AbstractContextManager[Any]:
    return transaction_connection(connection)


@contextmanager
def workflow_import_transaction(
    registry_store: Any,
    project_connection_store: Any,
) -> Iterator[Any]:
    """Atomically bind workflow and connection writes to one DB transaction."""
    connection = getattr(project_connection_store, "conn", None)
    participate = getattr(registry_store, "participate_in_external_transaction", None)
    if connection is None or not callable(participate):
        raise RuntimeError("workflow import stores cannot share a transaction")
    with transaction_connection(connection) as transaction_conn, participate(transaction_conn):
        yield transaction_conn


def dict_cursor_kwargs() -> dict[str, Any]:
    return {"cursor_factory": extras.RealDictCursor}


def get_project_connection_row(connection: Any, connection_id: str) -> Any:
    return project_connection_store.get_project_connection_row(connection, connection_id)


def get_job_context_by_thread_id(connection: Any, thread_id: str) -> Any:
    return job_thread_store.get_job_context_by_thread_id(connection, thread_id)


def resolve_provider_project_run_config(*args: Any, **kwargs: Any) -> Any:
    from core.application.extensions.provider_runtime import (
        resolve_provider_project_run_config as impl,
    )

    return impl(*args, **kwargs)


def provider_signature_enforcers() -> tuple[Any, Any, Any]:
    from core.application.extensions.provider_webhook_security import (
        enforce_provider_webhook_effective_signature,
        enforce_provider_webhook_project_secret,
        enforce_provider_webhook_signature,
    )

    return (
        enforce_provider_webhook_effective_signature,
        enforce_provider_webhook_project_secret,
        enforce_provider_webhook_signature,
    )


def ingress_runtime():
    from core.infrastructure.db import ingress_runtime as runtime

    return runtime


__all__ = [
    "DATABASE_OPERATION_ERRORS",
    "MAX_RETENTION_DAYS",
    "MAX_REVISIONS_KEEP",
    "DatabaseSchemaRecoveryRequiredError",
    "connect_database",
    "create_connection_store",
    "create_control_stores",
    "create_identity_store",
    "create_job_store",
    "create_observability_store",
    "create_project_store",
    "create_reporting_store",
    "create_worker_store",
    "database_transaction",
    "dict_cursor_kwargs",
    "enforce_runtime_schema",
    "get_job_context_by_thread_id",
    "get_project_connection_row",
    "ingress_runtime",
    "job_thread_store",
    "provider_signature_enforcers",
    "resolve_platform_dsn",
    "resolve_provider_project_run_config",
]
