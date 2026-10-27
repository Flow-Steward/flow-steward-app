# mypy: ignore-errors
"""Provider webhook router assembly."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core.application.management.command_models import (
    ProviderWebhookFileInput,
    ProviderWebhookIngestInput,
)
from core.application.management.errors import ServiceError
from core.application.webhooks.provider_delivery_repository import (
    _claim_provider_webhook_delivery,
    _mark_provider_webhook_delivery_accepted,
    _mark_provider_webhook_delivery_failed,
    _provider_delivery_table_missing,
)
from core.entrypoints.api.provider_webhook_callbacks import (
    _MESSAGE_INVALID_WEBHOOK_SIGNATURE,
    _MESSAGE_INVALID_WEBHOOK_SIGNATURE_FOR_ROUTING_PROJECT,
    _MESSAGE_PROVIDER_PROJECT_STATE_UPDATE_FAILED,
    NotificationWebhookCallbacks,
    TriggerWebhookCallbacks,
)
from core.entrypoints.api.provider_webhook_notifications import handle_notification_provider_webhook
from core.entrypoints.api.provider_webhook_trigger import handle_workflow_trigger_webhook
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.config.size_limits import clamp_size_limit_bytes
from core.infrastructure.runtime import entrypoint_db_adapters as _db_adapters

# Anonymous webhook intake: cap the body before buffering/parsing/HMAC so an
# unauthenticated caller cannot force unbounded in-memory work (DoS).
# Operator-tunable via env (read once at import; clamped to a safe range).
_MAX_PROVIDER_WEBHOOK_BODY_BYTES = clamp_size_limit_bytes(
    _runtime_env.get("FS_PROVIDER_WEBHOOK_BODY_MAX_BYTES"),
    default=1 * 1024 * 1024,  # 1 MiB
    minimum=4 * 1024,  # 4 KiB
    maximum=64 * 1024 * 1024,  # 64 MiB
)


class _ProviderWebhookBodyTooLarge(Exception):
    """Raised when a webhook body exceeds the intake size cap."""


async def _read_capped_provider_webhook_body(request: Request) -> bytes:
    """Read the request body, rejecting (413) before materializing oversized input.

    Checks Content-Length first, then enforces the cap *during* streaming so a
    chunked / mislabelled request cannot bypass the header pre-check.
    """
    cap = _MAX_PROVIDER_WEBHOOK_BODY_BYTES
    content_length = str(request.headers.get("content-length") or "").strip()
    if content_length.isdigit() and int(content_length) > cap:
        raise _ProviderWebhookBodyTooLarge()
    cached = getattr(request, "_body", None)
    if isinstance(cached, (bytes, bytearray)):
        if len(cached) > cap:
            raise _ProviderWebhookBodyTooLarge()
        return bytes(cached)
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        if not chunk:
            continue
        total += len(chunk)
        if total > cap:
            raise _ProviderWebhookBodyTooLarge()
        chunks.append(chunk)
    body = b"".join(chunks)
    with suppress(Exception):
        request._body = body
    return body


def create_provider_webhooks_router(
    *,
    facade: Any,
    request_scope: Callable[..., Any],
    get_logger_only: Callable[[], Any],
    service_error_response: Callable[..., JSONResponse],
    api_error_response: Callable[..., JSONResponse],
    event_command_service: Callable[[], Any],
    resolve_task_provider_project_run_config: Callable[..., tuple[int | None, dict | None]],
    resolve_provider_extension_for_runtime: Callable[..., dict],
    provider_supports_ingress_events: Callable[[dict | None], bool],
    normalize_task_provider_payload: Callable[..., dict],
    ingress_notification_provider_payload: Callable[..., dict],
    logger: Any,
    err_database_not_configured: str,
    err_webhook_processing_failed: str,
) -> APIRouter:
    """Configure and return provider webhook routes."""

    _as_json_dict = facade._as_json_dict
    _as_json_list = facade._as_json_list
    _provider_request_context_summary = facade._provider_request_context_summary
    _record_provider_ingress_event = facade._record_provider_ingress_event
    _scope_provider_trigger_rows_to_project = facade._scope_provider_trigger_rows_to_project
    _provider_trigger_event_name = facade._provider_trigger_event_name
    _scalar_binding_hints = facade._scalar_binding_hints
    _provider_trigger_matches = facade._provider_trigger_matches
    _build_provider_workflow_inputs = facade._build_provider_workflow_inputs
    workflow_provider_trigger_repository = facade.workflow_provider_trigger_repository
    workflow_interface_service_cls = facade.WorkflowInterfaceService
    enqueue_workflow_start = facade.enqueue_workflow_start
    apply_provider_project_state_updates = getattr(
        facade, "_apply_provider_project_state_updates", None
    )

    request_scope_fn = request_scope
    get_logger_only_fn = get_logger_only
    service_error_response_fn = service_error_response
    api_error_response_fn = api_error_response
    event_command_service_fn = event_command_service
    logger_obj = logger
    err_database_not_configured_msg = err_database_not_configured
    err_webhook_processing_failed_msg = err_webhook_processing_failed

    router = APIRouter(tags=["provider-webhooks"])

    def _apply_deferred_project_state_updates(
        *,
        bl: Any,
        request: Request,
        project_id: str,
        deferred_updates: list[dict[str, Any]],
    ) -> tuple[bool, str]:
        if not deferred_updates or not callable(apply_provider_project_state_updates):
            return True, ""
        scope = request_scope_fn(request, project_id=str(project_id))
        for row in deferred_updates:
            invocation = row.get("invocation")
            if not isinstance(invocation, dict):
                continue
            apply_provider_project_state_updates(
                invocation,
                extension=_as_json_dict(row.get("extension")),
                provider=_as_json_dict(row.get("provider")),
                payload=_as_json_dict(row.get("payload")),
                bl=bl,
                scope=scope,
                request=row.get("request") or request,
                project_id_override=str(project_id),
            )
            if invocation.get("status") != "succeeded":
                detail = str(
                    invocation.get("detail") or "provider_project_state_update_failed"
                ).strip()
                return False, detail or "provider_project_state_update_failed"
        return True, ""

    @router.post("/webhook/providers/{provider_key}")
    async def provider_task_webhook(provider_key: str, request: Request):
        """Generic provider webhook intake."""
        try:
            raw_body = await _read_capped_provider_webhook_body(request)
        except _ProviderWebhookBodyTooLarge:
            return JSONResponse({"error": "Request body exceeds size limit"}, status_code=413)
        bl = get_logger_only_fn()
        if not bl:
            return JSONResponse({"error": err_database_not_configured_msg}, status_code=503)
        try:
            payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except Exception:
            payload = {}
        payload = payload if isinstance(payload, dict) else {}

        normalized_provider_key = str(provider_key or "").strip().lower()
        (
            enforce_provider_webhook_effective_signature,
            enforce_provider_webhook_project_secret,
            enforce_provider_webhook_signature,
        ) = _db_adapters.provider_signature_enforcers()

        provider_hint = (
            bl.get_provider(normalized_provider_key) if hasattr(bl, "get_provider") else None
        )
        provider_account_id = (
            str((provider_hint or {}).get("account_id") or "").strip() or None
            if isinstance(provider_hint, dict)
            else None
        )
        base_payload_summary = _provider_request_context_summary(
            payload=payload,
            request=request,
        )
        if not hasattr(bl, "get_provider"):
            _record_provider_ingress_event(
                bl,
                provider_key=normalized_provider_key,
                project_id=None,
                account_id=None,
                workflow_id=None,
                request=request,
                reason="provider_registry_unavailable",
                detail="Provider registry is not available",
                status_code=503,
                payload_summary=base_payload_summary,
            )
            return JSONResponse({"error": "Provider registry is not available"}, status_code=503)
        provider = provider_hint or bl.get_provider(normalized_provider_key)
        if not provider:
            _record_provider_ingress_event(
                bl,
                provider_key=normalized_provider_key,
                project_id=None,
                account_id=None,
                workflow_id=None,
                request=request,
                reason="provider_not_found",
                detail=f"Unknown provider '{normalized_provider_key}'",
                status_code=404,
                payload_summary=base_payload_summary,
            )
            return JSONResponse(
                {"error": f"Unknown provider '{normalized_provider_key}'"}, status_code=404
            )
        if not bool(provider.get("enabled", True)):
            _record_provider_ingress_event(
                bl,
                provider_key=normalized_provider_key,
                project_id=None,
                account_id=str(provider.get("account_id") or "").strip() or None,
                workflow_id=None,
                request=request,
                reason="provider_disabled",
                detail=f"Provider '{normalized_provider_key}' is disabled",
                status_code=409,
                payload_summary=base_payload_summary,
            )
            return JSONResponse(
                {"error": f"Provider '{normalized_provider_key}' is disabled"}, status_code=409
            )

        # Pre-routing gate: require a configured secret + a signature header, but
        # DEFER the authoritative value check to the post-routing effective-secret
        # verification (enforce_provider_webhook_project_secret). This lets a
        # request legitimately signed with a per-project secret (which differs from
        # the global secret) pass the gate and be authenticated against the routing
        # project's secret afterwards (FS-DEEP-CAND-069).
        signature_ok, signature_error = enforce_provider_webhook_signature(
            normalized_provider_key,
            request_headers=dict(request.headers),
            raw_body=raw_body,
            bl=bl,
            required=True,
            match_required=False,
        )
        if not signature_ok:
            status_code = (
                503 if signature_error and "not configured" in str(signature_error).lower() else 401
            )
            _record_provider_ingress_event(
                bl,
                provider_key=normalized_provider_key,
                project_id=None,
                account_id=str(provider.get("account_id") or "").strip() or provider_account_id,
                workflow_id=None,
                request=request,
                reason="provider_signature_failed",
                detail=signature_error or _MESSAGE_INVALID_WEBHOOK_SIGNATURE,
                status_code=status_code,
                payload_summary=base_payload_summary,
            )
            return JSONResponse(
                {"error": signature_error or _MESSAGE_INVALID_WEBHOOK_SIGNATURE},
                status_code=status_code,
            )

        # Authoritative HMAC value check BEFORE any extension action / side effect.
        # The pre-routing gate above only confirmed a secret is configured and a
        # signature header is present (it defers the value check so per-project
        # secrets are supported). Here we verify the signature against the routing
        # project's effective secret — resolving the project from the RAW request
        # only (no extension, no side effects) — falling back to a strict global
        # check when the project is not yet known. This guarantees no
        # run_extension_action runs on an unauthenticated request
        # (FS-DEEP-CAND-069). The final routing project is re-bound to its
        # per-project secret after the extension resolves it.
        ingress_runtime = _db_adapters.ingress_runtime()

        preauth_project_id = ingress_runtime.resolve_ingress_project_id(
            ingress_result={},
            ingress_events=[],
            request_payload=payload,
            query_params=dict(request.query_params),
            bl=bl,
        )
        preauth_ok, preauth_error = enforce_provider_webhook_effective_signature(
            normalized_provider_key,
            project_id=preauth_project_id,
            request_headers=dict(request.headers),
            raw_body=raw_body,
            bl=bl,
        )
        if not preauth_ok:
            _record_provider_ingress_event(
                bl,
                provider_key=normalized_provider_key,
                project_id=str(preauth_project_id) if preauth_project_id else None,
                account_id=str(provider.get("account_id") or "").strip() or provider_account_id,
                workflow_id=None,
                request=request,
                reason="provider_signature_failed",
                detail=preauth_error or _MESSAGE_INVALID_WEBHOOK_SIGNATURE,
                status_code=401,
                payload_summary=base_payload_summary,
            )
            return JSONResponse(
                {"error": preauth_error or _MESSAGE_INVALID_WEBHOOK_SIGNATURE}, status_code=401
            )

        provider_type_key = str(provider.get("provider_type_key") or "").strip()
        ingress_extension = None
        try:
            ingress_extension = resolve_provider_extension_for_runtime(
                bl,
                provider,
                expected_kind="notification_provider",
            )
        except Exception:
            ingress_extension = None

        if provider_type_key == "notification_provider" or provider_supports_ingress_events(
            ingress_extension
        ):
            return handle_notification_provider_webhook(
                callbacks=NotificationWebhookCallbacks(
                    apply_deferred_project_state_updates=_apply_deferred_project_state_updates,
                    as_json_dict=_as_json_dict,
                    provider_request_context_summary=_provider_request_context_summary,
                    record_provider_ingress_event=_record_provider_ingress_event,
                    api_error_response_fn=api_error_response_fn,
                    enforce_provider_webhook_project_secret=enforce_provider_webhook_project_secret,
                    err_webhook_processing_failed_msg=err_webhook_processing_failed_msg,
                    event_command_service_fn=event_command_service_fn,
                    ingress_notification_provider_payload=ingress_notification_provider_payload,
                    ingress_runtime=ingress_runtime,
                    logger_obj=logger_obj,
                    request_scope_fn=request_scope_fn,
                    service_error_response_fn=service_error_response_fn,
                ),
                base_payload_summary=base_payload_summary,
                bl=bl,
                ingress_extension=ingress_extension,
                normalized_provider_key=normalized_provider_key,
                payload=payload,
                provider=provider,
                raw_body=raw_body,
                request=request,
            )

        if provider_type_key != "task_provider":
            _record_provider_ingress_event(
                bl,
                provider_key=normalized_provider_key,
                project_id=None,
                account_id=str(provider.get("account_id") or "").strip() or None,
                workflow_id=None,
                request=request,
                reason="provider_not_supported",
                detail=f"Provider '{normalized_provider_key}' is not a supported webhook provider",
                status_code=400,
                payload_summary=base_payload_summary,
            )
            return JSONResponse(
                {
                    "error": (
                        f"Provider '{normalized_provider_key}' is not a supported webhook provider"
                    )
                },
                status_code=400,
            )

        deferred_project_state_updates: list[dict[str, Any]] = []
        try:
            normalized = normalize_task_provider_payload(
                provider=provider,
                payload=payload,
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

        project_hint = (
            normalized.get("project_id")
            or payload.get("project_id")
            or payload.get("platform_project_id")
            or request.query_params.get("project_id")
        )
        project_id, run_cfg = resolve_task_provider_project_run_config(
            bl,
            provider_key=normalized_provider_key,
            explicit_project_id=project_hint,
            payload=normalized if isinstance(normalized, dict) and normalized else payload,
        )
        provider_trigger_rows = (
            workflow_provider_trigger_repository.list_enabled_provider_triggers_for_provider(
                bl,
                provider_key=normalized_provider_key,
            )
        )
        if project_id and workflow_provider_trigger_repository.has_retained_provider_triggers(
            bl, provider_key=normalized_provider_key, project_id=str(project_id)
        ):
            return handle_workflow_trigger_webhook(
                callbacks=TriggerWebhookCallbacks(
                    apply_deferred_project_state_updates=_apply_deferred_project_state_updates,
                    as_json_dict=_as_json_dict,
                    as_json_list=_as_json_list,
                    build_provider_workflow_inputs=_build_provider_workflow_inputs,
                    claim_provider_webhook_delivery=_claim_provider_webhook_delivery,
                    mark_provider_webhook_delivery_accepted=_mark_provider_webhook_delivery_accepted,
                    mark_provider_webhook_delivery_failed=_mark_provider_webhook_delivery_failed,
                    provider_delivery_table_missing=_provider_delivery_table_missing,
                    provider_request_context_summary=_provider_request_context_summary,
                    provider_trigger_event_name=_provider_trigger_event_name,
                    provider_trigger_matches=_provider_trigger_matches,
                    record_provider_ingress_event=_record_provider_ingress_event,
                    scalar_binding_hints=_scalar_binding_hints,
                    scope_provider_trigger_rows_to_project=_scope_provider_trigger_rows_to_project,
                    api_error_response_fn=api_error_response_fn,
                    enforce_provider_webhook_project_secret=enforce_provider_webhook_project_secret,
                    enqueue_workflow_start=enqueue_workflow_start,
                    err_webhook_processing_failed_msg=err_webhook_processing_failed_msg,
                    event_command_service_fn=event_command_service_fn,
                    logger_obj=logger_obj,
                    workflow_interface_service_cls=workflow_interface_service_cls,
                ),
                bl=bl,
                deferred_project_state_updates=deferred_project_state_updates,
                normalized=normalized,
                normalized_provider_key=normalized_provider_key,
                payload=payload,
                provider=provider,
                provider_trigger_rows=provider_trigger_rows,
                raw_body=raw_body,
                request=request,
            )

        if not project_id or not run_cfg:
            _record_provider_ingress_event(
                bl,
                provider_key=normalized_provider_key,
                project_id=None,
                account_id=str(provider.get("account_id") or "").strip() or None,
                workflow_id=None,
                request=request,
                reason="project_provider_connection_not_found",
                detail="No enabled project connection matched this webhook",
                status_code=404,
                payload_summary=_provider_request_context_summary(
                    payload=payload,
                    request=request,
                    file_rows=_as_json_list(normalized.get("files"))
                    if isinstance(normalized, dict)
                    else [],
                ),
            )
            return JSONResponse(
                {"error": "No enabled project connection matched this webhook"},
                status_code=404,
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
                detail=project_secret_error
                or _MESSAGE_INVALID_WEBHOOK_SIGNATURE_FOR_ROUTING_PROJECT,
                status_code=401,
                payload_summary=_provider_request_context_summary(
                    payload=payload,
                    request=request,
                    file_rows=_as_json_list(normalized.get("files"))
                    if isinstance(normalized, dict)
                    else [],
                ),
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
                payload_summary=_provider_request_context_summary(
                    payload=payload,
                    request=request,
                    file_rows=_as_json_list(normalized.get("files"))
                    if isinstance(normalized, dict)
                    else [],
                ),
            )
            return JSONResponse(
                {"error": state_error or _MESSAGE_PROVIDER_PROJECT_STATE_UPDATE_FAILED},
                status_code=502,
            )

        task_id = str(
            normalized.get("task_id")
            or normalized.get("source_id")
            or payload.get("task_id")
            or payload.get("id")
            or ""
        ).strip()
        if not task_id:
            _record_provider_ingress_event(
                bl,
                provider_key=normalized_provider_key,
                project_id=str(project_id),
                account_id=str(provider.get("account_id") or "").strip() or None,
                workflow_id=None,
                request=request,
                reason="provider_task_id_missing",
                detail="No task_id provided by provider payload",
                status_code=400,
                payload_summary=_provider_request_context_summary(
                    payload=payload,
                    request=request,
                    file_rows=_as_json_list(normalized.get("files"))
                    if isinstance(normalized, dict)
                    else [],
                ),
            )
            return JSONResponse(
                {"error": "No task_id provided by provider payload"},
                status_code=400,
            )

        delivery_key = "|".join((str(project_id), "legacy_task_provider", task_id))
        try:
            delivery_claim = _claim_provider_webhook_delivery(
                bl,
                provider_key=normalized_provider_key,
                delivery_key=delivery_key,
                request_id=task_id,
            )
        except Exception as exc:
            if _provider_delivery_table_missing(exc):
                logger_obj.warning(
                    "provider_webhook_deliveries table is missing; "
                    "reject provider webhook replay protection for %s",
                    normalized_provider_key,
                )
            else:
                logger_obj.warning(
                    "Provider webhook replay reservation failed for %s: %s",
                    normalized_provider_key,
                    exc,
                )
            return JSONResponse(
                {"error": "Provider webhook replay protection is unavailable"},
                status_code=503,
            )
        if not bool(delivery_claim.get("claimed")):
            existing_job_id = str(delivery_claim.get("job_id") or "").strip()
            if existing_job_id:
                return JSONResponse(
                    {
                        "status": "accepted",
                        "deduplicated": True,
                        "job_id": existing_job_id,
                        "provider_key": normalized_provider_key,
                    },
                    status_code=200,
                )
            return JSONResponse(
                {"error": "Provider webhook delivery is already being processed"},
                status_code=409,
            )

        try:
            webhook_files: list[ProviderWebhookFileInput] = []
            for file_item in _as_json_list(normalized.get("files")):
                if isinstance(file_item, ProviderWebhookFileInput):
                    webhook_files.append(file_item)
                elif isinstance(file_item, dict):
                    webhook_files.append(ProviderWebhookFileInput.model_validate(file_item))
            result = event_command_service_fn().ingest_provider_webhook(
                ProviderWebhookIngestInput(
                    provider_key=normalized_provider_key,
                    project_id=str(project_id),
                    project_config=run_cfg,
                    task_id=task_id,
                    source_id=str(normalized.get("source_id") or task_id),
                    title=str(
                        normalized.get("title")
                        or payload.get("task_name")
                        or payload.get("name")
                        or ""
                    ),
                    description=str(
                        normalized.get("description") or payload.get("description") or ""
                    ),
                    task_type=str(normalized.get("task_type") or ""),
                    metadata=_as_json_dict(normalized.get("metadata")),
                    trigger_config=_as_json_dict(normalized.get("trigger_config")),
                    files=webhook_files,
                ),
                scope=request_scope_fn(
                    request,
                    project_id=str(project_id),
                    actor_id=f"{normalized_provider_key}_webhook",
                    default_actor="system",
                ),
            )
        except ServiceError as e:
            with suppress(Exception):
                _mark_provider_webhook_delivery_failed(
                    bl,
                    provider_key=normalized_provider_key,
                    delivery_key=delivery_key,
                    request_id=task_id,
                    error_text=str(getattr(e, "message", e) or err_webhook_processing_failed_msg),
                )
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
                workflow_id=(
                    str(
                        _as_json_dict(normalized.get("trigger_config")).get("workflow_id") or ""
                    ).strip()
                    or None
                ),
                request=request,
                reason="provider_webhook_ingest_failed",
                detail=detail or err_webhook_processing_failed_msg,
                status_code=status_code,
                payload_summary=_provider_request_context_summary(
                    payload=payload,
                    request=request,
                    file_rows=_as_json_list(normalized.get("files")),
                ),
            )
            return service_error_response_fn(e)
        except Exception as e:
            logger_obj.exception(
                "Provider webhook enqueue failed for provider=%s", normalized_provider_key
            )
            with suppress(Exception):
                _mark_provider_webhook_delivery_failed(
                    bl,
                    provider_key=normalized_provider_key,
                    delivery_key=delivery_key,
                    request_id=task_id,
                    error_text=err_webhook_processing_failed_msg,
                )
            _record_provider_ingress_event(
                bl,
                provider_key=normalized_provider_key,
                project_id=str(project_id),
                account_id=str(provider.get("account_id") or "").strip() or None,
                workflow_id=(
                    str(
                        _as_json_dict(normalized.get("trigger_config")).get("workflow_id") or ""
                    ).strip()
                    or None
                ),
                request=request,
                reason="provider_webhook_ingest_failed",
                detail=err_webhook_processing_failed_msg,
                status_code=500,
                payload_summary=_provider_request_context_summary(
                    payload=payload,
                    request=request,
                    file_rows=_as_json_list(normalized.get("files")),
                ),
            )
            return api_error_response_fn(e)

        payload = result.dump()
        with suppress(Exception):
            _mark_provider_webhook_delivery_accepted(
                bl,
                provider_key=normalized_provider_key,
                delivery_key=delivery_key,
                request_id=task_id,
                job_id=str(payload.get("job_id") or ""),
            )
        return payload

    return router
