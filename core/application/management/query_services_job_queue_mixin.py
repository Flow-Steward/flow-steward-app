# ruff: noqa: F403, F405
"""Job queues read by operators: the pending-approvals queue and one project's jobs."""

from __future__ import annotations

from core.application.jobs.query_scope import job_row_in_scope
from core.application.observability_vocabulary import job_status_filter_statuses

from .query_models import AwaitingApprovalJobsPageView
from .query_services_common import *  # NOSONAR
from .query_services_job_core_mixin import job_page_window, job_status_filter_values

#: Page-size ceiling of the pending-approvals page.
_APPROVALS_PAGE_MAX = 200


class JobQueryServiceQueueMixin:
    def list_awaiting_approval_jobs(
        self, *, scope: ScopeContext | None = None
    ) -> list[AwaitingApprovalJobView]:
        resolved = self._scope(scope)
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(DATABASE_NOT_CONFIGURED_MESSAGE)
        try:
            jobs = bl.get_awaiting_approval_jobs(account_id=resolved.account_id)
        except Exception as exc:
            # An empty list renders as "No approvals are waiting", so returning
            # one on failure tells the operator there is nothing to decide.
            # That is the worst possible failure mode for this surface: someone
            # shown an error goes looking, someone told the queue is empty does
            # not. Fail loudly instead (AUDIT-038 symptom d).
            logger.warning("list_awaiting_approval_jobs failed: %s", exc)
            raise ServiceUnavailableError("Approvals queue unavailable") from exc
        return [
            AwaitingApprovalJobView.model_validate(
                self._serialize_awaiting_approval_job_row(dict(job))
            )
            for job in (jobs or [])
            if isinstance(job, dict) and job_row_in_scope(resolved, job)
        ]

    def page_awaiting_approval_jobs(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        scope: ScopeContext | None = None,
    ) -> AwaitingApprovalJobsPageView:
        """One bounded page of the scoped approvals queue, newest first, with its total.

        The page is cut from the queue after the scope filter, so ``total`` and
        ``has_more`` describe exactly what this caller may decide.
        """
        normalized_page, normalized_per_page, offset = job_page_window(
            page=page, per_page=per_page or 25, offset=0, cap=_APPROVALS_PAGE_MAX
        )
        jobs = self.list_awaiting_approval_jobs(scope=scope)
        total = len(jobs)
        page_jobs = jobs[offset : offset + normalized_per_page]
        return AwaitingApprovalJobsPageView(
            jobs=page_jobs,
            total=total,
            page=normalized_page,
            per_page=normalized_per_page,
            total_pages=max(1, -(-total // normalized_per_page)),
            has_more=offset + len(page_jobs) < total,
        )

    def list_project_jobs(
        self,
        project_id: Any,
        *,
        page: int = 1,
        per_page: int = 25,
        status: str = "",
        scope: ScopeContext | None = None,
    ) -> ProjectJobsListView:
        resolved = self._scope(scope, project_id=project_id)
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(DATABASE_NOT_CONFIGURED_MESSAGE)
        normalized_project_id = coerce_optional_project_id(project_id)
        if normalized_project_id is None:
            raise ServiceNotFoundError(PROJECT_NOT_FOUND_MESSAGE)
        project = bl.get_project(normalized_project_id)
        if not project:
            raise ServiceNotFoundError(PROJECT_NOT_FOUND_MESSAGE)
        require_project_access(resolved, project)

        normalized_per_page = max(1, min(int(per_page or 25), 500))
        normalized_page = max(1, int(page or 1))
        offset = (normalized_page - 1) * normalized_per_page
        # Status (group keys select their whole group) and the caller's workflow
        # restrictions filter in the store, before the page and the total.
        filters: dict[str, Any] = {}
        statuses = job_status_filter_statuses(job_status_filter_values(status))
        if statuses:
            filters["status"] = statuses
        if resolved.workflow_id:
            filters["workflow_id"] = str(resolved.workflow_id)
        if resolved.allowed_workflow_ids:
            filters["workflow_ids"] = sorted(resolved.allowed_workflow_ids)
        rows = (
            bl.get_jobs(normalized_project_id, limit=normalized_per_page, offset=offset, **filters)
            if hasattr(bl, "get_jobs")
            else []
        )
        total = (
            bl.count_jobs(normalized_project_id, **filters)
            if hasattr(bl, "count_jobs")
            else len(rows)
        )

        serialized_rows: list[ProjectJobRowView] = []
        for row in rows or []:
            payload = {
                key: self._serialize_project_job_value(value)
                for key, value in self._project_job_row_payload(row).items()
            }
            display_summary = self._build_display_summary(payload.get("task_summary"))
            payload["display_summary"] = display_summary or str(payload.get("task_summary") or "")
            serialized_rows.append(ProjectJobRowView.model_validate(payload))

        return ProjectJobsListView(
            jobs=serialized_rows,
            total=int(total or 0),
            page=normalized_page,
            per_page=normalized_per_page,
            total_pages=max(1, -(-int(total or 0) // normalized_per_page)),
        )


__all__ = ["JobQueryServiceQueueMixin"]
