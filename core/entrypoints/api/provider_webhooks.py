# ruff: noqa: F401
# mypy: ignore-errors
"""Provider webhook routes and helper functions extracted from serve.py."""

from __future__ import annotations

import logging
import sys
import uuid
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.extensions.provider_install_binding import (
    resolve_install_bound_provider_extension,
)
from core.application.registry.workflow_contract_service import (
    WorkflowInterfaceService,
)
from core.application.workflows import (
    workflow_entrypoint_repository,
    workflow_provider_trigger_repository,
)
from core.application.workflows.domain_event_runtime import project_account_id
from core.application.workflows.workflow_start_runtime import enqueue_workflow_start
from core.entrypoints.api.provider_webhooks_router import (
    create_provider_webhooks_router as _create_provider_webhooks_router_impl,
)
from core.infrastructure.runtime import entrypoint_db_adapters as _db_adapters


# Runtime-configured dependencies; set by create_provider_webhooks_router().
def _default_env_flag(name: str, default: bool) -> bool:
    return bool(default)


def _default_request_scope(request: Request, **kwargs: Any) -> Any:
    return None


def _default_to_json_obj(value: Any) -> dict[str, Any] | list[Any] | None:
    if isinstance(value, (dict, list)):
        return value
    return None


def _as_json_dict(value: Any) -> dict[str, Any]:
    parsed = _to_json_obj(value)
    return dict(parsed) if isinstance(parsed, dict) else {}


def _as_json_list(value: Any) -> list[Any]:
    parsed = _to_json_obj(value)
    return list(parsed) if isinstance(parsed, list) else []


def _default_get_logger_only() -> Any:
    return None


def _default_service_error_response(exc: Exception, request: Request | None = None) -> JSONResponse:
    return JSONResponse(
        {"error": str(getattr(exc, "message", exc))},
        status_code=int(getattr(exc, "status_code", 500) or 500),
    )


def _default_api_error_response(
    exc: Exception,
    request: Request | None = None,
    *,
    status_code: int = 500,
    fallback_message: str | None = None,
) -> JSONResponse:
    payload = {"error": fallback_message or "Request failed"}
    return JSONResponse(payload, status_code=status_code)


def _default_event_command_service() -> Any:
    return None


_env_flag: Callable[[str, bool], bool] = _default_env_flag
_request_scope: Callable[..., Any] = _default_request_scope
_to_json_obj: Callable[[Any], dict[str, Any] | list[Any] | None] = _default_to_json_obj
_get_logger_only: Callable[[], Any] = _default_get_logger_only
_service_error_response: Callable[..., JSONResponse] = _default_service_error_response
_api_error_response: Callable[..., JSONResponse] = _default_api_error_response
_event_command_service: Callable[[], Any] = _default_event_command_service
_logger: Any = logging.getLogger(__name__)
_err_database_not_configured = "Database not configured"
_err_webhook_processing_failed = "Webhook processing failed"


def _scalar_binding_hints(value: dict[str, Any] | None) -> dict[str, str]:
    hints: dict[str, str] = {}
    for key, raw_value in _as_json_dict(value).items():
        if raw_value is None or raw_value == "":
            continue
        if isinstance(raw_value, (str, int, float, bool)):
            hints[str(key)] = str(raw_value)
    return hints


def _provider_trigger_event_name(normalized: dict[str, Any], payload: dict[str, Any]) -> str:
    trigger_config = _as_json_dict(normalized.get("trigger_config"))
    metadata = _as_json_dict(normalized.get("metadata"))
    return str(
        normalized.get("event_name")
        or trigger_config.get("event_name")
        or trigger_config.get("event")
        or metadata.get("event_name")
        or metadata.get("event")
        or payload.get("event_name")
        or payload.get("event")
        or ""
    ).strip()


def _provider_trigger_matches(
    row: dict[str, Any],
    *,
    event_name: str,
    binding_hints: dict[str, str],
) -> bool:
    configured_event = str(row.get("event_name") or "").strip()
    if configured_event:
        if not event_name:
            return False
        if configured_event != event_name:
            return False
    trigger_config = _as_json_dict(row.get("trigger_config_jsonb"))
    for key, expected in _scalar_binding_hints(trigger_config).items():
        if key in {"event", "event_name"}:
            continue
        actual = str(binding_hints.get(key) or "").strip()
        if not actual or actual != expected:
            return False
    return True


def _build_provider_workflow_inputs(
    *,
    workflow_id: str,
    provider_key: str,
    event_name: str,
    normalized: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    _ = workflow_id
    metadata = _as_json_dict(normalized.get("metadata"))
    trigger_config = _as_json_dict(normalized.get("trigger_config"))
    files = _as_json_list(normalized.get("files"))
    return {
        **payload,
        **normalized,
        **metadata,
        "provider_key": provider_key,
        "event_name": event_name,
        "metadata": metadata,
        "trigger_config": trigger_config,
        "provider_payload": payload,
        "provider_event": normalized,
        "files": files,
    }


def _scope_provider_trigger_rows_to_project(
    bl,
    *,
    provider_key: str,
    normalized: dict[str, Any],
    payload: dict[str, Any],
    request: Request,
    provider_trigger_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str | None, dict | None]:
    project_hint = (
        normalized.get("project_id")
        or payload.get("project_id")
        or payload.get("platform_project_id")
        or request.query_params.get("project_id")
    )
    scoped_project_id, scoped_run_cfg = _resolve_task_provider_project_run_config(
        bl,
        provider_key=provider_key,
        explicit_project_id=project_hint,
        payload=normalized,
    )
    if not scoped_project_id or not scoped_run_cfg:
        return [], None, None
    scoped_rows: list[dict[str, Any]] = []
    for row in provider_trigger_rows:
        row_project_id = str(row.get("project_id") or "").strip()
        if row_project_id:
            if row_project_id == str(scoped_project_id):
                scoped_rows.append(row)
            continue
        workflow_id = str(row.get("workflow_id") or "").strip()
        if not workflow_id:
            continue
        workflow_project_id, _ = (
            workflow_entrypoint_repository.resolve_project_run_config_for_workflow(
                bl,
                workflow_id,
            )
        )
        if str(workflow_project_id or "").strip() == str(scoped_project_id):
            scoped_rows.append(row)
    return scoped_rows, str(scoped_project_id), scoped_run_cfg


def _resolve_task_provider_project_run_config(
    bl,
    *,
    provider_key: str,
    explicit_project_id: int | str | None = None,
    payload: dict | None = None,
) -> tuple[str | None, dict | None]:
    normalized_provider_key = str(provider_key or "").strip().lower()
    payload_obj = payload if isinstance(payload, dict) else {}
    # Pass opaque string values from the extension-normalized trigger_config as
    # binding hints.  Extensions that need multi-project disambiguation (e.g. a
    # provider with per-project list/channel bindings) emit the relevant field in
    # their trigger_config; resolve_provider_project_run_config matches it against
    # project connection configs without knowing provider-specific semantics.
    trigger_config = _as_json_dict(payload_obj.get("trigger_config"))
    binding_hints = {
        k: str(v)
        for k, v in trigger_config.items()
        if isinstance(v, (str, int)) and v != "" and v is not None
    }
    project_id, run_cfg = _db_adapters.resolve_provider_project_run_config(
        bl,
        provider_key=normalized_provider_key,
        provider_type_key="task_provider",
        explicit_project_id=explicit_project_id,
        binding_hints=binding_hints,
    )
    if not project_id or not run_cfg:
        return None, None
    return project_id, run_cfg


def _resolve_provider_extension_for_runtime(
    bl,
    provider: dict,
    *,
    expected_kind: str,
) -> dict:
    if not hasattr(bl, "get_extension"):
        raise RuntimeError("Extension runtime is not available")
    normalized_provider_key = str(provider.get("provider_key") or "").strip().lower()
    provider_cfg = _as_json_dict(provider.get("config"))
    extension_key = str(
        provider_cfg.get("extension_key")
        or provider.get("integration_key")
        or normalized_provider_key
        or ""
    ).strip()
    if not extension_key:
        raise ValueError("Provider config is missing extension_key")
    extension = bl.get_extension(extension_key)
    if not extension:
        raise ValueError(f"Extension '{extension_key}' not found")
    if not bool(extension.get("enabled")):
        raise ValueError(f"Extension '{extension_key}' is disabled")
    kind = str(extension.get("kind") or "").strip()
    if expected_kind and kind != expected_kind:
        raise ValueError(
            f"Extension '{extension_key}' kind '{kind}' does not match expected '{expected_kind}'"
        )
    return extension


def _record_provider_ingress_event(
    bl,
    *,
    provider_key: str,
    project_id: str | None,
    account_id: str | None,
    workflow_id: str | None,
    request: Request,
    reason: str,
    detail: str,
    status_code: int,
    event_name: str = "",
    trigger_id: str = "",
    existing_job_id: str = "",
    payload_summary: dict[str, Any] | None = None,
    validation_errors: list[dict[str, Any]] | None = None,
) -> None:
    if not hasattr(bl, "record_domain_event"):
        return
    request_id = (
        str(
            request.headers.get("x-request-id") or request.headers.get("x-correlation-id") or ""
        ).strip()
        or f"provider-webhook-{uuid.uuid4().hex}"
    )
    try:
        bl.record_domain_event(
            event_id=f"provider-ingress:{provider_key}:{request_id}:{reason}:{uuid.uuid4().hex}",
            event_type=(
                "workflow.ingress.duplicate"
                if reason == "duplicate_idempotency_key"
                else "workflow.ingress.rejected"
            ),
            account_id=str(account_id or "").strip()
            or project_account_id(bl, project_id)
            or "acc_default",
            project_id=str(project_id or "").strip() or None,
            workflow_id=str(workflow_id or "").strip() or None,
            job_id=str(existing_job_id or "").strip() or None,
            severity="warning" if existing_job_id else "error",
            summary=detail,
            payload={
                "channel": "provider_webhook",
                "trigger_family": "provider_trigger",
                "provider_key": provider_key,
                "request_id": request_id,
                "reason": reason,
                "event_name": event_name or None,
                "trigger_id": trigger_id or None,
                "no_job_created": not bool(existing_job_id),
                "existing_job_id": existing_job_id or None,
                "http_status": status_code,
                "content_type": str(request.headers.get("content-type") or "")
                .split(";", 1)[0]
                .strip()
                or None,
                "source_ip": str(getattr(request.client, "host", "") or "").strip() or None,
                "request_context_summary": dict(payload_summary or {}),
                "validation_errors": list(validation_errors or []),
            },
            actor={"type": "system", "id": f"provider_webhook:{provider_key}"},
            urls={},
            envelope={},
            occurred_at=None,
            source=f"provider_webhook:{provider_key}",
            aggregate_type="workflow",
            aggregate_id=str(workflow_id or "").strip() or None,
            correlation_id=request_id,
            status="recorded",
        )
    except Exception:
        return


def _provider_request_context_summary(
    *,
    payload: dict[str, Any],
    request: Request,
    file_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    normalized_files = [row for row in (file_rows or []) if isinstance(row, dict)]
    return {
        "body_keys": sorted(str(key) for key in payload),
        "form_keys": [],
        "query_keys": sorted(str(key) for key in request.query_params),
        "file_count": len(normalized_files),
        "file_names": [
            str(item.get("filename") or "").strip()
            for item in normalized_files
            if str(item.get("filename") or "").strip()
        ],
    }


def _provider_supports_ingress_events(extension: dict | None) -> bool:
    payload = extension if isinstance(extension, dict) else {}
    manifest = _as_json_dict(payload.get("compiled_manifest"))
    features = {
        str(item or "").strip()
        for item in (payload.get("features") or manifest.get("features") or [])
        if str(item or "").strip()
    }
    return "ingress_event" in features


def _extension_runtime_id(extension: dict[str, Any] | None) -> str:
    payload = _as_json_dict(extension)
    compiled = _as_json_dict(payload.get("compiled_manifest"))
    return (
        str(payload.get("extension_key") or "").strip()
        or str(payload.get("extension_id") or "").strip()
        or str(compiled.get("extension_id") or "").strip()
    )


def _scope_value(scope: Any, key: str) -> str:
    return str(getattr(scope, key, "") or "").strip()


def _provider_connection_id(provider: dict[str, Any]) -> str:
    project_connection = _as_json_dict(provider.get("project_connection"))
    config = _as_json_dict(provider.get("config"))
    return (
        str(provider.get("connection_id") or "").strip()
        or str(project_connection.get("connection_id") or "").strip()
        or str(config.get("connection_id") or "").strip()
    )


def _invocation_project_id(
    *,
    scope: Any,
    payload: dict[str, Any],
    invocation: dict[str, Any],
    request: Request | None = None,
    project_id_override: str | None = None,
) -> str:
    override = str(project_id_override or "").strip()
    if override:
        return override
    response = _as_json_dict(invocation.get("response"))
    result = _as_json_dict(response.get("result"))
    normalized = _as_json_dict(result.get("normalized"))
    request_project_id = str(request.query_params.get("project_id") if request else "").strip()
    for candidate in (
        _scope_value(scope, "project_id"),
        payload.get("project_id"),
        payload.get("platform_project_id"),
        request_project_id,
        normalized.get("project_id"),
        result.get("project_id"),
        response.get("project_id"),
    ):
        text = str(candidate or "").strip()
        if text:
            return text
    return ""


def _apply_provider_project_state_updates(
    invocation: dict[str, Any],
    *,
    extension: dict[str, Any],
    provider: dict[str, Any],
    payload: dict[str, Any],
    bl: Any,
    scope: Any,
    request: Request | None = None,
    project_id_override: str | None = None,
) -> None:
    if invocation.get("status") != "succeeded":
        return
    try:
        from core.application.extensions.response_envelope import process_project_state_updates

        resolved_extension = extension
        trusted_connection_id = _provider_connection_id(provider)
        trusted_install_id = ""
        response = _as_json_dict(invocation.get("response"))
        result = _as_json_dict(response.get("result"))
        host_mutations = response.get("host_mutations")
        if not isinstance(host_mutations, list):
            host_mutations = result.get("host_mutations")
        if isinstance(host_mutations, list) and host_mutations:
            resolved_extension, connection = resolve_install_bound_provider_extension(
                bl,
                extension=extension,
                connection_id=_provider_connection_id(provider),
                project_id=_invocation_project_id(
                    scope=scope,
                    payload=payload,
                    invocation=invocation,
                    request=request,
                    project_id_override=project_id_override,
                ),
                account_id=(
                    str(extension.get("account_id") or "").strip()
                    or str(provider.get("account_id") or "").strip()
                    or _scope_value(scope, "account_id")
                    or DEFAULT_ACCOUNT_ID
                ),
            )
            trusted_connection_id = str(connection.get("connection_id") or "").strip()
            trusted_install_id = str(connection.get("extension_install_id") or "").strip()
        process_project_state_updates(
            invocation,
            extension=resolved_extension,
            extension_id=_extension_runtime_id(resolved_extension),
            db=bl,
            account_id=(
                str(extension.get("account_id") or "").strip()
                or str(provider.get("account_id") or "").strip()
                or _scope_value(scope, "account_id")
                or DEFAULT_ACCOUNT_ID
            ),
            project_id=_invocation_project_id(
                scope=scope,
                payload=payload,
                invocation=invocation,
                request=request,
                project_id_override=project_id_override,
            ),
            connection_id=trusted_connection_id or None,
            extension_install_id=trusted_install_id or None,
            actor_id=_scope_value(scope, "actor_id") or None,
        )
    except Exception as exc:
        invocation["status"] = "failed"
        invocation["detail"] = "project_state_update_failed"
        invocation["errors"] = [
            *list(invocation.get("errors") or []),
            {"code": "project_state_update_failed", "message": str(exc)},
        ]


def _normalize_task_provider_payload(
    *,
    provider: dict,
    payload: dict,
    bl=None,
    scope=None,
    apply_project_state_updates: bool = True,
    deferred_project_state_updates: list[dict[str, Any]] | None = None,
) -> dict:
    provider_key = str(provider.get("provider_key") or "").strip().lower()
    implementation_kind = str(provider.get("implementation_kind") or "builtin").strip().lower()
    payload_obj = payload if isinstance(payload, dict) else {}
    extension = None

    if bl is not None and hasattr(bl, "get_extension"):
        try:
            extension = _resolve_provider_extension_for_runtime(
                bl, provider, expected_kind="task_provider"
            )
        except Exception:
            extension = None

    # Build audit fields from scope for runtime and logger.
    _audit: dict = {}
    if scope is not None:
        if getattr(scope, "account_id", None):
            _audit["account_id"] = scope.account_id
        if getattr(scope, "workflow_id", None):
            _audit["workflow_id"] = scope.workflow_id
        if getattr(scope, "actor_id", None):
            _audit["actor_id"] = scope.actor_id
        if getattr(scope, "principal_type", None):
            _audit["actor_type"] = scope.principal_type

    if extension is not None:
        from core.infrastructure.extension_runtime.extension_runtime import run_extension_action

        ingress_runtime = _db_adapters.ingress_runtime()

        action_input = dict(payload_obj)
        action_input["provider"] = {
            "provider_key": provider_key,
            "integration_key": str(provider.get("integration_key") or "").strip(),
            "provider_config": _as_json_dict(provider.get("config")),
        }

        invocation = run_extension_action(
            extension,
            action_id="normalize_task_action",
            input_payload=action_input,
            runtime_context=_audit or None,
        )
        if apply_project_state_updates:
            _apply_provider_project_state_updates(
                invocation,
                extension=extension,
                provider=provider,
                payload=payload_obj,
                bl=bl,
                scope=scope,
            )
        elif deferred_project_state_updates is not None:
            deferred_project_state_updates.append(
                {
                    "invocation": invocation,
                    "extension": extension,
                    "provider": provider,
                    "payload": payload_obj,
                    "request": None,
                }
            )
        if hasattr(bl, "log_extension_invocation"):
            bl.log_extension_invocation(
                extension_key=str(extension.get("extension_key") or ""),
                invocation_type="task_provider_normalize",
                status="ok" if invocation.get("status") == "succeeded" else "failed",
                request={"provider_key": provider_key, "payload": payload_obj},
                response=invocation,
                error_text=invocation.get("detail"),
                account_id=_audit.get("account_id"),
                actor_type=_audit.get("actor_type"),
                actor_id=_audit.get("actor_id"),
                workflow_id=_audit.get("workflow_id"),
            )
        if invocation.get("status") != "succeeded":
            raise RuntimeError(
                ingress_runtime.format_task_ingress_error(
                    {"code": "runtime_error", "message": invocation.get("detail")},
                    fallback="Task provider invocation failed",
                )
            )
        response = ingress_runtime.normalize_task_ingress_response(
            _as_json_dict(invocation.get("response")),
            default_error_message="Task provider normalize action returned ok=false",
        )
        if not bool(response.get("ok")):
            errors = response.get("errors") if isinstance(response.get("errors"), list) else []
            first_error = errors[0] if errors else {}
            raise ValueError(
                ingress_runtime.format_task_ingress_error(
                    first_error,
                    fallback="Task provider normalize action returned ok=false",
                )
            )
        result = _as_json_dict(response.get("result"))
        normalized = _as_json_dict(result.get("normalized"))
        if not normalized:
            raise ValueError(
                ingress_runtime.format_task_ingress_error(
                    {
                        "code": "invalid_response",
                        "message": "Task provider response is missing result.normalized payload",
                    },
                    fallback="Task provider returned invalid response",
                )
            )
        return normalized

    if implementation_kind in {"builtin", "extension"}:
        raise ValueError(f"Provider '{provider_key}' requires an enabled task provider extension")
    raise ValueError(f"Unsupported provider implementation_kind '{implementation_kind}'")


def _ingress_notification_provider_payload(
    *,
    provider: dict,
    payload: dict,
    request: Request,
    bl=None,
    scope=None,
    apply_project_state_updates: bool = True,
    deferred_project_state_updates: list[dict[str, Any]] | None = None,
) -> dict:
    from core.infrastructure.extension_runtime.extension_runtime import run_extension_action

    ingress_runtime = _db_adapters.ingress_runtime()

    provider_key = str(provider.get("provider_key") or "").strip().lower()
    extension = _resolve_provider_extension_for_runtime(
        bl, provider, expected_kind="notification_provider"
    )
    project_hint = _invocation_project_id(
        scope=scope,
        payload=payload if isinstance(payload, dict) else {},
        invocation={},
        request=request,
    )
    connection: dict[str, Any] = {}
    if project_hint:
        extension, connection = resolve_install_bound_provider_extension(
            bl,
            extension=extension,
            connection_id=_provider_connection_id(provider),
            project_id=project_hint,
            account_id=(
                _scope_value(scope, "account_id")
                or str(provider.get("account_id") or "").strip()
                or DEFAULT_ACCOUNT_ID
            ),
        )
    compiled_manifest = _as_json_dict(extension.get("compiled_manifest"))
    supported_events_raw = compiled_manifest.get("supported_events") or extension.get(
        "supported_events"
    )
    supported_events = list(supported_events_raw) if isinstance(supported_events_raw, list) else []

    _audit: dict[str, Any] = {}
    if scope is not None:
        if getattr(scope, "account_id", None):
            _audit["account_id"] = scope.account_id
        if getattr(scope, "workflow_id", None):
            _audit["workflow_id"] = scope.workflow_id
        if getattr(scope, "actor_id", None):
            _audit["actor_id"] = scope.actor_id
        if getattr(scope, "principal_type", None):
            _audit["actor_type"] = scope.principal_type

    runtime_context = dict(_audit)
    if project_hint:
        runtime_context.update(
            {
                "logger": bl,
                "project_id": project_hint,
                "extension_install_id": str(connection.get("extension_install_id") or "").strip(),
            }
        )
    invocation = run_extension_action(
        extension,
        action_id="ingress_event",
        input_payload={
            "request": {
                "payload": payload if isinstance(payload, dict) else {},
                "headers": dict(request.headers),
                "query": dict(request.query_params),
                "provider": {
                    "provider_key": provider_key,
                    "integration_key": str(provider.get("integration_key") or "").strip(),
                    "provider_config": _as_json_dict(provider.get("config")),
                },
            },
        },
        context={"project_id": project_hint} if project_hint else None,
        scope=scope,
        runtime_context=runtime_context or None,
    )
    if apply_project_state_updates:
        _apply_provider_project_state_updates(
            invocation,
            extension=extension,
            provider=provider,
            payload=payload if isinstance(payload, dict) else {},
            bl=bl,
            scope=scope,
            request=request,
        )
    elif deferred_project_state_updates is not None:
        deferred_project_state_updates.append(
            {
                "invocation": invocation,
                "extension": extension,
                "provider": provider,
                "payload": payload if isinstance(payload, dict) else {},
                "request": request,
            }
        )
    if hasattr(bl, "log_extension_invocation"):
        bl.log_extension_invocation(
            extension_key=str(extension.get("extension_key") or ""),
            invocation_type="notification_provider_ingress",
            status="ok" if invocation.get("status") == "succeeded" else "failed",
            request={"provider_key": provider_key, "payload": payload},
            response=invocation,
            error_text=invocation.get("detail"),
            account_id=_audit.get("account_id"),
            actor_type=_audit.get("actor_type"),
            actor_id=_audit.get("actor_id"),
            workflow_id=_audit.get("workflow_id"),
        )
    if invocation.get("status") != "succeeded":
        raise RuntimeError(
            str(invocation.get("detail") or "Notification ingress invocation failed")
        )

    normalized = ingress_runtime.normalize_notification_ingress_response(
        _as_json_dict(invocation.get("response")),
        provider_key=provider_key,
        declared_event_types=supported_events,
        default_source=f"webhook:{provider_key}",
    )
    if not bool(normalized.get("ok")):
        detail = str(normalized.get("error") or "Notification provider returned ok=false")
        raise ValueError(detail)

    normalized_result = _as_json_dict(normalized.get("result"))
    normalized_events = _as_json_list(normalized.get("events"))
    return {
        "result": normalized_result,
        "events": normalized_events,
    }


def create_provider_webhooks_router(
    *,
    env_flag: Callable[[str, bool], bool],
    request_scope: Callable[..., Any],
    to_json_obj: Callable[[Any], dict[str, Any] | list[Any] | None],
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
    global _env_flag
    global _request_scope
    global _to_json_obj
    global _get_logger_only
    global _service_error_response
    global _api_error_response
    global _event_command_service
    global _logger
    global _err_database_not_configured
    global _err_webhook_processing_failed

    _env_flag = env_flag
    _request_scope = request_scope
    _to_json_obj = to_json_obj
    _get_logger_only = get_logger_only
    _service_error_response = service_error_response
    _api_error_response = api_error_response
    _event_command_service = event_command_service
    _logger = logger
    _err_database_not_configured = err_database_not_configured
    _err_webhook_processing_failed = err_webhook_processing_failed

    return _create_provider_webhooks_router_impl(
        facade=sys.modules[__name__],
        request_scope=request_scope,
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


__all__ = [
    "_ingress_notification_provider_payload",
    "_normalize_task_provider_payload",
    "_provider_supports_ingress_events",
    "_resolve_provider_extension_for_runtime",
    "_resolve_task_provider_project_run_config",
    "create_provider_webhooks_router",
]
