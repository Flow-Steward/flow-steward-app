"""Workflow authoring CLI bindings: transport only, onto the shared services.

Each binding validates the same request model its HTTP route takes, then calls
the method that route's application service exposes. Reads run in the
project-scoped operator scope; writes in the deployment-admin scope.
"""

from __future__ import annotations

from pathlib import Path

from core.application.management.command_models import WorkflowRunInput
from core.application.management.command_specs.workflow_authoring import (
    WorkflowApplyChangesCommandInput,
    WorkflowAuthoringSchemaCommandInput,
    WorkflowDraftCommandInput,
    WorkflowExportCommandInput,
    WorkflowImportCommandInput,
    WorkflowRunCommandInput,
)
from core.application.management.command_specs.workflow_shared import WorkflowTargetCommandInput
from core.application.management.errors import ServiceValidationError
from core.application.management.idempotency_keys import validate_idempotency_key
from core.application.workflows.workflow_authoring_requests import (
    WorkflowChangeSet,
    WorkflowDraftInput,
    WorkflowImportRequest,
)
from core.entrypoints.cli.platform.service_bindings.common import (
    RuntimeServices,
    ServiceFactory,
    await_maybe,
    cli_admin_scope,
    cli_operator_scope,
)


def _bundle_text(path: str) -> str:
    try:
        return Path(path).expanduser().read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ServiceValidationError(f"Cannot read the bundle file '{path}'.") from exc


def _draft_body(command: WorkflowDraftCommandInput) -> WorkflowDraftInput:
    return WorkflowDraftInput(
        bundle_yaml=_bundle_text(command.bundle_file),
        resolutions=command.resolutions or None,
    )


def _target_scope(command: WorkflowTargetCommandInput):
    return cli_operator_scope(project_id=command.project_id, workflow_id=command.workflow_id)


async def workflows_authoring_schema(
    runtime: RuntimeServices, value: object, **_kwargs: object
) -> object:
    command = WorkflowAuthoringSchemaCommandInput.model_validate(value)
    return await await_maybe(
        runtime.workflow_operator_service.authoring_schema(
            step_kind=command.step_kind.strip() or None, scope=cli_operator_scope()
        )
    )


async def workflows_draft_validate(
    runtime: RuntimeServices, value: object, **_kwargs: object
) -> object:
    command = WorkflowDraftCommandInput.model_validate(value)
    body = _draft_body(command)
    return await await_maybe(
        runtime.workflow_operator_service.draft_validate(
            project_id=command.project_id,
            bundle_yaml=body.bundle_yaml,
            resolutions=body.resolutions,
            scope=cli_operator_scope(project_id=command.project_id),
        )
    )


async def workflows_draft_save(
    runtime: RuntimeServices, value: object, **_kwargs: object
) -> object:
    command = WorkflowDraftCommandInput.model_validate(value)
    body = _draft_body(command)
    return await await_maybe(
        runtime.workflow_operator_service.draft_save(
            project_id=command.project_id,
            bundle_yaml=body.bundle_yaml,
            resolutions=body.resolutions,
            scope=cli_admin_scope(),
        )
    )


async def workflows_definition(
    runtime: RuntimeServices, value: object, **_kwargs: object
) -> object:
    command = WorkflowTargetCommandInput.model_validate(value)
    return await await_maybe(
        runtime.workflow_operator_service.workflow_definition(
            project_id=command.project_id,
            workflow_id=command.workflow_id,
            scope=_target_scope(command),
        )
    )


async def workflows_apply_changes(
    runtime: RuntimeServices, value: object, **_kwargs: object
) -> object:
    command = WorkflowApplyChangesCommandInput.model_validate(value)
    body = WorkflowChangeSet(
        expected_revision=command.expected_revision,
        operations=command.operations,
        dry_run=command.dry_run,
    )
    return await await_maybe(
        runtime.workflow_operator_service.apply_changes(
            project_id=command.project_id,
            workflow_id=command.workflow_id,
            expected_revision=body.expected_revision,
            operations=body.operations,
            dry_run=body.dry_run,
            scope=cli_admin_scope(),
        )
    )


async def workflows_export(runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    command = WorkflowExportCommandInput.model_validate(value)
    return await await_maybe(
        runtime.workflow_operator_service.export_workflow(
            project_id=command.project_id,
            workflow_id=command.workflow_id,
            include_children=command.include_children,
            reference_only=command.reference_only,
            scope=_target_scope(command),
        )
    )


async def workflows_import(runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    command = WorkflowImportCommandInput.model_validate(value)
    body = WorkflowImportRequest(
        bundle_yaml=_bundle_text(command.bundle_file),
        target_workflow_id=command.target_workflow_id or None,
        project_id=command.project_id,
        resolutions=command.resolutions,
    )
    return await await_maybe(
        runtime.workflow_operator_service.import_workflow(
            project_id=command.project_id,
            bundle_yaml=body.bundle_yaml or "",
            target_workflow_id=body.target_workflow_id,
            resolutions=body.resolutions,
            scope=cli_admin_scope(),
        )
    )


async def workflows_run(runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    command = WorkflowRunCommandInput.model_validate(value)
    body = WorkflowRunInput(
        project_id=command.project_id,
        workflow_id=command.workflow_id,
        inputs=command.inputs,
        client_context=command.client_context,
        test_mode=command.test_mode,
    )
    result = await await_maybe(
        runtime.project_command_service.start_mcp_workflow_run(
            body,
            idempotency_key=validate_idempotency_key(command.idempotency_key or None),
            scope=cli_admin_scope(),
        )
    )
    dump = getattr(result, "dump", None)
    return dump() if callable(dump) else result


async def workflows_stats(runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    command = WorkflowTargetCommandInput.model_validate(value)
    return await await_maybe(
        runtime.workflow_operator_service.workflow_stats(
            project_id=command.project_id,
            workflow_id=command.workflow_id,
            scope=_target_scope(command),
        )
    )


WORKFLOW_AUTHORING_SERVICE_FACTORIES: dict[str, ServiceFactory] = {
    "workflows.authoring-schema": workflows_authoring_schema,
    "workflows.draft-validate": workflows_draft_validate,
    "workflows.draft-save": workflows_draft_save,
    "workflows.definition": workflows_definition,
    "workflows.apply-changes": workflows_apply_changes,
    "workflows.export": workflows_export,
    "workflows.import": workflows_import,
    "workflows.run": workflows_run,
    "workflows.stats": workflows_stats,
}
