"""API routes for first-party tabular feed Builder support."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from core.application.management import ServiceForbiddenError
from core.application.management.guards import require_project_access, require_workflow_access
from core.application.tabular_feed.context import TabularFeedExecutionContext
from core.application.tabular_feed.service import TabularFeedService
from core.application.workflows.workflow_portability import (
    WorkflowPortabilityError,
    WorkflowPortabilityService,
)
from core.infrastructure.tabular_feed.storage import build_tabular_artifact_storage


class TabularFeedInspectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_handle: str = Field(min_length=1)
    options: dict[str, Any] = Field(default_factory=dict)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _safe_result(value: dict[str, Any]) -> dict[str, Any]:
    raw_result = value.get("result")
    result = dict(raw_result) if isinstance(raw_result, dict) else {}
    sheets = result.get("sheets")
    columns = result.get("columns")
    warnings = result.get("warnings")
    preview_rows = result.get("preview_rows")
    return {
        "format": _text(result.get("format")),
        "encoding": _text(result.get("encoding")),
        "delimiter": _text(result.get("delimiter")),
        "sheets": list(sheets) if isinstance(sheets, list) else [],
        "columns": list(columns) if isinstance(columns, list) else [],
        "warnings": list(warnings) if isinstance(warnings, list) else [],
        "row_count_estimate": int(result.get("row_count_estimate") or 0),
        "preview_rows": list(preview_rows[:10]) if isinstance(preview_rows, list) else [],
    }


def create_api_tabular_feed_router(
    *,
    get_logger_only: Callable[[], Any],
    request_scope: Callable[..., Any],
    service_error_response: Callable[..., JSONResponse],
    api_error_response: Callable[..., JSONResponse],
) -> APIRouter:
    router = APIRouter(tags=["tabular-feed"])

    @router.post("/api/projects/{project_id}/workflows/{workflow_id}/tabular-feed/inspect")
    async def api_tabular_feed_inspect(
        request: Request,
        project_id: str,
        workflow_id: str,
        body: TabularFeedInspectRequest,
    ):
        bl = get_logger_only()
        if bl is None:
            return JSONResponse({"error": "Database backend is not configured"}, status_code=503)
        try:
            scope = request_scope(request, project_id=project_id, workflow_id=workflow_id)
            project = bl.get_project(project_id) if hasattr(bl, "get_project") else None
            if not isinstance(project, dict):
                return JSONResponse({"error": "Project not found"}, status_code=404)
            require_project_access(scope, project)
            require_workflow_access(scope, workflow_id)
            # An authoring helper for a step's file input: a protected author
            # graph is read-only, so there is no step of it to author.
            WorkflowPortabilityService(bl).assert_structure_change_allowed(
                workflow_id,
                action="tabular_feed_inspect",
            )
        except ServiceForbiddenError as exc:
            return service_error_response(exc)
        except WorkflowPortabilityError as exc:
            return JSONResponse(exc.detail(), status_code=exc.status_code)
        except Exception as exc:
            return api_error_response(exc)

        def _inspect() -> dict[str, Any]:
            plane = build_tabular_artifact_storage()
            service = TabularFeedService(plane=plane)
            context = TabularFeedExecutionContext(
                account_id=_text(getattr(scope, "account_id", "")),
                tenant_id=_text(getattr(scope, "tenant_id", "")),
                workspace_id=_text(getattr(scope, "workspace_id", "")),
                project_id=project_id,
                workflow_id=workflow_id,
                actor_id=_text(getattr(scope, "actor_id", "")),
            )
            return service.execute(
                operation="inspect",
                payload={
                    "input": {
                        "artifact_handle": body.artifact_handle,
                        "options": dict(body.options or {}),
                    },
                    "artifact_handle": body.artifact_handle,
                    "options": dict(body.options or {}),
                },
                context=context,
            )

        try:
            envelope = await asyncio.wait_for(asyncio.to_thread(_inspect), timeout=10.0)
        except TimeoutError:
            return JSONResponse(
                {"ok": False, "errors": [{"code": "tabular_feed_inspect_timeout"}]},
                status_code=408,
            )
        except Exception as exc:
            return api_error_response(exc)
        if not envelope.get("ok"):
            return JSONResponse(
                {
                    "ok": False,
                    "errors": envelope.get("errors")
                    if isinstance(envelope.get("errors"), list)
                    else [],
                },
                status_code=400,
            )
        return {"ok": True, "result": _safe_result(envelope)}

    return router


__all__ = ["TabularFeedInspectRequest", "create_api_tabular_feed_router"]
