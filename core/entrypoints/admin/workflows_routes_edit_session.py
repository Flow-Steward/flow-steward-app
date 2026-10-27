# ruff: noqa: F403, F405, I001
"""Edit session routes: compare a browser copy with the server, keep it on explicit choice.

Thin transport over ``workflow_edit_session_service``. The server stores no
session; both routes use the workflow's existing read and write policy.
"""

from __future__ import annotations

from typing import Any

from pydantic import ConfigDict, Field

from core.entrypoints.admin.workflows_common import *  # NOSONAR
from core.entrypoints.admin.workflows_projection import *  # NOSONAR
from core.entrypoints.admin.workflows_scope import *  # NOSONAR
from core.entrypoints.admin.workflows_models import *  # NOSONAR
from core.entrypoints.admin.workflows_routes_write import (
    _raise_consumer_conflict,
    _raise_workflow_revision_conflict,
)
from core.entrypoints.admin.workflows_definition_router import _change_reference_access
from core.application.registry.changelog_projection import (
    redact_changelog_diff,
    redact_changelog_payload,
)
from core.application.registry.db_store_revisions_mixin import WorkflowHistoryComparisonError
from core.application.registry.db_store_workflows_mixin import WorkflowRevisionConflictError
from core.application.workflows.workflow_consumer_guard import WorkflowConsumerConflictError
from core.application.workflows.workflow_definition_service import (
    WorkflowDefinitionChangeError,
    readiness_from_validator,
)
from core.application.workflows.workflow_edit_session_contract import authoring_content
from core.application.workflows.workflow_edit_session_service import (
    apply_workflow_content,
    compare_workflow_session,
)

_EDIT_SESSION_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"description": "The content is not acceptable authoring content or is refused"},
    403: {"description": "Forbidden"},
    404: {"description": "Not found"},
    409: {"description": "The workflow changed after the reviewed revision"},
    503: {"description": "Service unavailable"},
}


class WorkflowEditSessionCompareRequest(BaseModel):
    """A session's base (its last server checkpoint) and its local copy."""

    model_config = ConfigDict(extra="forbid")

    base_content: dict[str, Any]
    local_content: dict[str, Any]


class WorkflowEditSessionApplyRequest(BaseModel):
    """The reviewed local copy and the server revision the author reviewed it against."""

    model_config = ConfigDict(extra="forbid")

    content: dict[str, Any]
    expected_revision: int = Field(ge=1)
    dry_run: bool = False


def _refusal(workflow_id: str, exc: WorkflowDefinitionChangeError, action: str) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail=format_validation_error_detail(
            entity_type="workflow",
            entity_id=workflow_id,
            action=action,
            validation={"valid": False, "errors": exc.issues, "warnings": []},
        ),
    )


def _public_content(portability: Any, workflow: dict[str, Any]) -> dict[str, Any]:
    """The workflow's authoring content without authority and credential values."""
    return redact_changelog_payload(
        authoring_content(portability.redact_protected_workflow(dict(workflow)))
    )


def _public_comparison(portability: Any, comparison: dict[str, Any]) -> dict[str, Any]:
    """The same authority and credential projection as both history diff routes."""
    return redact_changelog_diff(portability.redact_workflow_history_payload(comparison))


@router.post(
    "/{workflow_id}/edit-session/compare",
    responses=_EDIT_SESSION_RESPONSES,
)
def compare_workflow_edit_session(
    workflow_id: str, body: WorkflowEditSessionCompareRequest, request: Request
) -> dict[str, Any]:
    """Compare a browser edit session's copy with the current workflow; writes nothing."""
    db = _get_db()
    workflow, _scope = _resolve_active_workflow_scope_target(db, request, workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail=f"Workflow '{workflow_id}' not found")
    portability = _get_workflow_portability_service(db)
    try:
        result = compare_workflow_session(
            db,
            workflow_id,
            base_content=body.base_content,
            local_content=body.local_content,
            current=workflow,
        )
    except WorkflowDefinitionChangeError as exc:
        raise _refusal(workflow_id, exc, "compare_edit_session") from exc
    except WorkflowHistoryComparisonError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "workflow_id": result["workflow_id"],
        "current_revision": result["current_revision"],
        "current_content": _public_content(portability, workflow),
        **{
            key: _public_comparison(portability, result[key])
            for key in ("base_to_local", "base_to_server", "local_to_server")
        },
    }


@router.post(
    "/{workflow_id}/edit-session/apply",
    responses=_EDIT_SESSION_RESPONSES,
)
def apply_workflow_edit_session(
    workflow_id: str, body: WorkflowEditSessionApplyRequest, request: Request
) -> dict[str, Any]:
    """Write the author's reviewed copy as one revision ("keep my version"), or judge it."""
    db = _get_db()
    workflow, scope = _resolve_active_workflow_scope_target(db, request, workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail=f"Workflow '{workflow_id}' not found")
    _assert_workflow_mutation_allowed(workflow)
    _assert_workflow_structure_change_allowed(db, workflow_id, "apply_changes")
    portability = _get_workflow_portability_service(db)

    def _authorize_locked(locked: dict[str, Any]) -> None:
        _assert_workflow_mutation_allowed(locked)
        _enforce_workflow_row_scope(db, scope, locked)

    try:
        result = apply_workflow_content(
            db,
            workflow_id,
            content=body.content,
            expected_revision=body.expected_revision,
            author=scope.actor_name,
            reference_access=_change_reference_access(db, scope, workflow),
            authorize=_authorize_locked,
            readiness=readiness_from_validator(
                db, lambda store, target_id: _get_workflow_validation(store, target_id)
            ),
            dry_run=body.dry_run,
        )
    except WorkflowRevisionConflictError as exc:
        _raise_workflow_revision_conflict(exc)
    except WorkflowConsumerConflictError as exc:
        _raise_consumer_conflict(exc)
    except WorkflowPortabilityError as exc:
        _raise_portability_http_error(exc)
    except WorkflowDefinitionChangeError as exc:
        raise _refusal(workflow_id, exc, "apply_edit_session") from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result.applied:
        reload_all = getattr(_get_registry(), "reload_all", None)
        if callable(reload_all):
            reload_all()
    return {
        "workflow_id": workflow_id,
        "revision": result.revision,
        "applied": result.applied,
        "valid": result.valid,
        "issues": result.issues,
        "blocking_issues": result.blocking_issues,
        "content": _public_content(portability, result.workflow) if result.workflow else None,
    }


__all__ = [
    "WorkflowEditSessionApplyRequest",
    "WorkflowEditSessionCompareRequest",
    "apply_workflow_edit_session",
    "compare_workflow_edit_session",
]
