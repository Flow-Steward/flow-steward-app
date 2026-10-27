"""HTTP adapter for trigger provider ingress."""

from __future__ import annotations

import hashlib
import json
from contextlib import suppress
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from core.application.workflows.workflow_trigger_guards import provider_trigger_admission
from core.entrypoints.api.provider_webhook_callbacks import (
    _MESSAGE_INVALID_WEBHOOK_SIGNATURE_FOR_ROUTING_PROJECT,
    _MESSAGE_PROVIDER_PROJECT_STATE_UPDATE_FAILED,
    TriggerWebhookCallbacks,
)


def handle_workflow_trigger_webhook(
    *,
    callbacks: TriggerWebhookCallbacks,
    bl: Any,
    deferred_project_state_updates: list[dict[str, Any]],
    normalized: dict[str, Any],
    normalized_provider_key: str,
    payload: dict[str, Any],
    provider: dict[str, Any],
    provider_trigger_rows: list[dict[str, Any]],
    raw_body: bytes,
    request: Request,
) -> Any:
    _apply_deferred_project_state_updates = callbacks.apply_deferred_project_state_updates
    _as_json_dict = callbacks.as_json_dict
    _as_json_list = callbacks.as_json_list
    _build_provider_workflow_inputs = callbacks.build_provider_workflow_inputs
    _claim_provider_webhook_delivery = callbacks.claim_provider_webhook_delivery
    _mark_provider_webhook_delivery_accepted = callbacks.mark_provider_webhook_delivery_accepted
    _mark_provider_webhook_delivery_failed = callbacks.mark_provider_webhook_delivery_failed
    _provider_delivery_table_missing = callbacks.provider_delivery_table_missing
    _provider_request_context_summary = callbacks.provider_request_context_summary
    _provider_trigger_event_name = callbacks.provider_trigger_event_name
    _provider_trigger_matches = callbacks.provider_trigger_matches
    _record_provider_ingress_event = callbacks.record_provider_ingress_event
    _scalar_binding_hints = callbacks.scalar_binding_hints
    _scope_provider_trigger_rows_to_project = callbacks.scope_provider_trigger_rows_to_project
    api_error_response_fn = callbacks.api_error_response_fn
    enforce_provider_webhook_project_secret = callbacks.enforce_provider_webhook_project_secret
    enqueue_workflow_start = callbacks.enqueue_workflow_start
    err_webhook_processing_failed_msg = callbacks.err_webhook_processing_failed_msg
    event_command_service_fn = callbacks.event_command_service_fn
    logger_obj = callbacks.logger_obj
    workflow_interface_service_cls = callbacks.workflow_interface_service_cls

    normalized_obj = _as_json_dict(normalized)
    event_name = _provider_trigger_event_name(normalized_obj, payload)
    binding_hints = _scalar_binding_hints(normalized_obj.get("trigger_config"))
    payload_summary = _provider_request_context_summary(
        payload=payload,
        request=request,
        file_rows=_as_json_list(normalized_obj.get("files")),
    )
    scoped_rows, scoped_project_id, scoped_run_cfg = _scope_provider_trigger_rows_to_project(
        bl,
        provider_key=normalized_provider_key,
        normalized=normalized_obj,
        payload=payload,
        request=request,
        provider_trigger_rows=provider_trigger_rows,
    )
    if not scoped_project_id:
        _record_provider_ingress_event(
            bl,
            provider_key=normalized_provider_key,
            project_id=None,
            account_id=str(provider.get("account_id") or "").strip() or None,
            workflow_id=None,
            request=request,
            reason="project_provider_connection_not_found",
            detail="No enabled project/provider connection matched this webhook",
            status_code=404,
            event_name=event_name,
            payload_summary=payload_summary,
        )
        return JSONResponse(
            {"error": "No enabled project/provider connection matched this webhook"},
            status_code=404,
        )
    matches = [
        row
        for row in scoped_rows
        if _provider_trigger_matches(
            row,
            event_name=event_name,
            binding_hints=binding_hints,
        )
    ]
    if not matches:
        _record_provider_ingress_event(
            bl,
            provider_key=normalized_provider_key,
            project_id=scoped_project_id,
            account_id=str(provider.get("account_id") or "").strip() or None,
            workflow_id=None,
            request=request,
            reason="provider_trigger_not_matched",
            detail="No workflow provider trigger matched this webhook",
            status_code=404,
            event_name=event_name,
            payload_summary=payload_summary,
        )
        return JSONResponse(
            {"error": "No workflow provider trigger matched this webhook"},
            status_code=404,
        )
    if len(matches) > 1:
        _record_provider_ingress_event(
            bl,
            provider_key=normalized_provider_key,
            project_id=scoped_project_id,
            account_id=str(provider.get("account_id") or "").strip() or None,
            workflow_id=None,
            request=request,
            reason="provider_trigger_ambiguous",
            detail="Provider webhook matched multiple workflow triggers",
            status_code=409,
            event_name=event_name,
            payload_summary=payload_summary,
        )
        return JSONResponse(
            {
                "error": "Provider webhook matched multiple workflow triggers",
                "trigger_ids": [
                    str(row.get("trigger_id") or "").strip()
                    for row in matches
                    if str(row.get("trigger_id") or "").strip()
                ],
            },
            status_code=409,
        )
    selected_trigger = matches[0]
    with provider_trigger_admission(bl, selected_trigger) as active:
        if not active:
            return JSONResponse(
                {"error": "No workflow provider trigger matched this webhook"}, status_code=404
            )
        selected_workflow_id = str(selected_trigger.get("workflow_id") or "").strip()
        project_id = str(selected_trigger.get("project_id") or scoped_project_id or "").strip()
        run_cfg = dict(scoped_run_cfg or {})
        if not project_id or not run_cfg:
            _record_provider_ingress_event(
                bl,
                provider_key=normalized_provider_key,
                project_id=scoped_project_id,
                account_id=str(provider.get("account_id") or "").strip() or None,
                workflow_id=selected_workflow_id,
                request=request,
                reason="workflow_project_binding_not_found",
                detail="No enabled project is linked to the matched workflow",
                status_code=404,
                event_name=event_name,
                trigger_id=str(selected_trigger.get("trigger_id") or "").strip(),
                payload_summary=payload_summary,
            )
            return JSONResponse(
                {"error": "No enabled project is linked to the matched workflow"},
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
                project_id=project_id,
                account_id=str(provider.get("account_id") or "").strip() or None,
                workflow_id=selected_workflow_id,
                request=request,
                reason="provider_project_signature_failed",
                detail=project_secret_error
                or _MESSAGE_INVALID_WEBHOOK_SIGNATURE_FOR_ROUTING_PROJECT,
                status_code=401,
                event_name=event_name,
                trigger_id=str(selected_trigger.get("trigger_id") or "").strip(),
                payload_summary=payload_summary,
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
                project_id=project_id,
                account_id=str(provider.get("account_id") or "").strip() or None,
                workflow_id=selected_workflow_id,
                request=request,
                reason="provider_project_state_update_failed",
                detail=state_error or _MESSAGE_PROVIDER_PROJECT_STATE_UPDATE_FAILED,
                status_code=502,
                event_name=event_name,
                trigger_id=str(selected_trigger.get("trigger_id") or "").strip(),
                payload_summary=payload_summary,
            )
            return JSONResponse(
                {"error": state_error or _MESSAGE_PROVIDER_PROJECT_STATE_UPDATE_FAILED},
                status_code=502,
            )
        workflow_inputs = _build_provider_workflow_inputs(
            workflow_id=selected_workflow_id,
            provider_key=normalized_provider_key,
            event_name=event_name,
            normalized=normalized_obj,
            payload=payload,
        )
        input_validation = workflow_interface_service_cls().validate_inputs_for_workflow_id(
            selected_workflow_id,
            workflow_inputs,
            channel="webhook",
        )
        if not input_validation.ok:
            validation_errors = [
                row for row in (input_validation.errors or []) if isinstance(row, dict)
            ]
            _record_provider_ingress_event(
                bl,
                provider_key=normalized_provider_key,
                project_id=project_id,
                account_id=str(provider.get("account_id") or "").strip() or None,
                workflow_id=selected_workflow_id,
                request=request,
                reason="workflow_input_validation_failed",
                detail="Workflow input validation failed",
                status_code=400,
                event_name=event_name,
                trigger_id=str(selected_trigger.get("trigger_id") or "").strip(),
                payload_summary=payload_summary,
                validation_errors=validation_errors,
            )
            return JSONResponse(
                {
                    "error": "Workflow input validation failed",
                    "errors": list(input_validation.errors or []),
                },
                status_code=400,
            )
        if input_validation.interface.get("enabled"):
            workflow_inputs = dict(input_validation.inputs)
        task_id = (
            str(
                normalized_obj.get("source_id")
                or normalized_obj.get("task_id")
                or payload.get("id")
                or payload.get("event_id")
                or ""
            ).strip()
            or hashlib.sha256(raw_body or json.dumps(payload, sort_keys=True).encode()).hexdigest()
        )
        provider_task_id = f"provider-trigger:{normalized_provider_key}:{task_id}"
        delivery_key = "|".join(
            (
                str(project_id),
                selected_workflow_id,
                provider_task_id,
            )
        )
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
        delivery_claimed = bool(delivery_claim.get("claimed"))
        if not delivery_claimed:
            existing_job_id = str(delivery_claim.get("job_id") or "").strip()
            if existing_job_id:
                return JSONResponse(
                    {
                        "status": "accepted",
                        "deduplicated": True,
                        "job_id": existing_job_id,
                        "selected_workflow_id": selected_workflow_id,
                        "workflow_selection_source": "provider_trigger",
                    },
                    status_code=200,
                )
            return JSONResponse(
                {"error": "Provider webhook delivery is already being processed"},
                status_code=409,
            )
        task_summary = str(
            normalized_obj.get("title")
            or payload.get("task_name")
            or selected_trigger.get("display_name")
            or f"{normalized_provider_key} workflow trigger"
        ).strip()
        command_service = event_command_service_fn()
        enqueue_task_fn = getattr(command_service, "_enqueue_task", None)
        if not callable(enqueue_task_fn):
            with suppress(Exception):
                _mark_provider_webhook_delivery_failed(
                    bl,
                    provider_key=normalized_provider_key,
                    delivery_key=delivery_key,
                    request_id=task_id,
                    error_text="workflow_trigger_enqueue_unavailable",
                )
            _record_provider_ingress_event(
                bl,
                provider_key=normalized_provider_key,
                project_id=project_id,
                account_id=str(provider.get("account_id") or "").strip() or None,
                workflow_id=selected_workflow_id,
                request=request,
                reason="workflow_trigger_enqueue_unavailable",
                detail="Workflow trigger enqueue is unavailable",
                status_code=503,
                event_name=event_name,
                trigger_id=str(selected_trigger.get("trigger_id") or "").strip(),
                payload_summary=payload_summary,
            )
            return JSONResponse(
                {"error": "Workflow trigger enqueue is unavailable"},
                status_code=503,
            )
        try:
            from core.application.registry.workflow_secret_defaults import (
                workflow_secret_launch_snapshot,
            )

            job_id = enqueue_workflow_start(
                enqueue_task_fn=enqueue_task_fn,
                task_id=provider_task_id,
                task_summary=task_summary,
                task_source="provider",
                project_id=str(project_id),
                project_name=str(run_cfg.get("project_name") or run_cfg.get("name") or "").strip(),
                project_config=dict(run_cfg or {}),
                selected_workflow_id=selected_workflow_id,
                workflow_selection_source="provider_trigger",
                entry_path="provider_webhook",
                input_contract="direct_inputs",
                source_system=normalized_provider_key,
                source_event_id=task_id,
                source_object_id=task_id,
                inputs=workflow_inputs,
                files=list(workflow_inputs.get("files") or []),
                extra_state={
                    "provider_key": normalized_provider_key,
                    "provider_trigger_id": str(selected_trigger.get("trigger_id") or "").strip(),
                    "provider_trigger_type_id": str(
                        selected_trigger.get("trigger_type_id") or ""
                    ).strip(),
                },
                workflow_secret_ref_fingerprints=workflow_secret_launch_snapshot(
                    input_validation.interface,
                    inputs=workflow_inputs,
                ),
                job_meta={
                    "provider_key": normalized_provider_key,
                    "provider_trigger_id": str(selected_trigger.get("trigger_id") or "").strip(),
                    "event_name": event_name,
                    "invocation": {
                        "channel": "provider_webhook",
                        "trigger_family": "provider_trigger",
                        "entry_path": "provider_webhook",
                        "source_system": normalized_provider_key,
                        "request_id": task_id,
                        "content_type": str(request.headers.get("content-type") or "")
                        .split(";", 1)[0]
                        .strip(),
                        "source_ip": str(getattr(request.client, "host", "") or "").strip() or None,
                        "trigger_id": str(selected_trigger.get("trigger_id") or "").strip(),
                        "received_at": str(normalized_obj.get("received_at") or "").strip() or None,
                        "request_context_summary": payload_summary,
                    },
                },
            )
        except Exception as exc:
            with suppress(Exception):
                _mark_provider_webhook_delivery_failed(
                    bl,
                    provider_key=normalized_provider_key,
                    delivery_key=delivery_key,
                    request_id=task_id,
                    error_text=f"enqueue_failed: {exc}",
                )
            _record_provider_ingress_event(
                bl,
                provider_key=normalized_provider_key,
                project_id=project_id,
                account_id=str(provider.get("account_id") or "").strip() or None,
                workflow_id=selected_workflow_id,
                request=request,
                reason="workflow_trigger_enqueue_failed",
                detail="Workflow trigger enqueue failed before job creation",
                status_code=502,
                event_name=event_name,
                trigger_id=str(selected_trigger.get("trigger_id") or "").strip(),
                payload_summary=payload_summary,
            )
            return api_error_response_fn(
                exc,
                status_code=502,
                fallback_message=err_webhook_processing_failed_msg,
            )
        with suppress(Exception):
            _mark_provider_webhook_delivery_accepted(
                bl,
                provider_key=normalized_provider_key,
                delivery_key=delivery_key,
                request_id=task_id,
                job_id=str(job_id),
            )
        return {
            "status": "accepted",
            "job_id": str(job_id),
            "provider_key": normalized_provider_key,
            "selected_workflow_id": selected_workflow_id,
            "workflow_selection_source": "provider_trigger",
            "provider_trigger_id": str(selected_trigger.get("trigger_id") or "").strip(),
        }
