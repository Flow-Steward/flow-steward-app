"""Reusable request/transport helpers extracted from serve.py."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import date, datetime
from datetime import time as datetime_time
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi.responses import FileResponse, JSONResponse, RedirectResponse


def template_json_default(value: Any) -> Any:
    if isinstance(value, Path | uuid.UUID):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime | date | datetime_time):
        return value.isoformat()
    if isinstance(value, set):
        return list(value)
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        try:
            return isoformat()
        except Exception:
            pass
    return str(value)


def request_correlation_id(request, *, fallback: str = "", new_id_fn) -> str:
    if request is not None:
        state = getattr(request, "state", None)
        state_corr = str(getattr(state, "correlation_id", "") or "").strip()
        if state_corr:
            return state_corr
        header_corr = (
            str(request.headers.get("x-correlation-id") or "").strip()
            or str(request.headers.get("x-request-id") or "").strip()
            or str(request.headers.get("x-amzn-trace-id") or "").strip()
        )
        if header_corr:
            return header_corr
    normalized_fallback = str(fallback or "").strip()
    if normalized_fallback:
        return normalized_fallback
    return str(new_id_fn())


def to_json_obj(value):
    """Normalize JSONB-ish values into Python dict/list when possible."""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


def error_code_for_status(status_code: int) -> str:
    code = int(status_code or 500)
    if code == 400:
        return "invalid_request"
    if code == 401:
        return "unauthorized"
    if code == 403:
        return "forbidden"
    if code == 404:
        return "not_found"
    if code == 409:
        return "conflict"
    if code == 422:
        return "validation_error"
    if code == 429:
        return "rate_limited"
    if code == 503:
        return "service_unavailable"
    if code >= 500:
        return "internal_error"
    return "request_error"


def path_matches_any_pattern(path: str, *, patterns) -> bool:
    path_text = str(path or "").strip()
    if not path_text:
        return False
    return any(pattern.match(path_text) for pattern in patterns)


def build_path_pattern_matcher(*, patterns):
    def _matcher(path: str) -> bool:
        return path_matches_any_pattern(path, patterns=patterns)

    return _matcher


def error_payload(
    *,
    status_code: int,
    message: str,
    details: dict[str, Any] | None = None,
    correlation_id: str = "",
    code: str = "",
    extra: dict[str, Any] | None = None,
    err_request_failed: str,
    default_correlation_id_fn,
) -> dict[str, Any]:
    payload = {
        "status": "error",
        "code": str(code or error_code_for_status(status_code)).strip()
        or error_code_for_status(status_code),
        "message": str(message or err_request_failed).strip() or err_request_failed,
        "details": dict(details or {}),
        "correlation_id": str(correlation_id or "").strip() or str(default_correlation_id_fn()),
    }
    payload["error"] = payload["message"]
    if isinstance(extra, dict):
        for key, value in extra.items():
            if key in payload:
                continue
            payload[key] = value
    return payload


def build_error_payload_factory(*, err_request_failed: str, default_correlation_id_fn):
    def _payload(
        *,
        status_code: int,
        message: str,
        details: dict[str, Any] | None = None,
        correlation_id: str = "",
        code: str = "",
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return error_payload(
            status_code=status_code,
            message=message,
            details=details,
            correlation_id=correlation_id,
            code=code,
            extra=extra,
            err_request_failed=err_request_failed,
            default_correlation_id_fn=default_correlation_id_fn,
        )

    return _payload


def json_safe_payload(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, default=template_json_default))
    except Exception:
        return value


def contains_nul_text(value: Any) -> bool:
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any(contains_nul_text(key) or contains_nul_text(item) for key, item in value.items())
    if isinstance(value, list | tuple):
        return any(contains_nul_text(item) for item in value)
    return False


async def request_nul_text_location(request: Any) -> list[str] | None:
    path = str(request.scope.get("path") or "")
    if contains_nul_text(path):
        return ["path"]
    if any(
        contains_nul_text(key) or contains_nul_text(value)
        for key, value in request.query_params.multi_items()
    ):
        return ["query"]

    media_type = str(request.headers.get("content-type") or "").partition(";")[0].strip().lower()
    if media_type != "application/json" and not media_type.endswith("+json"):
        return None
    body = await request.body()
    if not body:
        return None
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return ["body"] if contains_nul_text(payload) else None


def service_error_response(
    exc,
    request=None,
    *,
    error_payload,
    request_correlation_id,
    error_code_for_status,
    err_request_failed: str,
) -> JSONResponse:
    status_code = int(getattr(exc, "status_code", 500) or 500)
    raw_payload = getattr(exc, "payload", None)
    payload_dict = dict(raw_payload) if isinstance(raw_payload, dict) else {}
    message = (
        str(payload_dict.get("message") or "").strip()
        or str(payload_dict.get("error") or "").strip()
        or str(getattr(exc, "message", "") or "").strip()
        or str(exc)
    )
    details_raw = payload_dict.get("details")
    details = dict(details_raw) if isinstance(details_raw, dict) else {}
    extra = {
        key: value
        for key, value in payload_dict.items()
        if key not in {"code", "message", "error", "details", "correlation_id"}
    }
    if extra:
        details.setdefault("context", extra)

    payload = error_payload(
        status_code=status_code,
        message=message or err_request_failed,
        details=details,
        correlation_id=request_correlation_id(
            request,
            fallback=str(payload_dict.get("correlation_id") or "").strip(),
        ),
        code=str(payload_dict.get("code") or "").strip() or error_code_for_status(status_code),
        extra=extra,
    )
    return JSONResponse(json_safe_payload(payload), status_code=status_code)


def api_error_response(
    exc: Exception,
    *,
    request=None,
    status_code: int = 500,
    fallback_message: str = "Internal server error",
    code: str = "",
    logger,
    error_payload,
    request_correlation_id,
) -> JSONResponse:
    logger.error("API error (status=%d): %s", status_code, exc, exc_info=True)
    payload = error_payload(
        status_code=status_code,
        message=fallback_message,
        details={},
        correlation_id=request_correlation_id(request),
        code=code,
    )
    return JSONResponse(payload, status_code=status_code)


def build_api_error_response_factory(*, logger, error_payload, request_correlation_id):
    def _response(
        exc: Exception,
        *,
        request=None,
        status_code: int = 500,
        fallback_message: str = "Internal server error",
        code: str = "",
    ) -> JSONResponse:
        return api_error_response(
            exc,
            request=request,
            status_code=status_code,
            fallback_message=fallback_message,
            code=code,
            logger=logger,
            error_payload=error_payload,
            request_correlation_id=request_correlation_id,
        )

    return _response


def redirect_with_flash(
    url: str,
    *,
    flash: str | None = None,
    flash_type: str = "info",
    redirect_response_cls=RedirectResponse,
):
    if flash:
        from urllib.parse import urlencode

        params = urlencode({"flash": flash, "flash_type": flash_type})
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}{params}"
    return redirect_response_cls(url=url, status_code=303)


def build_static_versioned_asset_handler(*, static_root):
    def _handler(asset_version: str, file_path: str):
        _ = asset_version
        normalized = str(file_path or "").lstrip("/")
        target = (static_root / normalized).resolve()
        try:
            target.relative_to(static_root)
        except ValueError:
            return JSONResponse({"detail": "Static asset not found"}, status_code=404)
        if not target.is_file():
            return JSONResponse({"detail": "Static asset not found"}, status_code=404)
        response = FileResponse(target)
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

    return _handler


def build_request_validation_exception_handler(
    *,
    validation_errors_without_url_fn,
    error_payload_fn,
    request_correlation_id_fn,
):
    def _handler(request, exc):
        errors = validation_errors_without_url_fn(exc)
        try:
            errors = json.loads(json.dumps(errors, default=str))
        except Exception:
            errors = [{"msg": "Validation error"}]
        payload = error_payload_fn(
            status_code=422,
            code="validation_error",
            message="Request validation failed",
            details={"errors": errors},
            correlation_id=request_correlation_id_fn(request),
        )
        return JSONResponse(payload, status_code=422)

    return _handler


def build_static_asset_cache_middleware(*, static_cache_max_age_seconds: int):
    async def _middleware(request, call_next):
        response = await call_next(request)
        path_text = str(request.url.path or "").strip()
        if path_text.startswith("/static/") and "cache-control" not in response.headers:
            if path_text.startswith("/static/platform-kit/"):
                # Platform Kit modules use stable URLs rather than content hashes.
                # Revalidate them so a rebuilt app cannot keep executing a stale
                # custom-element contract for the stale-while-revalidate window.
                cache_value = "no-cache"
            else:
                cache_value = f"public, max-age={static_cache_max_age_seconds}"
                if static_cache_max_age_seconds > 0:
                    cache_value += ", stale-while-revalidate=86400"
            response.headers["Cache-Control"] = cache_value
        return response

    return _middleware


def view_payload_dict(view: Any) -> dict[str, Any]:
    if isinstance(view, dict):
        return dict(view)
    for attr in ("model_dump", "dump"):
        fn = getattr(view, attr, None)
        if not callable(fn):
            continue
        try:
            payload = fn()
        except Exception:
            continue
        if isinstance(payload, dict):
            return dict(payload)
    return {}


def route_endpoint(router, path: str, method: str):
    normalized_method = str(method or "").strip().upper()
    for route in getattr(router, "routes", []):
        route_path = getattr(route, "path", None)
        route_methods = getattr(route, "methods", None) or set()
        if route_path == path and normalized_method in route_methods:
            return route.endpoint
    raise RuntimeError(f"Auth users route endpoint not found: {method} {path}")


def route_registered(router, path: str, method: str) -> bool:
    method_upper = str(method or "").upper()
    path_text = str(path or "").strip()
    if not path_text:
        return False

    try:
        from starlette.routing import Match as _MatchClass

        starlette_match_cls = _MatchClass
    except Exception:
        starlette_match_cls = None

    scope = {
        "type": "http",
        "path": path_text,
        "method": method_upper or "GET",
        "root_path": "",
        "scheme": "http",
        "query_string": b"",
        "headers": [],
    }
    for route in getattr(router, "routes", []):
        route_path = str(getattr(route, "path", "") or "")
        methods = getattr(route, "methods", set()) or set()
        if route_path == path_text and method_upper in methods:
            return True
        matches = getattr(route, "matches", None)
        if not callable(matches) or starlette_match_cls is None:
            continue
        try:
            match, _child_scope = matches(scope)
        except Exception:
            continue
        if match == starlette_match_cls.FULL:
            return True
    return False


def secret_storage_config_error(exc: Exception) -> bool:
    text = str(exc or "").strip().lower()
    if not text:
        return False
    return "encrypted secret storage" in text and (
        "fs_secret_key" in text or "aesgcm" in text or "cryptography" in text
    )


# AC17-R94: pydantic puts the rejected value in `input`, and this handler serves
# every validated route, so a failed login or a too-short password answered the
# caller with their own password in clear text. Dropping `url` was never enough:
# the leak is the echoed value, not the documentation link. Nothing needs the
# submitted value returned to the sender -- they are the one who sent it -- and a
# handler this general cannot know which field is a secret, so none are echoed.
_VALIDATION_ERROR_DROPPED_KEYS = frozenset({"url", "input"})


def validation_errors_without_url(exc, *, err_validation: str) -> list[dict[str, Any]]:
    try:
        errors = exc.errors()
    except Exception:
        return [{"msg": err_validation}]
    if not isinstance(errors, list):
        return [{"msg": err_validation}]
    normalized: list[dict[str, Any]] = []
    for item in errors:
        if isinstance(item, dict):
            normalized.append(
                {
                    key: value
                    for key, value in item.items()
                    if key not in _VALIDATION_ERROR_DROPPED_KEYS
                }
            )
        else:
            normalized.append({"msg": str(item)})
    return normalized or [{"msg": err_validation}]


def service_validation_message(exc, *, validation_errors_without_url_fn) -> str:
    parts: list[str] = []
    for item in validation_errors_without_url_fn(exc):
        location = ".".join(str(part) for part in (item.get("loc") or []))
        message = str(item.get("msg") or "Invalid input").strip()
        parts.append(f"{location}: {message}" if location else message)
    return "; ".join(parts) or "Invalid input"


async def ws_authenticate(
    websocket,
    *,
    local_auth_enabled_fn,
    external_auth_enabled: bool,
    auth_cookie_name: str,
    get_logger_only_fn,
    auth_tables_available_fn,
    get_user_by_session_token_fn,
    normalize_live_principal_fn,
    resolved_principal_type,
    err_authentication_required: str,
    sleep_fn=asyncio.sleep,
) -> dict | None:
    """Authenticate a WebSocket connection using cookie or bearer token."""
    if not (local_auth_enabled_fn() or external_auth_enabled):
        return None
    await sleep_fn(0)

    token = str(websocket.query_params.get("token") or "").strip()
    if not token:
        token = str(websocket.cookies.get(auth_cookie_name) or "").strip()
    if not token:
        raise PermissionError(err_authentication_required)

    logger_ref = get_logger_only_fn()
    if not logger_ref or not auth_tables_available_fn(logger_ref):
        raise PermissionError("Auth service unavailable")

    principal = normalize_live_principal_fn(
        get_user_by_session_token_fn(logger_ref, token),
        default_auth_source="local",
    )
    if not isinstance(principal, resolved_principal_type) or not bool(principal.is_active):
        raise PermissionError("Invalid or expired session")
    return principal.model_dump()


async def build_runtime_payload_for_api(
    job: dict,
    bl,
    *,
    build_runtime_payload,
    build_runtime_payload_fallback,
    timeout_seconds: float,
    logger,
    asyncio_module=asyncio,
) -> dict:
    try:
        return await asyncio_module.wait_for(
            asyncio_module.to_thread(build_runtime_payload, job, bl),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        logger.warning(
            "api_job_status runtime payload timed out for job_id=%s thread_id=%s",
            job.get("id"),
            job.get("thread_id"),
        )
    except Exception as exc:
        logger.warning(
            "api_job_status runtime payload failed for job_id=%s: %s",
            job.get("id"),
            exc,
        )
    return build_runtime_payload_fallback(job)


def compute_wait_reason(
    job: dict,
    runtime_state: dict,
    *,
    retryable_interrupted_step_status: str,
    approval_payload: dict | None = None,
    queue_delivery: dict | None = None,
) -> str:
    status = job.get("status") or ""
    queue_delivery_payload = queue_delivery if isinstance(queue_delivery, dict) else {}
    queue_health_state = str(queue_delivery_payload.get("health_state") or "").strip().lower()
    if status == "pending":
        if queue_health_state == "retrying_delivery":
            retry_count = int(queue_delivery_payload.get("retry_count") or 0)
            retry_max_attempts = int(queue_delivery_payload.get("retry_max_attempts") or 0)
            if retry_max_attempts > 0:
                return (
                    f"Retrying RabbitMQ delivery ({retry_count}/{retry_max_attempts} retries used)"
                )
            return "Retrying RabbitMQ delivery before execution can begin"
        if queue_health_state == "dead_lettered":
            return "RabbitMQ delivery retries were exhausted — operator intervention is required"
        queue_name = job.get("queue_name") or "worker queue"
        return f"Queued in {queue_name}"
    if status == "awaiting_approval":
        if isinstance(approval_payload, dict):
            stage = approval_payload.get("stage") or "Review"
            msg = approval_payload.get("message") or "Awaiting user approval"
            return f"{stage}: {msg}"
        if runtime_state.get("approval_context"):
            return str(runtime_state.get("approval_context"))
        return "Awaiting user approval"
    if status == "awaiting_timer":
        meta = to_json_obj(job.get("meta"))
        wait = meta.get("workflow_wait") if isinstance(meta, dict) else None
        due_at = str(wait.get("due_at") or "").strip() if isinstance(wait, dict) else ""
        if due_at:
            return f"Paused on a Wait step until {due_at}"
        return "Paused on a Wait step until it is due"
    if status == "awaiting_child":
        return "Waiting for a child workflow to finish"
    if status == retryable_interrupted_step_status:
        return "Interrupted in-flight step detected — manual Retry Step is required"
    if status == "in_progress":
        agent = job.get("current_agent") or runtime_state.get("active_agent")
        step_id = job.get("current_step_id") or runtime_state.get("current_step_id")
        if agent:
            return f"Agent '{agent}' is executing"
        if step_id:
            return f"Running step '{step_id}'"
        if job.get("worker_id"):
            return f"Worker '{job.get('worker_id')}' processing task"
        return "Waiting for worker pickup"
    if status == "stuck":
        return "Marked stuck by watchdog"
    if status == "orphaned":
        return "Marked orphaned by watchdog"
    if queue_health_state == "dead_lettered":
        return "RabbitMQ delivery retries were exhausted — operator intervention is required"
    if queue_health_state == "execution_failed_after_delivery":
        return "Workflow execution failed after RabbitMQ delivery succeeded"
    return ""
