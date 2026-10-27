"""HTTP adapter for notifications provider ingress."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from core.application.management.command_models import (
    ProviderEventIngestInput,
)
from core.application.management.errors import ServiceError
from core.entrypoints.api.provider_webhook_callbacks import (
    _MESSAGE_INVALID_WEBHOOK_SIGNATURE_FOR_ROUTING_PROJECT,
    _MESSAGE_PROVIDER_PROJECT_STATE_UPDATE_FAILED,
    NotificationWebhookCallbacks,
)


def handle_notification_provider_webhook(
    *,
    callbacks: NotificationWebhookCallbacks,
    base_payload_summary: dict[str, Any],
    bl: Any,
    ingress_extension: Any,
    normalized_provider_key: str,
    payload: dict[str, Any],
    provider: dict[str, Any],
    raw_body: bytes,
    request: Request,
) -> Any:
    _apply_deferred_project_state_updates = callbacks.apply_deferred_project_state_updates
    _as_json_dict = callbacks.as_json_dict
    _provider_request_context_summary = callbacks.provider_request_context_summary
    _record_provider_ingress_event = callbacks.record_provider_ingress_event
    api_error_response_fn = callbacks.api_error_response_fn
    enforce_provider_webhook_project_secret = callbacks.enforce_provider_webhook_project_secret
    err_webhook_processing_failed_msg = callbacks.err_webhook_processing_failed_msg
    event_command_service_fn = callbacks.event_command_service_fn
    ingress_notification_provider_payload = callbacks.ingress_notification_provider_payload
    ingress_runtime = callbacks.ingress_runtime
    logger_obj = callbacks.logger_obj
    request_scope_fn = callbacks.request_scope_fn
    service_error_response_fn = callbacks.service_error_response_fn

    deferred_project_state_updates: list[dict[str, Any]] = []
    try:
        ingress = ingress_notification_provider_payload(
            provider=provider,
            payload=payload,
            request=request,
            bl=bl,
            scope=request_scope_fn(request),
            apply_project_state_updates=False,
            deferred_project_state_updates=deferred_project_state_updates,
        )
    except ValueError as e:
        _record_provider_ingress_event(
            bl,
            provider_key=normalized_provider_key,
            project_id=None,
            account_id=str(provider.get("account_id") or "").strip() or None,
            workflow_id=None,
            request=request,
            reason="provider_payload_invalid",
            detail=str(e),
            status_code=400,
            payload_summary=base_payload_summary,
        )
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        _record_provider_ingress_event(
            bl,
            provider_key=normalized_provider_key,
            project_id=None,
            account_id=str(provider.get("account_id") or "").strip() or None,
            workflow_id=None,
            request=request,
            reason="provider_payload_normalization_failed",
            detail=err_webhook_processing_failed_msg,
            status_code=502,
            payload_summary=base_payload_summary,
        )
        return api_error_response_fn(
            e,
            status_code=502,
            fallback_message=err_webhook_processing_failed_msg,
        )

    ingress_result = ingress.get("result") if isinstance(ingress, dict) else {}
    ingress_events = ingress.get("events") if isinstance(ingress, dict) else []
    ingress_events = ingress_runtime.normalize_ingress_provider_events(
        list(ingress_events or []),
        provider_key=normalized_provider_key,
        default_source=f"webhook:{normalized_provider_key}",
    )
    project_id = ingress_runtime.resolve_ingress_project_id(
        ingress_result=_as_json_dict(ingress_result),
        ingress_events=list(ingress_events or []),
        request_payload=payload,
        query_params=dict(request.query_params),
        bl=bl,
    )
    if not project_id:
        _record_provider_ingress_event(
            bl,
            provider_key=normalized_provider_key,
            project_id=None,
            account_id=str(provider.get("account_id") or "").strip() or None,
            workflow_id=None,
            request=request,
            reason="provider_project_id_missing",
            detail="project_id is required for notification provider ingress",
            status_code=400,
            payload_summary=base_payload_summary,
        )
        return JSONResponse(
            {"error": "project_id is required for notification provider ingress"},
            status_code=400,
        )
    project_secret_ok, project_secret_error = enforce_provider_webhook_project_secret(
        normalized_provider_key,
        project_id=str(project_id),
        request_headers=dict(request.headers),
        raw_body=raw_body,
        bl=bl,
    )
    if not project_secret_ok:
        _record_provider_ingress_event(
            bl,
            provider_key=normalized_provider_key,
            project_id=str(project_id),
            account_id=str(provider.get("account_id") or "").strip() or None,
            workflow_id=None,
            request=request,
            reason="provider_project_signature_failed",
            detail=project_secret_error or _MESSAGE_INVALID_WEBHOOK_SIGNATURE_FOR_ROUTING_PROJECT,
            status_code=401,
            payload_summary=base_payload_summary,
        )
        return JSONResponse(
            {
                "error": project_secret_error
                or _MESSAGE_INVALID_WEBHOOK_SIGNATURE_FOR_ROUTING_PROJECT
            },
            status_code=401,
        )
    state_ok, state_error = _apply_deferred_project_state_updates(
        bl=bl,
        request=request,
        project_id=str(project_id),
        deferred_updates=deferred_project_state_updates,
    )
    if not state_ok:
        _record_provider_ingress_event(
            bl,
            provider_key=normalized_provider_key,
            project_id=str(project_id),
            account_id=str(provider.get("account_id") or "").strip() or None,
            workflow_id=None,
            request=request,
            reason="provider_project_state_update_failed",
            detail=state_error or _MESSAGE_PROVIDER_PROJECT_STATE_UPDATE_FAILED,
            status_code=502,
            payload_summary=base_payload_summary,
        )
        return JSONResponse(
            {"error": state_error or _MESSAGE_PROVIDER_PROJECT_STATE_UPDATE_FAILED},
            status_code=502,
        )
    try:
        ingress_payload = event_command_service_fn().ingest_provider_events(
            ProviderEventIngestInput(
                provider_key=normalized_provider_key,
                project_id=str(project_id),
                result=_as_json_dict(ingress_result),
                events=list(ingress_events or []),
            ),
            scope=request_scope_fn(request, project_id=project_id),
        )
    except ServiceError as e:
        status_code = int(getattr(e, "status_code", 500) or 500)
        error_payload = getattr(e, "payload", None)
        detail = ""
        if isinstance(error_payload, dict):
            candidate = error_payload.get("error") or error_payload.get("message")
            if isinstance(candidate, str):
                detail = candidate.strip()
        if not detail:
            detail = str(getattr(e, "message", e) or err_webhook_processing_failed_msg).strip()
        _record_provider_ingress_event(
            bl,
            provider_key=normalized_provider_key,
            project_id=str(project_id),
            account_id=str(provider.get("account_id") or "").strip() or None,
            workflow_id=None,
            request=request,
            reason="provider_event_ingest_failed",
            detail=detail or err_webhook_processing_failed_msg,
            status_code=status_code,
            payload_summary=_provider_request_context_summary(
                payload=payload,
                request=request,
            ),
        )
        return service_error_response_fn(e)
    except Exception as e:
        _record_provider_ingress_event(
            bl,
            provider_key=normalized_provider_key,
            project_id=str(project_id),
            account_id=str(provider.get("account_id") or "").strip() or None,
            workflow_id=None,
            request=request,
            reason="provider_event_ingest_failed",
            detail=err_webhook_processing_failed_msg,
            status_code=502,
            payload_summary=_provider_request_context_summary(
                payload=payload,
                request=request,
            ),
        )
        return api_error_response_fn(
            e,
            status_code=502,
            fallback_message=err_webhook_processing_failed_msg,
        )
    if ingress_events and bl:
        from core.infrastructure.extension_runtime.extension_runtime import (
            emit_extension_event,
        )

        scope_for_events = request_scope_fn(request, project_id=project_id)
        for evt in ingress_events or []:
            try:
                emit_extension_event(
                    ingress_extension or {},
                    event_type=str(evt.get("event_type") or "").strip(),
                    correlation_id=str(evt.get("correlation_id") or "").strip(),
                    scope=scope_for_events,
                    summary=str(evt.get("summary") or "").strip(),
                    severity=str(evt.get("severity") or "info").strip(),
                    payload=dict(evt.get("payload") or {}),
                    project_id=str(project_id),
                    source=f"webhook:{normalized_provider_key}",
                    runtime_context={"logger": bl},
                )
            except Exception as exc:
                logger_obj.warning(
                    "Webhook event emission failed: provider=%s evt=%s error=%s",
                    normalized_provider_key,
                    evt.get("event_type"),
                    exc,
                )
    return ingress_payload.dump()
