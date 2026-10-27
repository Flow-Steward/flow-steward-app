"""Authenticated transport for previewing and applying a group extraction."""

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.application.registry.db_store_workflows_mixin import WorkflowRevisionConflictError
from core.application.workflows.workflow_group_extraction_service import (
    GroupExtractionError,
    GroupExtractionService,
)
from core.application.workflows.workflow_portability import WorkflowPortabilityError
from core.entrypoints.admin.workflows_common import (
    _get_db,
    _platform_logger_for_admin_catalog,
    router,
)
from core.entrypoints.admin.workflows_definition_router import (
    _change_reference_access,
    _project_visible,
)
from core.entrypoints.admin.workflows_routes_write import _raise_workflow_revision_conflict
from core.entrypoints.admin.workflows_scope import (
    _assert_workflow_mutation_allowed,
    _refresh_workflow_cache_after_commit,
    _resolve_active_workflow_scope_target,
)


class GroupExtractionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    child_workflow_id: str
    display_name: str
    expected_revision: int = Field(strict=True, gt=0)
    dry_run: bool = True
    preview_digest: str = ""

    @model_validator(mode="after")
    def require_preview_for_apply(self):
        if not self.dry_run and not self.preview_digest:
            raise ValueError("Apply requires the preview_digest returned by preview.")
        return self


@router.post("/{workflow_id}/groups/{group_id}/convert-to-child")
def convert_group_to_child(
    workflow_id: str, group_id: str, body: GroupExtractionRequest, request: Request
):
    db = _get_db()
    workflow, scope = _resolve_active_workflow_scope_target(db, request, workflow_id)
    if not workflow:
        raise HTTPException(404, "Workflow not found.")
    _assert_workflow_mutation_allowed(workflow)
    logger = _platform_logger_for_admin_catalog(db)
    if logger is None:
        raise HTTPException(503, "Workflow reaction validation is unavailable.")
    service = GroupExtractionService(
        db,
        account_id=str(workflow.get("account_id") or scope.account_id),
        reference_access=_change_reference_access(db, scope, workflow),
        project_visible=lambda project_id: _project_visible(db, scope, project_id),
        reactions=lambda workflow_id, account_id: logger.list_workflow_reactions(
            workflow_id=workflow_id, account_id=account_id, enabled_only=False
        ),
    )
    arguments = {
        **body.model_dump(exclude={"dry_run", "preview_digest"}),
        "workflow_id": workflow_id,
        "group_id": group_id,
    }
    try:
        if body.dry_run:
            return service.preview(**arguments)
        result = service.apply(
            **arguments, preview_digest=body.preview_digest, author=scope.actor_name
        )
    except WorkflowRevisionConflictError as exc:
        _raise_workflow_revision_conflict(exc)
    except GroupExtractionError as exc:
        raise HTTPException(exc.status_code, {"message": str(exc), "issues": exc.issues}) from exc
    except WorkflowPortabilityError as exc:
        raise HTTPException(exc.status_code, exc.detail()) from exc
    _refresh_workflow_cache_after_commit()
    return result
