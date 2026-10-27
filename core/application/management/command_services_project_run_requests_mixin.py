"""MCP launch and schedule commands using the shared task admission service."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from core.application.jobs.workflow_definition_snapshot_repository import (
    WorkflowRevisionChangedError,
)
from core.application.management.command_models import ProjectTaskFileInput
from core.application.management.command_models_api_inputs import BuilderWorkflowRunInput
from core.application.management.command_services_common import (
    ERR_SCHEDULED_JOBS_SCHEMA_UNAVAILABLE,
    SCHEDULED_JOB_NOT_FOUND_MESSAGE,
    ProjectScheduledJobCreateInput,
    ProjectScheduledJobDeleteResultView,
    ProjectScheduledJobMutationResultView,
    ProjectScheduledJobRunResultView,
    ProjectScheduledJobToggleInput,
    ProjectScheduledJobUpdateInput,
    ProjectTaskEnqueueInput,
    ProjectTaskEnqueueResultView,
    ScopeContext,
    ServiceError,
    ServiceNotFoundError,
    ServiceUnavailableError,
    ServiceValidationError,
    WorkflowRunInput,
    require_workflow_access,
)
from core.application.management.guards import (
    require_account_access,
    require_default_workflow_catalog_account,
)
from core.application.workflows.workflow_start_runtime import (
    workflow_run_summary,
    workflow_run_summary_metadata,
)
from core.application.workflows.workflow_trigger_guards import workflow_trigger_transaction

_CRON_UNAVAILABLE = "Cron scheduling is unavailable"
_SCHEDULE_ACCOUNT_REQUIRED = "Project account context is required for scheduled jobs."


class ProjectCommandServiceRunRequestsMixin:
    # Supplied by the composed ProjectCommandService.
    _assert_can_start_workflow: Callable[..., None]
    _cron_next_run_at: Callable[..., datetime] | None
    _form_flag: Callable[[Any], bool]
    _logger: Callable[[], Any]
    _normalize_cron_expr: Callable[[str], str]
    _project_run_config: Callable[..., Any]
    _require_project_in_scope: Callable[..., Any]
    _resolve_selected_workflow: Callable[..., Any]
    _run_scheduled_job_once: Callable[..., Any] | None
    _sanitize_external_input: Callable[[str], str]
    _scheduled_job: Callable[..., Any]
    _scope: Callable[..., ScopeContext]
    _validate_explicit_workflow_override: Callable[..., Any] | None
    enqueue_manual_task: Callable[..., Any]

    def start_builder_workflow_run(
        self,
        workflow_id: str,
        command: BuilderWorkflowRunInput | dict[str, Any],
        *,
        idempotency_key: str = "",
        scope: ScopeContext | None = None,
    ) -> ProjectTaskEnqueueResultView:
        """Admit an acknowledged UI run with actual uploads and a transactional revision pin."""
        command = BuilderWorkflowRunInput.model_validate(command)
        workflow_id = str(workflow_id or "").strip()
        resolved = self._scope(scope, project_id=command.project_id, workflow_id=workflow_id)
        require_default_workflow_catalog_account(resolved)
        bl = self._logger()
        project = self._require_project_in_scope(bl, command.project_id, resolved)
        require_workflow_access(resolved, workflow_id)
        config = self._project_run_config(bl, command.project_id, project=project)
        if not any(
            isinstance(row, dict)
            and row.get("workflow_id") == workflow_id
            and row.get("enabled", True)
            and not row.get("deleted_at")
            for row in config.get("project_workflows") or []
        ):
            raise ServiceValidationError("Select an enabled project workflow link before running")
        from core.application.registry.registry_manager import RegistryManager

        registry = RegistryManager.instance()
        from core.application.registry.workflow_lookup import get_workflow_row

        workflow = get_workflow_row(registry.db_store, workflow_id) if registry.db_store else None
        if not isinstance(workflow, dict):
            raise ServiceNotFoundError("Workflow not found")
        # The editor also reads deployment-global system definitions with no
        # account owner; ordinary launch does not grant mutation authority.
        if workflow.get("account_id") or workflow.get("scope_kind") != "system":
            require_account_access(resolved, workflow)
        if workflow.get("project_id"):
            self._require_project_in_scope(bl, workflow["project_id"], resolved)
        if not workflow.get("enabled") or workflow.get("deleted_at"):
            raise ServiceValidationError("Workflow is disabled or unavailable")
        from core.application.registry.workflow_contract_service import WorkflowInterfaceService

        interface = WorkflowInterfaceService().interface_from_definition(workflow)
        if not interface.enabled or "ui" not in interface.trigger_channels:
            raise ServiceValidationError("Enable the UI trigger channel before running")
        revision = int(workflow.get("revision") or 0)
        if revision != command.expected_revision:
            raise WorkflowRevisionChangedError(workflow_id, command.expected_revision, revision)
        correlation = (
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                json.dumps(
                    [
                        "builder",
                        resolved.account_id,
                        resolved.actor_id,
                        command.project_id,
                        workflow_id,
                        idempotency_key,
                    ]
                ),
            ).hex
            if idempotency_key
            else uuid.uuid4().hex
        )
        return self.enqueue_manual_task(
            ProjectTaskEnqueueInput(
                project_id=command.project_id,
                workflow_id=workflow_id,
                task_id=f"workflow-run-{correlation}",
                task_summary=command.summary,
                job_meta={"expected_workflow_revision": command.expected_revision},
                task_source="manual_ui",
                entry_path="manual_ui",
                test_mode=False,
                inputs=command.inputs,
                files=[
                    ProjectTaskFileInput(
                        input_name=row.input_name,
                        filename=row.filename,
                        mime_type=row.mime_type,
                        content=row.decoded_content(),
                    )
                    for row in command.files
                ],
                idempotency_key=idempotency_key,
            ),
            allow_implicit_project=False,
            trusted_internal=True,
            scope=resolved,
        )

    def start_mcp_workflow_run(
        self,
        command: WorkflowRunInput | dict[str, Any],
        *,
        idempotency_key: str = "",
        scope: ScopeContext | None = None,
    ) -> ProjectTaskEnqueueResultView:
        command = WorkflowRunInput.model_validate(command)
        resolved = self._scope(
            scope, project_id=command.project_id, workflow_id=command.workflow_id
        )
        # The queue correlation must remain stable on an idempotent retry; a
        # random task id would change the existing admission fingerprint.
        correlation = (
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                json.dumps(
                    [
                        resolved.account_id,
                        resolved.actor_id,
                        command.project_id,
                        command.workflow_id,
                        idempotency_key,
                    ]
                ),
            ).hex
            if idempotency_key
            else uuid.uuid4().hex
        )
        return self.enqueue_manual_task(
            ProjectTaskEnqueueInput(
                project_id=command.project_id,
                workflow_id=command.workflow_id,
                task_id=f"workflow-run-{correlation}",
                task_summary=workflow_run_summary(command.workflow_id),
                job_meta=workflow_run_summary_metadata(command.workflow_id),
                task_source="mcp_workflow",
                entry_path="mcp",
                inputs=command.inputs,
                files=[
                    ProjectTaskFileInput(
                        input_name=row.input_name,
                        filename=row.filename,
                        mime_type=row.mime_type,
                        content=row.decoded_content(),
                    )
                    for row in command.files
                ],
                client_context=command.client_context,
                test_mode=command.test_mode,
                idempotency_key=idempotency_key,
            ),
            allow_implicit_project=False,
            trusted_internal=True,
            scope=resolved,
        )

    def _scheduled_job_admission(
        self,
        *,
        workflow_id: str,
        project: dict,
        project_config: dict,
        inputs: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        validator = self._validate_explicit_workflow_override
        if not callable(validator):
            raise ServiceUnavailableError("Workflow admission validation is unavailable")
        try:
            validator(
                selected_workflow_id=workflow_id,
                selection_source="explicit_request",
                task_summary=workflow_run_summary(workflow_id),
                project_name=str(project.get("name") or "").strip(),
                project_config=project_config,
            )
        except ServiceError:
            raise
        except Exception as exc:
            raise ServiceValidationError(f"Workflow admission failed: {exc}") from exc
        try:
            from core.application.registry.registry_manager import RegistryManager

            registry = RegistryManager.instance()
            workflow = registry.db_store.get_workflow(workflow_id) if registry.db_store else None
            revision = int((workflow or {}).get("revision") or 0)
            from core.application.registry.workflow_contract_service import WorkflowInterfaceService

            interface_service = WorkflowInterfaceService()
            workflow_interface = interface_service.interface_for_workflow_id(workflow_id)
            validation = interface_service.validate_inputs(
                workflow_interface,
                dict(inputs),
                channel="schedule",
            )
        except Exception as exc:
            message = "Workflow contract service is unavailable; retry later"
            raise ServiceUnavailableError(
                message,
                payload={"error": message, "code": "workflow_contract_unavailable"},
            ) from exc
        if revision < 1:
            raise ServiceUnavailableError("Workflow revision is unavailable")
        if not validation.ok:
            messages = [
                str(item.get("message") or item.get("code") or "").strip()
                for item in validation.errors[:5]
                if isinstance(item, dict)
            ]
            detail = "; ".join(item for item in messages if item)
            raise ServiceValidationError(
                f"Workflow input validation failed{': ' + detail if detail else ''}"
            )
        # A schedule stores caller intent, never the materialized launch payload.
        # Configured defaults (including secret references) are resolved again at
        # execution so pause/enable and later launches cannot be mistaken for a
        # caller override or retain stale secret authority.
        return (
            {
                "admission_allowed": True,
                "admission_reason": "validated",
                "admission_workflow_revision": revision,
                "admission_checked_at": datetime.now(UTC),
            },
            dict(inputs),
        )

    def _prepare_scheduled_job_edit(
        self,
        project_id: Any,
        command: ProjectScheduledJobCreateInput,
        *,
        bl: Any,
        project: dict[str, Any],
        resolved: ScopeContext,
    ) -> dict[str, Any]:
        if self._cron_next_run_at is None:
            raise ServiceUnavailableError(_CRON_UNAVAILABLE)
        clean_requested_workflow = str(command.workflow_id or "").strip()
        clean_cron = self._normalize_cron_expr(str(command.cron_expr or ""))
        if not clean_requested_workflow:
            raise ServiceValidationError("Scheduled job requires an explicit workflow.")
        if not clean_cron:
            raise ServiceValidationError("Cron expression is required.")

        run_cfg = self._project_run_config(bl, str(project_id))
        account_id = str(project.get("account_id") or resolved.account_id or "").strip()
        if not account_id:
            raise ServiceValidationError(_SCHEDULE_ACCOUNT_REQUIRED)
        selected_workflow_id, selection_source = self._resolve_selected_workflow(
            project_config=run_cfg,
            requested_workflow_id=clean_requested_workflow,
        )
        if selection_source != "explicit_request":
            raise ServiceValidationError(
                "Scheduled job requires a linked explicit workflow override."
            )

        require_workflow_access(resolved, selected_workflow_id)

        workflow_inputs = dict(command.inputs) if isinstance(command.inputs, dict) else {}
        admission, workflow_inputs = self._scheduled_job_admission(
            workflow_id=selected_workflow_id,
            project=project,
            project_config=run_cfg,
            inputs=workflow_inputs,
        )

        self._assert_can_start_workflow(
            workflow_id=selected_workflow_id,
            account_id=account_id,
            project_id=str(project_id),
        )

        enabled_flag = self._form_flag(command.enabled)
        from core.application.settings.timezone_service import validate_timezone

        requested_schedule_tz = str(command.schedule_timezone or "").strip()
        try:
            job_schedule_tz = (
                validate_timezone(requested_schedule_tz)
                if requested_schedule_tz
                else str(project.get("schedule_timezone") or "").strip()
            )
        except ValueError as exc:
            raise ServiceValidationError(str(exc)) from exc
        project_schedule_tz = job_schedule_tz or None
        try:
            next_run_at = self._cron_next_run_at(
                clean_cron, datetime.now(UTC), schedule_tz=project_schedule_tz
            )
        except ValueError as exc:
            raise ServiceValidationError(f"Invalid cron expression: {exc}") from exc

        return {
            "account_id": account_id,
            "project_id": str(project_id),
            "workflow_id": selected_workflow_id,
            "cron_expr": clean_cron,
            "inputs": workflow_inputs,
            "enabled": enabled_flag,
            "status": "active" if enabled_flag else "paused",
            "next_run_at": next_run_at if enabled_flag else None,
            "schedule_timezone": job_schedule_tz,
            **admission,
        }

    def update_scheduled_job(
        self,
        project_id: Any,
        scheduled_job_id: str,
        command: ProjectScheduledJobUpdateInput,
        *,
        scope: ScopeContext | None = None,
    ) -> ProjectScheduledJobMutationResultView:
        resolved = self._scope(scope, project_id=project_id, workflow_id=command.workflow_id)
        bl = self._logger()
        project = self._require_project_in_scope(bl, str(project_id), resolved)
        account_id = str(project.get("account_id") or resolved.account_id or "").strip()
        if not account_id:
            raise ServiceValidationError(_SCHEDULE_ACCOUNT_REQUIRED)
        current = self._scheduled_job(bl, project_id, scheduled_job_id, account_id=account_id)
        if (
            str(current.get("project_id") or "") != str(project_id)
            or str(current.get("account_id") or "") != account_id
        ):
            raise ServiceNotFoundError(SCHEDULED_JOB_NOT_FOUND_MESSAGE)
        if str(command.workflow_id).strip() != str(current.get("workflow_id") or "").strip():
            raise ServiceValidationError("Schedule editing cannot change the owning workflow.")
        values = {
            name: current[name]
            for name in ProjectScheduledJobCreateInput.model_fields
            if name in current
        }
        values.update({name: value for name, value in command.dump().items() if value is not None})
        prepared = self._prepare_scheduled_job_edit(
            project_id,
            ProjectScheduledJobCreateInput(**values),
            bl=bl,
            project=project,
            resolved=resolved,
        )
        prepared.pop("project_id")
        with workflow_trigger_transaction(bl, str(current["workflow_id"]), account_id=account_id):
            try:
                updated = bl.update_scheduled_job(
                    str(project_id), str(scheduled_job_id), **prepared
                )
            except Exception as exc:
                raise ServiceError("Failed to update scheduled job") from exc
        if not updated:
            raise ServiceNotFoundError(SCHEDULED_JOB_NOT_FOUND_MESSAGE)
        return ProjectScheduledJobMutationResultView(
            project_id=str(project_id),
            scheduled_job_id=str(scheduled_job_id),
            status="updated",
            enabled=bool(updated.get("enabled")),
            next_run_at=updated.get("next_run_at"),
            job=updated,
        )

    def create_scheduled_job(
        self,
        project_id: Any,
        command: ProjectScheduledJobCreateInput,
        *,
        scope: ScopeContext | None = None,
    ) -> ProjectScheduledJobMutationResultView:
        resolved = self._scope(scope, project_id=project_id, workflow_id=command.workflow_id)
        bl = self._logger()
        project = self._require_project_in_scope(bl, str(project_id), resolved)
        if not hasattr(bl, "create_scheduled_job"):
            raise ServiceUnavailableError(ERR_SCHEDULED_JOBS_SCHEMA_UNAVAILABLE)
        if self._cron_next_run_at is None:
            raise ServiceUnavailableError(_CRON_UNAVAILABLE)

        prepared = self._prepare_scheduled_job_edit(
            project_id,
            command,
            bl=bl,
            project=project,
            resolved=resolved,
        )
        with workflow_trigger_transaction(
            bl, str(prepared["workflow_id"]), account_id=str(prepared["account_id"])
        ):
            try:
                created = bl.create_scheduled_job(
                    **prepared,
                    created_by=str(command.created_by or "operator").strip() or "operator",
                )
            except Exception as exc:
                raise ServiceError("Failed to create scheduled job") from exc
        if not created:
            raise ServiceError("Failed to create scheduled job")
        payload = created if isinstance(created, dict) else {}
        return ProjectScheduledJobMutationResultView(
            project_id=str(project_id),
            scheduled_job_id=str(payload.get("id") or "").strip(),
            status="created",
            enabled=bool(prepared["enabled"]),
            next_run_at=payload.get("next_run_at"),
            job=payload,
        )

    def toggle_scheduled_job(
        self,
        project_id: Any,
        scheduled_job_id: str,
        command: ProjectScheduledJobToggleInput,
        *,
        scope: ScopeContext | None = None,
    ) -> ProjectScheduledJobMutationResultView:
        resolved = self._scope(scope, project_id=project_id)
        bl = self._logger()
        project = self._require_project_in_scope(bl, str(project_id), resolved)
        account_id = str(project.get("account_id") or resolved.account_id or "").strip()
        scheduled_job = self._scheduled_job(
            bl,
            str(project_id),
            str(scheduled_job_id),
            account_id=account_id or None,
        )

        raw_enabled = command.enabled
        if raw_enabled is None or str(raw_enabled).strip() == "":
            target_enabled = not bool(scheduled_job.get("enabled"))
        else:
            target_enabled = self._form_flag(raw_enabled)
        target_status = "active" if target_enabled else "paused"

        next_run_at = None
        admission = {
            "admission_allowed": False,
            "admission_reason": "disabled",
            "admission_workflow_revision": scheduled_job.get("admission_workflow_revision"),
            "admission_checked_at": datetime.now(UTC),
        }
        if target_enabled:
            if self._cron_next_run_at is None:
                raise ServiceUnavailableError(_CRON_UNAVAILABLE)
            project_schedule_tz = str(project.get("schedule_timezone") or "").strip() or None
            schedule_tz = (
                str(scheduled_job.get("schedule_timezone") or "").strip() or project_schedule_tz
            )
            try:
                next_run_at = self._cron_next_run_at(
                    str(scheduled_job.get("cron_expr") or ""),
                    datetime.now(UTC),
                    schedule_tz=schedule_tz,
                )
            except Exception as exc:
                raise ServiceValidationError(
                    f"Cannot enable scheduled job: invalid cron expression ({exc})."
                ) from exc
            workflow_id = str(scheduled_job.get("workflow_id") or "").strip()
            require_workflow_access(resolved, workflow_id)
            self._assert_can_start_workflow(
                workflow_id=workflow_id,
                account_id=account_id,
                project_id=str(project_id),
            )
            admission, _validated_inputs = self._scheduled_job_admission(
                workflow_id=workflow_id,
                project=project,
                project_config=self._project_run_config(bl, str(project_id)),
                inputs=(
                    dict(scheduled_job.get("inputs") or {})
                    if isinstance(scheduled_job.get("inputs"), dict)
                    else {}
                ),
            )
        with workflow_trigger_transaction(
            bl,
            str(scheduled_job.get("workflow_id") or ""),
            account_id=account_id,
            include_trashed=not target_enabled,
        ):
            try:
                bl.update_scheduled_job(
                    str(project_id),
                    str(scheduled_job_id),
                    account_id=account_id or None,
                    enabled=target_enabled,
                    status=target_status,
                    next_run_at=next_run_at,
                    last_error="",
                    **admission,
                )
            except TypeError:
                bl.update_scheduled_job(
                    str(project_id),
                    str(scheduled_job_id),
                    enabled=target_enabled,
                    status=target_status,
                    next_run_at=next_run_at,
                    last_error="",
                    **admission,
                )
            except Exception as exc:
                raise ServiceError("Failed to update scheduled job") from exc
        return ProjectScheduledJobMutationResultView(
            project_id=str(project_id),
            scheduled_job_id=str(scheduled_job_id),
            status="updated",
            enabled=target_enabled,
            next_run_at=next_run_at,
        )

    def delete_scheduled_job(
        self,
        project_id: Any,
        scheduled_job_id: str,
        *,
        scope: ScopeContext | None = None,
    ) -> ProjectScheduledJobDeleteResultView:
        resolved = self._scope(scope, project_id=project_id)
        bl = self._logger()
        project = self._require_project_in_scope(bl, str(project_id), resolved)
        if not hasattr(bl, "delete_scheduled_job"):
            raise ServiceUnavailableError(ERR_SCHEDULED_JOBS_SCHEMA_UNAVAILABLE)
        account_id = str(project.get("account_id") or resolved.account_id or "").strip()
        try:
            deleted = bool(
                bl.delete_scheduled_job(
                    str(project_id),
                    str(scheduled_job_id),
                    account_id=account_id or None,
                )
            )
        except TypeError:
            deleted = bool(bl.delete_scheduled_job(str(project_id), str(scheduled_job_id)))
        except Exception as exc:
            raise ServiceError("Failed to delete scheduled job") from exc
        if not deleted:
            raise ServiceNotFoundError(SCHEDULED_JOB_NOT_FOUND_MESSAGE)
        return ProjectScheduledJobDeleteResultView(
            project_id=str(project_id),
            scheduled_job_id=str(scheduled_job_id),
        )

    def run_scheduled_job_now(
        self,
        project_id: Any,
        scheduled_job_id: str,
        *,
        scope: ScopeContext | None = None,
    ) -> ProjectScheduledJobRunResultView:
        resolved = self._scope(scope, project_id=project_id)
        bl = self._logger()
        project = self._require_project_in_scope(bl, str(project_id), resolved)
        account_id = str(project.get("account_id") or resolved.account_id or "").strip()
        scheduled_job = self._scheduled_job(
            bl,
            str(project_id),
            str(scheduled_job_id),
            account_id=account_id or None,
        )
        scheduled_workflow_id = str((scheduled_job or {}).get("workflow_id") or "").strip()
        if scheduled_workflow_id:
            require_workflow_access(resolved, scheduled_workflow_id)
        self._assert_can_start_workflow(
            workflow_id=scheduled_workflow_id,
            account_id=account_id,
            project_id=str(project_id),
        )
        admission, _validated_inputs = self._scheduled_job_admission(
            workflow_id=scheduled_workflow_id,
            project=project,
            project_config=self._project_run_config(bl, str(project_id)),
            inputs=(
                dict(scheduled_job.get("inputs") or {})
                if isinstance(scheduled_job.get("inputs"), dict)
                else {}
            ),
        )
        updated = bl.update_scheduled_job(
            str(project_id),
            str(scheduled_job_id),
            account_id=account_id or None,
            **admission,
        )
        if not updated:
            raise ServiceNotFoundError(SCHEDULED_JOB_NOT_FOUND_MESSAGE)
        scheduled_job = {**scheduled_job, **admission}
        if self._run_scheduled_job_once is None:
            raise ServiceUnavailableError("Scheduled job execution is unavailable")
        result = self._run_scheduled_job_once(
            bl,
            scheduled_job,
            run_type="manual",
            scheduled_for=datetime.now(UTC),
            advance_schedule=False,
        )
        payload = result if isinstance(result, dict) else {}
        return ProjectScheduledJobRunResultView(
            project_id=str(project_id),
            scheduled_job_id=str(scheduled_job_id),
            status=str(payload.get("status") or "failed"),
            job_id=str(payload.get("job_id") or "").strip() or None,
            reason=str(payload.get("reason") or "").strip() or None,
            next_run_at=payload.get("next_run_at"),
        )
