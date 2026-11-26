# ruff: noqa: F403, F405
from __future__ import annotations

from typing import Any

from core.application.auth.principal import is_token_scoped_principal
from core.application.jobs.public_approval_serializer import (
    serialize_approval_history_entry,
    serialize_token_approval_payload,
)
from core.application.jobs.workflow_identity import resolve_job_workflow_id
from core.application.workflows.approval_review import REVIEW_PAYLOAD_KEY

from .approval_decision_service import ApprovalDecisionService
from .query_services_common import *  # NOSONAR


def job_in_section(job: dict[str, Any], *, project_id: str | None, workflow_id: str | None) -> bool:
    """Whether a job belongs to the section (project and workflow) it is opened from.

    Without a section pair the caller is not a section page and nothing is
    narrowed; with one, both must match the job's own project and workflow.
    """
    if not project_id and not workflow_id:
        return True
    return str(job.get("project_id") or "") == str(project_id or "") and (
        resolve_job_workflow_id(job) == str(workflow_id or "")
    )


def _is_token_principal(scope: ScopeContext) -> bool:
    return is_token_scoped_principal(scope)


class ApprovalQueryService:
    def __init__(
        self,
        *,
        get_logger: Callable[[], Any],
        get_thread_state_snapshot: Callable[[str], Any],
        approval_fallback_payload: Callable[[dict[str, Any]], dict[str, Any]],
        to_json_obj: Callable[[Any], Any] | None = None,
        get_project_by_id: Callable[[Any], dict[str, Any] | None] | None = None,
        resolve_artifact: Callable[[str], dict[str, Any] | None] | None = None,
    ):
        self._get_logger = get_logger
        self._get_thread_state_snapshot = get_thread_state_snapshot
        self._approval_fallback_payload = approval_fallback_payload
        self._to_json_obj = to_json_obj or (lambda value: value)
        self._get_project_by_id = get_project_by_id
        self._resolve_artifact = resolve_artifact

    def _public_review(self, job: dict[str, Any], review: Any) -> dict[str, Any] | None:
        """The review a caller who may see the job is shown, original files included."""
        from core.application.jobs.workflow_run_projection import public_review_projection

        return public_review_projection(review, job=job, resolve_artifact=self._resolve_artifact)

    def _project(self, bl: Any, project_id: Any) -> dict[str, Any] | None:
        if project_id in (None, ""):
            return None
        if self._get_project_by_id is not None:
            return self._get_project_by_id(str(project_id))
        return bl.get_project(str(project_id))

    @staticmethod
    def _scope(scope: ScopeContext | None, *, project_id: Any = None) -> ScopeContext:
        return _service_scope(scope, project_id=project_id)

    @staticmethod
    def _require_job(bl: Any, job_id: str) -> dict[str, Any]:
        job = bl.get_job(job_id) if hasattr(bl, "get_job") else None
        if not job:
            raise ServiceNotFoundError("Job not found")
        return dict(job) if isinstance(job, dict) else job

    @staticmethod
    def _require_job_workflow_access(scope: ScopeContext, job: dict[str, Any]) -> None:
        require_workflow_access(scope, resolve_job_workflow_id(job))

    def get_current_gate(
        self, job_id: str, *, scope: ScopeContext | None = None
    ) -> ApprovalGateView:
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(DATABASE_NOT_CONFIGURED_MESSAGE)
        job = self._require_job(bl, str(job_id))
        resolved = self._scope(scope, project_id=job.get("project_id"))
        project = self._project(bl, job.get("project_id"))
        if project:
            require_project_access(resolved, project)
        self._require_job_workflow_access(resolved, job)

        if str(job.get("status") or "").strip() != "awaiting_approval":
            raise ServiceConflictError(
                "Job is not awaiting approval",
                payload={
                    "error": "Job is not awaiting approval",
                    "code": "approval_not_awaiting",
                },
            )

        thread_id = str(job.get("thread_id") or "").strip()
        if not thread_id:
            raise ServiceError("No thread_id for job", status_code=500)

        payload: dict[str, Any] = {}
        try:
            from core.application.jobs.workflow_run_projection import (
                MultipleApprovalGatesUnsupported,
                ensure_single_approval_interrupt,
            )

            state = self._get_thread_state_snapshot(thread_id)
            if not state:
                raise RuntimeError("state unavailable")
            interrupt = ensure_single_approval_interrupt(state)
            if interrupt is None:
                from core.application.orchestration.approval_payload import (
                    state_based_approval_payload,
                )

                payload_value = state_based_approval_payload(getattr(state, "values", {}) or {})
            else:
                payload_value = getattr(interrupt, "value", {})
            payload = self._to_json_obj(payload_value) or {}
            raw_interrupt_id = getattr(interrupt, "id", None) or getattr(
                interrupt, "interrupt_id", None
            )
            interrupt_id = (
                str(raw_interrupt_id).strip()
                if isinstance(raw_interrupt_id, (str, int, uuid.UUID))
                else ""
            )
            if isinstance(payload, dict) and interrupt_id and not payload.get("interrupt_id"):
                payload["interrupt_id"] = interrupt_id
        except MultipleApprovalGatesUnsupported as exc:
            raise ServiceConflictError(
                str(exc),
                payload={"error": str(exc), "code": exc.code},
            ) from exc
        except Exception as exc:
            logger.warning("Could not read interrupt payload: %s", exc)
            payload = {}

        if not isinstance(payload, dict) or not payload:
            payload = self._approval_fallback_payload(job)
        state_approval_id = ApprovalDecisionService.state_based_approval_id(job)
        if state_approval_id and isinstance(payload, dict) and not payload.get("approval_id"):
            # A Test Run parks at a phase completion gate by state, so there is no
            # interrupt to read the gate identity from. Without this the surface
            # answered `approval_id: null` and `allowed_actions: []`, leaving the
            # operator a visible approval with no way to act on it (AUDIT-038
            # symptom b). The id is a server-side fact from the job row.
            payload["approval_id"] = state_approval_id
        if isinstance(payload, dict):
            payload["allowed_actions"] = ApprovalDecisionService.normalized_allowed_actions(payload)
        # The captured review leaves the raw payload and is shown in its public
        # shape only, to browser and token callers alike.
        review = (
            self._public_review(job, payload.pop(REVIEW_PAYLOAD_KEY, None))
            if isinstance(payload, dict)
            else None
        )
        if _is_token_principal(resolved):
            payload = serialize_token_approval_payload(payload)

        approval_id = (
            str(
                (payload or {}).get("approval_id") or (payload or {}).get("interrupt_id") or ""
            ).strip()
            or None
        )
        if _is_token_principal(resolved) and not approval_id:
            # Token callers cannot submit a decision without the immutable
            # gate identity; a browser fallback is not a usable public result.
            raise ServiceUnavailableError("Current approval gate is unavailable")
        allowed_actions = payload.get("allowed_actions") if isinstance(payload, dict) else []
        if not isinstance(allowed_actions, list):
            allowed_actions = []
        expires_at = str((payload or {}).get("expires_at") or "").strip() or None
        return ApprovalGateView(
            job_id=str(job_id),
            thread_id=thread_id,
            task_summary=str(job.get("task_summary") or ""),
            project_id=str(job.get("project_id") or "").strip() or None,
            approval_id=approval_id,
            expires_at=expires_at,
            allowed_actions=[str(item) for item in allowed_actions if str(item).strip()],
            payload=payload if isinstance(payload, dict) else {},
            review=review,
        )

    def get_review(
        self,
        job_id: str,
        *,
        scope: ScopeContext | None = None,
        can_decide: bool = False,
        project_id: str | None = None,
        workflow_id: str | None = None,
    ) -> JobReviewView:
        """The review this job waits for (ADR 0075), with the caller's access to it.

        A job ``awaiting_approval`` holds its own; an ``awaiting_child`` job waits
        on the approval of a descendant down its Invoke chain. That approval is
        read with the descendant's own access checks: a caller who may not read
        it learns only that a decision is pending, never its fields, values,
        files or job id. ``can_decide`` is the caller's right to decide
        approvals (operator or admin); otherwise the review is read-only.

        ``project_id`` and ``workflow_id`` name the section the job is opened
        from: a job of another project or workflow is not found, so a section
        page never reads a job of another section (nor, through it, the deep
        approval of a chain that section does not own).
        """
        from core.application.workflows.approval_waiting_propagation import (
            STATUS_AWAITING_APPROVAL,
            STATUS_AWAITING_CHILD,
            awaiting_review_targets,
        )

        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(DATABASE_NOT_CONFIGURED_MESSAGE)
        job = self._require_job(bl, str(job_id))
        resolved = self._scope(scope, project_id=job.get("project_id"))
        project = self._project(bl, job.get("project_id"))
        if project:
            require_project_access(resolved, project)
        self._require_job_workflow_access(resolved, job)
        if not job_in_section(job, project_id=project_id, workflow_id=workflow_id):
            raise ServiceNotFoundError("Job not found")
        status = str(job.get("status") or "").strip().lower()
        view = JobReviewView(
            job_id=str(job_id),
            status=status,
            job_values=self._operator_job_values(bl, resolved, job, str(job_id)),
            decision=self._last_review_decision(bl, scope, job, str(job_id)),
        )
        if status == STATUS_AWAITING_APPROVAL:
            target_id = str(job_id)
        elif status == STATUS_AWAITING_CHILD:
            target = awaiting_review_targets(bl, [str(job_id)]).get(str(job_id)) or {}
            target_id = str(target.get("review_job_id") or "")
        else:
            target_id = ""
        if not target_id:
            return view
        view.waiting = True
        try:
            gate = self.get_current_gate(target_id, scope=scope)
        except ServiceForbiddenError:
            view.access = "none"
            return view
        except (ServiceNotFoundError, ServiceConflictError):
            # Decided or gone since the chain was read: nothing waits any more.
            view.waiting = False
            return view
        view.review_job_id = target_id
        view.access = "decide" if can_decide else "read"
        view.gate = gate
        return view

    @staticmethod
    def _operator_job_values(
        bl: Any, resolved: ScopeContext, job: dict[str, Any], job_id: str
    ) -> dict[str, Any]:
        """What the job submitted and returned, read and shown as the operator list does."""
        from core.application.registry.workflow_operator_list import operator_job_values
        from core.application.security.credential_redaction import redact

        reader = getattr(bl, "read_job_operator_payloads", None)
        payloads = (
            reader(job_ids=[job_id], account_id=resolved.account_id) if callable(reader) else {}
        )
        payload = payloads.get(job_id) if isinstance(payloads, dict) else None
        redacted = redact(operator_job_values(job, payload if isinstance(payload, dict) else None))
        return dict(redacted) if isinstance(redacted, dict) else {}

    def _can_read_job(self, bl: Any, scope: ScopeContext | None, row: dict[str, Any]) -> bool:
        """The review endpoint's access rule for a job of the chain: project and workflow."""
        resolved = self._scope(scope, project_id=row.get("project_id"))
        try:
            project = self._project(bl, row.get("project_id"))
            if project:
                require_project_access(resolved, project)
            self._require_job_workflow_access(resolved, row)
        except (ServiceForbiddenError, ServiceNotFoundError):
            return False
        return True

    def _last_review_decision(
        self, bl: Any, scope: ScopeContext | None, job: dict[str, Any], job_id: str
    ) -> dict[str, Any] | None:
        from core.application.workflows.approval_review_history import last_review_decision
        from core.application.workflows.approval_waiting_propagation import (
            invoke_chain_max_depth,
        )

        return last_review_decision(
            bl,
            job_id,
            job,
            can_read=lambda row: self._can_read_job(bl, scope, dict(row)),
            max_depth=invoke_chain_max_depth(),
        )

    def list_history(
        self,
        job_id: str,
        *,
        limit: int = 100,
        scope: ScopeContext | None = None,
    ) -> ApprovalHistoryView:
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(DATABASE_NOT_CONFIGURED_MESSAGE)
        job = self._require_job(bl, str(job_id))
        resolved = self._scope(scope, project_id=job.get("project_id"))
        project = self._project(bl, job.get("project_id"))
        if project:
            require_project_access(resolved, project)
        self._require_job_workflow_access(resolved, job)

        meta = job.get("meta") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        if not isinstance(meta, dict):
            meta = {}
        approvals = meta.get("approvals")
        if not isinstance(approvals, list):
            approvals = []
        safe_limit = max(1, min(int(limit or 100), 500))
        return ApprovalHistoryView(
            job_id=str(job_id),
            approvals=[
                serialize_approval_history_entry(item)
                for item in approvals
                if isinstance(item, dict)
            ][-safe_limit:],
        )


__all__ = [
    "ApprovalQueryService",
    "job_in_section",
]
