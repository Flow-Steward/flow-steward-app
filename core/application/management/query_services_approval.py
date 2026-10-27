# ruff: noqa: F403, F405
from __future__ import annotations

from typing import Any

from core.application.jobs.public_approval_serializer import (
    serialize_approval_history_entry,
    serialize_token_approval_payload,
)
from core.application.jobs.workflow_identity import resolve_job_workflow_id

from .approval_decision_service import ApprovalDecisionService
from .query_services_common import *  # NOSONAR


def _is_token_principal(scope: ScopeContext) -> bool:
    source = str(getattr(scope, "auth_source", "") or "").strip().lower()
    principal_type = str(getattr(scope, "principal_type", "") or "").strip().lower()
    return source in {"machine", "pat", "token"} or principal_type == "service_account"


class ApprovalQueryService:
    def __init__(
        self,
        *,
        get_logger: Callable[[], Any],
        get_thread_state_snapshot: Callable[[str], Any],
        approval_fallback_payload: Callable[[dict[str, Any]], dict[str, Any]],
        to_json_obj: Callable[[Any], Any] | None = None,
        get_project_by_id: Callable[[Any], dict[str, Any] | None] | None = None,
    ):
        self._get_logger = get_logger
        self._get_thread_state_snapshot = get_thread_state_snapshot
        self._approval_fallback_payload = approval_fallback_payload
        self._to_json_obj = to_json_obj or (lambda value: value)
        self._get_project_by_id = get_project_by_id

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
]
