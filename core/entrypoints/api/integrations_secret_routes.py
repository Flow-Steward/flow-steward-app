"""Register the approval routes of catalog secrets used by project connections (ADR 0055)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from core.application.management import ServiceError
from core.entrypoints.api.integrations_request_helpers import _valid_project_id


class ConnectionSecretApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fingerprint: str = Field(min_length=1, max_length=128)


class ConnectionSecretApprovalsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    release_id: str = Field(default="", max_length=256)
    approvals: dict[str, str] = Field(min_length=1, max_length=500)


class WorkflowSecretApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fingerprint: str = Field(min_length=1, max_length=128)
    destinations: list[dict[str, Any]] = Field(default_factory=list, max_length=50)


class SecretBindingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    secret_id: str = Field(min_length=1, max_length=64)


def register_secret_approval_routes(
    *,
    api_error_response: Callable[..., JSONResponse],
    err_not_found: str,
    get_logger: Callable[[], Any],
    request_scope: Callable[..., Any],
    router: APIRouter,
    service_error_response: Callable[..., JSONResponse],
) -> None:
    def _service() -> Any:
        from core.application.secrets.service import SecretCatalogService

        return SecretCatalogService(get_logger)

    @router.get("/api/projects/{project_id}/connections/{connection_id}/secret-approval")
    async def api_connection_secret_approval(request: Request, project_id: str, connection_id: str):
        if not _valid_project_id(project_id):
            return JSONResponse({"error": err_not_found}, status_code=404)
        try:
            scope = request_scope(request, project_id=project_id)
            return _service().connection_approval_status(
                connection_id, scope, project_id=project_id
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)

    @router.post("/api/projects/{project_id}/connections/{connection_id}/secret-approval")
    async def api_connection_secret_approve(
        request: Request,
        project_id: str,
        connection_id: str,
        body: ConnectionSecretApprovalRequest,
    ):
        if not _valid_project_id(project_id):
            return JSONResponse({"error": err_not_found}, status_code=404)
        try:
            scope = request_scope(request, project_id=project_id)
            results = _service().approve_connections(
                scope, approvals={connection_id: body.fingerprint}, project_id=project_id
            )
            return results[0]
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)

    @router.get("/api/projects/{project_id}/secret-approvals")
    async def api_project_secret_approvals(
        request: Request, project_id: str, extension_install_id: str = ""
    ):
        if not _valid_project_id(project_id):
            return JSONResponse({"error": err_not_found}, status_code=404)
        try:
            scope = request_scope(request, project_id=project_id)
            return {
                "connections": _service().project_approval_statuses(
                    project_id, scope, extension_install_id=extension_install_id
                )
            }
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)

    @router.post("/api/projects/{project_id}/secret-approvals")
    async def api_project_secret_approve_release(
        request: Request, project_id: str, body: ConnectionSecretApprovalsRequest
    ):
        if not _valid_project_id(project_id):
            return JSONResponse({"error": err_not_found}, status_code=404)
        try:
            scope = request_scope(request, project_id=project_id)
            return {
                "connections": _service().approve_connections(
                    scope,
                    approvals=dict(body.approvals),
                    release_id=body.release_id or None,
                    project_id=project_id,
                )
            }
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)

    @router.get("/api/projects/{project_id}/workflows/{workflow_id}/secret-approval")
    async def api_workflow_secret_approval(request: Request, project_id: str, workflow_id: str):
        if not _valid_project_id(project_id):
            return JSONResponse({"error": err_not_found}, status_code=404)
        try:
            scope = request_scope(request, project_id=project_id)
            return _service().workflow_approval_status(workflow_id, scope, project_id=project_id)
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)

    @router.post("/api/projects/{project_id}/workflows/{workflow_id}/secret-approval")
    async def api_workflow_secret_approve(
        request: Request, project_id: str, workflow_id: str, body: WorkflowSecretApprovalRequest
    ):
        if not _valid_project_id(project_id):
            return JSONResponse({"error": err_not_found}, status_code=404)
        try:
            scope = request_scope(request, project_id=project_id)
            return _service().approve_workflow(
                workflow_id,
                scope,
                project_id=project_id,
                fingerprint=body.fingerprint,
                destinations=list(body.destinations),
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)

    @router.put("/api/projects/{project_id}/workflows/{workflow_id}/secret-bindings/{field}")
    async def api_workflow_secret_bind(
        request: Request, project_id: str, workflow_id: str, field: str, body: SecretBindingRequest
    ):
        if not _valid_project_id(project_id):
            return JSONResponse({"error": err_not_found}, status_code=404)
        try:
            scope = request_scope(request, project_id=project_id)
            return _service().bind_workflow_input(
                workflow_id, scope, project_id=project_id, field=field, secret_id=body.secret_id
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)

    @router.delete("/api/projects/{project_id}/workflows/{workflow_id}/secret-bindings/{field}")
    async def api_workflow_secret_unbind(
        request: Request, project_id: str, workflow_id: str, field: str
    ):
        if not _valid_project_id(project_id):
            return JSONResponse({"error": err_not_found}, status_code=404)
        try:
            scope = request_scope(request, project_id=project_id)
            return _service().unbind_workflow_input(
                workflow_id, scope, project_id=project_id, field=field
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)

    @router.put("/api/projects/{project_id}/connections/{connection_id}/secret-bindings/{field}")
    async def api_connection_secret_bind(
        request: Request,
        project_id: str,
        connection_id: str,
        field: str,
        body: SecretBindingRequest,
    ):
        if not _valid_project_id(project_id):
            return JSONResponse({"error": err_not_found}, status_code=404)
        try:
            scope = request_scope(request, project_id=project_id)
            return _service().bind_connection_field(
                connection_id, scope, field=field, secret_id=body.secret_id
            )
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)

    @router.delete("/api/projects/{project_id}/connections/{connection_id}/secret-bindings/{field}")
    async def api_connection_secret_unbind(
        request: Request, project_id: str, connection_id: str, field: str
    ):
        if not _valid_project_id(project_id):
            return JSONResponse({"error": err_not_found}, status_code=404)
        try:
            scope = request_scope(request, project_id=project_id)
            return _service().unbind_connection_field(connection_id, scope, field=field)
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)


__all__ = ["register_secret_approval_routes"]
