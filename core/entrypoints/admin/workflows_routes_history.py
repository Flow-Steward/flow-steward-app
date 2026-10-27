# ruff: noqa: F403, F405, I001, RUF022
"""Workflow admin route/helper module extracted from workflows."""

from __future__ import annotations

from core.entrypoints.admin.workflows_common import *  # NOSONAR
from core.entrypoints.admin.workflows_projection import *  # NOSONAR
from core.entrypoints.admin.workflows_scope import *  # NOSONAR
from core.entrypoints.admin.workflows_models import *  # NOSONAR
from core.entrypoints.admin.workflows_routes_write import (
    _workflow_revision_guard,
    _raise_consumer_conflict,
    _raise_workflow_revision_conflict,
)
from core.entrypoints.admin.workflows_definition_router import _change_reference_access
from core.application.registry.changelog_projection import redact_changelog_diff
from core.application.registry.db_store_common import LIFECYCLE_CHANGELOG_ACTIONS
from core.application.registry.db_store_revisions_mixin import WorkflowHistoryComparisonError
from core.application.registry.db_store_workflows_mixin import WorkflowRevisionConflictError
from core.application.workflows.workflow_consumer_guard import WorkflowConsumerConflictError
from core.application.workflows.workflow_definition_service import (
    WorkflowDefinitionChangeError,
    readiness_from_validator,
    restore_workflow_snapshot,
)


@router.get(
    "/{workflow_id}/stats",
    responses={
        400: {"description": "Bad request"},
        403: {"description": "Forbidden"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def get_workflow_stats(workflow_id: str, request: Request):
    """Aggregate job + cost statistics for a workflow."""
    db = _get_db()
    wf, scope = _resolve_workflow_scope_target(db, request, workflow_id)
    if not wf:
        raise HTTPException(status_code=404, detail=f"Workflow '{workflow_id}' not found")
    bl = _platform_job_store_for_workflow_stats(db)
    if bl is None:
        raise HTTPException(
            status_code=503,
            detail="Workflow statistics store is unavailable",
        )
    try:
        return bl.get_workflow_stats(
            workflow_id=workflow_id,
            account_id=scope.account_id,
        )
    except Exception as exc:
        logger.warning("workflow stats query failed for %s: %s", workflow_id, exc)
        raise HTTPException(status_code=500, detail="Failed to load workflow stats") from exc


@router.get(
    "/{workflow_id}/history",
    responses={
        400: {"description": "Bad request"},
        403: {"description": "Forbidden"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def get_workflow_history(
    workflow_id: str,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
):
    db = _get_db()
    _resolve_workflow_scope_target(db, request, workflow_id)
    entries = db.get_entity_history(entity_type="workflow", entity_id=workflow_id, limit=limit)
    return _get_workflow_portability_service(db).redact_workflow_history(
        entries,
        workflow_id=workflow_id,
    )


@router.get(
    "/{workflow_id}/history/{revision}",
    responses={
        400: {"description": "Bad request"},
        403: {"description": "Forbidden"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def get_workflow_revision(workflow_id: str, revision: int, request: Request):
    db = _get_db()
    entry = db.get_entity_revision(entity_type="workflow", entity_id=workflow_id, revision=revision)
    if not entry:
        raise HTTPException(
            status_code=404, detail=f"Revision {revision} not found for workflow '{workflow_id}'"
        )
    workflow, _scope = _resolve_workflow_scope_target(
        db, request, workflow_id, history_revision=revision
    )
    if workflow is None or _normalize_text(
        workflow.get("workflow_id") or workflow.get("id")
    ) != _normalize_text(workflow_id):
        raise HTTPException(status_code=404, detail=f"Workflow '{workflow_id}' not found")
    return _get_workflow_portability_service(db).redact_workflow_history_entry(
        entry,
        workflow_id=workflow_id,
    )


@router.get(
    "/{workflow_id}/diff",
    responses={
        400: {"description": "Bad request"},
        403: {"description": "Forbidden"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def get_workflow_diff(
    workflow_id: str,
    request: Request,
    from_entry_id: Annotated[str | None, Query()] = None,
    to_entry_id: Annotated[str | None, Query()] = None,
):
    db = _get_db()
    try:
        _resolve_workflow_scope_target(
            db,
            request,
            workflow_id,
            changelog_entry_id=to_entry_id or from_entry_id,
        )
        _get_workflow_portability_service(db).assert_history_diff_allowed(
            workflow_id,
            from_entry_id=from_entry_id,
            to_entry_id=to_entry_id,
        )
        # The same projection as GET /api/changelog/workflow/{id}/diff: drop
        # server-owned secret authority, then mask credential values by path.
        return redact_changelog_diff(
            _get_workflow_portability_service(db).redact_workflow_history_payload(
                db.diff_entity_history(
                    entity_type="workflow",
                    entity_id=workflow_id,
                    from_entry_id=from_entry_id,
                    to_entry_id=to_entry_id,
                )
            )
        )
    except WorkflowPortabilityError as exc:
        _raise_portability_http_error(exc)
    except WorkflowHistoryComparisonError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post(
    "/{workflow_id}/restore",
    responses={
        400: {"description": "Bad request"},
        403: {"description": "Forbidden"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def restore_workflow_revision(workflow_id: str, body: RestoreRevisionRequest, request: Request):
    return restore_workflow_history_entry(
        request,
        workflow_id,
        entry_id=str(body.entry_id),
        expected_revision=body.expected_revision,
    )


def restore_workflow_history_entry(
    request: Request,
    workflow_id: str,
    *,
    entry_id: str,
    expected_revision: int | None,
    db: Any = None,
    registry: Any = None,
) -> dict[str, Any]:
    """Restore one history entry as a new revision; both restore routes answer with this.

    The definition service judges the restored definition like an Apply: an
    enabled workflow takes only a ready definition, a disabled one stores an
    incomplete definition and reports its issues.
    """
    db = db if db is not None else _get_db()
    try:
        workflow, scope = _resolve_workflow_scope_target(
            db,
            request,
            workflow_id,
            changelog_entry_id=entry_id,
        )
        if workflow is None:
            raise HTTPException(status_code=404, detail=f"Workflow '{workflow_id}' not found")
        _assert_workflow_mutation_allowed(workflow)
        portability = _get_workflow_portability_service(db)
        portability.assert_restore_allowed(
            workflow_id, entry_id=entry_id, expected_revision=expected_revision
        )
        guarded_revision = _workflow_revision_guard(expected_revision, workflow)
        entry = db.get_changelog_entry(entry_id, entity_type="workflow", entity_id=workflow_id)
        if not entry:
            raise HTTPException(
                status_code=404,
                detail=f"Revision '{entry_id}' was not found for workflow '{workflow_id}'",
            )
        if str(entry.get("action") or "").strip().lower() in LIFECYCLE_CHANGELOG_ACTIONS:
            raise HTTPException(
                status_code=400,
                detail="Selected entry records a lifecycle transition, not a content revision",
            )

        def _authorize_locked(locked: dict[str, Any]) -> None:
            _assert_workflow_mutation_allowed(locked)
            portability.assert_restore_allowed(
                workflow_id, entry_id=entry_id, expected_revision=expected_revision
            )

        result = restore_workflow_snapshot(
            db,
            workflow_id,
            snapshot=entry.get("snapshot") or {},
            expected_revision=int(guarded_revision or 0),
            author=scope.actor_name,
            reference_access=_change_reference_access(db, scope, workflow),
            authorize=_authorize_locked,
            readiness=readiness_from_validator(
                db, lambda store, target_id: _get_workflow_validation(store, target_id)
            ),
        )
    except WorkflowRevisionConflictError as exc:
        _raise_workflow_revision_conflict(exc)
    except WorkflowConsumerConflictError as exc:
        _raise_consumer_conflict(exc)
    except WorkflowPortabilityError as exc:
        _raise_portability_http_error(exc)
    except WorkflowDefinitionChangeError as exc:
        raise HTTPException(
            status_code=400,
            detail=format_validation_error_detail(
                entity_type="workflow",
                entity_id=workflow_id,
                action="restore",
                validation={"valid": False, "errors": exc.issues, "warnings": []},
            ),
        ) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    (registry if registry is not None else _get_registry()).reload_all()
    return {
        "status": "restored",
        "workflow_id": workflow_id,
        "restored_from_entry_id": entry_id,
        "revision": result.revision,
        "blocking_issues": result.blocking_issues,
        "validation": {"valid": result.valid, "errors": result.issues, "warnings": []},
        "snapshot": _get_workflow_portability_service(db).redact_workflow_history_payload(
            result.workflow
        ),
    }


@router.get(
    "/meta/agent-skill-pairs",
    responses={
        400: {"description": "Bad request"},
        403: {"description": "Forbidden"},
        404: {"description": "Not found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def list_agent_skill_pairs(request: Request, agent_id: Annotated[str | None, Query()] = None):
    """Return existing agent+skill pairs for quick step creation."""
    db = _get_db()
    scope = _request_scope(request)
    if agent_id:
        agent_row = db.get_agent(agent_id) if hasattr(db, "get_agent") else None
        if isinstance(agent_row, dict) and not _agent_visible_to_scope(scope, agent_row):
            raise HTTPException(status_code=403, detail=ACCESS_DENIED_VALUE)
    pairs = db.list_agent_skill_pairs(agent_id=agent_id, enabled_only=True)
    visible_pairs = []
    for item in pairs:
        if not isinstance(item, dict):
            continue
        pair_agent_id = _normalize_text(item.get("agent_id"))
        agent_row = (
            db.get_agent(pair_agent_id) if pair_agent_id and hasattr(db, "get_agent") else None
        )
        if isinstance(agent_row, dict) and not _agent_visible_to_scope(scope, agent_row):
            continue
        visible_pairs.append(item)
    return {
        "pairs": visible_pairs,
        "count": len(visible_pairs),
    }


__all__ = [
    "get_workflow_stats",
    "get_workflow_history",
    "get_workflow_revision",
    "get_workflow_diff",
    "restore_workflow_revision",
    "restore_workflow_history_entry",
    "list_agent_skill_pairs",
]
