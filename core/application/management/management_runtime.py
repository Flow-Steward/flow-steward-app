"""Reusable service wiring for management surfaces."""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from typing import Any

from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
from core.application.extensions.artifact_storage import create_storage_adapter
from core.application.jobs.job_invocation_artifacts import load_replay_snapshot
from core.application.jobs.job_runtime_payload import build_persisted_diagnostic_payload
from core.application.jobs.job_trace_runtime import build_resolve_job_project_name
from core.application.jobs.task_enqueue_runtime import build_enqueue_task_handler
from core.application.management import query_service_runtime as _query_service_runtime
from core.application.management.command_services import JobCommandService, ProjectCommandService
from core.application.management.project_grid_rows import serialize_projects_grid_row
from core.application.management.query_services import JobQueryService, ProjectQueryService
from core.application.management.snapshot_serializers import (
    build_project_config_snapshot,
    serialize_agent_message_row,
    serialize_job_error_row,
    serialize_log_row,
)
from core.application.management.workflow_operator_service import WorkflowOperatorService
from core.application.orchestration.agents.common import sanitize_external_input
from core.application.workflows.domain_event_runtime import project_account_id
from core.application.workflows.serve_workflow_helpers import (
    build_rerun_workflow_selection,
    build_resolve_workflow_selection,
    build_task_envelope_for_preview,
    list_workflow_choices,
)
from core.application.workflows.workflow_availability_service import WorkflowAvailabilityService
from core.application.workflows.workflow_override_runtime import (
    build_explicit_workflow_override_validator,
)
from core.infrastructure.config import runtime_config as core_config
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.runtime.artifact_storage_factory import build_artifact_storage
from core.infrastructure.runtime.serve_interrupt_helpers import approval_fallback_payload
from core.infrastructure.runtime.serve_request_runtime import (
    build_runtime_payload_for_api as _build_runtime_payload_for_api,
)
from core.infrastructure.runtime.serve_request_runtime import to_json_obj
from core.infrastructure.runtime.test_mode import parse_test_mode

_LOGGER = logging.getLogger("platform.manage.runtime")
_SENSITIVE_KEY_RE = re.compile(
    r"(api[_-]?key|token|secret|password|passphrase|private[_-]?key|dsn)",
    re.IGNORECASE,
)


@lru_cache(maxsize=1)
def _job_invocation_artifact_plane() -> ArtifactDatasetPlane:
    return build_artifact_storage(
        environ=dict(_runtime_env),
        create_storage_adapter=create_storage_adapter,
        artifact_plane_factory=ArtifactDatasetPlane,
    )


def create_project_query_service(*, get_logger, get_job_store=None) -> ProjectQueryService:
    workflow_resolver = build_resolve_workflow_selection(
        list_workflow_choices=lambda **kwargs: list_workflow_choices(
            enabled_only=bool(kwargs.get("enabled_only", False)),
            logger=_LOGGER,
            account_id=str(kwargs.get("account_id") or "").strip() or None,
        ),
        logger=_LOGGER,
    )
    workflow_validator = build_explicit_workflow_override_validator(
        build_task_envelope_for_preview=build_task_envelope_for_preview,
    )
    availability_service = WorkflowAvailabilityService(get_logger=get_logger)
    return ProjectQueryService(
        get_logger=get_logger,
        get_job_store=get_job_store,
        serialize_projects_grid_row=serialize_projects_grid_row,
        get_queue_depth=lambda _project_id: -1,
        get_ready_queue_depth=lambda: -1,
        to_json_obj=to_json_obj,
        sanitize_external_input=sanitize_external_input,
        resolve_workflow_selection=workflow_resolver,
        validate_explicit_workflow_override=workflow_validator,
        build_task_envelope_for_preview=build_task_envelope_for_preview,
        workflow_availability_service=availability_service,
    )


def create_project_command_service(
    *,
    get_logger,
    get_notification_logger=None,
) -> ProjectCommandService:
    workflow_resolver = build_resolve_workflow_selection(
        list_workflow_choices=lambda **kwargs: list_workflow_choices(
            enabled_only=bool(kwargs.get("enabled_only", False)),
            logger=_LOGGER,
            account_id=str(kwargs.get("account_id") or "").strip() or None,
        ),
        logger=_LOGGER,
    )
    workflow_validator = build_explicit_workflow_override_validator(
        build_task_envelope_for_preview=build_task_envelope_for_preview,
    )
    availability_service = WorkflowAvailabilityService(get_logger=get_logger)
    enqueue_task = build_enqueue_task_handler(
        get_config=lambda: core_config.CONFIG,
        create_logger_fn=lambda _dsn: get_logger(),
        enqueue_task_fn=lambda **kwargs: _publish_task(**kwargs),
        build_project_config_snapshot=_project_config_snapshot,
        project_account_id_fn=lambda bl, project_id: project_account_id(bl, project_id),
        logger=_LOGGER,
        log_prefix="platform.manage.enqueue",
        artifact_plane=_job_invocation_artifact_plane,
    )

    def dispatch_project_notification_event(_project_store, **kwargs):
        from core.application.notifications.dispatch import (
            dispatch_project_notification_event as dispatch,
        )

        notification_store = (get_notification_logger or get_logger)()
        return dispatch(notification_store, **kwargs)

    return ProjectCommandService(
        get_logger=get_logger,
        resolve_workflow_selection=workflow_resolver,
        validate_explicit_workflow_override=workflow_validator,
        sanitize_external_input=sanitize_external_input,
        enqueue_task=enqueue_task,
        parse_test_mode=parse_test_mode,
        dispatch_project_notification_event=dispatch_project_notification_event,
        workflow_availability_assert=lambda workflow_id, account_id, project_id: (
            availability_service.assert_can_start_workflow(
                workflow_id,
                account_id=account_id,
                project_id=project_id,
            )
        ),
        to_json_obj=to_json_obj,
        idempotency_result_retention_days=int(
            core_config.CONFIG.get("idempotency_result_retention_days") or 30
        ),
    )


def create_workflow_operator_service(
    *,
    get_logger,
    project_query_service,
    get_project_store=None,
    get_integration_store=None,
    get_install_store=None,
    get_job_store=None,
) -> WorkflowOperatorService:
    return WorkflowOperatorService(
        get_logger=get_logger,
        project_query_service=project_query_service,
        get_project_store=get_project_store,
        get_integration_store=get_integration_store,
        get_install_store=get_install_store,
        get_job_store=get_job_store,
    )


def create_job_query_service(*, get_logger) -> JobQueryService:
    def _persisted_runtime_payload(job: dict[str, Any], bl: Any) -> dict[str, Any]:
        return build_persisted_diagnostic_payload(
            job,
            bl,
            serialize_job_error_row=lambda row: serialize_job_error_row(
                row, to_json_obj=to_json_obj
            ),
        )

    async def _runtime_payload_for_api(job: dict[str, Any], bl: Any) -> dict[str, Any]:
        return await _build_runtime_payload_for_api(
            job,
            bl,
            build_runtime_payload=_persisted_runtime_payload,
            build_runtime_payload_fallback=lambda _job: {},
            timeout_seconds=max(
                0.1,
                float(str(core_config.CONFIG.get("job_runtime_payload_timeout_seconds") or 2.5)),
            ),
            logger=_LOGGER,
        )

    return JobQueryService(
        get_logger=get_logger,
        # The canonical serializer, not a pass-through. A raw row hands a
        # datetime to AttentionJobView.acknowledged_at, which is declared
        # str | None, so `jobs list` broke after an acknowledgement while the
        # API - which has always used this serializer - was fine. One
        # projection for both surfaces is the point.
        serialize_attention_job_row=_query_service_runtime.build_attention_job_row_serializer(
            truncate_text_fn=lambda value, max_len=4000: _query_service_runtime.truncate_text(
                value, max_len=max_len
            ),
            build_display_summary_fn=lambda value: str(value or "").strip(),
        ),
        serialize_awaiting_approval_job_row=lambda row: dict(row),
        job_attention_group=lambda status, acknowledged_at=None: (
            str(status or "").strip() or ("acknowledged" if acknowledged_at else "unknown")
        ),
        build_runtime_payload_for_api=_runtime_payload_for_api,
        build_runtime_payload=_persisted_runtime_payload,
        build_display_summary=lambda value: str(value or "").strip(),
        serialize_log_row=lambda row, job_id: serialize_log_row(
            row, job_id, to_json_obj=to_json_obj
        ),
        serialize_agent_message_row=lambda row: serialize_agent_message_row(
            row,
            to_json_obj=to_json_obj,
        ),
        serialize_job_error_row=lambda row: serialize_job_error_row(row, to_json_obj=to_json_obj),
        resolve_job_project_name=build_resolve_job_project_name(to_json_obj=to_json_obj),
        approval_query_service=_query_service_runtime.create_approval_query_service(
            get_logger=get_logger,
            get_thread_state_snapshot=lambda _thread_id: None,
            approval_fallback_payload=lambda job: approval_fallback_payload(
                job,
                to_json_obj=to_json_obj,
                get_logger_only=get_logger,
            ),
            to_json_obj=to_json_obj,
        ),
    )


def create_job_command_service(*, get_logger, get_project_run_config=None) -> JobCommandService:
    enqueue_task = build_enqueue_task_handler(
        get_config=lambda: core_config.CONFIG,
        create_logger_fn=lambda _dsn: get_logger(),
        enqueue_task_fn=lambda **kwargs: _publish_task(**kwargs),
        build_project_config_snapshot=_project_config_snapshot,
        project_account_id_fn=lambda bl, project_id: project_account_id(bl, project_id),
        logger=_LOGGER,
        log_prefix="platform.manage.jobs.enqueue",
        artifact_plane=_job_invocation_artifact_plane,
    )
    return JobCommandService(
        get_logger=get_logger,
        build_rerun_workflow_selection=lambda job, *, replay_snapshot: (
            build_rerun_workflow_selection(
                job,
                replay_snapshot=replay_snapshot,
                to_json_obj=to_json_obj,
                parse_test_mode=parse_test_mode,
            )
        ),
        load_replay_snapshot=lambda **kwargs: load_replay_snapshot(
            artifact_plane=_job_invocation_artifact_plane(),
            **kwargs,
        ),
        enqueue_task=enqueue_task,
        get_project_run_config=get_project_run_config,
    )


def resolve_project_reference(
    project_query_service: ProjectQueryService,
    *,
    project_id: str | None = None,
    project_name: str | None = None,
) -> dict[str, Any]:
    from core.application.management import ServiceNotFoundError, ServiceValidationError

    if project_id is not None:
        runtime_payload = project_query_service.get_runtime_config(str(project_id)).dump()
        resolved_id = str(runtime_payload.get("project_id") or "").strip()
        resolved_name = str(runtime_payload.get("project_name") or "").strip()
        if resolved_name:
            listing = project_query_service.list_projects(
                page=1,
                per_page=500,
                search=resolved_name,
            ).dump()
            for row in listing.get("projects") or []:
                if str((row or {}).get("id") or "").strip() == resolved_id:
                    return dict(row)
        return {"id": resolved_id, "name": resolved_name}

    normalized_name = str(project_name or "").strip()
    if not normalized_name:
        raise ServiceValidationError("Either project_id or project_name is required")

    listing = project_query_service.list_projects(
        page=1,
        per_page=500,
        search=normalized_name,
    ).dump()
    exact = [
        dict(row)
        for row in (listing.get("projects") or [])
        if str((row or {}).get("name") or "").strip().lower() == normalized_name.lower()
    ]
    if not exact:
        raise ServiceNotFoundError(f"Project '{normalized_name}' not found")
    if len(exact) > 1:
        ids = ", ".join(str(item.get("id")) for item in exact)
        raise ServiceValidationError(
            f"Project name '{normalized_name}' is ambiguous, use project_id ({ids})"
        )
    return exact[0]


def _project_config_snapshot(project_config: dict | None) -> dict[str, Any]:
    return build_project_config_snapshot(project_config, secret_key_re=_SENSITIVE_KEY_RE)


def create_enqueue_task_handler(*, get_logger):
    """Job-creating ``enqueue_task(task_id=..., test_mode=...)`` for a process without the HTTP runtime.

    The worker hands the event service this handler, not the raw queue publisher, so an
    event that starts a workflow (a provider trigger, a reaction) creates the job row
    first and publishes it second, the same as the API process does.
    """
    return build_enqueue_task_handler(
        get_config=lambda: core_config.CONFIG,
        create_logger_fn=lambda _dsn: get_logger(),
        enqueue_task_fn=lambda **kwargs: _publish_task(**kwargs),
        build_project_config_snapshot=_project_config_snapshot,
        project_account_id_fn=lambda bl, project_id: project_account_id(bl, project_id),
        logger=_LOGGER,
        log_prefix="platform.worker.enqueue",
        artifact_plane=_job_invocation_artifact_plane,
    )


def _publish_task(**kwargs) -> None:
    from core.infrastructure.runtime.worker.queue_publisher import enqueue_task

    enqueue_task(**kwargs)
