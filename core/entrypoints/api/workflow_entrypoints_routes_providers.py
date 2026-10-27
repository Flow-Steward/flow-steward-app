from __future__ import annotations

import uuid
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from core.application.workflows.workflow_provider_trigger_service import (
    WorkflowProviderTriggerCreateCommand,
    WorkflowProviderTriggerError,
    WorkflowProviderTriggerUpdateCommand,
    create_workflow_provider_trigger_service,
)
from core.application.workflows.workflow_trigger_catalog import (
    find_provider_trigger_definition,
    validate_provider_trigger_config,
)

from .workflow_entrypoints_common import _require_project_scope, _require_workflow_scope
from .workflow_entrypoints_registry import (
    _is_provider_trigger_scope_conflict,
    _load_workflow_manifest,
    _resolve_provider_trigger_project_binding,
)
from .workflow_entrypoints_serializers import (
    WorkflowProviderTriggerRequest,
    _serialize_provider_trigger,
)


def _provider_trigger_service(*, include_trashed: bool = False):
    return create_workflow_provider_trigger_service(
        resolve_project_binding=_resolve_provider_trigger_project_binding,
        load_workflow=(lambda value: _load_workflow_manifest(value, include_trashed=True))
        if include_trashed
        else _load_workflow_manifest,
        is_scope_conflict=_is_provider_trigger_scope_conflict,
        find_definition=find_provider_trigger_definition,
        validate_config=validate_provider_trigger_config,
    )


def _error_response(exc: WorkflowProviderTriggerError) -> JSONResponse:
    return JSONResponse(
        {"error": str(exc), "code": exc.code},
        status_code=exc.status_code,
    )


def _register_workflow_provider_trigger_routes(ctx: dict[str, Any]) -> None:
    router = ctx["router"]
    get_logger_only = ctx["get_logger_only"]
    scope_resolver = ctx["scope_resolver"]
    err_database_not_configured = ctx["err_database_not_configured"]

    @router.post(
        "/api/workflows/{workflow_id}/triggers/providers",
        responses={403: {"description": "Forbidden"}, 404: {"description": "Not found"}},
    )
    def create_provider_trigger(
        workflow_id: str, body: WorkflowProviderTriggerRequest, request: Request
    ):
        bl = get_logger_only()
        if not bl:
            return JSONResponse({"error": err_database_not_configured}, status_code=503)
        scope = scope_resolver(request)
        _require_workflow_scope(scope, workflow_id)
        command = WorkflowProviderTriggerCreateCommand(
            workflow_id=workflow_id,
            trigger_id=str(body.trigger_id or uuid.uuid4().hex).strip(),
            project_id=body.project_id,
            provider_key=str(body.provider_key or "").strip().lower(),
            trigger_type_id=str(body.trigger_type_id or "").strip(),
            display_name=str(body.display_name or "").strip(),
            enabled=True if body.enabled is None else bool(body.enabled),
            trigger_config=dict(body.trigger_config or {}),
        )
        try:
            created, definition = _provider_trigger_service().create(
                bl=bl,
                command=command,
                authorize_workflow=lambda value: _require_workflow_scope(scope, value),
                authorize_project=lambda value: _require_project_scope(bl, scope, value),
            )
        except WorkflowProviderTriggerError as exc:
            return _error_response(exc)
        return JSONResponse(
            _serialize_provider_trigger(created, definition=definition, request=request),
            status_code=201,
        )

    @router.patch(
        "/api/workflows/{workflow_id}/triggers/providers/{trigger_id}",
        responses={403: {"description": "Forbidden"}, 404: {"description": "Not found"}},
    )
    def update_provider_trigger(
        workflow_id: str,
        trigger_id: str,
        body: WorkflowProviderTriggerRequest,
        request: Request,
    ):
        bl = get_logger_only()
        if not bl:
            return JSONResponse({"error": err_database_not_configured}, status_code=503)
        scope = scope_resolver(request)
        _require_workflow_scope(scope, workflow_id)
        command = WorkflowProviderTriggerUpdateCommand(
            workflow_id=workflow_id,
            trigger_id=trigger_id,
            project_id=body.project_id,
            provider_key=body.provider_key,
            trigger_type_id=body.trigger_type_id,
            display_name=body.display_name,
            enabled=body.enabled,
            trigger_config=dict(body.trigger_config or {}),
        )
        try:
            updated, definition = _provider_trigger_service().update(
                bl=bl,
                command=command,
                authorize_workflow=lambda value: _require_workflow_scope(scope, value),
                authorize_project=lambda value: _require_project_scope(bl, scope, value),
            )
        except WorkflowProviderTriggerError as exc:
            return _error_response(exc)
        return _serialize_provider_trigger(updated, definition=definition, request=request)

    @router.delete(
        "/api/workflows/{workflow_id}/triggers/providers/{trigger_id}",
        responses={403: {"description": "Forbidden"}, 404: {"description": "Not found"}},
    )
    def delete_provider_trigger(workflow_id: str, trigger_id: str, request: Request):
        bl = get_logger_only()
        if not bl:
            return JSONResponse({"error": err_database_not_configured}, status_code=503)
        scope = scope_resolver(request)
        _require_workflow_scope(scope, workflow_id)
        try:
            _provider_trigger_service(include_trashed=True).delete(
                bl=bl,
                workflow_id=workflow_id,
                trigger_id=trigger_id,
                authorize_workflow=lambda value: _require_workflow_scope(scope, value),
                authorize_project=lambda value: _require_project_scope(bl, scope, value),
            )
        except WorkflowProviderTriggerError as exc:
            return _error_response(exc)
        return {"status": "deleted", "trigger_id": trigger_id}


__all__ = [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
