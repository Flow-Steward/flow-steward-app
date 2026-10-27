"""Authenticated authoring transport for workflow canvas notes."""

from contextlib import contextmanager
from typing import Annotated

from fastapi import File, Form, HTTPException, Query, Request, Response, UploadFile
from pydantic import BaseModel, ConfigDict

from core.application.registry.db_store_workflows_mixin import WorkflowRevisionConflictError
from core.application.workflows.workflow_canvas_annotation_contract import (
    CanvasAnnotation,
    WorkflowAnnotationsRevisionConflictError,
)
from core.application.workflows.workflow_canvas_annotation_service import (
    WorkflowCanvasAnnotationError,
    WorkflowCanvasAnnotationService,
)
from core.application.workflows.workflow_portability import (
    WorkflowPortabilityError,
    WorkflowPortabilityService,
)
from core.entrypoints.admin.deps import get_artifact_plane
from core.entrypoints.admin.workflows_common import _get_db, router
from core.entrypoints.admin.workflows_routes_write import _raise_workflow_revision_conflict
from core.entrypoints.admin.workflows_scope import (
    _assert_workflow_mutation_allowed,
    _refresh_workflow_cache_after_commit,
    _resolve_active_workflow_scope_target,
)
from core.infrastructure.config.workflow_file_limits import file_preview_max_bytes


class AnnotationSave(BaseModel):
    model_config = ConfigDict(extra="forbid")
    annotation: CanvasAnnotation
    expected_revision: int | None = None
    expected_annotations_revision: int | None = None


@contextmanager
def _annotation_request(request: Request, workflow_id: str, *, write: bool = False):
    db = _get_db()
    workflow, scope = _resolve_active_workflow_scope_target(db, request, workflow_id)
    if not workflow:
        raise HTTPException(404, "Workflow not found.")
    if write:
        _assert_workflow_mutation_allowed(workflow)
    try:
        if write:
            # Notes are part of the author graph; reading them is allowed.
            WorkflowPortabilityService(db).assert_structure_change_allowed(
                workflow_id, action="canvas_annotations"
            )
        service = WorkflowCanvasAnnotationService(db, get_artifact_plane())
        yield (
            service,
            {
                "workflow_id": workflow_id,
                "account_id": str(workflow.get("account_id") or scope.account_id),
            },
        )
    except WorkflowCanvasAnnotationError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    except WorkflowRevisionConflictError as exc:
        _raise_workflow_revision_conflict(exc)
    except WorkflowAnnotationsRevisionConflictError as exc:
        raise HTTPException(
            409,
            detail={
                "code": "workflow_annotations_revision_conflict",
                "workflow_id": exc.workflow_id,
                "expected_annotations_revision": exc.expected_annotations_revision,
                "message": str(exc),
            },
        ) from exc
    except WorkflowPortabilityError as exc:
        raise HTTPException(exc.status_code, exc.detail()) from exc
    if write:
        _refresh_workflow_cache_after_commit()


@router.get("/{workflow_id}/annotations")
def list_canvas_annotations(workflow_id: str, request: Request):
    with _annotation_request(request, workflow_id) as (service, scope):
        return service.list_notes(**scope)


@router.put("/{workflow_id}/annotations/{annotation_id}")
def save_canvas_annotation(
    workflow_id: str, annotation_id: str, body: AnnotationSave, request: Request
):
    if annotation_id != body.annotation.id:
        raise HTTPException(422, "The note identifier must match its URL.")
    with _annotation_request(request, workflow_id, write=True) as (service, scope):
        return service.save_note(
            **scope,
            annotation=body.annotation.model_dump(),
            expected_revision=body.expected_revision,
            expected_annotations_revision=body.expected_annotations_revision,
        )


@router.delete("/{workflow_id}/annotations/{annotation_id}")
def delete_canvas_annotation(
    workflow_id: str,
    annotation_id: str,
    request: Request,
    expected_revision: Annotated[int | None, Query()] = None,
    expected_annotations_revision: Annotated[int | None, Query()] = None,
):
    with _annotation_request(request, workflow_id, write=True) as (service, scope):
        return service.delete_note(
            **scope,
            annotation_id=annotation_id,
            expected_revision=expected_revision,
            expected_annotations_revision=expected_annotations_revision,
        )


@router.put("/{workflow_id}/annotations/{annotation_id}/image")
def upload_canvas_annotation_image(
    workflow_id: str,
    annotation_id: str,
    request: Request,
    file: Annotated[UploadFile, File(...)],
    expected_revision: Annotated[int | None, Form()] = None,
    expected_annotations_revision: Annotated[int | None, Form()] = None,
):
    with _annotation_request(request, workflow_id, write=True) as (service, scope):
        return service.set_image(
            **scope,
            annotation_id=annotation_id,
            expected_revision=expected_revision,
            expected_annotations_revision=expected_annotations_revision,
            filename=file.filename or "",
            content_type=file.content_type or "",
            body=file.file.read(file_preview_max_bytes() + 1),
        )


@router.delete("/{workflow_id}/annotations/{annotation_id}/image")
def remove_canvas_annotation_image(
    workflow_id: str,
    annotation_id: str,
    request: Request,
    expected_revision: Annotated[int | None, Query()] = None,
    expected_annotations_revision: Annotated[int | None, Query()] = None,
):
    with _annotation_request(request, workflow_id, write=True) as (service, scope):
        return service.clear_image(
            **scope,
            annotation_id=annotation_id,
            expected_revision=expected_revision,
            expected_annotations_revision=expected_annotations_revision,
        )


@router.get("/{workflow_id}/annotations/{annotation_id}/image")
def preview_canvas_annotation_image(workflow_id: str, annotation_id: str, request: Request):
    with _annotation_request(request, workflow_id) as (service, scope):
        body, content_type = service.preview_image(**scope, annotation_id=annotation_id)
        return Response(
            content=body,
            media_type=content_type,
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "sandbox; default-src 'none'; img-src 'self'",
            },
        )
