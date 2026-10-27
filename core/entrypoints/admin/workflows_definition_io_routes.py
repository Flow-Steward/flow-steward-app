# mypy: ignore-errors
from __future__ import annotations

from typing import Annotated, Any, Literal

from core.entrypoints.admin.workflows_definition_common import (
    HTTPException,
    Query,
    Request,
    TransformPreviewRequest,
    WorkflowEditorAvailableVariablesView,
    WorkflowEditorInputDefaultsView,
    WorkflowEditorIOIssueView,
    WorkflowEditorOutputView,
    WorkflowEditorStepContractPreviewView,
)


def _require_step(workflow: Any, step_id: str) -> None:
    steps = workflow.get("steps") if isinstance(workflow, dict) else None
    for step in steps if isinstance(steps, list) else []:
        if isinstance(step, dict) and str(step.get("step_id") or "") == step_id:
            return
    raise HTTPException(status_code=404, detail=f"Step '{step_id}' not found")


def _require_for_each(workflow: Any, for_each_id: str) -> None:
    items = workflow.get("for_each") if isinstance(workflow, dict) else None
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and str(item.get("for_each_id") or "") == for_each_id:
            return
    raise HTTPException(status_code=404, detail=f"For each '{for_each_id}' not found")


def _preview_state_for_source(source_path: str, sample_data: Any) -> dict[str, Any]:
    from core.application.orchestration.step_templating import STATE_PREFIX, set_state_path

    source = str(source_path or "").strip()
    if source == "state":
        return dict(sample_data) if isinstance(sample_data, dict) else {"preview": sample_data}
    state: dict[str, Any] = {}
    set_state_path(state, {}, source, sample_data, mirror_workflow_data=False)
    if source.startswith(STATE_PREFIX):
        without_prefix = source[len(STATE_PREFIX) :]
        if without_prefix.startswith("workflow_data."):
            set_state_path(state, {}, without_prefix, sample_data, mirror_workflow_data=False)
    return state


def register_io_routes(router, ctx) -> None:
    get_db = ctx.get_db
    _assert_step_test_run_allowed = ctx.assert_step_test_run_allowed
    _resolve_step_contract_preview = ctx.resolve_step_contract_preview
    _authorize_workflow_request = ctx.authorize_workflow_request

    @router.get(
        "/{workflow_id}/steps/{step_id}/available-outputs",
        response_model=list[WorkflowEditorOutputView],
        responses={
            400: {"description": "Bad request"},
            404: {"description": "Not found"},
            409: {"description": "Conflict"},
            422: {"description": "Validation error"},
            500: {"description": "Internal server error"},
            503: {"description": "Service unavailable"},
        },
    )
    def get_step_available_outputs(request: Request, workflow_id: str, step_id: str):
        """Outputs of the steps that run before this one (what it can read)."""
        db = get_db()
        _require_step(_authorize_workflow_request(request, db, workflow_id), step_id)
        from core.application.registry.io_resolver import IOResolver

        resolver = IOResolver(db)
        return resolver.get_available_outputs_for_step(workflow_id, step_id)

    @router.get(
        "/{workflow_id}/steps/{step_id}/outputs",
        response_model=list[WorkflowEditorOutputView],
        responses={
            400: {"description": "Bad request"},
            404: {"description": "Not found"},
            503: {"description": "Service unavailable"},
        },
    )
    def get_step_outputs(request: Request, workflow_id: str, step_id: str):
        """Outputs this step produces (for its boundary exit picker)."""
        db = get_db()
        _require_step(_authorize_workflow_request(request, db, workflow_id), step_id)
        from core.application.registry.io_resolver import IOResolver

        resolver = IOResolver(db)
        return resolver.get_step_outputs(workflow_id, step_id)

    @router.get(
        "/{workflow_id}/steps/{step_id}/io-validation",
        response_model=list[WorkflowEditorIOIssueView],
        responses={
            400: {"description": "Bad request"},
            404: {"description": "Not found"},
            409: {"description": "Conflict"},
            422: {"description": "Validation error"},
            500: {"description": "Internal server error"},
            503: {"description": "Service unavailable"},
        },
    )
    def validate_step_io(request: Request, workflow_id: str, step_id: str):
        """Validate that the step's required inputs are available from upstream steps."""
        db = get_db()
        _require_step(_authorize_workflow_request(request, db, workflow_id), step_id)
        from core.application.registry.io_resolver import IOResolver

        resolver = IOResolver(db)
        return resolver.validate_step_io(workflow_id, step_id)

    @router.get(
        "/{workflow_id}/steps/{step_id}/available-variables",
        response_model=WorkflowEditorAvailableVariablesView,
        responses={
            400: {"description": "Bad request"},
            404: {"description": "Not found"},
            409: {"description": "Conflict"},
            422: {"description": "Validation error"},
            500: {"description": "Internal server error"},
            503: {"description": "Service unavailable"},
        },
    )
    def get_step_available_variables(
        request: Request,
        workflow_id: str,
        step_id: str,
        position: Annotated[Literal["before", "after"], Query()] = "before",
    ):
        """Variables available to this step (state + dotted paths + upstream outputs).

        ``position=after`` answers for a new step placed right after this one:
        it adds this step's own outputs. Both include the item names of the
        For each the step sits in.
        """
        db = get_db()
        _require_step(_authorize_workflow_request(request, db, workflow_id), step_id)
        from core.application.registry.io_resolver import IOResolver

        return IOResolver(db).get_available_variables(
            workflow_id, step_id=step_id, position=position
        )

    @router.get(
        "/{workflow_id}/available-variables",
        response_model=WorkflowEditorAvailableVariablesView,
        responses={
            400: {"description": "Bad request"},
            404: {"description": "Not found"},
            409: {"description": "Conflict"},
            422: {"description": "Validation error"},
            500: {"description": "Internal server error"},
            503: {"description": "Service unavailable"},
        },
    )
    def get_workflow_available_variables(
        request: Request,
        workflow_id: str,
        for_each_id: Annotated[str, Query()] = "",
    ):
        """Variables for a step whose place in the graph is not known yet.

        Every saved step's outputs (``position`` is ``unknown``), the global and
        dotted-path variables, and with ``for_each_id`` that For each's item names.
        """
        db = get_db()
        workflow = _authorize_workflow_request(request, db, workflow_id)
        if for_each_id:
            _require_for_each(workflow, for_each_id)
        from core.application.registry.io_resolver import IOResolver

        return IOResolver(db).get_available_variables(workflow_id, for_each_id=for_each_id)

    @router.get(
        "/{workflow_id}/steps/{step_id}/input-defaults",
        response_model=WorkflowEditorInputDefaultsView,
        responses={
            400: {"description": "Bad request"},
            404: {"description": "Not found"},
            409: {"description": "Conflict"},
            422: {"description": "Validation error"},
            500: {"description": "Internal server error"},
            503: {"description": "Service unavailable"},
        },
    )
    def get_step_input_defaults(request: Request, workflow_id: str, step_id: str):
        """Required/optional boundary entry defaults derived from the step's contracts."""
        db = get_db()
        _require_step(_authorize_workflow_request(request, db, workflow_id), step_id)
        from core.application.registry.io_resolver import IOResolver

        resolver = IOResolver(db)
        return resolver.get_step_input_defaults(workflow_id, step_id)

    @router.get(
        "/{workflow_id}/step-contract-preview",
        response_model=WorkflowEditorStepContractPreviewView,
        responses={
            400: {"description": "Bad request"},
            404: {"description": "Not found"},
            409: {"description": "Conflict"},
            422: {"description": "Validation error"},
            500: {"description": "Internal server error"},
            503: {"description": "Service unavailable"},
        },
    )
    def get_step_contract_preview(
        request: Request,
        workflow_id: str,
        step_id: Annotated[str, Query()] = "",
        agent_id: Annotated[str, Query()] = "",
        participant_key: Annotated[str, Query()] = "",
        block_key: Annotated[str, Query()] = "",
        block_binding_mode: Annotated[Literal["auto", "explicit"] | None, Query()] = None,
    ):
        db = get_db()
        _authorize_workflow_request(request, db, workflow_id)
        return _resolve_step_contract_preview(
            workflow_id=workflow_id,
            step_id=step_id,
            agent_id=agent_id,
            participant_key=participant_key,
            block_key=block_key,
            **(
                {"block_binding_mode": block_binding_mode} if block_binding_mode is not None else {}
            ),
        )

    @router.get(
        "/{workflow_id}/transform-actions",
        responses={
            400: {"description": "Bad request"},
            404: {"description": "Not found"},
            409: {"description": "Conflict"},
            422: {"description": "Validation error"},
            500: {"description": "Internal server error"},
            503: {"description": "Service unavailable"},
        },
    )
    def get_transform_actions(request: Request, workflow_id: str):
        db = get_db()
        _authorize_workflow_request(request, db, workflow_id)

        from core.application.orchestration.transform_step_runtime import (
            TRANSFORM_ACTION_CATALOG_VERSION,
            list_transform_action_contracts,
        )

        return {
            "catalog_version": TRANSFORM_ACTION_CATALOG_VERSION,
            "actions": list_transform_action_contracts(),
        }

    @router.post(
        "/{workflow_id}/transform-preview",
        responses={
            400: {"description": "Bad request"},
            404: {"description": "Not found"},
            409: {"description": "Conflict"},
            422: {"description": "Validation error"},
            500: {"description": "Internal server error"},
            503: {"description": "Service unavailable"},
        },
    )
    def preview_transform_step(
        request: Request,
        workflow_id: str,
        payload: TransformPreviewRequest,
    ):
        db = get_db()
        _authorize_workflow_request(request, db, workflow_id)
        # Runs an author transform on sample data: isolated execution, not a read.
        _assert_step_test_run_allowed(db, workflow_id, "transform_preview")

        from core.application.orchestration.transform_step_runtime import execute_transform_step

        config = dict(payload.config or {})
        source_path = str(((config.get("input") or {}).get("source")) or "").strip()
        state = _preview_state_for_source(source_path, payload.sample_data)
        try:
            result = execute_transform_step(config=config, state=state)
        except Exception as exc:
            result = {
                "ok": False,
                "errors": [{"reason": "preview_failed", "detail": str(exc)}],
                "state_patch": {},
                "detail": "runtime_failed",
            }
        result["preview"] = True
        return result
