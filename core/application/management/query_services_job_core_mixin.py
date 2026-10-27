# ruff: noqa: F403, F405
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, cast

from core.application.jobs.query_scope import job_row_in_scope
from core.application.jobs.workflow_identity import resolve_job_workflow_id
from core.application.jobs.workflow_run_models import PublicWorkflowResultV1
from core.application.jobs.workflow_run_projection import (
    ArtifactResolverUnavailable,
    build_public_result,
)
from core.application.management.errors import ACCESS_DENIED_MESSAGE
from core.application.management.query_services_approval import ApprovalQueryService
from core.application.reports.job_trigger import job_trigger
from core.application.security.credential_redaction import redact
from core.application.workflows.workflow_hook_contract import hook_run_label

from .query_models import JobDetailContractView
from .query_services_common import *  # NOSONAR


def job_status_filter_values(status: str | Sequence[str] | None) -> tuple[str, ...]:
    """Normalize a jobs status filter: one value, a list, or comma lists, deduplicated."""
    raw = (status,) if isinstance(status, str) else tuple(status or ())
    values = (part.strip().lower() for item in raw for part in str(item or "").split(","))
    return tuple(dict.fromkeys(value for value in values if value))


class _JobDiagnosticQueries(Protocol):
    """Sibling-mixin surface required by diagnostic report assembly."""

    def list_errors(self, *args: Any, **kwargs: Any) -> Any: ...

    def get_job_definition(self, *args: Any, **kwargs: Any) -> Any: ...

    def list_decision_overrides(self, *args: Any, **kwargs: Any) -> Any: ...

    def get_runtime_journal(self, *args: Any, **kwargs: Any) -> Any: ...

    def get_causal_chain(self, *args: Any, **kwargs: Any) -> Any: ...

    def get_cost_breakdown(self, *args: Any, **kwargs: Any) -> Any: ...


class JobQueryServiceCoreMixin:
    def __init__(
        self,
        *,
        get_logger: Callable[[], Any],
        get_event_store: Callable[[], Any] | None = None,
        get_trace_store: Callable[[], Any] | None = None,
        serialize_attention_job_row: Callable[[dict[str, Any]], dict[str, Any]],
        serialize_awaiting_approval_job_row: Callable[[dict[str, Any]], dict[str, Any]],
        job_attention_group: Callable[[str, Any], str],
        build_runtime_payload_for_api: Callable[[dict[str, Any], Any], Awaitable[dict[str, Any]]],
        build_display_summary: Callable[[Any], str] | None = None,
        serialize_log_row: Callable[[dict[str, Any], Any], dict[str, Any]] | None = None,
        serialize_event_row: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        serialize_agent_message_row: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        serialize_decision_override_row: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        serialize_job_error_row: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        serialize_job_step_replay_row: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        build_job_causal_chain: Callable[[Any, Any], dict[str, Any]] | None = None,
        resolve_replay_message_row: Callable[..., tuple[dict[str, Any] | None, str | None, int]]
        | None = None,
        collect_replay_step_trace: Callable[[Any, Any, dict[str, Any]], dict[str, Any]]
        | None = None,
        get_thread_state_snapshot: Callable[[str], Any] | None = None,
        get_committed_checkpoint_values: Callable[[str], Any] | None = None,
        build_runtime_payload: Callable[[dict[str, Any], Any], dict[str, Any]] | None = None,
        resolve_job_project_name: Callable[[Any, dict[str, Any]], str] | None = None,
        resolve_artifact: Callable[[str], dict[str, Any] | None] | None = None,
        get_project_by_id: Callable[[Any], dict[str, Any] | None] | None = None,
        approval_query_service: ApprovalQueryService | None = None,
    ):
        self._get_committed_checkpoint_values = get_committed_checkpoint_values
        self._get_logger = get_logger
        self._get_event_store = get_event_store
        self._get_trace_store = get_trace_store
        self._serialize_attention_job_row = serialize_attention_job_row
        self._serialize_awaiting_approval_job_row = serialize_awaiting_approval_job_row
        self._job_attention_group = job_attention_group
        self._build_runtime_payload_for_api = build_runtime_payload_for_api
        self._build_display_summary = build_display_summary or (
            lambda value: str(value or "").strip()
        )
        self._serialize_log_row = serialize_log_row or (lambda row, _job_id: dict(row))
        self._serialize_event_row = serialize_event_row or (lambda row: dict(row))
        self._serialize_agent_message_row = serialize_agent_message_row or (lambda row: dict(row))
        self._serialize_decision_override_row = serialize_decision_override_row or (
            lambda row: dict(row)
        )
        self._serialize_job_error_row = serialize_job_error_row or (lambda row: dict(row))
        self._serialize_job_step_replay_row = serialize_job_step_replay_row or (
            lambda row: dict(row)
        )
        self._build_job_causal_chain = build_job_causal_chain or (
            lambda _bl, job_id: {"job_id": str(job_id)}
        )
        self._resolve_replay_message_row = resolve_replay_message_row
        self._collect_replay_step_trace = collect_replay_step_trace or (
            lambda _bl, _job_id, row: {"message": dict(row)}
        )
        self._get_thread_state_snapshot = get_thread_state_snapshot or (lambda _thread_id: None)
        self._build_runtime_payload = build_runtime_payload or (lambda _job, _bl: {})
        self._resolve_job_project_name = resolve_job_project_name or (lambda _bl, _job: "")
        self._resolve_artifact = resolve_artifact
        self._get_project_by_id = get_project_by_id
        self._approval_query_service = approval_query_service

    @staticmethod
    def _scope(
        scope: ScopeContext | None,
        *,
        project_id: Any = None,
        workspace_id: Any = None,
    ) -> ScopeContext:
        return _service_scope(scope, project_id=project_id, workspace_id=workspace_id)

    @staticmethod
    def _scope_allows_project_id(scope: ScopeContext, project_id: Any) -> bool:
        if not scope.allowed_project_ids:
            return True
        normalized_project_id = coerce_optional_project_id(project_id)
        return (
            normalized_project_id is not None and normalized_project_id in scope.allowed_project_ids
        )

    @staticmethod
    def _serialize_project_job_value(value: Any) -> Any:
        if isinstance(value, uuid.UUID):
            return str(value)
        if hasattr(value, "isoformat"):
            try:
                return value.isoformat()
            except Exception:
                return str(value)
        return value

    @staticmethod
    def _project_job_row_payload(row: Any) -> dict[str, Any]:
        if isinstance(row, dict):
            return dict(row)
        try:
            return dict(row)
        except Exception:
            try:
                return dict(vars(row))
            except Exception:
                return {}

    def _logger_or_raise(self) -> Any:
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(DATABASE_NOT_CONFIGURED_MESSAGE)
        return bl

    def _job_context(
        self,
        job_id: str,
        *,
        scope: ScopeContext | None = None,
    ) -> tuple[Any, dict[str, Any], ScopeContext]:
        bl = self._logger_or_raise()
        job = bl.get_job(job_id)
        if not job:
            raise ServiceNotFoundError("Job not found")
        resolved_scope = self._scope(scope, project_id=job.get("project_id"))
        normalized_project_id = coerce_optional_project_id(job.get("project_id"))
        if normalized_project_id is not None:
            try:
                project = (
                    self._get_project_by_id(normalized_project_id)
                    if self._get_project_by_id is not None
                    else bl.get_project(normalized_project_id)
                )
            except Exception as exc:
                logger.warning(
                    "JobQueryService._job_context project lookup failed for job %s: %s", job_id, exc
                )
                raise ServiceUnavailableError("Project lookup unavailable") from exc
            if project is None:
                # Lookup succeeded and the project simply no longer exists. That
                # is an orphaned job, not an outage: reporting it as "Project
                # lookup unavailable" (503) hid the job from the operator who
                # needs to clear it. Authorize against the job's own account,
                # the same rule used below for jobs that never had a project.
                require_account_access(resolved_scope, job)
            elif not isinstance(project, dict):
                raise ServiceUnavailableError("Project lookup unavailable")
            else:
                require_project_access(resolved_scope, project)
        else:
            require_account_access(resolved_scope, job)
        require_workflow_access(resolved_scope, resolve_job_workflow_id(job))
        return bl, job, resolved_scope

    @staticmethod
    def _json_obj(value: Any) -> Any:
        if value is None:
            return {}
        if isinstance(value, str):
            try:
                return json.loads(value)
            except Exception:
                return {}
        return value

    @staticmethod
    def _job_workflow_metadata(job: dict[str, Any], runtime: dict[str, Any]) -> dict[str, str]:
        runtime_state = JobQueryServiceCoreMixin._json_obj(job.get("runtime_state"))
        if not isinstance(runtime_state, dict):
            runtime_state = JobQueryServiceCoreMixin._json_obj(runtime.get("runtime_state"))
        if not isinstance(runtime_state, dict):
            runtime_state = {}

        meta = JobQueryServiceCoreMixin._json_obj(job.get("meta"))
        if not isinstance(meta, dict):
            meta = {}
        invocation = JobQueryServiceCoreMixin._json_obj(meta.get("invocation"))
        if not isinstance(invocation, dict):
            invocation = {}
        candidates = (
            ("job.selected_workflow", job.get("selected_workflow")),
            ("job.selected_workflow_key", job.get("selected_workflow_key")),
            ("job.selected_workflow_id", job.get("selected_workflow_id")),
            ("job.meta.selected_workflow_id", meta.get("selected_workflow_id")),
            (
                "job.meta.invocation.selected_workflow_id",
                invocation.get("selected_workflow_id"),
            ),
            ("runtime_state.workflow_id", runtime_state.get("workflow_id")),
            ("job.workflow_id", job.get("workflow_id")),
        )
        for source, value in candidates:
            workflow_id = str(value or "").strip()
            if workflow_id:
                return {
                    "selected_workflow": workflow_id,
                    "selected_workflow_source": source,
                    "workflow_selection_reason": f"Resolved from {source}.",
                }
        return {
            "selected_workflow": "",
            "selected_workflow_source": "",
            "workflow_selection_reason": "Workflow was not recorded for this job.",
        }

    @staticmethod
    def _job_workflow_display_name(bl: Any, project_id: Any, workflow_id: str) -> str:
        normalized_project_id = coerce_optional_project_id(project_id)
        normalized_workflow_id = str(workflow_id or "").strip()
        if normalized_project_id is None or not normalized_workflow_id:
            return ""
        get_project_workflows = getattr(bl, "get_project_workflows", None)
        if not callable(get_project_workflows):
            return ""
        try:
            rows = get_project_workflows(normalized_project_id, enabled_only=False) or []
        except Exception as exc:
            logger.debug("Job workflow display-name lookup skipped: %s", exc)
            return ""
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("workflow_id") or "").strip() != normalized_workflow_id:
                continue
            for key in ("workflow_display_name", "display_name", "name", "workflow_name"):
                label = str(row.get(key) or "").strip()
                if label:
                    return label
        return ""

    def bootstrap_attention_jobs(
        self,
        *,
        limit: int = 200,
        offset: int = 0,
        scope: ScopeContext | None = None,
    ) -> list[AttentionJobView]:
        resolved = self._scope(scope)
        bl = self._get_logger()
        if not bl or not hasattr(bl, "list_jobs_for_attention"):
            return []
        try:
            rows = bl.list_jobs_for_attention(
                limit=max(1, min(int(limit or 200), 2000)),
                offset=max(0, int(offset or 0)),
                account_id=resolved.account_id,
            )
        except Exception as exc:
            logger.warning("bootstrap_attention_jobs failed: %s", exc)
            return []
        return [
            AttentionJobView.model_validate(self._serialize_attention_job_row(dict(row)))
            for row in (rows or [])
            if isinstance(row, dict)
        ]

    def list_jobs(
        self,
        *,
        group_by: str = "",
        project_id: Any = None,
        limit: int = 25,
        offset: int = 0,
        page: int = 1,
        per_page: int = 0,
        status: str | Sequence[str] = "",
        workflow_id: str = "",
        task_source: str | None = None,
        search: str = "",
        workspace_id: Any = None,
        scope: ScopeContext | None = None,
        hide_acknowledged: bool = False,
    ) -> JobsListView | JobsAttentionGroupsView:
        """List jobs; ``status`` takes one status, several, or comma lists of them (any match).

        ``hide_acknowledged`` leaves out runs an operator has acknowledged.
        ``task_source`` optionally restricts history to whole-workflow Test Runs.
        """
        resolved = self._scope(scope)
        for selected, exact in (
            (project_id, resolved.project_id),
            (workspace_id, resolved.workspace_id),
        ):
            if exact and selected and str(selected) != str(exact):
                raise ServiceForbiddenError(ACCESS_DENIED_MESSAGE)
        if task_source is not None and task_source != "workflow_test_run":
            raise ServiceValidationError(
                "Unsupported task_source value",
                payload={
                    "error": "Unsupported task_source value",
                    "supported": ["workflow_test_run"],
                },
            )
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(DATABASE_NOT_CONFIGURED_MESSAGE)

        normalized_group = str(group_by or "").strip().lower()
        normalized_limit = max(1, min(int(limit or 25), 2000))
        normalized_project_id = coerce_optional_project_id(project_id)
        normalized_statuses = job_status_filter_values(status)
        normalized_status = normalized_statuses or None
        echoed_status = ",".join(normalized_statuses) or None
        normalized_workflow_id = str(workflow_id or "").strip() or None
        normalized_search = " ".join(str(search or "").split()) or None
        if (
            normalized_project_id
            and resolved.allowed_project_ids
            and normalized_project_id not in resolved.allowed_project_ids
        ):
            raise ServiceForbiddenError(ACCESS_DENIED_MESSAGE)
        if normalized_workflow_id:
            require_workflow_access(resolved, normalized_workflow_id)
            if resolved.workflow_id and normalized_workflow_id != resolved.workflow_id:
                raise ServiceForbiddenError(ACCESS_DENIED_MESSAGE)
        normalized_workspace_id = coerce_optional_workspace_id(workspace_id)
        acct = resolved.account_id
        workspace_allowed = (
            not resolved.allowed_workspace_ids
            or normalized_workspace_id is None
            or normalized_workspace_id in resolved.allowed_workspace_ids
        )

        def _workspace_filter_kwargs() -> dict[str, str]:
            return {"workspace_id": normalized_workspace_id} if normalized_workspace_id else {}

        def _list_filter_kwargs() -> dict[str, Any]:
            filters: dict[str, Any] = {"scope": resolved}
            if normalized_workflow_id:
                filters["workflow_id"] = normalized_workflow_id
            if task_source is not None:
                filters["task_source"] = task_source
            if normalized_search:
                filters["search"] = normalized_search
            if hide_acknowledged:
                filters["hide_acknowledged"] = True
            return filters

        if not normalized_group:
            if not workspace_allowed:
                normalized_per_page = max(1, min(int(per_page or normalized_limit), 500))
                normalized_page = max(1, int(page or 1))
                return JobsListView(
                    jobs=[],
                    total=0,
                    page=normalized_page,
                    per_page=normalized_per_page,
                    offset=max(0, int(offset or 0)),
                    total_pages=1,
                    project_id=str(normalized_project_id) if normalized_project_id else None,
                    workspace_id=normalized_workspace_id,
                    status=echoed_status,
                )
            normalized_per_page = max(1, min(int(per_page or normalized_limit), 500))
            normalized_offset = max(0, int(offset or 0))
            normalized_page = (
                (normalized_offset // normalized_per_page) + 1
                if normalized_offset
                else max(1, int(page or 1))
            )
            db_offset = (
                normalized_offset
                if normalized_offset
                else (normalized_page - 1) * normalized_per_page
            )
            rows: list[dict[str, Any]]
            total: int
            if resolved.allowed_project_ids and normalized_project_id is None:
                # Restricted principals should be filtered in DB by allowed
                # project ids, not by scanning the full account dataset.
                allowed_project_ids = sorted(resolved.allowed_project_ids)
                rows = (
                    bl.list_jobs_for_attention(
                        project_id=None,
                        project_ids=allowed_project_ids,
                        limit=normalized_per_page,
                        offset=db_offset,
                        status=normalized_status,
                        account_id=acct,
                        **_list_filter_kwargs(),
                        **_workspace_filter_kwargs(),
                    )
                    if hasattr(bl, "list_jobs_for_attention")
                    else []
                )
                rows = [dict(row) for row in rows if isinstance(row, dict)]
                total = (
                    int(
                        bl.count_jobs_for_attention(
                            project_id=None,
                            project_ids=allowed_project_ids,
                            status=normalized_status,
                            account_id=acct,
                            **_list_filter_kwargs(),
                            **_workspace_filter_kwargs(),
                        )
                        or 0
                    )
                    if hasattr(bl, "count_jobs_for_attention")
                    else len(rows)
                )
            else:
                rows = (
                    bl.list_jobs_for_attention(
                        project_id=normalized_project_id,
                        limit=normalized_per_page,
                        offset=db_offset,
                        status=normalized_status,
                        account_id=acct,
                        **_list_filter_kwargs(),
                        **_workspace_filter_kwargs(),
                    )
                    if hasattr(bl, "list_jobs_for_attention")
                    else []
                )
                rows = [
                    dict(row)
                    for row in rows
                    if isinstance(row, dict)
                    and self._scope_allows_project_id(resolved, row.get("project_id"))
                ]
                if (
                    resolved.allowed_project_ids
                    and normalized_project_id is not None
                    and str(normalized_project_id) not in resolved.allowed_project_ids
                ):
                    rows = []
                    total = 0
                else:
                    total = (
                        int(
                            bl.count_jobs_for_attention(
                                project_id=normalized_project_id,
                                status=normalized_status,
                                account_id=acct,
                                **_list_filter_kwargs(),
                                **_workspace_filter_kwargs(),
                            )
                            or 0
                        )
                        if hasattr(bl, "count_jobs_for_attention")
                        else len(rows)
                    )
            return JobsListView(
                jobs=[
                    AttentionJobView.model_validate(self._serialize_attention_job_row(dict(row)))
                    for row in rows
                    if isinstance(row, dict)
                ],
                total=int(total or 0),
                page=normalized_page,
                per_page=normalized_per_page,
                offset=db_offset,
                total_pages=max(1, -(-int(total or 0) // normalized_per_page)),
                project_id=str(normalized_project_id) if normalized_project_id else None,
                workspace_id=normalized_workspace_id,
                status=echoed_status,
            )

        if normalized_group != "attention":
            raise ServiceValidationError(
                "Unsupported group_by value",
                payload={"error": "Unsupported group_by value", "supported": ["attention"]},
            )

        if not workspace_allowed:
            return JobsAttentionGroupsView(
                project_id=str(normalized_project_id) if normalized_project_id else None,
                groups=AttentionGroupsView(),
            )

        rows = (
            bl.list_jobs_for_attention(
                project_id=normalized_project_id,
                limit=normalized_limit,
                account_id=acct,
                status=normalized_status,
                **_list_filter_kwargs(),
                **_workspace_filter_kwargs(),
            )
            if hasattr(bl, "list_jobs_for_attention")
            else []
        )
        rows = [
            dict(row)
            for row in rows
            if isinstance(row, dict)
            and self._scope_allows_project_id(resolved, row.get("project_id"))
        ]
        groups = AttentionGroupsView()
        for row in rows:
            payload = AttentionJobView.model_validate(self._serialize_attention_job_row(dict(row)))
            group_key = self._job_attention_group(
                str(row.get("status") or ""),
                row.get("acknowledged_at"),
            )
            getattr(groups, group_key).append(payload)
        return JobsAttentionGroupsView(
            group_by="attention",
            project_id=str(normalized_project_id) if normalized_project_id else None,
            groups=groups,
        )

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

    def list_project_jobs(
        self,
        project_id: Any,
        *,
        page: int = 1,
        per_page: int = 25,
        status: str = "",
        scope: ScopeContext | None = None,
    ) -> ProjectJobsListView:
        _ = status
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
        rows = (
            bl.get_jobs(normalized_project_id, limit=normalized_per_page, offset=offset)
            if hasattr(bl, "get_jobs")
            else []
        )
        total = bl.count_jobs(normalized_project_id) if hasattr(bl, "count_jobs") else len(rows)

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
                PublicWorkflowResultV1.model_validate(redact(public_result.model_dump(mode="json")))
                if public_result is not None
                else None
            )
        cost = bl.get_job_cost_summary(job_id)
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
        unknown_cli_tokens = int((cost or {}).get("unknown_cli_tokens") or 0)
        known_cost_usd = float((cost or {}).get("cost_usd") or 0)
        if unknown_cli_tokens > 0 and known_cost_usd > 0:
            cost_knownness = "partial"
        elif unknown_cli_tokens > 0:
            cost_knownness = "unknown"
        else:
            cost_knownness = "known"
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
            cost=JobCostSummaryView(
                tokens_prompt=int((cost or {}).get("tokens_prompt") or 0),
                tokens_completion=int((cost or {}).get("tokens_completion") or 0),
                tokens_total=int((cost or {}).get("tokens_total") or 0),
                cost_usd=known_cost_usd,
                known_cost_usd=known_cost_usd,
                unknown_cli_tokens=unknown_cli_tokens,
                cost_knownness=cost_knownness,
                llm_calls=int((cost or {}).get("llm_calls") or 0),
            ),
            failure_summary=build_job_failure_summary(job=job, runtime=runtime or {}),
        )

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
        cost_view = diagnostic_queries.get_cost_breakdown(str(job_id), scope=scope)

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
            cost_breakdown=_dump(cost_view),
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
    "JobQueryServiceCoreMixin",
]
