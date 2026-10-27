from __future__ import annotations

import uuid
from typing import Any

from fastapi import Response

from core.application.jobs.workflow_definition_snapshot_repository import (
    WorkflowDefinitionPinError,
    WorkflowRevisionChangedError,
)
from core.application.management.errors import ServiceForbiddenError
from core.application.management.guards import require_project_access, require_workflow_access
from core.entrypoints.admin.workflows_definition_common import (
    SIMULATE_REQUEST_BODY,
    SIMULATE_RESPONSE_EXAMPLE,
    STEP_TEST_RUN_REQUEST_BODY,
    TEST_RUN_REQUEST_BODY,
    TEST_RUN_RESPONSE_EXAMPLE,
    HTTPException,
    Request,
    SimulateRequest,
    StepTestRunRequest,
    TestRunRequest,
    WorkflowEditorSimulationResultView,
    WorkflowEditorTestRunResultView,
    _test_run_task_envelope,
    format_validation_error_detail,
)


def _revision_conflict(workflow_id: str, expected: int, revision: int) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "code": "workflow_revision_conflict",
            "message": "The workflow changed after it was loaded. Reload before testing.",
            "workflow_id": workflow_id,
            "expected_revision": expected,
            "revision": revision,
        },
    )


def _not_runnable(exc: WorkflowDefinitionPinError) -> HTTPException:
    """The pinned definition cannot run: say why, as a conflict the author can fix."""
    return HTTPException(
        status_code=409,
        detail={"code": "workflow_not_runnable", "message": str(exc), "issues": list(exc.issues)},
    )


def _assert_expected_revision(workflow_id: str, workflow: Any, expected: int | None) -> None:
    """Refuse a test run of a revision the caller no longer shows."""
    if expected is None or not isinstance(workflow, dict):
        return
    revision = int(workflow.get("revision") or 0)
    if revision != int(expected):
        raise _revision_conflict(workflow_id, int(expected), revision)


def register_run_routes(router, ctx) -> None:
    get_db = ctx.get_db
    get_registry = ctx.get_registry
    get_workflow_validation = ctx.get_workflow_validation
    get_step_test_run_validation = ctx.get_step_test_run_validation
    resolve_workflow_row = ctx.resolve_workflow_row
    _assert_step_test_run_allowed = ctx.assert_step_test_run_allowed
    _build_simulate_step_preview = ctx.build_simulate_step_preview
    _authorize_workflow_request = ctx.authorize_workflow_request
    _request_scope = ctx.request_scope
    _durable_test_run_job_state = ctx.durable_test_run_job_state
    _enqueue_test_run = ctx.enqueue_test_run

    def _caller_context(request: Request) -> dict:
        auth_user = getattr(request.state, "auth_user", None)
        scope = _request_scope(request)
        return {
            "actor_id": str(
                (auth_user or {}).get("username") or (auth_user or {}).get("id") or ""
            ).strip()
            if isinstance(auth_user, dict)
            else "",
            "principal_type": str((auth_user or {}).get("principal_type") or "").strip()
            if isinstance(auth_user, dict)
            else "",
            "allowed_project_ids": list(getattr(scope, "allowed_project_ids", None) or ()),
            "allowed_workflow_ids": list(getattr(scope, "allowed_workflow_ids", None) or ()),
            "correlation_id": str(
                request.headers.get("x-correlation-id") or request.headers.get("x-request-id") or ""
            ).strip(),
            "idempotency_key": str(request.headers.get("idempotency-key") or "").strip(),
        }

    def _test_run_sample_files(workflow: object, project: dict) -> object:
        """Copies of the attached test files, for the file inputs a run does not pass."""
        from core.application.workflows.workflow_sample_file_service import TestRunSampleFiles
        from core.entrypoints.admin.deps import get_artifact_plane

        workflow_row = workflow if isinstance(workflow, dict) else {}
        return TestRunSampleFiles(
            artifact_plane=get_artifact_plane(),
            workflow_id=str(workflow_row.get("workflow_id") or ""),
            workflow_account_id=str(workflow_row.get("account_id") or ""),
            account_id=str(project.get("account_id") or ""),
            project_id=str(project.get("id") or ""),
        )

    def _enforce_project_request_scope(request: Request, project: dict) -> None:
        if not isinstance(getattr(request.state, "auth_user", None), dict):
            return
        try:
            require_project_access(_request_scope(request), project)
        except ServiceForbiddenError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    def _enforce_test_run_payload_scope(request: Request, payload: dict) -> None:
        if not isinstance(getattr(request.state, "auth_user", None), dict):
            return
        db = get_db()
        workflow_id = str(payload.get("workflow_id") or "").strip()
        if workflow_id:
            _authorize_workflow_request(request, db, workflow_id)
        project_id = str(payload.get("project_id") or "").strip()
        if not project_id:
            return
        dsn = str(getattr(db, "dsn", "") or "").strip()
        project = None
        if dsn:
            from core.infrastructure.runtime.entrypoint_db_adapters import create_project_store

            project = create_project_store(dsn).get_project(project_id)
        if isinstance(project, dict):
            _enforce_project_request_scope(request, project)
            return
        allowed_project_ids = getattr(_request_scope(request), "allowed_project_ids", None)
        if allowed_project_ids and project_id not in allowed_project_ids:
            raise HTTPException(status_code=403, detail="Access denied")

    @router.post(
        "/{workflow_id}/simulate",
        response_model=WorkflowEditorSimulationResultView,
        responses={
            200: {
                "content": {"application/json": {"example": SIMULATE_RESPONSE_EXAMPLE}},
            },
            400: {"description": "Bad request"},
            404: {"description": "Not found"},
            409: {"description": "Conflict"},
            422: {"description": "Validation error"},
            500: {"description": "Internal server error"},
            503: {"description": "Service unavailable"},
        },
    )
    def simulate_workflow(
        request: Request,
        workflow_id: str,
        body: SimulateRequest = SIMULATE_REQUEST_BODY,
    ):
        """Simulate workflow selection and step planning without running agents."""
        db = get_db()
        registry_workflow = get_registry().workflows.get_workflow(workflow_id)
        db_workflow = db.get_workflow(workflow_id)
        if not registry_workflow and not (isinstance(db_workflow, dict) and db_workflow):
            raise HTTPException(status_code=404, detail=f"Workflow '{workflow_id}' not found")
        try:
            require_workflow_access(_request_scope(request), workflow_id)
        except ServiceForbiddenError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        if isinstance(db_workflow, dict) and db_workflow:
            _authorize_workflow_request(request, db, workflow_id)
        _assert_step_test_run_allowed(db, workflow_id, "simulate_workflow")
        validation = get_workflow_validation(db, workflow_id)
        if not validation.get("valid", False):
            raise HTTPException(
                status_code=400,
                detail=format_validation_error_detail(
                    entity_type="workflow",
                    entity_id=workflow_id,
                    action="simulate",
                    validation=validation,
                ),
            )

        from core.application.orchestration.classifier import classify_task

        classification = classify_task(body.task_envelope, deterministic=True)
        steps = _build_simulate_step_preview(registry_workflow or db_workflow)

        return {
            "workflow_id": workflow_id,
            "validation": validation,
            "classification": {
                "task_type": str(body.task_envelope.get("task_type") or "").strip(),
                "effort_hint": classification.effort_hint,
                "confidence": classification.confidence,
                "risk_level": classification.risk_level,
                "automation_recommendation": classification.automation_recommendation,
                "reasoning": classification.reasoning,
            },
            "steps": steps,
        }

    @router.post(
        "/{workflow_id}/test-run",
        response_model=WorkflowEditorTestRunResultView,
        response_model_exclude_none=True,
        responses={
            200: {
                "content": {"application/json": {"example": TEST_RUN_RESPONSE_EXAMPLE}},
            },
            202: {
                "model": WorkflowEditorTestRunResultView,
                "description": "Accepted for durable worker execution",
            },
            400: {"description": "Bad request"},
            404: {"description": "Not found"},
            409: {"description": "Conflict"},
            422: {"description": "Validation error"},
            500: {"description": "Internal server error"},
            503: {"description": "Service unavailable"},
        },
    )
    def test_run_workflow(
        request: Request,
        workflow_id: str,
        response: Response,
        body: TestRunRequest = TEST_RUN_REQUEST_BODY,
    ):
        """Execute a builder-native workflow test run without persisting a normal job."""
        db = get_db()
        _authorize_workflow_request(request, db, workflow_id)
        wf = resolve_workflow_row(db, workflow_id)
        if not isinstance(wf, dict):
            raise HTTPException(status_code=404, detail=f"Workflow '{workflow_id}' not found")
        _assert_expected_revision(workflow_id, wf, body.expected_revision)
        validation = get_workflow_validation(db, workflow_id)
        if not validation.get("valid", False):
            raise HTTPException(
                status_code=400,
                detail=format_validation_error_detail(
                    entity_type="workflow",
                    entity_id=workflow_id,
                    action="test_run",
                    validation=validation,
                ),
            )

        from core.application.orchestration.agents.common import sanitize_external_input
        from core.application.workflows.test_run_submission_service import (
            TestRunInputRejected,
            submit_test_run,
        )
        from core.infrastructure.runtime.entrypoint_db_adapters import (
            create_job_store,
            create_project_store,
        )

        summary = sanitize_external_input(str(body.task_summary or "").strip())
        if not summary:
            raise HTTPException(status_code=400, detail="task_summary is required")

        dsn = str(getattr(db, "dsn", "") or "").strip()
        if not dsn:
            raise HTTPException(status_code=503, detail="Registry DB DSN is not configured")
        bl = create_project_store(dsn)
        project = bl.get_project(body.project_id)
        if not isinstance(project, dict):
            raise HTTPException(status_code=404, detail=f"Project '{body.project_id}' not found")
        _enforce_project_request_scope(request, project)
        run_cfg = bl.get_project_run_config(body.project_id) or {}
        if isinstance(run_cfg, dict):
            run_cfg = {
                **run_cfg,
                "project_id": project.get("id") or body.project_id,
                "account_id": project.get("account_id"),
                "tenant_id": project.get("tenant_id"),
                "workspace_id": project.get("workspace_id"),
            }
        task_id = str(body.task_id or "").strip() or uuid.uuid4().hex
        run_mode = str(body.mode or "").strip()
        task_envelope = _test_run_task_envelope(
            workflow_id,
            body.project_id,
            summary,
            body.task_envelope,
        )
        from core.application.registry.workflow_contract_service import WorkflowInterfaceService

        workflow_interface = WorkflowInterfaceService().interface_from_settings(wf.get("settings"))

        if body.dry_run:
            from core.application.workflows.test_run_submission_service import (
                TestRunInputRejected,
                materialize_test_run_envelope,
            )
            from core.infrastructure.security.prompt_safety import redact_sensitive_structure

            try:
                from core.application.workflows.workflow_sample_file_service import (
                    preview_sample_file,
                )

                materialized_envelope, _snapshot = materialize_test_run_envelope(
                    workflow_id=workflow_id,
                    project_id=body.project_id,
                    task_envelope=task_envelope,
                    workflow_interface=workflow_interface,
                    sample_files=preview_sample_file,
                )
            except TestRunInputRejected as exc:
                raise HTTPException(
                    status_code=422,
                    detail={"code": "workflow_input_validation_failed", "errors": exc.errors},
                ) from exc
            return {
                "status": "dry_run",
                "dry_run": True,
                "would_execute_ephemerally": True,
                "workflow_id": workflow_id,
                "project_id": body.project_id,
                "project_name": run_cfg.get("project_name", project.get("name", "")),
                "task_id": task_id,
                "task_summary": summary,
                "task_envelope": redact_sensitive_structure(materialized_envelope),
                "mode": run_mode,
                "test_mode": True,
                "validation": validation,
            }

        project_name = str(run_cfg.get("project_name") or project.get("name") or "").strip()
        # A disabled workflow is a draft, and a test run is how a draft gets
        # tested. Only real runs (webhook, API, schedule) need it enabled; the
        # runtime already lets a test-mode job select a disabled workflow.
        linked = run_cfg.get("project_workflows") if isinstance(run_cfg, dict) else None
        if (
            isinstance(linked, list)
            and linked
            and workflow_id
            not in {
                str(item.get("workflow_id") if isinstance(item, dict) else item).strip()
                for item in linked
            }
        ):
            raise HTTPException(
                status_code=422,
                detail=f"Explicit workflow override '{workflow_id}' is not linked to this project",
            )
        from core.application.workflows.workflow_kept_test_outputs import (
            kept_test_outputs_for_workflow,
        )

        try:
            payload = submit_test_run(
                enqueue=_enqueue_test_run,
                job_store=create_job_store(dsn),
                kind="workflow",
                workflow_id=workflow_id,
                project_id=body.project_id,
                task_summary=summary,
                task_id=task_id,
                task_envelope=task_envelope,
                project_name=project_name,
                project_config=run_cfg,
                mode=run_mode,
                caller=_caller_context(request),
                workflow_interface=workflow_interface,
                sample_files=_test_run_sample_files(wf, project),
                expected_revision=body.expected_revision,
                kept_test_outputs=kept_test_outputs_for_workflow(db, wf),
            )
        except WorkflowRevisionChangedError as exc:
            raise _revision_conflict(workflow_id, exc.expected_revision, exc.revision) from exc
        except WorkflowDefinitionPinError as exc:
            raise _not_runnable(exc) from exc
        except TestRunInputRejected as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "workflow_input_validation_failed", "errors": exc.errors},
            ) from exc
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        response.status_code = 202
        return payload

    @router.get(
        "/test-runs/{thread_id}",
        response_model=WorkflowEditorTestRunResultView,
        responses={
            400: {"description": "Bad request"},
            404: {"description": "Not found"},
            409: {"description": "Conflict"},
            422: {"description": "Validation error"},
            500: {"description": "Internal server error"},
            503: {"description": "Service unavailable"},
        },
    )
    def get_workflow_test_run_state_view(request: Request, thread_id: str):
        payload = _durable_test_run_job_state(thread_id)
        if not payload:
            raise HTTPException(
                status_code=404,
                detail=f"Workflow test run '{thread_id}' not found",
            )
        _enforce_test_run_payload_scope(request, payload)
        return payload

    @router.post(
        "/{workflow_id}/steps/{step_id}/test-run",
        response_model=WorkflowEditorTestRunResultView,
        responses={
            200: {
                "content": {"application/json": {"example": TEST_RUN_RESPONSE_EXAMPLE}},
            },
            202: {
                "model": WorkflowEditorTestRunResultView,
                "description": "Accepted for durable worker execution",
            },
            400: {"description": "Bad request"},
            404: {"description": "Not found"},
            409: {"description": "Conflict"},
            422: {"description": "Validation error"},
            500: {"description": "Internal server error"},
            503: {"description": "Service unavailable"},
        },
    )
    def test_run_step(
        request: Request,
        workflow_id: str,
        step_id: str,
        response: Response,
        body: StepTestRunRequest = STEP_TEST_RUN_REQUEST_BODY,
    ):
        db = get_db()
        _authorize_workflow_request(request, db, workflow_id)
        _assert_step_test_run_allowed(db, workflow_id, "step_test_run")
        from core.application.workflows.platform_step_test_run_helpers import (
            resolve_step_for_test_run,
        )

        workflow = resolve_workflow_row(db, workflow_id)
        _assert_expected_revision(workflow_id, workflow, body.expected_revision)
        step = resolve_step_for_test_run(
            db,
            workflow_id=workflow_id,
            step_id=step_id,
            workflow=workflow,
        )
        validation = get_step_test_run_validation(db, workflow_id, step_id)

        from core.application.orchestration.agents.common import sanitize_external_input
        from core.application.workflows.test_run_submission_service import (
            TestRunInputRejected,
            TestRunReadinessRejected,
            submit_test_run,
        )
        from core.infrastructure.runtime.entrypoint_db_adapters import (
            create_job_store,
            create_project_store,
        )

        summary = sanitize_external_input(str(body.task_summary or "").strip())
        if not summary:
            raise HTTPException(status_code=400, detail="task_summary is required")

        dsn = str(getattr(db, "dsn", "") or "").strip()
        if not dsn:
            raise HTTPException(status_code=503, detail="Registry DB DSN is not configured")
        bl = create_project_store(dsn)
        project = bl.get_project(body.project_id)
        if not isinstance(project, dict):
            raise HTTPException(status_code=404, detail=f"Project '{body.project_id}' not found")
        _enforce_project_request_scope(request, project)
        run_cfg = bl.get_project_run_config(body.project_id) or {}
        if isinstance(run_cfg, dict):
            run_cfg = {
                **run_cfg,
                "project_id": project.get("id") or body.project_id,
                "account_id": project.get("account_id"),
                "tenant_id": project.get("tenant_id"),
                "workspace_id": project.get("workspace_id"),
            }
        task_id = str(body.task_id or "").strip() or uuid.uuid4().hex
        task_envelope = _test_run_task_envelope(
            workflow_id,
            body.project_id,
            summary,
            body.task_envelope,
        )
        from core.application.registry.workflow_contract_service import WorkflowInterfaceService

        step_contract_preview = ctx.resolve_step_contract_preview(
            workflow_id=workflow_id, step_id=step_id
        )
        step_workflow = workflow or {}
        workflow_interface = WorkflowInterfaceService().interface_from_settings(
            step_workflow.get("settings")
        )
        project_name = str(run_cfg.get("project_name") or project.get("name") or "").strip()
        try:
            payload = submit_test_run(
                enqueue=_enqueue_test_run,
                job_store=create_job_store(dsn),
                kind="step",
                workflow_id=workflow_id,
                step=step,
                step_contract_preview=step_contract_preview,
                step_validation=validation,
                project_id=body.project_id,
                task_summary=summary,
                task_id=task_id,
                task_envelope=task_envelope,
                project_name=project_name,
                project_config=run_cfg,
                caller=_caller_context(request),
                workflow_interface=workflow_interface,
                sample_files=_test_run_sample_files(step_workflow, project),
                expected_revision=body.expected_revision,
            )
        except WorkflowRevisionChangedError as exc:
            raise _revision_conflict(workflow_id, exc.expected_revision, exc.revision) from exc
        except WorkflowDefinitionPinError as exc:
            raise _not_runnable(exc) from exc
        except TestRunReadinessRejected as exc:
            raise HTTPException(
                status_code=400,
                detail=format_validation_error_detail(
                    entity_type="workflow",
                    entity_id=workflow_id,
                    action="step_test_run",
                    validation=exc.validation,
                ),
            ) from exc
        except TestRunInputRejected as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "workflow_input_validation_failed", "errors": exc.errors},
            ) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        response.status_code = 202
        return payload
