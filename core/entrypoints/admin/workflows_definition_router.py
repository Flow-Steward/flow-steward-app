# ruff: noqa: F403, F405, I001
"""Workflow phase/step nested router registration."""

from __future__ import annotations

from core.entrypoints.admin.workflows_common import *  # NOSONAR
from core.entrypoints.admin.workflows_models import *  # NOSONAR
from core.entrypoints.admin.workflows_projection import *  # NOSONAR
from core.entrypoints.admin.workflows_scope import *  # NOSONAR
from core.application.workflows.test_run_query_service import WorkflowTestRunQueryService
from core.application.workflows.platform_test_run_runtime import get_workflow_test_run_state
from core.entrypoints.admin.deps import enqueue_task
from core.application.workflows.workflow_change_authorization import (
    ChangeReferenceAccess,
    store_reference_access,
)
from core.infrastructure.runtime.workflow_db_adapters import (
    create_workflow_test_run_repository,
)


_workflow_test_run_query_service = WorkflowTestRunQueryService(
    repository=create_workflow_test_run_repository(get_db=lambda: _get_db()),
)


def _durable_test_run_state(thread_id: str) -> dict:
    """Combine the checkpoint payload with the durable job lifecycle state.

    The job row is authoritative for queue/terminal status, while the checkpoint
    owns the selected step's structured inputs and result. Returning only the
    former makes a successful asynchronous step test look empty to the builder.
    """

    return get_workflow_test_run_state(
        thread_id,
        durable_state_reader=_workflow_test_run_query_service.get_public_state,
    )


def _project_visible(db, scope, project_id: str) -> bool:
    try:
        _enforce_project_scope(db, scope, project_id)
    except HTTPException:
        return False
    return True


def _change_reference_access(db, scope, workflow: dict) -> ChangeReferenceAccess:
    """What a change set of *workflow* may reference, for this request's scope."""
    return store_reference_access(
        db,
        account_id=str(workflow.get("account_id") or "").strip() or DEFAULT_ACCOUNT_ID,
        workflow_visible=lambda row: _workflow_visible_to_scope(scope, dict(row)),
        project_visible=lambda project_id: _project_visible(db, scope, project_id),
    )


router.include_router(
    create_workflow_definition_router(
        get_db=lambda: _get_db(),
        get_registry=lambda: _get_registry(),
        get_workflow_validation=lambda db, workflow_id: _get_workflow_validation(db, workflow_id),
        get_step_test_run_validation=lambda db, workflow_id, step_id: _get_step_test_run_validation(
            db, workflow_id, step_id
        ),
        get_readiness_validation=lambda store, workflow_id: _get_workflow_validation(
            store, workflow_id
        ),
        resolve_workflow_row=lambda db, workflow_id: _resolve_workflow_row(
            db,
            workflow_id,
        ),
        assert_workflow_structure_change_allowed=(
            lambda db, workflow_id, action: _assert_workflow_structure_change_allowed(
                db,
                workflow_id,
                action,
            )
        ),
        assert_workflow_step_test_run_allowed=(
            lambda db, workflow_id, action: _assert_workflow_step_test_run_allowed(
                db,
                workflow_id,
                action,
            )
        ),
        request_scope=lambda request: _request_scope(request),
        enforce_workflow_row_scope=lambda db, scope, workflow: _enforce_workflow_row_scope(
            db,
            scope,
            workflow,
        ),
        assert_workflow_mutation_allowed=lambda workflow: _assert_workflow_mutation_allowed(
            workflow
        ),
        validate_context_inject_sources=lambda context_inject: _validate_context_inject_sources(
            context_inject
        ),
        change_reference_access=_change_reference_access,
        durable_test_run_job_state=_durable_test_run_state,
        enqueue_test_run=lambda **kwargs: enqueue_task(**kwargs),
    )
)


__all__ = []
