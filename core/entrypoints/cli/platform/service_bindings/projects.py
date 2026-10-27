"""Project-domain CLI bindings to shared project services."""

from __future__ import annotations

from core.application.management.command_models import ProjectNotificationDispatchInput
from core.application.management.command_specs.projects import (
    ProjectDispatchNotificationCommandInput,
    ProjectListCommandInput,
    ProjectNotificationDeliveriesCommandInput,
    ProjectNotificationRoutesCommandInput,
    ProjectShowCommandInput,
)
from core.application.management.project_cli_projection import project_detail_cli_projection
from core.entrypoints.cli.platform.service_bindings.common import (
    RuntimeServices,
    ServiceFactory,
    await_maybe,
    cli_operator_scope,
)


async def projects_list(runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    command = (
        value
        if isinstance(value, ProjectListCommandInput)
        else ProjectListCommandInput.model_validate(value)
    )
    return await await_maybe(
        runtime.project_query_service.list_projects(
            page=command.page,
            per_page=command.per_page,
            search=command.search,
            scope=cli_operator_scope(),
        )
    )


async def projects_show(runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    command = (
        value
        if isinstance(value, ProjectShowCommandInput)
        else ProjectShowCommandInput.model_validate(value)
    )
    detail = await await_maybe(
        runtime.project_query_service.get_project_detail(
            command.project_id,
            scope=cli_operator_scope(project_id=command.project_id),
        )
    )
    return project_detail_cli_projection(
        detail,
        include_schema=command.include_schema,
        schema_page=command.schema_page,
        schema_per_page=command.schema_per_page,
    )


async def projects_notification_routes(
    runtime: RuntimeServices, value: object, **_kwargs: object
) -> object:
    command = (
        value
        if isinstance(value, ProjectNotificationRoutesCommandInput)
        else ProjectNotificationRoutesCommandInput.model_validate(value)
    )
    return await await_maybe(
        runtime.project_query_service.list_notification_routes(
            command.project_id,
            enabled_only=command.enabled_only,
            scope=cli_operator_scope(project_id=command.project_id),
        )
    )


async def projects_notification_deliveries(
    runtime: RuntimeServices, value: object, **_kwargs: object
) -> object:
    command = (
        value
        if isinstance(value, ProjectNotificationDeliveriesCommandInput)
        else ProjectNotificationDeliveriesCommandInput.model_validate(value)
    )
    return await await_maybe(
        runtime.project_query_service.list_notification_deliveries(
            command.project_id,
            job_id=str(command.job_id or "").strip() or None,
            limit=command.limit,
            scope=cli_operator_scope(project_id=command.project_id),
        )
    )


async def projects_dispatch_notification(
    runtime: RuntimeServices, value: object, **_kwargs: object
) -> object:
    command = (
        value
        if isinstance(value, ProjectDispatchNotificationCommandInput)
        else ProjectDispatchNotificationCommandInput.model_validate(value)
    )
    service_input = ProjectNotificationDispatchInput(
        event_type=command.event_type,
        severity=command.severity,
        workflow_id=str(command.workflow_id or "").strip() or None,
        job_id=str(command.job_id or "").strip() or None,
        payload=dict(command.payload or {}),
    )
    return await await_maybe(
        runtime.project_command_service.dispatch_project_notification(
            command.project_id,
            service_input,
            scope=cli_operator_scope(
                project_id=command.project_id,
                workflow_id=service_input.workflow_id,
            ),
        )
    )


PROJECT_SERVICE_FACTORIES: dict[str, ServiceFactory] = {
    "projects.list": projects_list,
    "projects.show": projects_show,
    "projects.notification-routes": projects_notification_routes,
    "projects.notification-deliveries": projects_notification_deliveries,
    "projects.dispatch-notification": projects_dispatch_notification,
}
