"""Typed collaborators for provider webhook HTTP adapters."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

_MESSAGE_INVALID_WEBHOOK_SIGNATURE = "Invalid webhook signature"
_MESSAGE_INVALID_WEBHOOK_SIGNATURE_FOR_ROUTING_PROJECT = (
    "Invalid webhook signature for routing project"
)
_MESSAGE_PROVIDER_PROJECT_STATE_UPDATE_FAILED = "Provider project-state update failed"


@dataclass(frozen=True, slots=True)
class NotificationWebhookCallbacks:
    apply_deferred_project_state_updates: Callable[..., Any]
    as_json_dict: Callable[..., Any]
    provider_request_context_summary: Callable[..., Any]
    record_provider_ingress_event: Callable[..., Any]
    api_error_response_fn: Callable[..., Any]
    enforce_provider_webhook_project_secret: Callable[..., Any]
    err_webhook_processing_failed_msg: str
    event_command_service_fn: Callable[..., Any]
    ingress_notification_provider_payload: Callable[..., Any]
    ingress_runtime: Any
    logger_obj: Any
    request_scope_fn: Callable[..., Any]
    service_error_response_fn: Callable[..., Any]


@dataclass(frozen=True, slots=True)
class TriggerWebhookCallbacks:
    apply_deferred_project_state_updates: Callable[..., Any]
    as_json_dict: Callable[..., Any]
    as_json_list: Callable[..., Any]
    build_provider_workflow_inputs: Callable[..., Any]
    claim_provider_webhook_delivery: Callable[..., Any]
    mark_provider_webhook_delivery_accepted: Callable[..., Any]
    mark_provider_webhook_delivery_failed: Callable[..., Any]
    provider_delivery_table_missing: Callable[..., Any]
    provider_request_context_summary: Callable[..., Any]
    provider_trigger_event_name: Callable[..., Any]
    provider_trigger_matches: Callable[..., Any]
    record_provider_ingress_event: Callable[..., Any]
    scalar_binding_hints: Callable[..., Any]
    scope_provider_trigger_rows_to_project: Callable[..., Any]
    api_error_response_fn: Callable[..., Any]
    enforce_provider_webhook_project_secret: Callable[..., Any]
    enqueue_workflow_start: Callable[..., Any]
    err_webhook_processing_failed_msg: str
    event_command_service_fn: Callable[..., Any]
    logger_obj: Any
    workflow_interface_service_cls: Any
