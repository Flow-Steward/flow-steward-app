# ruff: noqa: F403, F405
"""Job status, detail contract, diagnostic report, workflow run and trace reads."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, cast

from core.application.jobs.query_scope import job_row_in_scope
from core.application.jobs.workflow_result_links import with_artifact_downloads
from core.application.jobs.workflow_run_models import PublicWorkflowResultV1
from core.application.jobs.workflow_run_projection import (
    ArtifactResolverUnavailable,
    build_public_result,
)
from core.application.management.errors import ACCESS_DENIED_MESSAGE
from core.application.reports.job_trigger import job_trigger
from core.application.security.credential_redaction import redact
from core.application.workflows.workflow_action_contract import action_run_label
from core.application.workflows.workflow_hook_contract import hook_run_label

from .query_models import JobDetailContractView
from .query_services_common import *  # NOSONAR


class _JobDiagnosticQueries(Protocol):
    """Sibling-mixin surface required by diagnostic report assembly."""

    def list_errors(self, *args: Any, **kwargs: Any) -> Any: ...

    def get_job_definition(self, *args: Any, **kwargs: Any) -> Any: ...

    def list_decision_overrides(self, *args: Any, **kwargs: Any) -> Any: ...

    def get_runtime_journal(self, *args: Any, **kwargs: Any) -> Any: ...

    def get_causal_chain(self, *args: Any, **kwargs: Any) -> Any: ...

    def get_usage_breakdown(self, *args: Any, **kwargs: Any) -> Any: ...


class JobQueryServiceStatusMixin:
    """Single-job reads; relies on the core mixin for scope and job context."""

    async def get_job_status(
        self,
        job_id: str,
        *,
        scope: ScopeContext | None = None,
        include_workflow_result: bool = False,
    ) -> JobStatusView:
        bl, job, resolved_scope = self._job_context(job_id, scope=scope)
        workflow_result = None
        if include_workflow_result:
            workflow_id = self._job_workflow_metadata(job, {}).get("selected_workflow") or ""
            if workflow_id:
                require_workflow_access(resolved_scope, workflow_id)
            try:
                public_result = build_public_result(
                    job, str(job.get("status") or ""), resolve_artifact=self._resolve_artifact
                )
            except ArtifactResolverUnavailable as exc:
                raise ServiceUnavailableError("Public workflow result is unavailable") from exc
            workflow_result = (
                PublicWorkflowResultV1.model_validate(
                    with_artifact_downloads(
                        cast(dict[str, object], redact(public_result.model_dump(mode="json"))),
                        job_id,
                    )
                )
                if public_result is not None
                else None
            )
        usage = bl.get_job_usage_summary(job_id)
        runtime = await self._build_runtime_payload_for_api(job, bl)
        terminal_job = str(job.get("status") or "").strip().lower() in _TERMINAL_JOB_STATUSES
        persisted_step = str(job.get("current_step_id") or "").strip()
        persisted_group = str(job.get("current_group_id") or "").strip()
        if terminal_job and isinstance(runtime, dict):
            runtime = {
                **runtime,
                "current_step_id": persisted_step or None,
                "current_group_id": persisted_group or None,
                "active_steps": [],
                "active_agent": "",
            }
        # The step and its group are one scope: take both from the same source.
        # A running job's scope is the checkpoint cursor (in a parallel group the
        # step is empty); a finished job keeps the last persisted step.
        if terminal_job:
            current_step_id, current_group_id = persisted_step, persisted_group
        else:
            current_step_id = str((runtime or {}).get("current_step_id") or "").strip()
            current_group_id = str((runtime or {}).get("current_group_id") or "").strip()
        current_agent = (
            ""
            if terminal_job
            else str(job.get("current_agent") or (runtime or {}).get("active_agent") or "").strip()
        )
        workflow_metadata = self._job_workflow_metadata(job, runtime or {})
        selected_workflow = workflow_metadata["selected_workflow"]
        selected_workflow_display_name = (
            hook_run_label(job)
            or action_run_label(job)
            or str(job.get("selected_workflow_display_name") or "").strip()
            or self._job_workflow_display_name(
                bl,
                job.get("project_id"),
                selected_workflow,
            )
        )
        meta = self._json_obj(job.get("meta"))
        if not isinstance(meta, dict):
            meta = {}
        invocation = self._json_obj(meta.get("invocation"))
        if not isinstance(invocation, dict):
            invocation = {}
        from core.application.workflows.workflow_start_runtime import (
            _sanitize_invocation_value,
        )

        sanitized_invocation, _ = _sanitize_invocation_value(invocation)
        invocation = sanitized_invocation if isinstance(sanitized_invocation, dict) else {}
        acknowledged_at = job.get("acknowledged_at")
        return JobStatusView(
            id=str(job["id"]),
            status=str(job["status"]),
            project_id=str(job.get("project_id") or "").strip() or None,
            thread_id=job.get("thread_id"),
            task_summary=str(job["task_summary"]),
            mode=str(job.get("mode") or "").strip() or None,
            selected_workflow=selected_workflow or None,
            selected_workflow_display_name=selected_workflow_display_name or None,
            selected_workflow_source=workflow_metadata["selected_workflow_source"] or None,
            workflow_selection_reason=workflow_metadata["workflow_selection_reason"] or None,
            started_at=job["started_at"].isoformat() if job.get("started_at") else None,
            finished_at=job["finished_at"].isoformat() if job.get("finished_at") else None,
            duration_ms=job.get("duration_ms"),
            acknowledged_at=(
                acknowledged_at.isoformat()
                if isinstance(acknowledged_at, datetime)
                else str(acknowledged_at or "").strip() or None
            ),
            acknowledged_by=str(job.get("acknowledged_by") or "").strip() or None,
            current_step_id=current_step_id or None,
            current_group_id=current_group_id or None,
            active_steps=[] if terminal_job else list((runtime or {}).get("active_steps") or []),
            current_agent=current_agent or None,
            invocation=invocation,
            workflow_result=workflow_result,
            trigger=job_trigger({**job, "meta": meta, "invocation": invocation}),
            runtime=runtime or {},
            usage=JobUsageSummaryView(
                tokens_prompt=int((usage or {}).get("tokens_prompt") or 0),
                tokens_completion=int((usage or {}).get("tokens_completion") or 0),
                tokens_total=int((usage or {}).get("tokens_total") or 0),
                llm_calls=int((usage or {}).get("llm_calls") or 0),
            ),
            failure_summary=build_job_failure_summary(job=job, runtime=runtime or {}),
        )

    def extension_display_names(
        self, extension_ids: list[str], *, scope: ScopeContext | None = None
    ) -> dict[str, str]:
        """The names a run's extension steps are shown by; ``{}`` when they cannot be read."""
        from core.application.extensions.display_labels import installed_extension_names

        try:
            connection = getattr(self._logger_or_raise(), "conn", None)
            return installed_extension_names(
                connection,
                account_id=self._scope(scope).account_id,
                extension_ids=extension_ids,
            )
        except Exception as exc:
            # A name is a courtesy: the run page still shows the extension's id.
            logger.debug("Extension display names unavailable: %s", exc)
            return {}

    async def get_job_detail_contract(
        self,
        job_id: str,
        *,
        scope: ScopeContext | None = None,
        user_role: str = "operator",
    ) -> JobDetailContractView:
        """Build the canonical job detail contract from shared query-service methods."""
        from core.application.reports.job_detail_contract import build_job_detail_contract

        payload = await build_job_detail_contract(
            job_id=str(job_id),
            scope=scope,
            job_query_service=self,
            approval_query_service=self._approval_query_service,
            get_thread_state_values=None,
            user_role=str(user_role or "operator").strip() or "operator",
            resolve_artifact=self._resolve_artifact,
        )
        return JobDetailContractView.model_validate(payload)

    def get_job_artifact_download(
        self,
        job_id: str,
        artifact_id: str,
        *,
        scope: ScopeContext | None = None,
    ):
        """Authorize one job-owned artifact without reading its body."""
        from core.application.jobs.job_artifact_access import resolve_job_artifact_download

        _, job, _ = self._job_context(job_id, scope=scope)
        return resolve_job_artifact_download(
            job=job,
            artifact_id=artifact_id,
            resolve_artifact=self._resolve_artifact,
        )

    @staticmethod
    def _diagnostic_report_rows(
        fetch_rows: Callable[..., Any],
        serialize_row: Callable[[dict[str, Any]], dict[str, Any]],
        *,
        job_id: str,
        page_size: int = 2000,
        max_pages: int = 1000,
    ) -> list[dict[str, Any]]:
        """Read an authorized job stream forward without an unbounded query."""
        collected: list[dict[str, Any]] = []
        cursor = ""
        for _ in range(max_pages):
            rows = fetch_rows(str(job_id), since_id=cursor, limit=page_size) or []
            normalized = [dict(row) for row in rows if isinstance(row, dict)]
            if not normalized:
                return collected
            collected.extend(serialize_row(row) for row in normalized)
            next_cursor = str(normalized[-1].get("id") or "").strip()
            if not next_cursor or next_cursor == cursor:
                raise ServiceUnavailableError("Diagnostic report pagination did not advance")
            cursor = next_cursor
            if len(normalized) < page_size:
                return collected
        raise ServiceUnavailableError("Diagnostic report exceeds the supported page count")

    def validate_job_diagnostic_report_access(
        self,
        job_id: str,
        *,
        scope: ScopeContext | None = None,
    ) -> None:
        """Authorize a report download before its response headers are sent."""
        _, job, _ = self._job_context(job_id, scope=scope)
        resolved = self._scope(scope)
        scoped_job = {
            **job,
            "selected_workflow": self._job_workflow_metadata(job, {}).get("selected_workflow")
            or "",
        }
        if not job_row_in_scope(resolved, scoped_job):
            raise ServiceForbiddenError(ACCESS_DENIED_MESSAGE)

    async def get_job_diagnostic_report(
        self,
        job_id: str,
        *,
        scope: ScopeContext | None = None,
    ) -> dict[str, Any]:
        """Build a complete redacted persisted report without artifact bodies."""
        from core.application.reports.job_detail_contract import build_job_detail_contract
        from core.application.reports.job_diagnostic_report import build_job_diagnostic_report

        bl, job, _ = self._job_context(job_id, scope=scope)
        detail = await build_job_detail_contract(
            job_id=str(job_id),
            scope=scope,
            job_query_service=self,
            approval_query_service=None,
            get_thread_state_values=None,
            user_role="operator",
            resolve_artifact=self._resolve_artifact,
        )
        messages = self._diagnostic_report_rows(
            lambda selected_job_id, **kwargs: bl.get_agent_messages(
                selected_job_id,
                newest=False,
                before_id=None,
                **kwargs,
            ),
            lambda row: self._serialize_agent_message_row(row),
            job_id=str(job_id),
        )
        logs = self._diagnostic_report_rows(
            lambda selected_job_id, **kwargs: bl.get_logs(
                selected_job_id,
                newest=False,
                before_id=None,
                **kwargs,
            ),
            lambda row: self._serialize_log_row(row, str(job_id)),
            job_id=str(job_id),
        )
        events = self._diagnostic_report_rows(
            bl.get_events,
            lambda row: self._serialize_event_row(row),
            job_id=str(job_id),
        )

        diagnostic_queries = cast(_JobDiagnosticQueries, self)
        errors_view = diagnostic_queries.list_errors(str(job_id), limit=1000, scope=scope)
        decisions_view = diagnostic_queries.list_decision_overrides(
            str(job_id), limit=1000, scope=scope
        )
        journal_view = diagnostic_queries.get_runtime_journal(str(job_id), limit=500, scope=scope)
        causal_view = diagnostic_queries.get_causal_chain(str(job_id), scope=scope)
        usage_view = diagnostic_queries.get_usage_breakdown(str(job_id), scope=scope)

        def _dump(value: Any) -> dict[str, Any]:
            if hasattr(value, "dump"):
                dumped = value.dump()
                return dict(dumped) if isinstance(dumped, dict) else {}
            if hasattr(value, "model_dump"):
                dumped = value.model_dump(mode="json")
                return dict(dumped) if isinstance(dumped, dict) else {}
            return dict(value) if isinstance(value, dict) else {}

        errors_payload = _dump(errors_view)
        decisions_payload = _dump(decisions_view)
        journal_payload = _dump(journal_view)
        return build_job_diagnostic_report(
            job_id=str(job_id),
            project_id=str(job.get("project_id") or "").strip(),
            job=self._project_job_row_payload(job),
            detail_contract=detail,
            messages=messages,
            logs=logs,
            events=events,
            errors=errors_payload.get("errors") or [],
            runtime_journal=journal_payload.get("runtime_journal") or [],
            causal_chain=_dump(causal_view),
            decisions=decisions_payload.get("overrides") or [],
            usage_breakdown=_dump(usage_view),
        )

    def get_workflow_run(
        self,
        job_id: str,
        *,
        scope: ScopeContext | None = None,
        approval_gate: dict[str, Any] | None = None,
        job_errors: list[dict[str, Any]] | None = None,
    ):
        """Return the stable durable integration projection for one authorized job."""
        from core.application.jobs.public_workflow_snapshot_query import (
            PublicWorkflowSnapshotQuery,
        )

        bl, job, resolved_scope = self._job_context(job_id, scope=scope)
        workflow_id = self._job_workflow_metadata(job, {}).get("selected_workflow") or ""
        if workflow_id:
            require_workflow_access(resolved_scope, workflow_id)
        return PublicWorkflowSnapshotQuery(
            bl,
            get_thread_state_snapshot=self._get_thread_state_snapshot,
            to_json_obj=self._json_obj,
            resolve_artifact=self._resolve_artifact,
            get_committed_checkpoint_values=self._get_committed_checkpoint_values,
        ).get(job, approval_gate=approval_gate, job_errors=job_errors)

    def get_trace(
        self,
        job_id: str,
        *,
        scope: ScopeContext | None = None,
        limit: int = 500,
    ) -> JobTraceView:
        _, job, resolved_scope = self._job_context(job_id, scope=scope)
        trace_store = self._get_trace_store() if self._get_trace_store else self._get_logger()
        build_trace = getattr(trace_store, "build_job_trace", None)
        if not callable(build_trace):
            raise ServiceUnavailableError("Trace projection is not available")
        payload = build_trace(
            str(job_id),
            account_id=resolved_scope.account_id,
            limit=max(1, min(int(limit or 500), 2000)),
            job=job,
        )
        if not payload:
            raise ServiceNotFoundError("Trace not found")
        return JobTraceView.model_validate(payload)


__all__ = [
    "JobQueryServiceStatusMixin",
]
