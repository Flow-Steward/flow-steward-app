"""Workflow-domain CLI bindings to the shared workflow operator service."""

from __future__ import annotations

from core.application.management.command_specs.workflows import (
    WorkflowConvertGroupCommandInput,
    WorkflowEntrypointsCommandInput,
    WorkflowHookBindingCommandInput,
    WorkflowHookCommandInput,
    WorkflowHookCreateCommandInput,
    WorkflowHookPreviewCommandInput,
    WorkflowHookUnbindCommandInput,
    WorkflowListCommandInput,
    WorkflowTargetCommandInput,
    WorkflowWhereUsedCommandInput,
)
from core.entrypoints.cli.platform.service_bindings.common import (
    RuntimeServices,
    ServiceFactory,
    await_maybe,
    cli_admin_scope,
    cli_operator_scope,
)
from core.entrypoints.cli.platform.service_bindings.workflow_authoring import (
    WORKFLOW_AUTHORING_SERVICE_FACTORIES,
)


async def workflows_list(runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    command = (
        value
        if isinstance(value, WorkflowListCommandInput)
        else WorkflowListCommandInput.model_validate(value)
    )
    return await await_maybe(
        runtime.workflow_operator_service.list_project_workflows(
            command.project_id,
            enabled_only=not command.include_disabled,
            include_validation=command.include_validation,
            scope=cli_operator_scope(project_id=command.project_id),
        )
    )


async def workflows_entrypoints(
    runtime: RuntimeServices, value: object, **_kwargs: object
) -> object:
    command = (
        value
        if isinstance(value, WorkflowEntrypointsCommandInput)
        else WorkflowEntrypointsCommandInput.model_validate(value)
    )
    return await await_maybe(
        runtime.workflow_operator_service.list_project_entrypoints(
            command.project_id,
            enabled_only=not command.include_disabled,
            scope=cli_operator_scope(project_id=command.project_id),
        )
    )


async def workflows_validate(runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    command = (
        value
        if isinstance(value, WorkflowTargetCommandInput)
        else WorkflowTargetCommandInput.model_validate(value)
    )
    return await await_maybe(
        runtime.workflow_operator_service.project_workflow_validate(
            command.project_id,
            command.workflow_id,
            scope=cli_operator_scope(
                project_id=command.project_id,
                workflow_id=command.workflow_id,
            ),
        )
    )


async def workflows_contract(runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    command = (
        value
        if isinstance(value, WorkflowTargetCommandInput)
        else WorkflowTargetCommandInput.model_validate(value)
    )
    return await await_maybe(
        runtime.workflow_operator_service.project_workflow_contract(
            command.project_id,
            command.workflow_id,
            scope=cli_operator_scope(
                project_id=command.project_id,
                workflow_id=command.workflow_id,
            ),
        )
    )


async def workflows_suggestions(
    runtime: RuntimeServices, value: object, **_kwargs: object
) -> object:
    command = (
        value
        if isinstance(value, WorkflowTargetCommandInput)
        else WorkflowTargetCommandInput.model_validate(value)
    )
    return await await_maybe(
        runtime.workflow_operator_service.project_workflow_suggestions(
            command.project_id,
            command.workflow_id,
            scope=cli_operator_scope(
                project_id=command.project_id,
                workflow_id=command.workflow_id,
            ),
        )
    )


async def workflows_convert_group(
    runtime: RuntimeServices, value: object, **_kwargs: object
) -> object:
    command = (
        value
        if isinstance(value, WorkflowConvertGroupCommandInput)
        else WorkflowConvertGroupCommandInput.model_validate(value)
    )
    return await await_maybe(
        runtime.workflow_operator_service.convert_group(
            **command.model_dump(exclude={"apply"}),
            dry_run=not command.apply,
            scope=cli_admin_scope(),
        )
    )


def _read_scope(command: WorkflowTargetCommandInput):
    return cli_operator_scope(project_id=command.project_id, workflow_id=command.workflow_id)


async def workflows_hooks_list(
    runtime: RuntimeServices, value: object, **_kwargs: object
) -> object:
    command = WorkflowTargetCommandInput.model_validate(value)
    return await await_maybe(
        runtime.workflow_operator_service.hooks_list(
            project_id=command.project_id,
            workflow_id=command.workflow_id,
            scope=_read_scope(command),
        )
    )


async def workflows_hook_get(runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    command = WorkflowHookCommandInput.model_validate(value)
    return await await_maybe(
        runtime.workflow_operator_service.hook_get(
            **command.model_dump(), scope=_read_scope(command)
        )
    )


async def workflows_hook_preview(
    runtime: RuntimeServices, value: object, **_kwargs: object
) -> object:
    command = WorkflowHookPreviewCommandInput.model_validate(value)
    return await await_maybe(
        runtime.workflow_operator_service.hook_preview(
            **command.model_dump(), scope=cli_admin_scope()
        )
    )


async def workflows_hook_bind(runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    command = WorkflowHookBindingCommandInput.model_validate(value)
    return await await_maybe(
        runtime.workflow_operator_service.hook_bind(**command.model_dump(), scope=cli_admin_scope())
    )


async def workflows_hook_unbind(
    runtime: RuntimeServices, value: object, **_kwargs: object
) -> object:
    command = WorkflowHookUnbindCommandInput.model_validate(value)
    return await await_maybe(
        runtime.workflow_operator_service.hook_unbind(
            **command.model_dump(), scope=cli_admin_scope()
        )
    )


async def workflows_hook_create_and_bind(
    runtime: RuntimeServices, value: object, **_kwargs: object
) -> object:
    command = WorkflowHookCreateCommandInput.model_validate(value)
    return await await_maybe(
        runtime.workflow_operator_service.hook_create_and_bind(
            **command.model_dump(), scope=cli_admin_scope()
        )
    )


async def workflows_where_used(
    runtime: RuntimeServices, value: object, **_kwargs: object
) -> object:
    command = WorkflowWhereUsedCommandInput.model_validate(value)
    return await await_maybe(
        runtime.workflow_operator_service.where_used(
            project_id=command.project_id,
            workflow_id=command.workflow_id,
            scope=_read_scope(command),
            locale=command.locale,
        )
    )


WORKFLOW_SERVICE_FACTORIES: dict[str, ServiceFactory] = {
    "workflows.convert-group": workflows_convert_group,
    "workflows.list": workflows_list,
    "workflows.entrypoints": workflows_entrypoints,
    "workflows.validate": workflows_validate,
    "workflows.contract": workflows_contract,
    "workflows.suggestions": workflows_suggestions,
    "workflows.hooks-list": workflows_hooks_list,
    "workflows.hook-get": workflows_hook_get,
    "workflows.hook-preview": workflows_hook_preview,
    "workflows.hook-bind": workflows_hook_bind,
    "workflows.hook-unbind": workflows_hook_unbind,
    "workflows.hook-create-and-bind": workflows_hook_create_and_bind,
    "workflows.where-used": workflows_where_used,
    **WORKFLOW_AUTHORING_SERVICE_FACTORIES,
}
