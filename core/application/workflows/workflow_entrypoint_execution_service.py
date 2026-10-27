"""Application coordination for incoming workflow entrypoints."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from core.application.management.errors import safe_operator_exception
from core.application.workflows import workflow_entrypoint_repository
from core.application.workflows.workflow_entrypoint_artifacts import (
    WorkflowEntrypointArtifactLifecycle,
)
from core.application.workflows.workflow_entrypoint_audit_service import (
    record_workflow_entrypoint_execution,
)
from core.application.workflows.workflow_entrypoint_idempotency import (
    accept_idempotency_reservation,
    claim_idempotency_reservation,
    fail_idempotency_reservation,
    find_existing_idempotent_job,
    journal_idempotency_reservation,
    renew_idempotency_reservation,
)
from core.application.workflows.workflow_entrypoint_idempotency_service import (
    WorkflowEntrypointIdempotencyService,
    durable_job_dispatch_confirmed,
)
from core.application.workflows.workflow_entrypoint_input_service import (
    WorkflowEntrypointInputService,
)
from core.application.workflows.workflow_entrypoint_launch import WorkflowEntrypointLaunchService
from core.application.workflows.workflow_entrypoint_models import (
    WorkflowEntrypointFailureKind,
    WorkflowEntrypointIdempotencyResolution,
    WorkflowEntrypointLaunchResult,
    WorkflowEntrypointLeaseLostError,
    WorkflowEntrypointPreparation,
)
from core.application.workflows.workflow_entrypoint_submission import (
    WorkflowEntrypointSubmissionOutcome,
    WorkflowEntrypointSubmissionUseCase,
)


class WorkflowEntrypointExecutionCoordinator:
    """Resolve entrypoint ownership and assemble its durable submission lifecycle."""

    def __init__(
        self,
        *,
        logger: Any,
        artifact_plane: Any,
        redeliver_task: Callable[..., Any] | None,
        enqueue_workflow_start: Callable[..., Any],
        now_iso: Callable[[], str],
        load_entrypoint: Callable[[Any, str], dict[str, Any] | None],
        resolve_bound_project: Callable[[Any, str, str], tuple[Any, dict[str, Any]]],
        load_workflow_state: Callable[[Any, str], dict[str, Any]] | None = None,
        storage_unavailable_error: Callable[[Exception], bool] | None = None,
        claim_reservation: Callable[..., dict[str, Any]] = claim_idempotency_reservation,
        find_existing_job: Callable[..., str | None] = find_existing_idempotent_job,
        accept_reservation: Callable[..., Any] = accept_idempotency_reservation,
        fail_reservation: Callable[..., Any] = fail_idempotency_reservation,
        renew_reservation: Callable[..., Any] = renew_idempotency_reservation,
        journal_reservation: Callable[..., Any] = journal_idempotency_reservation,
        dispatch_confirmed: Callable[[Any, str], bool] = durable_job_dispatch_confirmed,
        submission_use_case_factory: Callable[..., Any] = WorkflowEntrypointSubmissionUseCase,
        new_id: Callable[[], str] | None = None,
    ) -> None:
        self._logger = logger
        self._artifact_plane = artifact_plane
        self._redeliver_task = redeliver_task
        self._enqueue_workflow_start = enqueue_workflow_start
        self._now_iso = now_iso
        self._input_service = WorkflowEntrypointInputService()
        self._load_entrypoint = load_entrypoint
        self._resolve_bound_project = resolve_bound_project
        self._load_workflow_state = (
            load_workflow_state or workflow_entrypoint_repository.load_workflow_admission_state
        )
        self._storage_unavailable_error = storage_unavailable_error or (lambda _exc: False)
        self._claim_reservation = claim_reservation
        self._find_existing_job = find_existing_job
        self._accept_reservation = accept_reservation
        self._fail_reservation = fail_reservation
        self._renew_reservation = renew_reservation
        self._journal_reservation = journal_reservation
        self._dispatch_confirmed = dispatch_confirmed
        self._submission_use_case_factory = submission_use_case_factory
        self._new_id = new_id or (lambda: uuid.uuid4().hex)

    def record_execution(self, **kwargs: Any) -> None:
        record_workflow_entrypoint_execution(self._logger, **kwargs)

    @staticmethod
    def _preparation_failure(
        *,
        failure_kind: WorkflowEntrypointFailureKind,
        code: str,
        error: str,
        exc: BaseException | None = None,
        entrypoint: dict[str, Any] | None = None,
        workflow_id: str = "",
        project_id: str = "",
        retryable: bool = False,
    ) -> WorkflowEntrypointPreparation:
        return WorkflowEntrypointPreparation(
            entrypoint=dict(entrypoint or {}),
            workflow_id=workflow_id,
            project_id=project_id,
            failure_kind=failure_kind,
            code=code,
            error=error,
            operator_error=safe_operator_exception(exc) if exc is not None else "",
            retryable=retryable,
        )

    def prepare(self, entry_key: str) -> WorkflowEntrypointPreparation:
        clean_entry_key = str(entry_key or "").strip()
        if not clean_entry_key:
            return self._preparation_failure(
                failure_kind="invalid_request",
                code="entry_key_required",
                error="entry_key is required",
            )
        try:
            loaded = self._load_entrypoint(self._logger, clean_entry_key)
        except Exception as exc:
            storage_unavailable = bool(self._storage_unavailable_error(exc))
            return self._preparation_failure(
                failure_kind="unavailable" if storage_unavailable else "internal",
                code=(
                    "entrypoint_storage_unavailable"
                    if storage_unavailable
                    else "entrypoint_load_failed"
                ),
                error=(
                    "Workflow entrypoint storage is unavailable"
                    if storage_unavailable
                    else "Failed to load workflow entrypoint configuration"
                ),
                exc=exc,
            )
        if not isinstance(loaded, dict) or not loaded:
            return self._preparation_failure(
                failure_kind="not_found",
                code="entrypoint_not_found",
                error="Workflow entrypoint not found",
            )

        entrypoint = dict(loaded)
        workflow_id = str(entrypoint.get("workflow_id") or "").strip()
        if not bool(entrypoint.get("enabled", True)):
            return self._preparation_failure(
                failure_kind="conflict",
                code="entrypoint_disabled",
                error="Workflow entrypoint is disabled",
                entrypoint=entrypoint,
                workflow_id=workflow_id,
            )

        bound_project_id = str(entrypoint.get("project_id") or "").strip()
        if not bound_project_id:
            return self._preparation_failure(
                failure_kind="conflict",
                code="workflow_project_binding_required",
                error="Workflow entrypoint has no project binding",
                entrypoint=entrypoint,
                workflow_id=workflow_id,
            )
        try:
            project_id, run_config = self._resolve_bound_project(
                self._logger,
                workflow_id,
                bound_project_id,
            )
        except Exception as exc:
            return self._preparation_failure(
                failure_kind="unavailable",
                code="workflow_project_lookup_unavailable",
                error="Workflow project storage is unavailable; retry later",
                exc=exc,
                entrypoint=entrypoint,
                workflow_id=workflow_id,
                project_id=bound_project_id,
                retryable=True,
            )
        clean_project_id = str(project_id or "").strip()
        if not clean_project_id:
            return self._preparation_failure(
                failure_kind="not_found",
                code="workflow_project_binding_not_found",
                error="No enabled project is linked to the selected workflow",
                entrypoint=entrypoint,
                workflow_id=workflow_id,
            )
        try:
            get_project = getattr(self._logger, "get_project", None)
            if not callable(get_project):
                raise RuntimeError("Project storage is unavailable")
            project_is_resolvable = isinstance(get_project(clean_project_id), dict)
        except Exception as exc:
            return self._preparation_failure(
                failure_kind="unavailable",
                code="workflow_project_lookup_unavailable",
                error="Workflow project storage is unavailable; retry later",
                exc=exc,
                entrypoint=entrypoint,
                workflow_id=workflow_id,
                project_id=clean_project_id,
                retryable=True,
            )
        if not project_is_resolvable:
            return self._preparation_failure(
                failure_kind="not_found",
                code="workflow_project_unresolved",
                error=(
                    "Resolved project for this workflow entrypoint is unavailable; "
                    "select an explicit, accessible project"
                ),
                entrypoint=entrypoint,
                workflow_id=workflow_id,
                project_id=clean_project_id,
            )
        return WorkflowEntrypointPreparation(
            entrypoint=entrypoint,
            project_id=clean_project_id,
            run_config=dict(run_config or {}),
            workflow_id=workflow_id,
        )

    def check_workflow_admission(
        self, workflow_id: str
    ) -> WorkflowEntrypointSubmissionOutcome | None:
        """Reject unavailable owners before reservation, files or job creation."""
        try:
            workflow = self._load_workflow_state(self._logger, str(workflow_id or "").strip())
        except Exception as exc:
            return WorkflowEntrypointSubmissionOutcome(
                failure_kind="unavailable",
                code="workflow_lookup_unavailable",
                error="Workflow availability could not be checked; retry later",
                operator_error=safe_operator_exception(exc),
            )
        if not workflow:
            return WorkflowEntrypointSubmissionOutcome(
                failure_kind="not_found",
                code="workflow_not_found",
                error="Workflow is unavailable",
            )
        if not bool(workflow.get("enabled", False)):
            return WorkflowEntrypointSubmissionOutcome(
                failure_kind="conflict",
                code="workflow_disabled",
                error="This workflow is turned off. Enable it before sending a webhook.",
            )
        return None

    def submit(self, **kwargs: Any) -> WorkflowEntrypointSubmissionOutcome:
        refusal = self.check_workflow_admission(str(kwargs.get("workflow_id") or ""))
        if refusal is not None:
            return refusal
        entry_key = str(kwargs.get("entry_key") or "").strip()
        request_id = str(kwargs.get("request_id") or "").strip()
        claim_owner_id = self._new_id()

        def _idempotency_factory(request_fingerprint: str):
            return WorkflowEntrypointIdempotencyService(
                logger=self._logger,
                entry_key=entry_key,
                request_fingerprint=request_fingerprint,
                claim_owner_id=claim_owner_id,
                redeliver_task=self._redeliver_task,
                artifact_plane=self._artifact_plane,
                claim_reservation=self._claim_reservation,
                find_existing_job=self._find_existing_job,
                accept_reservation=self._accept_reservation,
                fail_reservation=self._fail_reservation,
                dispatch_confirmed=self._dispatch_confirmed,
            )

        def _artifact_lifecycle_factory(request_fingerprint: str, owned_keys: list[str]):
            return WorkflowEntrypointArtifactLifecycle(
                logger=self._logger,
                artifact_plane=self._artifact_plane,
                entry_key=entry_key,
                request_id=claim_owner_id,
                run_id=request_id,
                request_fingerprint=request_fingerprint,
                idempotency_keys=owned_keys,
                renew_reservation=self._renew_reservation,
                journal_reservation=self._journal_reservation,
            )

        use_case = self._submission_use_case_factory(
            build_inputs=self._input_service.build_inputs,
            validate_inputs=self._input_service.validate_inputs,
            idempotency_service_factory=_idempotency_factory,
            artifact_lifecycle_factory=_artifact_lifecycle_factory,
            launch_service=WorkflowEntrypointLaunchService(
                enqueue_workflow_start=self._enqueue_workflow_start,
                now_iso=self._now_iso,
            ),
        )
        return use_case.submit(**kwargs)


__all__ = [
    "WorkflowEntrypointArtifactLifecycle",
    "WorkflowEntrypointExecutionCoordinator",
    "WorkflowEntrypointIdempotencyResolution",
    "WorkflowEntrypointIdempotencyService",
    "WorkflowEntrypointLaunchResult",
    "WorkflowEntrypointLaunchService",
    "WorkflowEntrypointLeaseLostError",
    "WorkflowEntrypointPreparation",
    "WorkflowEntrypointSubmissionOutcome",
    "WorkflowEntrypointSubmissionUseCase",
    "durable_job_dispatch_confirmed",
]
