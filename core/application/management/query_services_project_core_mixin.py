# ruff: noqa: F403, F405
from __future__ import annotations

from core.application.workflows.workflow_hook_contract import is_hook_storage

from .query_services_common import *  # NOSONAR

_HAS_JOBS_TRUE = frozenset({"yes", "true", "1", "with"})
_HAS_JOBS_FALSE = frozenset({"no", "false", "0", "without"})
_DESCENDING = frozenset({"desc", "descending"})


def _project_row_matches_filters(project: dict[str, Any], *, has_jobs: str) -> bool:
    """In-memory mirror of the SQL filters, for the paths that cannot use them."""
    if has_jobs:
        job_count = int(project.get("job_count") or 0)
        if has_jobs in _HAS_JOBS_TRUE and job_count <= 0:
            return False
        if has_jobs in _HAS_JOBS_FALSE and job_count > 0:
            return False
    return True


def _sort_project_dicts(projects: list[dict[str, Any]], *, sort: str, direction: str) -> None:
    """Order raw project rows the way the SQL path orders them, in place."""
    descending = direction in _DESCENDING
    projects.sort(key=lambda project: str(project.get("name") or "").lower())
    if sort == "name":
        if descending:
            projects.reverse()
    elif sort == "job_count":
        # Stable sort keeps the name order as the tiebreaker, matching
        # `ORDER BY job_count <dir>, name ASC`.
        projects.sort(key=lambda project: int(project.get("job_count") or 0), reverse=descending)


class ProjectQueryServiceCoreMixin:
    def __init__(
        self,
        *,
        get_logger: Callable[[], Any],
        serialize_projects_grid_row: Callable[[dict[str, Any]], dict[str, Any]],
        get_queue_depth: Callable[[int], int],
        get_ready_queue_depth: Callable[[], int],
        get_job_store: Callable[[], Any] | None = None,
        to_json_obj: Callable[[Any], Any] | None = None,
        project_settings_diff: Callable[
            [dict[str, Any] | None, dict[str, Any] | None], dict[str, Any]
        ]
        | None = None,
        list_workflow_choices: Callable[..., list[dict[str, Any]]] | None = None,
        sanitize_external_input: Callable[[str], str] | None = None,
        resolve_workflow_selection: Callable[..., tuple[str, str]] | None = None,
        validate_explicit_workflow_override: Callable[..., Any] | None = None,
        build_task_envelope_for_preview: Callable[[str], dict[str, Any]] | None = None,
        normalize_orchestrator_state: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        plan_workflow_preview: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
        | None = None,
        workflow_availability_service: Any | None = None,
    ):
        self._get_logger = get_logger
        self._get_job_store = get_job_store or get_logger
        self._serialize_projects_grid_row = serialize_projects_grid_row
        self._get_queue_depth = get_queue_depth
        self._get_ready_queue_depth = get_ready_queue_depth
        self._to_json_obj = to_json_obj or (lambda value: value)
        self._project_settings_diff = project_settings_diff or (
            lambda from_snapshot, to_snapshot: {
                "changes": [],
                "summary": {"total": 0, "added": 0, "removed": 0, "changed": 0},
            }
        )
        self._list_workflow_choices = list_workflow_choices or (lambda **kwargs: [])
        self._sanitize_external_input = sanitize_external_input or (lambda value: str(value or ""))
        self._resolve_workflow_selection = resolve_workflow_selection or (
            lambda requested, _fallback, _choices: (
                str(requested or "").strip(),
                "explicit_request",
            )
        )
        self._validate_explicit_workflow_override = validate_explicit_workflow_override
        self._build_task_envelope_for_preview = build_task_envelope_for_preview or (
            lambda task_summary: {
                "title": str(task_summary or "").strip(),
                "description": str(task_summary or "").strip(),
            }
        )
        self._normalize_orchestrator_state = normalize_orchestrator_state or (
            lambda state: dict(state or {})
        )
        self._plan_workflow_preview = plan_workflow_preview or (lambda envelope, state: {})
        self._workflow_availability_service = workflow_availability_service

    @staticmethod
    def _require_project(bl: Any, project_id: Any) -> dict[str, Any]:
        project = bl.get_project(project_id) if hasattr(bl, "get_project") else None
        if not project:
            raise ServiceNotFoundError(PROJECT_NOT_FOUND_MESSAGE)
        return dict(project) if isinstance(project, dict) else project

    @staticmethod
    def _require_project_in_scope(bl: Any, project_id: Any, scope: ScopeContext) -> dict[str, Any]:
        project = bl.get_project(project_id) if hasattr(bl, "get_project") else None
        if not project:
            raise ServiceNotFoundError(PROJECT_NOT_FOUND_MESSAGE)
        project = dict(project) if isinstance(project, dict) else project
        require_project_access(scope, project)
        return project

    @staticmethod
    def _project_run_config(bl: Any, project_id: Any) -> dict[str, Any]:
        if not hasattr(bl, "get_project_run_config"):
            return {}
        run_cfg = bl.get_project_run_config(str(project_id)) or {}
        return dict(run_cfg) if isinstance(run_cfg, dict) else {}

    @staticmethod
    def _scope(
        scope: ScopeContext | None,
        *,
        project_id: Any = None,
        workflow_id: Any = None,
    ) -> ScopeContext:
        return _service_scope(scope, project_id=project_id, workflow_id=workflow_id)

    def _workflow_availability(self):
        if self._workflow_availability_service is not None:
            return self._workflow_availability_service
        try:
            from core.application.workflows.workflow_availability_service import (
                WorkflowAvailabilityService,
            )

            self._workflow_availability_service = WorkflowAvailabilityService(
                get_logger=self._get_logger
            )
            return self._workflow_availability_service
        except Exception as exc:
            logger.debug("ProjectQueryService workflow availability unavailable: %s", exc)
            return None

    @staticmethod
    def _project_data_connection_rows(bl: Any, project_id: Any) -> list[dict[str, Any]]:
        if not hasattr(bl, "list_project_data_connections"):
            return []
        rows = bl.list_project_data_connections(str(project_id)) or []
        return [dict(row) for row in rows if isinstance(row, dict)]

    @staticmethod
    def _workspace_data_connection_rows(bl: Any, workspace_id: str | None) -> list[dict[str, Any]]:
        normalized_workspace_id = str(workspace_id or "").strip()
        if not normalized_workspace_id or not hasattr(
            bl, "list_workspace_project_data_connection_options"
        ):
            return []
        rows = bl.list_workspace_project_data_connection_options(normalized_workspace_id) or []
        summaries: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            summary = dict(row)
            # Schema bodies are exposed through connection introspection, not
            # repeated once per option on the project edit/summary endpoint.
            summary.pop("schema_snapshot", None)
            summaries.append(summary)
        return summaries

    @classmethod
    def _enrich_project_surface_payload(
        cls,
        bl: Any,
        *,
        project: dict[str, Any],
    ) -> dict[str, Any]:
        payload = dict(project or {})
        raw_agents_config = payload.get("agents_config")
        agents_config = dict(raw_agents_config) if isinstance(raw_agents_config, dict) else {}
        assigned_connections = cls._project_data_connection_rows(bl, payload.get("id"))
        available_connections = cls._workspace_data_connection_rows(bl, payload.get("workspace_id"))
        assigned_connection_ids = [
            str(row.get("connection_id") or "").strip()
            for row in assigned_connections
            if str(row.get("connection_id") or "").strip()
        ]
        payload.pop("db_dsn", None)
        payload.pop("db_schema", None)
        payload.pop("db_type", None)
        payload["assigned_data_connections"] = assigned_connections
        payload["available_data_connections"] = available_connections
        payload["data_connection_ids"] = assigned_connection_ids
        payload["default_provider"] = str(agents_config.get("default_provider") or "").strip()
        payload["default_model"] = str(agents_config.get("default_model") or "").strip()
        payload["agent_model_overrides"] = (
            dict((agents_config.get("agents") or {}).get("config") or {})
            if isinstance(agents_config.get("agents"), dict)
            else {}
        )
        return payload

    def _annotate_workflow_rows(
        self,
        rows: list[dict[str, Any]],
        *,
        account_id: str,
        project_id: Any | None = None,
    ) -> list[dict[str, Any]]:
        normalized_rows = [dict(row) for row in rows if isinstance(row, dict)]
        if not normalized_rows:
            return []
        service = self._workflow_availability()
        if service is None:
            return normalized_rows
        try:
            return service.annotate_workflow_rows(
                normalized_rows,
                account_id=str(account_id or "").strip(),
                project_id=project_id,
            )
        except Exception as exc:
            logger.debug("ProjectQueryService workflow annotation skipped: %s", exc)
            return normalized_rows

    @staticmethod
    def _is_workflow_visible_in_catalog(row: dict[str, Any]) -> bool:
        if not isinstance(row, dict):
            return False
        # A hook's storage workflow is a place inside another workflow (R2-11).
        if is_hook_storage(row):
            return False
        if "visible_in_catalog" in row:
            return bool(row.get("visible_in_catalog"))
        return str(row.get("availability_status") or "available").strip().lower() != "removed"

    @staticmethod
    def _scope_allows_project_id(scope: ScopeContext, project_id: Any) -> bool:
        if not scope.allowed_project_ids:
            return True
        normalized_project_id = coerce_optional_project_id(project_id)
        return (
            normalized_project_id is not None and normalized_project_id in scope.allowed_project_ids
        )

    @classmethod
    def _filter_projects_in_scope(
        cls,
        projects: list[Any],
        scope: ScopeContext,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for project in projects or []:
            if not isinstance(project, dict):
                continue
            project_row = dict(project)
            if (
                scope.workspace_id
                and str(project_row.get("workspace_id") or "") != scope.workspace_id
            ):
                continue
            if not cls._scope_allows_project_id(scope, project_row.get("id")):
                continue
            rows.append(project_row)
        return rows

    def list_projects(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        search: str = "",
        has_jobs: str = "",
        sort: str = "",
        direction: str = "",
        scope: ScopeContext | None = None,
    ) -> ProjectsListView:
        resolved = self._scope(scope)
        acct = resolved.account_id
        bl = self._get_logger()
        if not bl:
            return ProjectsListView()

        normalized_page = max(1, int(page or 1))
        normalized_per_page = max(1, min(int(per_page or 25), 200))
        normalized_search = str(search or "").strip()
        normalized_has_jobs = str(has_jobs or "").strip().lower()
        normalized_sort = str(sort or "").strip().lower()
        normalized_direction = str(direction or "").strip().lower()
        offset = (normalized_page - 1) * normalized_per_page

        try:
            search_value = normalized_search or None
            if resolved.allowed_project_ids:
                if hasattr(bl, "get_projects"):
                    projects = bl.get_projects(account_id=acct)
                elif hasattr(bl, "list_projects"):
                    # Fallback for runtimes without get_projects(): fetch a broad slice
                    # then apply scope filtering in-memory.
                    projects = bl.list_projects(
                        limit=10000,
                        offset=0,
                        search=search_value,
                        account_id=acct,
                    )
                else:
                    projects = []
                projects = self._filter_projects_in_scope(projects, resolved)
                if normalized_search:
                    lowered_search = normalized_search.lower()
                    projects = [
                        project
                        for project in projects
                        if lowered_search in str(project.get("name") or "").lower()
                    ]
                projects = [
                    project
                    for project in projects
                    if _project_row_matches_filters(project, has_jobs=normalized_has_jobs)
                ]
                _sort_project_dicts(projects, sort=normalized_sort, direction=normalized_direction)
                total = len(projects)
                projects = projects[offset : offset + normalized_per_page]
            elif hasattr(bl, "list_projects"):
                # Three graded attempts rather than one flat fallback: an
                # older signature must not cost the workspace scope as well.
                try:
                    projects = bl.list_projects(
                        limit=normalized_per_page,
                        offset=offset,
                        search=search_value,
                        account_id=acct,
                        workspace_id=resolved.workspace_id,
                        has_jobs=normalized_has_jobs,
                        sort=normalized_sort,
                        direction=normalized_direction,
                    )
                except TypeError:
                    try:
                        projects = bl.list_projects(
                            limit=normalized_per_page,
                            offset=offset,
                            search=search_value,
                            account_id=acct,
                            workspace_id=resolved.workspace_id,
                        )
                    except TypeError:
                        projects = bl.list_projects(
                            limit=normalized_per_page,
                            offset=offset,
                            search=search_value,
                            account_id=acct,
                        )
                if hasattr(bl, "count_projects"):
                    try:
                        total = bl.count_projects(
                            search=search_value,
                            account_id=acct,
                            workspace_id=resolved.workspace_id,
                            has_jobs=normalized_has_jobs,
                        )
                    except TypeError:
                        try:
                            total = bl.count_projects(
                                search=search_value,
                                account_id=acct,
                                workspace_id=resolved.workspace_id,
                            )
                        except TypeError:
                            total = bl.count_projects(
                                search=search_value,
                                account_id=acct,
                            )
                else:
                    total = len(projects)
            else:
                projects = bl.get_projects(account_id=acct) if hasattr(bl, "get_projects") else []
                if normalized_search:
                    lowered_search = normalized_search.lower()
                    projects = [
                        project
                        for project in projects
                        if lowered_search in str(project.get("name") or "").lower()
                    ]
                projects = [
                    project
                    for project in projects
                    if _project_row_matches_filters(project, has_jobs=normalized_has_jobs)
                ]
                _sort_project_dicts(projects, sort=normalized_sort, direction=normalized_direction)
                total = len(projects)
                projects = projects[offset : offset + normalized_per_page]
            page_rows = []
            for project in projects:
                row = self._serialize_projects_grid_row(project)
                if resolved.allowed_workflow_ids:
                    # As in get_workflow_bindings: a token limited to some workflows
                    # does not learn which other workflows a project binds.
                    row["workflow_ids"] = [
                        workflow_id
                        for workflow_id in row.get("workflow_ids") or []
                        if workflow_id in resolved.allowed_workflow_ids
                    ]
                    if row.get("operator_default_workflow_id") not in resolved.allowed_workflow_ids:
                        row["operator_default_workflow_id"] = ""
                page_rows.append(ProjectGridRowView.model_validate(row))
        except Exception:
            page_rows = []
            total = 0

        return ProjectsListView(
            projects=page_rows,
            total=int(total),
            page=normalized_page,
            per_page=normalized_per_page,
            total_pages=max(1, -(-int(total) // normalized_per_page)),
        )

    def get_project_queue(
        self, project_id: Any, *, scope: ScopeContext | None = None
    ) -> ProjectQueueView:
        resolved = self._scope(scope, project_id=project_id)
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(DATABASE_NOT_CONFIGURED_MESSAGE)
        self._require_project_in_scope(bl, str(project_id), resolved)

        pending_jobs = bl.get_pending_jobs(project_id)
        queue_depth = -1
        ready_queue_depth = -1
        try:
            queue_depth = self._get_queue_depth(project_id)
            ready_queue_depth = self._get_ready_queue_depth()
        except Exception:
            pass
        broker_available = queue_depth >= 0 and ready_queue_depth >= 0
        queue_transport_state = "broker_available" if broker_available else "broker_unavailable"
        queue_transport_label = "Broker available" if broker_available else "Broker unavailable"
        queue_transport_message = (
            "RabbitMQ queue metrics are reachable."
            if broker_available
            else "RabbitMQ queue metrics are unavailable right now."
        )
        queue_health_summary = QueueHealthView.model_validate(
            build_queue_delivery_summary(load_queue_delivery_counts(bl, project_id=str(project_id)))
        )

        project_lease = None
        active_project_leases: list[dict[str, Any]] = []
        concurrency_cap = 1
        if hasattr(bl, "get_project_policy"):
            try:
                policy = bl.get_project_policy(str(project_id))
                if isinstance(policy, dict):
                    concurrency_cap = max(1, int(policy.get("concurrency_cap") or 1))
            except Exception:
                concurrency_cap = 1
        if hasattr(bl, "list_project_execution_leases"):
            try:
                lease_rows = bl.list_project_execution_leases(
                    active_only=True, project_id=project_id
                )
                if isinstance(lease_rows, list):
                    active_project_leases = [
                        dict(row) for row in lease_rows if isinstance(row, dict)
                    ]
            except Exception:
                active_project_leases = []
        if hasattr(bl, "get_project_execution_lease"):
            try:
                project_lease = (
                    active_project_leases[0]
                    if active_project_leases
                    else bl.get_project_execution_lease(project_id, active_only=True)
                )
            except Exception:
                project_lease = None
        elif active_project_leases:
            project_lease = active_project_leases[0]

        now = datetime.now(UTC)
        lease_heartbeat_age_sec = None
        if isinstance(project_lease, dict) and project_lease.get("heartbeat_at"):
            heartbeat = project_lease.get("heartbeat_at")
            if isinstance(heartbeat, datetime):
                try:
                    lease_heartbeat_age_sec = max(0, int((now - heartbeat).total_seconds()))
                except Exception:
                    lease_heartbeat_age_sec = None

        lease_payload = None
        if isinstance(project_lease, dict):
            heartbeat_at = project_lease.get("heartbeat_at")
            expires_at = project_lease.get("expires_at")
            acquired_at = project_lease.get("acquired_at")
            lease_payload = ProjectQueueLeaseView(
                project_id=str(project_lease.get("project_id") or project_id),
                worker_instance_id=str(project_lease.get("worker_instance_id") or ""),
                lease_token=str(project_lease.get("lease_token") or ""),
                heartbeat_at=heartbeat_at.isoformat()
                if isinstance(heartbeat_at, datetime)
                else None,
                expires_at=expires_at.isoformat() if isinstance(expires_at, datetime) else None,
                acquired_at=acquired_at.isoformat() if isinstance(acquired_at, datetime) else None,
                active_job_id=str(project_lease.get("active_job_id") or "") or None,
                drain_status=str(project_lease.get("drain_status") or ""),
                heartbeat_age_sec=lease_heartbeat_age_sec,
            )

        if active_project_leases:
            active_lease_count = len(active_project_leases)
        elif isinstance(project_lease, dict):
            active_lease_count = 1
        else:
            active_lease_count = 0
        is_ready = bool(pending_jobs) and active_lease_count < concurrency_cap
        pending_payload = []
        for job in pending_jobs:
            if not isinstance(job, dict):
                continue
            queued_at = job.get("queued_at")
            queue_delivery = build_queue_delivery_view(job, to_json_obj=self._to_json_obj)
            pending_payload.append(
                ProjectQueuePendingJobView(
                    job_id=str(job["id"]),
                    task_id=str(job.get("task_id") or ""),
                    task_summary=str(job.get("task_summary") or ""),
                    task_source=str(job.get("task_source") or ""),
                    queued_at=queued_at.isoformat() if isinstance(queued_at, datetime) else None,
                    queue_delivery_state=str(queue_delivery.get("state") or ""),
                    queue_delivery_state_label=str(queue_delivery.get("state_label") or ""),
                    queue_delivery_state_tone=str(queue_delivery.get("state_tone") or "gray"),
                    queue_health_state=str(queue_delivery.get("health_state") or ""),
                    queue_health_label=str(queue_delivery.get("health_label") or ""),
                    queue_health_tone=str(queue_delivery.get("health_tone") or "gray"),
                )
            )

        if not pending_jobs:
            ready_reason = "idle"
        elif active_lease_count >= concurrency_cap and active_lease_count > 0:
            ready_reason = "leased"
        elif active_lease_count > 0:
            ready_reason = "capacity_available"
        else:
            ready_reason = "backlog"

        return ProjectQueueView(
            project_id=str(project_id),
            pending_count=len(pending_jobs),
            project_backlog=len(pending_jobs),
            queue_depth=queue_depth,
            ready_queue_depth=ready_queue_depth,
            queue_name=f"platform.project.{project_id}",
            dead_letter_queue_name=queue_dead_letter_queue_name(f"platform.project.{project_id}"),
            broker_available=broker_available,
            queue_transport_state=queue_transport_state,
            queue_transport_label=queue_transport_label,
            queue_transport_message=queue_transport_message,
            lease_holder=lease_payload.worker_instance_id if lease_payload else None,
            project_lease=lease_payload,
            ready_state=ProjectReadyStateView(
                is_ready=is_ready,
                reason=ready_reason,
            ),
            queue_health=queue_health_summary,
            pending_jobs=pending_payload,
        )

    def get_runtime_config(
        self, project_id: Any, *, scope: ScopeContext | None = None
    ) -> ProjectRuntimeConfigView:
        resolved = self._scope(scope, project_id=project_id)
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(DATABASE_NOT_CONFIGURED_MESSAGE)
        project = self._require_project_in_scope(bl, str(project_id), resolved)
        return ProjectRuntimeConfigView(
            project_id=str(project_id),
            project_name=str(project.get("name") or "").strip(),
            run_config=self._project_run_config(bl, str(project_id)),
        )

    def get_workflow_bindings(
        self,
        project_id: Any,
        *,
        scope: ScopeContext | None = None,
    ) -> ProjectWorkflowBindingsView:
        resolved = self._scope(scope, project_id=project_id)
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(DATABASE_NOT_CONFIGURED_MESSAGE)
        self._require_project_in_scope(bl, str(project_id), resolved)

        if not is_default_workflow_catalog_account(resolved):
            return ProjectWorkflowBindingsView(
                project_id=str(project_id),
                workflow_ids=[],
                operator_default_workflow_id="",
                project_workflows=[],
            )

        # enabled_only=False so a disabled-but-present binding still shows;
        # include_deleted=False so one the operator removed does not.
        project_workflows = (
            bl.get_project_workflows(str(project_id), enabled_only=False, include_deleted=False)
            if hasattr(bl, "get_project_workflows")
            else []
        ) or []
        normalized_workflows = self._annotate_workflow_rows(
            [dict(item) for item in project_workflows if isinstance(item, dict)],
            account_id=str(resolved.account_id or ""),
            project_id=str(project_id),
        )
        normalized_workflows = [
            dict(item)
            for item in normalized_workflows
            if isinstance(item, dict) and self._is_workflow_visible_in_catalog(item)
        ]
        if resolved.allowed_workflow_ids:
            normalized_workflows = [
                item
                for item in normalized_workflows
                if str(item.get("workflow_id") or "").strip() in resolved.allowed_workflow_ids
            ]
        selected_workflows = [
            item for item in normalized_workflows if item.get("enabled") is not False
        ]
        operator_default_workflow_id = next(
            (
                str(item.get("workflow_id") or "").strip()
                for item in selected_workflows
                if bool(item.get("is_default"))
            ),
            "",
        )
        workflow_ids = [
            str(item.get("workflow_id") or "").strip()
            for item in selected_workflows
            if str(item.get("workflow_id") or "").strip()
        ]
        return ProjectWorkflowBindingsView(
            project_id=str(project_id),
            workflow_ids=workflow_ids,
            operator_default_workflow_id=operator_default_workflow_id,
            project_workflows=normalized_workflows,
        )

    def get_policy(
        self,
        project_id: Any,
        *,
        scope: ScopeContext | None = None,
    ) -> ProjectPolicyView:
        resolved = self._scope(scope, project_id=project_id)
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(DATABASE_NOT_CONFIGURED_MESSAGE)
        self._require_project_in_scope(bl, str(project_id), resolved)

        policy = bl.get_project_policy(str(project_id)) if hasattr(bl, "get_project_policy") else {}
        return ProjectPolicyView(
            project_id=str(project_id),
            policy=dict(policy or {}) if isinstance(policy, dict) else {},
        )

    def get_project_edit_form(
        self,
        project_id: Any,
        *,
        scope: ScopeContext | None = None,
    ) -> ProjectEditPageView:
        resolved = self._scope(scope, project_id=project_id)
        bl = self._get_logger()
        if not bl:
            raise ServiceUnavailableError(DATABASE_NOT_CONFIGURED_MESSAGE)
        project = self._require_project_in_scope(bl, str(project_id), resolved)

        workflows = [
            dict(item)
            for item in self._list_workflow_choices(
                enabled_only=False,
                account_id=str(project.get("account_id") or resolved.account_id or ""),
            )
            if isinstance(item, dict)
        ]
        workflows = self._annotate_workflow_rows(
            workflows,
            account_id=str(project.get("account_id") or resolved.account_id or ""),
            project_id=str(project_id),
        )
        workflows = [
            dict(item)
            for item in workflows
            if isinstance(item, dict) and self._is_workflow_visible_in_catalog(item)
        ]
        workflow_bindings = self.get_workflow_bindings(str(project_id), scope=scope)
        policy = self.get_policy(str(project_id), scope=scope)

        project_settings_revisions: list[dict[str, Any]] = []
        if hasattr(bl, "list_project_settings_revisions"):
            try:
                project_settings_revisions = [
                    dict(row)
                    for row in (bl.list_project_settings_revisions(str(project_id), limit=12) or [])
                    if isinstance(row, dict)
                ]
            except Exception:
                project_settings_revisions = []

        project_payload = self._enrich_project_surface_payload(bl, project=dict(project))
        project_payload["operator_default_workflow_id"] = (
            workflow_bindings.operator_default_workflow_id
        )

        selected_workflow_ids = list(workflow_bindings.workflow_ids)

        return ProjectEditPageView(
            project=project_payload,
            workflows=workflows,
            project_workflows=list(workflow_bindings.project_workflows),
            project_workflow_ids=selected_workflow_ids,
            project_policy=dict(policy.policy),
            project_settings_revisions=project_settings_revisions,
        )


__all__ = [
    "ProjectQueryServiceCoreMixin",
]
