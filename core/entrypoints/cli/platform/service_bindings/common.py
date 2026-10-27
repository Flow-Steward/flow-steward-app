"""Shared helpers for transport-only CLI service bindings."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel

from core.application.lifecycle.contracts import CancellationToken
from core.application.management.command_specs.contracts import (
    ChoiceItem,
    OperationProgressEvent,
)

if TYPE_CHECKING:
    from core.application.auth.bootstrap_tokens import BootstrapTokenService
    from core.application.clone.service import CloneService
    from core.application.extensions.scaffold import ExtensionScaffoldService
    from core.application.lifecycle.backup_command_service import BackupCommandService
    from core.application.lifecycle.configuration_service import ConfigurationService
    from core.application.lifecycle.environment_service import EnvironmentService
    from core.application.lifecycle.stack_service import StackService
    from core.application.maintenance.ports import MaintenanceServicePort
    from core.application.management.scope import ScopeContext
    from core.application.readiness import CoreReadinessService
    from core.application.release.service import ReleaseService
    from core.application.reports.reporting_service import ReportingService
    from core.application.tooling.db.service import DatabaseToolingService
    from core.application.tooling.i18n.service import LocalizationToolingService
    from core.application.tooling.quality.service import QualityToolingService
    from core.application.tooling.testing.service import TestingToolingService
    from core.entrypoints.cli.platform.service_bindings.contracts import (
        ApprovalCommandServiceProtocol,
        ExtensionBindingServiceProtocol,
        ExtensionQueryServiceProtocol,
        JobCommandServiceProtocol,
        JobQueryServiceProtocol,
        OpsHealthSnapshotServiceProtocol,
        ProjectCommandServiceProtocol,
        ProjectQueryServiceProtocol,
        UserCommandServiceProtocol,
        UserQueryServiceProtocol,
        WorkflowOperatorServiceProtocol,
    )


class RuntimeServices(Protocol):
    @property
    def clone_service(self) -> CloneService: ...

    @property
    def backup_command_service(self) -> BackupCommandService: ...

    @property
    def configuration_service(self) -> ConfigurationService: ...

    @property
    def stack_service(self) -> StackService: ...

    @property
    def environment_service(self) -> EnvironmentService: ...

    @property
    def release_service(self) -> ReleaseService: ...

    @property
    def maintenance_service(self) -> MaintenanceServicePort: ...

    @property
    def core_readiness_service(self) -> CoreReadinessService: ...

    @property
    def bootstrap_token_service(self) -> BootstrapTokenService: ...

    @property
    def user_query_service(self) -> UserQueryServiceProtocol: ...

    @property
    def user_command_service(self) -> UserCommandServiceProtocol: ...

    @property
    def project_query_service(self) -> ProjectQueryServiceProtocol: ...

    @property
    def project_command_service(self) -> ProjectCommandServiceProtocol: ...

    @property
    def job_query_service(self) -> JobQueryServiceProtocol: ...

    @property
    def job_command_service(self) -> JobCommandServiceProtocol: ...

    @property
    def approval_command_service(self) -> ApprovalCommandServiceProtocol: ...

    @property
    def reporting_service(self) -> ReportingService: ...

    @property
    def ops_health_snapshot_service(self) -> OpsHealthSnapshotServiceProtocol: ...

    @property
    def workflow_operator_service(self) -> WorkflowOperatorServiceProtocol: ...

    @property
    def extension_query_service(self) -> ExtensionQueryServiceProtocol: ...

    @property
    def extension_binding_service(self) -> ExtensionBindingServiceProtocol: ...

    @property
    def extension_scaffold_service(self) -> ExtensionScaffoldService: ...

    @property
    def database_tooling_service(self) -> DatabaseToolingService: ...

    @property
    def testing_tooling_service(self) -> TestingToolingService: ...

    @property
    def quality_tooling_service(self) -> QualityToolingService: ...

    @property
    def localization_tooling_service(self) -> LocalizationToolingService: ...

    def get_worker_store(self) -> Any: ...


ServiceFactory = Callable[..., object | Awaitable[object]]
ChoiceProvider = Callable[[RuntimeServices], object | Awaitable[object]]
PlanBuilder = Callable[[RuntimeServices, object], object | Awaitable[object]]

_CURRENT_CANCELLATION: ContextVar[CancellationToken | None] = ContextVar(
    "cli_service_cancellation", default=None
)


@dataclass(frozen=True)
class PreparedCommand:
    """Internal reviewed plan paired with the exact execution input it authorizes."""

    execution_input: BaseModel
    review: Mapping[str, Any]
    review_required: bool = True
    deferred_fields: tuple[str, ...] = ()
    cleanup: Callable[[], None] | None = None


@contextmanager
def bind_cancellation_token(token: CancellationToken | None) -> Iterator[None]:
    """Expose one invocation token without changing existing binding signatures."""
    reset_token = _CURRENT_CANCELLATION.set(token)
    try:
        yield
    finally:
        _CURRENT_CANCELLATION.reset(reset_token)


def current_cancellation_token() -> CancellationToken | None:
    """Return the token for the current service invocation, when opted in."""
    return _CURRENT_CANCELLATION.get()


async def await_maybe(value: object) -> Any:
    return await value if isinstance(value, Awaitable) else value


def payload(value: object) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump()
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def row_value(row: object, key: str, default: object = "") -> object:
    if isinstance(row, Mapping):
        return row.get(key, default)
    return getattr(row, key, default)


async def accounts_available(runtime: RuntimeServices) -> tuple[ChoiceItem, ...]:
    result = await await_maybe(runtime.user_query_service.list_accounts(limit=200))
    rows = (
        result.get("accounts", ())
        if isinstance(result, Mapping)
        else getattr(result, "accounts", result)
    )
    return tuple(
        ChoiceItem(
            value=str(row_value(row, "id") or row_value(row, "account_id")),
            label=str(
                row_value(row, "display_name")
                or row_value(row, "id")
                or row_value(row, "account_id")
            ),
        )
        for row in rows
        if bool(row_value(row, "is_active", True))
    )


async def projects_available(runtime: RuntimeServices) -> tuple[ChoiceItem, ...]:
    result = await await_maybe(
        runtime.project_query_service.list_projects(
            page=1,
            per_page=200,
            scope=cli_operator_scope(),
        )
    )
    rows = (
        result.get("projects", ())
        if isinstance(result, Mapping)
        else getattr(result, "projects", ())
    )
    return tuple(
        ChoiceItem(value=str(row_value(row, "id")), label=str(row_value(row, "name")))
        for row in rows
    )


def call_with_optional_progress(
    method: Callable[..., object],
    request: object,
    progress_sink: Callable[[OperationProgressEvent], None] | None,
) -> object:
    return method(request, progress_sink=progress_sink)


def cli_operator_scope(
    *,
    project_id: object | None = None,
    workflow_id: object | None = None,
) -> ScopeContext:
    from core.application.auth.config import DEFAULT_ACCOUNT_ID, DEFAULT_TENANT_ID
    from core.application.management.scope import ScopeContext

    """Return the explicit approved system scope used by local CLI automation."""
    normalized_project = str(project_id or "").strip() or None
    normalized_workflow = str(workflow_id or "").strip() or None
    return ScopeContext(
        account_id=DEFAULT_ACCOUNT_ID,
        tenant_id=DEFAULT_TENANT_ID,
        project_id=normalized_project,
        workflow_id=normalized_workflow,
        actor_id="flow-steward-cli",
        principal_type="system_actor",
        allowed_project_ids=frozenset({normalized_project}) if normalized_project else frozenset(),
        allowed_workflow_ids=frozenset({normalized_workflow})
        if normalized_workflow
        else frozenset(),
    )


def cli_admin_scope() -> ScopeContext:
    from core.application.auth.config import DEFAULT_ACCOUNT_ID, DEFAULT_TENANT_ID
    from core.application.management.scope import ScopeContext

    """Return the explicit deployment-admin scope used by local lifecycle CLI operations."""
    return ScopeContext(
        account_id=DEFAULT_ACCOUNT_ID,
        tenant_id=DEFAULT_TENANT_ID,
        actor_id="flow-steward-cli",
        principal_type="admin",
    )
