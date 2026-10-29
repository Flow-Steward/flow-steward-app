# mypy: ignore-errors
"""Route registration helpers for job detail and provider webhook routes."""

from __future__ import annotations

from fastapi import FastAPI

from core.entrypoints.api.provider_webhooks import create_provider_webhooks_router
from core.entrypoints.api.workflow_entrypoints import create_workflow_entrypoints_router


def register_provider_webhook_routes(
    app: FastAPI,
    *,
    env_flag,
    request_scope,
    to_json_obj,
    get_logger_only,
    service_error_response,
    api_error_response,
    event_command_service,
    resolve_task_provider_project_run_config,
    resolve_provider_extension_for_runtime,
    provider_supports_ingress_events,
    normalize_task_provider_payload,
    ingress_notification_provider_payload,
    logger,
    err_database_not_configured,
    err_webhook_processing_failed,
):
    router = create_provider_webhooks_router(
        env_flag=env_flag,
        request_scope=request_scope,
        to_json_obj=to_json_obj,
        get_logger_only=get_logger_only,
        service_error_response=service_error_response,
        api_error_response=api_error_response,
        event_command_service=event_command_service,
        resolve_task_provider_project_run_config=resolve_task_provider_project_run_config,
        resolve_provider_extension_for_runtime=resolve_provider_extension_for_runtime,
        provider_supports_ingress_events=provider_supports_ingress_events,
        normalize_task_provider_payload=normalize_task_provider_payload,
        ingress_notification_provider_payload=ingress_notification_provider_payload,
        logger=logger,
        err_database_not_configured=err_database_not_configured,
        err_webhook_processing_failed=err_webhook_processing_failed,
    )
    app.include_router(router)


def register_workflow_entrypoint_routes(
    app: FastAPI,
    *,
    get_logger_only,
    get_trigger_store=None,
    request_scope,
    enqueue_task,
    redeliver_task=None,
    artifact_plane,
    logger,
    err_database_not_configured,
):
    router = create_workflow_entrypoints_router(
        get_logger_only=get_logger_only,
        get_trigger_store=get_trigger_store,
        request_scope=request_scope,
        enqueue_task=enqueue_task,
        redeliver_task=redeliver_task,
        artifact_plane=artifact_plane,
        logger=logger,
        err_database_not_configured=err_database_not_configured,
    )
    app.include_router(router)
