"""Workflow definition, simulation, test-run and admin I/O routes."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.registry.db_store_workflows_mixin import WorkflowRevisionConflictError
from core.application.registry.step_skill_resolution import (
    agent_primary_skill_id,
    catalog_agent_row,
    catalog_skill_row,
)
from core.application.workflows.workflow_change_authorization import (
    ChangeReferenceAccess,
    authorize_change_operations,
)
from core.application.workflows.workflow_change_normalization import (
    WorkflowChangeNormalizationError,
    normalize_change_operations,
    resolve_agent_for_participant,
)
from core.application.workflows.workflow_consumer_guard import WorkflowConsumerConflictError
from core.application.workflows.workflow_definition_service import (
    WorkflowDefinitionChangeError,
    apply_workflow_changes,
    readiness_from_validator,
)
from core.entrypoints.admin.workflows_definition_common import (
    ASSESS_WHETHER_VERSION_2_4_0_IS_READY_TO_SHIP_MESSAGE,
    PROJECT_ID_IS_REQUIRED_MESSAGE,
    RELEASE_READINESS_REPORT_VALUE,
    SIMULATE_REQUEST_BODY,
    SIMULATE_RESPONSE_EXAMPLE,
    SIMULATE_TASK_ENVELOPE_EXAMPLE,
    STEP_KIND_AGENT,
    STEP_KIND_CONNECTOR,
    STEP_KIND_INVOKE_WORKFLOW,
    STEP_TEST_RUN_REQUEST_BODY,
    STEP_TEST_RUN_TASK_ENVELOPE_EXAMPLE,
    SUPPORTED_STEP_KINDS,
    TEST_RUN_REQUEST_BODY,
    TEST_RUN_RESPONSE_EXAMPLE,
    TEST_RUN_TASK_ENVELOPE_EXAMPLE,
    VALIDATE_WORKFLOW_PATH_VALUE,
    APIRouter,
    BaseModel,
    Body,
    ConfigDict,
    Field,
    HTTPException,
    Query,
    Request,
    SimulateRequest,
    StepTestRunRequest,
    TestRunRequest,
    WorkflowChangeSet,
    WorkflowDefinitionChangeResultView,
    WorkflowEditorAvailableVariablesView,
    WorkflowEditorInputDefaultsView,
    WorkflowEditorIOIssueView,
    WorkflowEditorOutputView,
    WorkflowEditorSimulationResultView,
    WorkflowEditorStepContractPreviewView,
    WorkflowEditorTestRunResultView,
    _test_run_task_envelope,
    field_validator,
    format_validation_error_detail,
    logger,
    normalize_step_kind,
    step_requires_agent,
    validate_contract_items,
    validate_invoke_workflow_step,
)
from core.entrypoints.admin.workflows_definition_io_routes import register_io_routes
from core.entrypoints.admin.workflows_definition_run_routes import register_run_routes

#: Step fields whose agent binding the route derives before the change service sees them.


def _definition_persistence_http_exception(error: Exception) -> HTTPException:
    """A database refusal of the definition write, as the author can act on it.

    A step that names a connector install, skill or block the database does not
    have is the author's reference to fix, not a server failure.
    """
    message = str(error)
    if "violates foreign key" not in message.lower():
        return HTTPException(status_code=500, detail=message)
    if "install_id_fkey" in message:
        return HTTPException(status_code=404, detail="Connector install not found")
    if "skill_id_fkey" in message:
        return HTTPException(status_code=404, detail="Skill not found")
    if "block_key_fkey" in message:
        return HTTPException(status_code=404, detail="Published block not found")
    return HTTPException(status_code=400, detail="Referenced workflow dependency not found")


def create_workflow_definition_router(
    *,
    get_db: Callable[[], Any],
    get_registry: Callable[[], Any],
    get_workflow_validation: Callable[[Any, str], dict],
    get_step_test_run_validation: Callable[[Any, str, str], dict],
    get_readiness_validation: Callable[[Any, str], dict],
    resolve_workflow_row: Callable[[Any, str], dict | None],
    assert_workflow_structure_change_allowed: Callable[[Any, str, str], None],
    assert_workflow_step_test_run_allowed: Callable[[Any, str, str], None],
    request_scope: Callable[[Request], Any],
    enforce_workflow_row_scope: Callable[[Any, Any, dict[str, Any]], dict[str, Any]],
    assert_workflow_mutation_allowed: Callable[[dict[str, Any]], None],
    validate_context_inject_sources: Callable[[dict | None], None],
    change_reference_access: Callable[[Any, Any, dict[str, Any]], ChangeReferenceAccess],
    durable_test_run_job_state: Callable[[str], dict[str, Any]],
    enqueue_test_run: Callable[..., int],
) -> APIRouter:
    router = APIRouter(tags=["workflows-definition"])

    def _assert_change_allowed(db: Any, workflow_id: str, action: str) -> None:
        assert_workflow_structure_change_allowed(db, workflow_id, action)

    def _assert_step_test_run_allowed(db: Any, workflow_id: str, action: str) -> None:
        assert_workflow_step_test_run_allowed(db, workflow_id, action)

    def _authorize_workflow_request(request: Request, db: Any, workflow_id: str) -> dict[str, Any]:
        workflow = resolve_workflow_row(db, workflow_id)
        if not workflow:
            raise HTTPException(status_code=404, detail=f"Workflow '{workflow_id}' not found")
        return enforce_workflow_row_scope(db, request_scope(request), workflow)

    def _authorize_workflow_mutation_request(
        request: Request, db: Any, workflow_id: str
    ) -> dict[str, Any]:
        workflow = _authorize_workflow_request(request, db, workflow_id)
        assert_workflow_mutation_allowed(workflow)
        return workflow

    def _preview_text(value: Any) -> str:
        if value is None or value.__class__.__name__ in {"MagicMock", "Mock"}:
            return ""
        if isinstance(value, (str, int)):
            return str(value).strip()
        return ""

    def _preview_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def _workflow_account_id(workflow: Any) -> str:
        """The account a workflow's capabilities must be resolved in."""
        if isinstance(workflow, dict):
            candidate = workflow.get("account_id")
        else:
            candidate = getattr(workflow, "account_id", "")
        normalized = str(candidate or "").strip()
        return normalized or DEFAULT_ACCOUNT_ID

    def _resolve_primary_skill_for_agent(agent_id: str, *, account_id: str = "") -> str:
        if account_id:
            return agent_primary_skill_id(get_db(), agent_id, account_id=account_id)
        return agent_primary_skill_id(get_db(), agent_id)

    def _build_simulate_step_preview(workflow: Any) -> list[dict[str, Any]]:
        """The workflow's own steps as a run would resolve their capabilities.

        Resolved in the workflow's own account: an extension-projected agent
        installed for a non-default account is invisible from the default one,
        so the preview reported "no primary skill" for a step that runs.
        """
        preview_account_id = _workflow_account_id(workflow)
        raw_steps = getattr(workflow, "steps", None)
        if not raw_steps and isinstance(workflow, dict):
            raw_steps = workflow.get("steps")

        def _get_attr(row: Any, key: str, default: Any = None) -> Any:
            if isinstance(row, dict):
                return row.get(key, default)
            return getattr(row, key, default)

        plan: list[dict[str, Any]] = []
        for step in raw_steps or []:
            step_id = _preview_text(_get_attr(step, "step_id", ""))
            if not step_id:
                continue
            step_kind = (
                normalize_step_kind(
                    _get_attr(step, "step_kind", STEP_KIND_AGENT),
                    default=STEP_KIND_AGENT,
                    preserve_unknown=True,
                )
                or STEP_KIND_AGENT
            )
            step_agent_id = _preview_text(_get_attr(step, "agent_id", "")) or _preview_text(
                _get_attr(step, "agent", "")
            )
            step_skill_id = (
                _resolve_primary_skill_for_agent(step_agent_id, account_id=preview_account_id)
                if step_requires_agent(step_kind) and step_agent_id
                else ""
            )
            plan.append(
                {
                    "step_id": step_id,
                    "step_kind": step_kind,
                    "group_id": _preview_text(_get_attr(step, "group_id", "")) or None,
                    "lane_id": _preview_text(_get_attr(step, "lane_id", "")) or None,
                    "participant_key": _preview_text(_get_attr(step, "participant_key", "")),
                    "agent": _preview_text(_get_attr(step, "agent", "")),
                    "agent_id": step_agent_id,
                    "skill": step_skill_id,
                    "skill_id": step_skill_id,
                    "connector_id": _preview_text(_get_attr(step, "connector_id", "")),
                    "connector_install_id": _preview_text(
                        _get_attr(step, "connector_install_id", "")
                    ),
                    "workflow_id": _preview_text(_get_attr(step, "workflow_id", "")),
                    "optional": _preview_bool(_get_attr(step, "optional", False)),
                    "disabled": _preview_bool(_get_attr(step, "disabled", False)),
                }
            )
        return plan

    def _resolve_agent_for_participant(
        workflow: dict[str, Any], *, participant_key: str, agent_id: str
    ) -> str:
        try:
            return resolve_agent_for_participant(
                workflow, participant_key=participant_key, agent_id=agent_id
            )
        except WorkflowChangeNormalizationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def _normalized_operations(workflow: dict[str, Any], operations: list[Any]) -> list[Any]:
        try:
            return normalize_change_operations(
                workflow, operations, validate_context_inject=validate_context_inject_sources
            )
        except WorkflowChangeNormalizationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post(
        "/{workflow_id}/changes",
        response_model=WorkflowDefinitionChangeResultView,
        responses={
            400: {"description": "The change set or its resulting definition is invalid"},
            403: {"description": "Forbidden"},
            404: {"description": "Not found"},
            409: {"description": "The workflow changed after it was loaded"},
            503: {"description": "Service unavailable"},
        },
    )
    def apply_workflow_definition_changes(
        request: Request, workflow_id: str, body: WorkflowChangeSet
    ) -> dict[str, Any]:
        """Apply one atomic set of step, group and graph changes.

        The whole set becomes one new revision or nothing is saved. An enabled
        workflow keeps running its current revision when the set is refused.
        """
        db = get_db()
        workflow = _authorize_workflow_mutation_request(request, db, workflow_id)
        _assert_change_allowed(db, workflow_id, "apply_changes")
        operations = _normalized_operations(workflow, list(body.operations))
        try:
            authorize_change_operations(
                operations, change_reference_access(db, request_scope(request), workflow)
            )
            result = apply_workflow_changes(
                db,
                workflow_id,
                expected_revision=body.expected_revision,
                operations=operations,
                author=request_scope(request).actor_name,
                readiness=readiness_from_validator(db, get_readiness_validation),
                dry_run=body.dry_run,
            )
        except WorkflowRevisionConflictError as exc:
            current = resolve_workflow_row(db, workflow_id) or {}
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "workflow_revision_conflict",
                    "message": "The workflow changed after it was loaded. Reload before saving.",
                    "workflow_id": exc.workflow_id,
                    "expected_revision": exc.expected_revision,
                    # What to reload to; an API or MCP caller has no other view.
                    "revision": int(current.get("revision") or 0) or None,
                },
            ) from exc
        except WorkflowConsumerConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "workflow_consumer_conflict",
                    "message": str(exc),
                    "workflow_id": exc.workflow_id,
                    "consumers": exc.issues,
                },
            ) from exc
        except WorkflowDefinitionChangeError as exc:
            raise HTTPException(
                status_code=400,
                detail=format_validation_error_detail(
                    entity_type="workflow",
                    entity_id=workflow_id,
                    action="apply_changes",
                    validation={"valid": False, "errors": exc.issues, "warnings": []},
                ),
            ) from exc
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            if "violates foreign key" not in str(exc).lower():
                raise
            raise _definition_persistence_http_exception(exc) from exc
        reload_all = getattr(get_registry(), "reload_all", None)
        if result.applied and callable(reload_all):
            # Simulation and preview read the registry; it must see the new revision.
            reload_all()
        return {
            "workflow_id": workflow_id,
            "revision": result.revision,
            "valid": result.valid,
            "issues": result.issues,
            "workflow": result.workflow,
            "applied": result.applied,
            "blocking_issues": result.blocking_issues,
        }

    def _resolve_step_contract_preview(
        workflow_id: str,
        step_id: str = "",
        agent_id: str = "",
        participant_key: str = "",
        block_key: str = "",
        block_binding_mode: str | None = None,
    ):
        """Resolve effective step contract groups before adding/updating a step."""
        db = get_db()
        workflow = resolve_workflow_row(db, workflow_id)
        if not workflow:
            raise HTTPException(status_code=404, detail=f"Workflow '{workflow_id}' not found")

        normalized_participant_key = str(participant_key or "").strip()
        normalized_step_id = str(step_id or "").strip()
        normalized_agent_id = str(agent_id or "").strip()
        if normalized_participant_key:
            normalized_agent_id = _resolve_agent_for_participant(
                workflow,
                participant_key=normalized_participant_key,
                agent_id=normalized_agent_id,
            )
        normalized_skill_id = ""
        normalized_block_key = str(block_key or "").strip()
        if (
            block_binding_mode not in {None, "auto", "explicit"}
            or (block_binding_mode == "auto" and normalized_block_key)
            or (block_binding_mode == "explicit" and not normalized_block_key)
        ):
            raise HTTPException(
                status_code=400, detail="Choose either automatic or an explicit Agent contract"
            )
        if not normalized_step_id and not normalized_block_key and not normalized_agent_id:
            raise HTTPException(
                status_code=400,
                detail="step_id, agent_id, participant_key, or block_key is required",
            )
        known_step_ids = {
            str(step.get("step_id") or "")
            for step in workflow.get("steps") or []
            if isinstance(step, dict)
        }
        if (
            normalized_step_id
            and normalized_step_id not in known_step_ids
            and not normalized_agent_id
            and not normalized_block_key
        ):
            # A step that is not saved yet is previewed by its agent or block.
            raise HTTPException(status_code=404, detail=f"Step '{normalized_step_id}' not found")

        if normalized_block_key:
            block_row = db.get_published_block_contract(normalized_block_key)
            if not isinstance(block_row, dict):
                raise HTTPException(
                    status_code=404, detail=f"Published block '{normalized_block_key}' not found"
                )
            if not bool(block_row.get("enabled", True)):
                raise HTTPException(
                    status_code=400, detail=f"Published block '{normalized_block_key}' is disabled"
                )

        # Capabilities resolve in the workflow's own account, not the default
        # one: a projected agent installed for a non-default account is invisible
        # otherwise, and authoring answers 404 for something the operator sees.
        catalog_account_id = _workflow_account_id(workflow)

        if normalized_agent_id:
            # An extension's agent is advertised through the same catalog as the
            # host's own and must author the same way; a host-only lookup answers
            # 404 for a capability the operator can see.
            agent = catalog_agent_row(db, normalized_agent_id, account_id=catalog_account_id)
            if not agent:
                raise HTTPException(
                    status_code=404, detail=f"Agent '{normalized_agent_id}' not found"
                )
            if not bool(agent.get("enabled", True)):
                raise HTTPException(
                    status_code=400, detail=f"Agent '{normalized_agent_id}' is disabled"
                )
            normalized_skill_id = _resolve_primary_skill_for_agent(
                normalized_agent_id, account_id=catalog_account_id
            )
            if not normalized_skill_id:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Agent '{normalized_agent_id}' has no primary skill; "
                        "agent steps must execute through an agent skill"
                    ),
                )
        if normalized_skill_id:
            cap_row = catalog_skill_row(db, normalized_skill_id, account_id=catalog_account_id)
            if not isinstance(cap_row, dict):
                raise HTTPException(
                    status_code=404, detail=f"Skill '{normalized_skill_id}' not found"
                )
            if not bool(cap_row.get("enabled", True)):
                raise HTTPException(
                    status_code=400, detail=f"Skill '{normalized_skill_id}' is disabled"
                )

        from core.application.registry.io_resolver import IOResolver

        resolver = IOResolver(db)
        return resolver.resolve_step_contract_preview(
            workflow_id=workflow_id,
            step_id=normalized_step_id,
            agent_id=normalized_agent_id,
            skill_id=normalized_skill_id,
            block_key=normalized_block_key,
            **(
                {"block_binding_mode": block_binding_mode} if block_binding_mode is not None else {}
            ),
        )

    ctx = SimpleNamespace(
        get_db=get_db,
        get_registry=get_registry,
        get_workflow_validation=get_workflow_validation,
        get_step_test_run_validation=get_step_test_run_validation,
        resolve_workflow_row=resolve_workflow_row,
        assert_step_test_run_allowed=_assert_step_test_run_allowed,
        build_simulate_step_preview=_build_simulate_step_preview,
        resolve_step_contract_preview=_resolve_step_contract_preview,
        authorize_workflow_request=_authorize_workflow_request,
        request_scope=request_scope,
        durable_test_run_job_state=durable_test_run_job_state,
        enqueue_test_run=enqueue_test_run,
    )
    register_run_routes(router, ctx)
    register_io_routes(router, ctx)
    return router


__all__ = [
    "ASSESS_WHETHER_VERSION_2_4_0_IS_READY_TO_SHIP_MESSAGE",
    "PROJECT_ID_IS_REQUIRED_MESSAGE",
    "RELEASE_READINESS_REPORT_VALUE",
    "SIMULATE_REQUEST_BODY",
    "SIMULATE_RESPONSE_EXAMPLE",
    "SIMULATE_TASK_ENVELOPE_EXAMPLE",
    "STEP_KIND_AGENT",
    "STEP_KIND_CONNECTOR",
    "STEP_KIND_INVOKE_WORKFLOW",
    "STEP_TEST_RUN_REQUEST_BODY",
    "STEP_TEST_RUN_TASK_ENVELOPE_EXAMPLE",
    "SUPPORTED_STEP_KINDS",
    "TEST_RUN_REQUEST_BODY",
    "TEST_RUN_RESPONSE_EXAMPLE",
    "TEST_RUN_TASK_ENVELOPE_EXAMPLE",
    "VALIDATE_WORKFLOW_PATH_VALUE",
    "APIRouter",
    "BaseModel",
    "Body",
    "ConfigDict",
    "Field",
    "HTTPException",
    "Query",
    "Request",
    "SimulateRequest",
    "StepTestRunRequest",
    "TestRunRequest",
    "WorkflowChangeSet",
    "WorkflowEditorAvailableVariablesView",
    "WorkflowEditorIOIssueView",
    "WorkflowEditorInputDefaultsView",
    "WorkflowEditorOutputView",
    "WorkflowEditorSimulationResultView",
    "WorkflowEditorStepContractPreviewView",
    "WorkflowEditorTestRunResultView",
    "_test_run_task_envelope",
    "create_workflow_definition_router",
    "field_validator",
    "format_validation_error_detail",
    "logger",
    "normalize_step_kind",
    "step_requires_agent",
    "validate_contract_items",
    "validate_invoke_workflow_step",
]
