# ruff: noqa: F403, F405
# mypy: ignore-errors
from __future__ import annotations

from .query_services_common import *  # NOSONAR


class ProjectQueryServiceOpsMixin:
    def list_scheduled_jobs(
        self,
        project_id: Any,
        *,
        limit_runs: int = 20,
        scope: ScopeContext | None = None,
    ) -> ProjectScheduledJobsView:
        from core.application.settings.timezone_service import (
            resolve_effective_project_tz,
            resolve_effective_user_tz,
            to_local,
        )

        def as_utc_datetime(value: Any) -> datetime | None:
            if value is None:
                return None
            dt_value: datetime | None = None
            if isinstance(value, datetime):
                dt_value = value
            elif isinstance(value, str):
                text = value.strip()
                if not text:
                    return None
                if text.endswith("Z"):
                    text = f"{text[:-1]}+00:00"
                try:
                    dt_value = datetime.fromisoformat(text)
                except Exception:
                    return None
            if dt_value is None:
                return None
            if dt_value.tzinfo is None:
                dt_value = dt_value.replace(tzinfo=UTC)
            return dt_value.astimezone(UTC)

        def utc_iso(value: Any) -> str | None:
            dt_value = as_utc_datetime(value)
            if not dt_value:
                return None
            iso_value = dt_value.isoformat()
            if iso_value.endswith("+00:00"):
                return f"{iso_value[:-6]}Z"
            return iso_value

        def local_text(value: Any, tz_name: str) -> str | None:
            dt_value = as_utc_datetime(value)
            if not dt_value:
                return None
            try:
                return to_local(dt_value, tz_name).strftime("%Y-%m-%d %H:%M:%S %Z")
            except Exception:
                return None

        resolved = self._scope(scope, project_id=project_id)
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(DATABASE_NOT_CONFIGURED_MESSAGE)
        project = self._require_project_in_scope(bl, str(project_id), resolved)
        if not hasattr(bl, "list_scheduled_jobs"):
            raise ServiceUnavailableError(
                "Scheduled jobs are not available in this database schema"
            )

        project_timezone = resolve_effective_project_tz(
            str(project.get("schedule_timezone") or "").strip() or None
        )
        user_timezone = resolve_effective_user_tz(
            resolved.user_timezone or resolved.project_timezone
        )
        account_id = str(project.get("account_id") or resolved.account_id or "").strip()
        try:
            jobs = (
                bl.list_scheduled_jobs(
                    str(project_id),
                    enabled_only=False,
                    account_id=account_id or None,
                )
                or []
            )
        except TypeError:
            jobs = bl.list_scheduled_jobs(str(project_id), enabled_only=False) or []
        runs = []
        if hasattr(bl, "list_scheduled_job_runs"):
            try:
                runs = (
                    bl.list_scheduled_job_runs(
                        str(project_id),
                        limit=max(1, min(int(limit_runs or 20), 200)),
                        account_id=account_id or None,
                    )
                    or []
                )
            except TypeError:
                runs = (
                    bl.list_scheduled_job_runs(
                        str(project_id), limit=max(1, min(int(limit_runs or 20), 200))
                    )
                    or []
                )

        normalized_jobs: list[dict[str, Any]] = []
        for row in jobs:
            if not isinstance(row, dict):
                continue
            payload = dict(row)
            job_schedule_timezone = (
                str(payload.get("schedule_timezone") or "").strip() or project_timezone
            )
            payload["next_run_at_utc"] = utc_iso(payload.get("next_run_at"))
            payload["next_run_at_user_local"] = local_text(
                payload.get("next_run_at"), user_timezone
            )
            payload["next_run_at_project_local"] = local_text(
                payload.get("next_run_at"), project_timezone
            )
            payload["next_run_at_schedule_local"] = local_text(
                payload.get("next_run_at"), job_schedule_timezone
            )
            payload["schedule_timezone"] = job_schedule_timezone
            payload["last_run_at_utc"] = utc_iso(payload.get("last_run_at"))
            payload["last_run_at_user_local"] = local_text(
                payload.get("last_run_at"), user_timezone
            )
            payload["last_run_at_project_local"] = local_text(
                payload.get("last_run_at"), project_timezone
            )
            normalized_jobs.append(payload)

        normalized_runs: list[dict[str, Any]] = []
        for row in runs:
            if not isinstance(row, dict):
                continue
            payload = dict(row)
            payload["created_at_utc"] = utc_iso(payload.get("created_at"))
            payload["created_at_user_local"] = local_text(payload.get("created_at"), user_timezone)
            payload["created_at_project_local"] = local_text(
                payload.get("created_at"), project_timezone
            )
            normalized_runs.append(payload)

        return ProjectScheduledJobsView(
            project_id=str(project_id),
            timezone_context={
                "display_timezone": user_timezone,
                "user_timezone": user_timezone,
                "project_schedule_timezone": project_timezone,
            },
            jobs=normalized_jobs,
            runs=normalized_runs,
        )

    def list_notification_routes(
        self,
        project_id: Any,
        *,
        enabled_only: bool = False,
        scope: ScopeContext | None = None,
    ) -> ProjectNotificationRoutesView:
        resolved = self._scope(scope, project_id=project_id)
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(DATABASE_NOT_CONFIGURED_MESSAGE)
        self._require_project_in_scope(bl, project_id, resolved)
        try:
            rows = (
                bl.list_notification_routes(project_id, enabled_only=enabled_only)
                if hasattr(bl, "list_notification_routes")
                else []
            )
        except Exception as exc:
            raise ServiceUnavailableError("Notification route lookup failed") from exc
        return ProjectNotificationRoutesView(
            project_id=str(project_id),
            notification_routes=[dict(row) for row in (rows or []) if isinstance(row, dict)],
        )

    def get_notification_overview(
        self,
        project_id: Any,
        *,
        scope: ScopeContext | None = None,
    ) -> ProjectNotificationOverviewView:
        """The project's automatic notifications, read-only, from the live settings.

        Automation rules come from the linked workflows the caller may read,
        so a restricted token sees only the rules of its allowed workflows.
        """
        from core.application.notifications.overview import build_project_notification_overview

        resolved = self._scope(scope, project_id=project_id)
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(DATABASE_NOT_CONFIGURED_MESSAGE)
        self._require_project_in_scope(bl, project_id, resolved)
        bindings = self.get_workflow_bindings(project_id, scope=resolved)
        workflows = [
            row
            for row in bindings.project_workflows or []
            if isinstance(row, dict) and row.get("enabled") is not False
        ]
        try:
            entries = build_project_notification_overview(
                bl,
                project_id=str(project_id),
                account_id=resolved.account_id,
                workflows=workflows,
            )
        except Exception as exc:
            raise ServiceUnavailableError("Notification overview lookup failed") from exc
        return ProjectNotificationOverviewView(project_id=str(project_id), entries=entries)

    def list_notification_deliveries(
        self,
        project_id: Any,
        *,
        job_id: Any | None = None,
        limit: int = 200,
        scope: ScopeContext | None = None,
    ) -> ProjectNotificationDeliveriesView:
        resolved = self._scope(scope, project_id=project_id)
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(DATABASE_NOT_CONFIGURED_MESSAGE)
        self._require_project_in_scope(bl, project_id, resolved)
        try:
            normalized_job_id = str(job_id) if job_id is not None else None
            if hasattr(bl, "list_notification_delivery_logs"):
                rows = bl.list_notification_delivery_logs(
                    project_id=project_id,
                    job_id=normalized_job_id,
                    limit=max(1, min(int(limit or 200), 1000)),
                )
            else:
                rows = []
        except Exception as exc:
            raise ServiceUnavailableError("Notification delivery lookup failed") from exc
        return ProjectNotificationDeliveriesView(
            project_id=str(project_id),
            job_id=normalized_job_id,
            notification_deliveries=[dict(row) for row in (rows or []) if isinstance(row, dict)],
        )

    def get_workflow_preview(
        self,
        project_id: Any,
        *,
        task_summary: str,
        workflow_id: str = "",
        scope: ScopeContext | None = None,
    ) -> ProjectWorkflowPreviewView:
        resolved = self._scope(scope, project_id=project_id)
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(DATABASE_NOT_CONFIGURED_MESSAGE)

        project = self._require_project_in_scope(bl, str(project_id), resolved)
        normalized_task_summary = self._sanitize_external_input(str(task_summary or "").strip())
        if not normalized_task_summary:
            raise ServiceValidationError("task_summary is required")

        run_cfg = self._project_run_config(bl, str(project_id))
        try:
            selected_workflow_id, selection_source = self._resolve_workflow_selection(
                workflow_id or "",
                "",
                run_cfg.get("project_workflows") if isinstance(run_cfg, dict) else None,
            )
        except ValueError as exc:
            raise ServiceValidationError(str(exc)) from exc

        try:
            if self._validate_explicit_workflow_override is not None:
                self._validate_explicit_workflow_override(
                    selected_workflow_id=selected_workflow_id,
                    selection_source=selection_source,
                    task_summary=normalized_task_summary,
                    project_name=project.get("name", ""),
                    project_config=run_cfg,
                )
        except ValueError as exc:
            raise ServiceValidationError(str(exc)) from exc

        availability: dict[str, Any] = {}
        if selected_workflow_id:
            availability_service = self._workflow_availability()
            if availability_service is not None:
                try:
                    availability = availability_service.get_workflow_availability(
                        selected_workflow_id,
                        account_id=str(project.get("account_id") or resolved.account_id or ""),
                        project_id=str(project_id),
                    )
                except Exception as exc:
                    logger.debug("Project workflow preview availability lookup skipped: %s", exc)

        envelope = self._build_task_envelope_for_preview(normalized_task_summary)
        state = self._normalize_orchestrator_state(
            {
                "task_summary": normalized_task_summary,
                "project_name": project.get("name", ""),
                "project_config": run_cfg,
            }
        )
        if selected_workflow_id:
            state["workflow_id"] = selected_workflow_id
        if selection_source:
            state["workflow_selection_source"] = selection_source

        try:
            preview = self._plan_workflow_preview(envelope, state) or {}
        except RuntimeError as exc:
            message = str(exc or "").strip()
            if message:
                raise ServiceValidationError(message) from exc
            raise
        if not isinstance(preview, dict):
            preview = {}
        resolved_workflow_id = selected_workflow_id
        planned_workflow_id = str(preview.get("workflow_id") or "").strip()
        if planned_workflow_id and planned_workflow_id != selected_workflow_id:
            raise ServiceValidationError(
                "Preview planned a different workflow than the requested workflow_id"
            )
        resolved_selection_source = selection_source or "explicit_request"

        classification = preview.get("classification")
        if not isinstance(classification, dict):
            classification = {}
        routing = preview.get("routing")
        if not isinstance(routing, dict):
            routing = {}

        step_plan = [
            {
                "step_id": step.get("step_id"),
                "step_kind": step.get("step_kind"),
                "display_name": step.get("display_name") or step.get("step_id"),
                "agent": step.get("agent") or None,
                "skill": step.get("skill") or None,
                "group_id": step.get("group_id"),
            }
            for step in preview.get("steps") or []
            if isinstance(step, dict)
        ]

        return ProjectWorkflowPreviewView(
            project_id=str(project_id),
            selected_workflow_id=resolved_workflow_id,
            requested_workflow_id=selected_workflow_id or None,
            selection_source=resolved_selection_source,
            selection_reason=str(routing.get("reason") or ""),
            classification={
                "task_type": str(
                    classification.get("task_type")
                    or envelope.get("task_type")
                    or envelope.get("inputs", {}).get("task_type")
                    or ""
                ).strip(),
                "confidence": classification.get("confidence"),
                "reasoning": classification.get("reasoning"),
            },
            step_plan=step_plan,
            step_count=len(step_plan),
            availability_status=str(availability.get("availability_status") or "available"),
            availability_reason_code=str(availability.get("availability_reason_code") or "none"),
            availability_reason_text=str(availability.get("availability_reason_text") or ""),
            new_runs_blocked=bool(availability.get("new_runs_blocked", False)),
            blocked_dependencies=list(availability.get("blocked_dependencies") or []),
            missing_connections=list(availability.get("missing_connections") or []),
        )

    def get_project_detail(
        self,
        project_id: Any,
        *,
        page: int = 1,
        per_page: int = 25,
        scope: ScopeContext | None = None,
    ) -> ProjectDetailPageView:
        resolved = self._scope(scope, project_id=project_id)
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(DATABASE_NOT_CONFIGURED_MESSAGE)

        project = self._require_project_in_scope(bl, str(project_id), resolved)
        job_store = self._get_job_store()
        if (
            not job_store
            or not callable(getattr(job_store, "count_jobs", None))
            or not callable(getattr(job_store, "get_jobs", None))
        ):
            raise ServiceUnavailableError("Project Job reader is unavailable")
        safe_page = max(1, int(page or 1))
        safe_per_page = max(1, min(int(per_page or 25), 200))
        total_jobs = job_store.count_jobs(str(project["id"]))
        if int(total_jobs or 0) <= 0:
            jobs = []
            total_pages = 1
            safe_page = 1
        else:
            total_pages = max(1, (int(total_jobs) + safe_per_page - 1) // safe_per_page)
            safe_page = min(safe_page, total_pages)
            offset = (safe_page - 1) * safe_per_page
            jobs = job_store.get_jobs(str(project["id"]), limit=safe_per_page, offset=offset)

        account_id = str(project.get("account_id") or resolved.account_id or "")
        all_workflows = [
            dict(item)
            for item in (
                self._list_workflow_choices(
                    enabled_only=True,
                    account_id=account_id,
                )
                or []
            )
            if isinstance(item, dict)
        ]
        project_workflows = (
            bl.get_project_workflows(str(project_id), enabled_only=True)
            if hasattr(bl, "get_project_workflows")
            else []
        )
        all_workflows = self._annotate_workflow_rows(
            all_workflows,
            account_id=account_id,
            project_id=str(project_id),
        )
        project_workflows = self._annotate_workflow_rows(
            [dict(item) for item in project_workflows if isinstance(item, dict)],
            account_id=account_id,
            project_id=str(project_id),
        )
        linked_workflow_ids = {
            str(item.get("workflow_id") or "").strip()
            for item in project_workflows
            if isinstance(item, dict) and str(item.get("workflow_id") or "").strip()
        }
        workflows = [
            dict(wf)
            for wf in (all_workflows or [])
            if isinstance(wf, dict)
            and (
                not linked_workflow_ids
                or str(wf.get("workflow_id") or "").strip() in linked_workflow_ids
            )
            and self._is_workflow_visible_in_catalog(wf)
        ]
        operator_default_workflow_id = next(
            (
                str(item.get("workflow_id") or "").strip()
                for item in project_workflows
                if isinstance(item, dict) and bool(item.get("is_default"))
            ),
            "",
        )

        project_payload = self._enrich_project_surface_payload(bl, project=dict(project))
        project_payload["operator_default_workflow_id"] = operator_default_workflow_id
        project_payload["project_workflows"] = [
            dict(item) for item in project_workflows if isinstance(item, dict)
        ]
        default_workflow = next(
            (
                dict(wf)
                for wf in workflows
                if str(wf.get("workflow_id") or "").strip() == operator_default_workflow_id
            ),
            None,
        )
        run_cfg = self._project_run_config(bl, str(project_id))
        project_policy = run_cfg.get("project_policy") if isinstance(run_cfg, dict) else {}
        scheduled_payload = self.list_scheduled_jobs(str(project_id), limit_runs=20, scope=scope)

        return ProjectDetailPageView(
            project=project_payload,
            jobs=[dict(job) for job in jobs if isinstance(job, dict)],
            workflows=workflows,
            default_workflow=default_workflow,
            project_policy=dict(project_policy or {}) if isinstance(project_policy, dict) else {},
            scheduled_jobs=list(scheduled_payload.jobs or []),
            scheduled_runs=list(scheduled_payload.runs or []),
            page=safe_page,
            per_page=safe_per_page,
            total_jobs=int(total_jobs or 0),
            total_pages=total_pages,
        )


__all__ = [
    "ProjectQueryServiceOpsMixin",
]
