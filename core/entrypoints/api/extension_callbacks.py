"""Generic extension callback routes."""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Request, concurrency
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from core.application.extensions.callbacks import (
    CallbackBodyTooLarge,
    ExtensionCallbackDispatchService,
    ExtensionCallbackRegistrationService,
    _base_url_from_request,
    callback_base_url_for_provider_setup,
    callback_url,
    find_callback_declaration,
)
from core.application.extensions.oauth_contract import (
    OAUTH_HELP_ANCHOR_MAX_LENGTH,
    OAUTH_HELP_ANCHOR_RE,
    OAUTH_ID_MAX_LENGTH,
    OAUTH_ID_RE,
)
from core.application.management.errors import ServiceForbiddenError
from core.application.management.guards import require_project_access
from core.application.workflows.project_credential_versions import WorkflowCredentialVersionConflict

CALLBACK_REGISTRATION_NOT_FOUND_MESSAGE = "Callback registration not found"
CALLBACK_REGISTRATION_NOT_FOUND_MESSAGE_2 = "callback registration not found"
EXTENSION_CALLBACK_REGISTRATION_STORAGE_IS_MESSAGE = (
    "Extension callback registration storage is unavailable"
)


class ExtensionCallbackRegistrationRequest(BaseModel):
    project_id: str
    connection_id: str | None = None
    enabled: bool = True
    configure: bool = False
    configure_action_id: str = "configure_webhook"


class ExtensionCallbackRotateRequest(BaseModel):
    configure: bool = False
    configure_action_id: str = "configure_webhook"


class ExtensionCallbackConfigureRequest(BaseModel):
    callback_url: str
    callback_secret: str = Field(default="", exclude=True)
    runtime: dict[str, Any] = Field(default_factory=dict)
    action_id: str = "configure_webhook"


class ExtensionCallbackStatusRequest(BaseModel):
    action_id: str = "get_webhook_status"
    runtime: dict[str, Any] = Field(default_factory=dict)


def _error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _redact_setup_payload(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key or "").strip()
            key_lower = key_text.lower()
            if key_lower == "callback_url" or any(
                fragment in key_lower
                for fragment in ("secret", "token", "authorization", "credential")
            ):
                redacted[key] = "<redacted>"
            else:
                redacted[key] = _redact_setup_payload(item)
        return redacted
    if isinstance(value, list):
        return [_redact_setup_payload(item) for item in value]
    return value


def _safe_registration_payload(
    result: Any,
    *,
    include_token: bool = True,
    include_secret: bool = False,
    redact_url: bool = False,
) -> dict[str, Any]:
    payload = result.to_api_dict(
        include_token=include_token,
        include_secret=include_secret,
        redact_url=redact_url,
    )
    payload.pop("callback_token_hash", None)
    payload.pop("auth_config", None)
    return payload


def _setup_payload_detail(payload: Any) -> str:
    entity = _as_dict(payload)
    response = _as_dict(entity.get("response"))
    result = _as_dict(entity.get("result"))
    for errors in (entity.get("errors"), result.get("errors"), response.get("errors")):
        if not isinstance(errors, list):
            continue
        for item in errors:
            if isinstance(item, dict):
                message = str(item.get("message") or item.get("detail") or "").strip()
                if message:
                    return message
            elif isinstance(item, str) and item.strip():
                return item.strip()
    result_error = str(result.get("error") or "").strip()
    if result_error:
        return result_error
    direct_error = str(entity.get("error") or "").strip()
    if direct_error:
        return direct_error
    return ""


def _registration_matches_extension(registration: dict[str, Any], extension_id: str) -> bool:
    registration_extension_id = str(registration.get("extension_id") or "").strip()
    return bool(
        registration_extension_id and registration_extension_id == str(extension_id or "").strip()
    )


#: A signed callback's message is built from the query it arrived with, so the
#: whole query is bounded, not only the fields this layer reads.
_MAX_OAUTH_CALLBACK_QUERY_BYTES = 8192


def _oauth_callback_query(request: Request) -> dict[str, Any] | None:
    """The decoded callback query, or None when it is not worth looking at.

    Every decoded pair is preserved, because a provider that signs its callback
    signs the parameters it sent, and dropping the ones this layer does not read
    would make the signature impossible to reproduce.

    Any repeated key is refused outright. A duplicate makes the canonical message
    ambiguous — two readers can disagree about which value counts — and no OAuth
    provider needs to repeat one, so a superset of the security-critical names is
    both simpler and safer than a list to keep in step.
    """
    raw_query = request.url.query or ""
    if len(raw_query.encode("utf-8", errors="ignore")) > _MAX_OAUTH_CALLBACK_QUERY_BYTES:
        return None
    items = list(request.query_params.multi_items())
    if not items or len(items) > 16:
        return None
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    selected: dict[str, list[str]] = {"state": [], "code": [], "error": []}
    for raw_key, raw_value in items:
        key = str(raw_key or "")
        value = str(raw_value or "")
        try:
            key_size = len(key.encode("utf-8"))
            value_size = len(value.encode("utf-8"))
        except UnicodeError:
            return None
        if not key or key_size > 64 or value_size > 8192:
            return None
        if key in seen:
            return None
        seen.add(key)
        if key in selected:
            selected[key].append(value)
        elif value_size > 2048:
            return None
        pairs.append((key, value))
    if len(selected["state"]) != 1:
        return None
    if len(selected["code"]) + len(selected["error"]) != 1:
        return None
    state = selected["state"][0]
    code = selected["code"][0] if selected["code"] else None
    error = selected["error"][0] if selected["error"] else None
    if (
        not state
        or len(state.encode("utf-8")) > 256
        or (code is not None and (not code or len(code.encode("utf-8")) > 8192))
        or (error is not None and (not error or len(error.encode("utf-8")) > 128))
    ):
        return None
    return {
        "opaque_state": state,
        "code": code,
        "provider_error": error,
        "query_pairs": tuple(pairs),
    }


def _oauth_callback_response(
    value: Any,
    *,
    extension_id: str,
    project_id: str,
) -> Response:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", exclude_none=True)
    payload = _as_dict(value)
    outcome = str(payload.get("outcome") or "").strip()
    if outcome not in {
        "connected",
        "needs_attention",
        "authorization_required",
        "authorization_denied",
        "restart_required",
        "provider_unavailable",
    }:
        outcome = "restart_required"
    help_target = _as_dict(payload.get("help_target"))
    page_id = str(help_target.get("page_id") or "").strip()
    anchor = str(help_target.get("anchor") or "").strip()
    if not page_id or len(page_id) > OAUTH_ID_MAX_LENGTH or OAUTH_ID_RE.fullmatch(page_id) is None:
        page_id = ""
        anchor = ""
    elif (
        not anchor
        or len(anchor) > OAUTH_HELP_ANCHOR_MAX_LENGTH
        or OAUTH_HELP_ANCHOR_RE.fullmatch(anchor) is None
    ):
        anchor = ""
    return_page_id = str(payload.get("return_page_id") or "").strip()
    if (
        not return_page_id
        or len(return_page_id) > OAUTH_ID_MAX_LENGTH
        or OAUTH_ID_RE.fullmatch(return_page_id) is None
    ):
        return_page_id = ""
    connection_id = str(payload.get("connection_id") or "").strip()
    if (
        not connection_id
        or len(connection_id) > 200
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", connection_id) is None
    ):
        connection_id = ""
    connection_preserved = payload.get("connection_preserved") is True
    reason_by_outcome = {
        "authorization_denied": "authorization_cancelled",
        "needs_attention": "provider_configuration_error",
        "authorization_required": "authorization_required",
        "restart_required": "restart_required",
        "provider_unavailable": "provider_unavailable",
    }
    params = {
        "project_id": str(project_id or "").strip(),
        "oauth": "success" if outcome == "connected" else "error",
    }
    if outcome != "connected":
        params["reason"] = reason_by_outcome[outcome]
    preserved_reconnect = bool(outcome != "connected" and connection_id and connection_preserved)
    if preserved_reconnect and return_page_id:
        params["extension_page"] = return_page_id
        if page_id != return_page_id:
            anchor = ""
    elif page_id or return_page_id:
        params["extension_page"] = page_id or return_page_id
    if connection_id and outcome == "connected":
        params["oauth_connection_id"] = connection_id
    elif preserved_reconnect:
        params["oauth_connection_id"] = connection_id
        params["oauth_connection_preserved"] = "1"
    location = (
        f"/extensions/{quote(str(extension_id or '').strip(), safe='')}?{urlencode(params)}"
        + (f"#{quote(anchor, safe='-_.~')}" if anchor else "")
    )
    return RedirectResponse(
        location,
        status_code=303,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


def _is_async_callable(value: Any) -> bool:
    return inspect.iscoroutinefunction(value)


def create_extension_callbacks_router(
    *,
    get_callback_store: Callable[[], Any],
    get_project_repository: Callable[[], Any],
    request_scope: Callable[..., Any],
    logger: Any,
    err_database_not_configured: str,
    oauth_authorization_callback: Callable[..., Any] | None = None,
    event_command_service_factory: Callable[[], Any] | None = None,
) -> APIRouter:
    router = APIRouter(tags=["extension-callbacks"])

    def _authorize_project_access(request: Request, project_id: str) -> JSONResponse | None:
        normalized_project_id = str(project_id or "").strip()
        if not normalized_project_id:
            return _error("project_id is required", 400)
        project_repository = get_project_repository()
        if not project_repository or not hasattr(project_repository, "get_project"):
            return _error("Project storage is not available", 503)
        project = project_repository.get_project(normalized_project_id)
        if not project:
            return _error("Project not found", 404)
        try:
            require_project_access(
                request_scope(request, project_id=normalized_project_id),
                project,
            )
        except ServiceForbiddenError:
            return _error("Access denied", 403)
        return None

    @router.get("/api/extensions/{extension_id}/callbacks")
    async def list_extension_callbacks(request: Request, extension_id: str):
        bl = get_callback_store()
        if not bl:
            return _error(err_database_not_configured, 503)
        if not hasattr(bl, "list_extension_callback_registrations"):
            return _error(EXTENSION_CALLBACK_REGISTRATION_STORAGE_IS_MESSAGE, 503)
        project_id = str(request.query_params.get("project_id") or "").strip() or None
        callback_id = str(request.query_params.get("callback_id") or "").strip() or None
        if not project_id:
            return _error("project_id is required", 400)
        auth_error = _authorize_project_access(request, project_id)
        if auth_error is not None:
            return auth_error
        rows = bl.list_extension_callback_registrations(
            extension_id=extension_id,
            callback_id=callback_id,
            project_id=project_id,
        )
        safe_rows = []
        for row in rows:
            item = dict(row or {})
            item["callback_url"] = (
                f"{_base_url_from_request(request)}/webhook/extensions/"
                f"{str(item.get('extension_id') or '').strip()}/"
                f"{str(item.get('callback_id') or '').strip()}/<redacted>"
            )
            item.pop("callback_token_hash", None)
            item.pop("callback_token", None)
            item.pop("callback_secret", None)
            item.pop("auth_config", None)
            safe_rows.append(item)
        return JSONResponse(jsonable_encoder({"callbacks": safe_rows}))

    @router.post("/api/extensions/{extension_id}/callbacks/{callback_id}")
    async def create_extension_callback_registration(
        request: Request,
        extension_id: str,
        callback_id: str,
        body: ExtensionCallbackRegistrationRequest,
    ):
        bl = get_callback_store()
        if not bl:
            return _error(err_database_not_configured, 503)
        auth_error = _authorize_project_access(request, body.project_id)
        if auth_error is not None:
            return auth_error
        try:
            service = ExtensionCallbackRegistrationService(
                bl,
                project_repository=get_project_repository(),
            )
            result = service.create_registration(
                extension_id=extension_id,
                callback_id=callback_id,
                project_id=body.project_id,
                connection_id=body.connection_id,
                base_url=_base_url_from_request(request),
                enabled=body.enabled,
            )
            if body.configure:
                # Publishing the callback URL (+ token) to an external provider:
                # do not trust request-derived Host/Forwarded origins. Require a
                # configured canonical public base URL (CAND-009 / 049).
                provider_callback_url = callback_url(
                    base_url=callback_base_url_for_provider_setup(request),
                    extension_id=extension_id,
                    callback_id=callback_id,
                    callback_token=result.callback_token,
                )
                scope = request_scope(
                    request,
                    project_id=body.project_id,
                    actor_id=f"{extension_id}_callback_setup",
                    default_actor="system",
                )
                setup_payload, setup_status = service.invoke_registration_action(
                    registration=result.registration,
                    action_id=body.configure_action_id,
                    scope=scope,
                    callback_url_value=provider_callback_url,
                    callback_secret_value=result.callback_secret,
                )
                setup_payload = _as_dict(_redact_setup_payload(setup_payload))
                payload = _safe_registration_payload(
                    result,
                    include_token=False,
                    include_secret=False,
                    redact_url=True,
                )
                payload["configuration"] = setup_payload
                payload["webhook_status"] = _as_dict(setup_payload.get("result"))
                if setup_status >= 400:
                    payload["detail"] = (
                        _setup_payload_detail(setup_payload) or "Webhook configuration failed."
                    )
                return JSONResponse(jsonable_encoder(payload), status_code=setup_status)
        except WorkflowCredentialVersionConflict as exc:
            return _error(str(exc), 409)
        except ValueError as exc:
            return _error(str(exc), 400)
        except Exception:
            logger.exception("Extension callback registration failed")
            return _error("Extension callback registration failed", 500)
        return _safe_registration_payload(result, include_token=True, include_secret=True)

    @router.post("/api/extensions/{extension_id}/callbacks/{registration_id}/rotate")
    async def rotate_extension_callback_registration(
        request: Request,
        extension_id: str,
        registration_id: str,
        body: ExtensionCallbackRotateRequest,
    ):
        bl = get_callback_store()
        if not bl:
            return _error(err_database_not_configured, 503)
        if not hasattr(bl, "get_extension_callback_registration_by_id"):
            return _error(EXTENSION_CALLBACK_REGISTRATION_STORAGE_IS_MESSAGE, 503)
        registration = bl.get_extension_callback_registration_by_id(registration_id)
        if not registration:
            return _error(CALLBACK_REGISTRATION_NOT_FOUND_MESSAGE_2, 404)
        if not _registration_matches_extension(registration, extension_id):
            return _error(CALLBACK_REGISTRATION_NOT_FOUND_MESSAGE_2, 404)
        auth_error = _authorize_project_access(
            request,
            str(registration.get("project_id") or "").strip(),
        )
        if auth_error is not None:
            return auth_error
        try:
            service = ExtensionCallbackRegistrationService(
                bl,
                project_repository=get_project_repository(),
            )
            result = service.rotate_registration_token(
                registration_id=registration_id,
                base_url=_base_url_from_request(request),
                registration=registration,
            )
            if body.configure:
                # See create route: do not trust request-derived origins when
                # publishing the callback URL to an external provider.
                provider_callback_url = callback_url(
                    base_url=callback_base_url_for_provider_setup(request),
                    extension_id=str(result.registration.get("extension_id") or "").strip(),
                    callback_id=str(result.registration.get("callback_id") or "").strip(),
                    callback_token=result.callback_token,
                )
                scope = request_scope(
                    request,
                    project_id=str(result.registration.get("project_id") or "").strip() or None,
                    actor_id=f"{extension_id}_callback_setup",
                    default_actor="system",
                )
                setup_payload, setup_status = service.invoke_registration_action(
                    registration=result.registration,
                    action_id=body.configure_action_id,
                    scope=scope,
                    callback_url_value=provider_callback_url,
                    callback_secret_value=service.secret_for_registration(result.registration),
                )
                setup_payload = _as_dict(_redact_setup_payload(setup_payload))
                payload = _safe_registration_payload(
                    result,
                    include_token=True,
                    include_secret=False,
                )
                payload["configuration"] = setup_payload
                payload["webhook_status"] = _as_dict(setup_payload.get("result"))
                if setup_status >= 400:
                    payload["detail"] = (
                        _setup_payload_detail(setup_payload) or "Webhook configuration failed."
                    )
                return JSONResponse(jsonable_encoder(payload), status_code=setup_status)
        except ValueError:
            return _error(CALLBACK_REGISTRATION_NOT_FOUND_MESSAGE_2, 404)
        except Exception:
            logger.exception("Extension callback token rotation failed")
            return _error("Extension callback token rotation failed", 500)
        return _safe_registration_payload(result, include_token=True, include_secret=False)

    @router.post("/api/extensions/{extension_id}/callbacks/{registration_id}/status")
    async def get_extension_callback_provider_status(
        request: Request,
        extension_id: str,
        registration_id: str,
        body: ExtensionCallbackStatusRequest,
    ):
        bl = get_callback_store()
        if not bl:
            return _error(err_database_not_configured, 503)
        if not hasattr(bl, "get_extension_callback_registration_by_id"):
            return _error(EXTENSION_CALLBACK_REGISTRATION_STORAGE_IS_MESSAGE, 503)
        registration = bl.get_extension_callback_registration_by_id(registration_id)
        if not registration or not _registration_matches_extension(registration, extension_id):
            return _error(CALLBACK_REGISTRATION_NOT_FOUND_MESSAGE_2, 404)
        auth_error = _authorize_project_access(
            request,
            str(registration.get("project_id") or "").strip(),
        )
        if auth_error is not None:
            return auth_error
        scope = request_scope(
            request,
            project_id=str(registration.get("project_id") or "").strip() or None,
            actor_id=f"{extension_id}_callback_setup",
            default_actor="system",
        )
        payload, status_code = ExtensionCallbackRegistrationService(
            bl,
            project_repository=get_project_repository(),
        ).invoke_registration_action(
            registration=registration,
            action_id=body.action_id,
            scope=scope,
            runtime=body.runtime,
        )
        payload = _as_dict(_redact_setup_payload(payload))
        return JSONResponse(
            jsonable_encoder(
                {
                    "ok": payload.get("ok", status_code < 400),
                    "webhook_status": _as_dict(payload.get("result")),
                    "status": payload,
                }
            ),
            status_code=status_code,
        )

    async def _dispatch_extension_callback_webhook(
        request: Request,
        extension_id: str,
        callback_id: str,
        callback_token: str,
    ):
        bl = get_callback_store()
        if not bl:
            return _error(err_database_not_configured, 503)
        service = ExtensionCallbackDispatchService(
            bl,
            event_command_service_factory=event_command_service_factory,
        )
        registration = service.resolve_registration(callback_token)
        if not registration:
            return _error(CALLBACK_REGISTRATION_NOT_FOUND_MESSAGE, 404)
        if str(registration.get("extension_id") or "").strip() != str(extension_id or "").strip():
            return _error(CALLBACK_REGISTRATION_NOT_FOUND_MESSAGE, 404)
        if str(registration.get("callback_id") or "").strip() != str(callback_id or "").strip():
            return _error(CALLBACK_REGISTRATION_NOT_FOUND_MESSAGE, 404)
        if not bool(registration.get("enabled")):
            return _error("Callback registration is disabled", 410)

        extension = service.extension_for_registration(registration)
        if not extension:
            return _error("Extension not found", 404)
        if not bool(extension.get("enabled", True)):
            return _error("Extension is disabled", 409)

        declaration = find_callback_declaration(extension, callback_id)
        if not declaration:
            return _error("Callback declaration not found", 404)
        allowed_methods = {
            str(item or "").strip().upper()
            for item in declaration.get("methods") or []
            if str(item or "").strip()
        }
        if request.method.upper() not in allowed_methods:
            return JSONResponse(
                {"error": "HTTP method is not allowed for this extension callback"},
                status_code=405,
                headers={"Allow": ", ".join(sorted(allowed_methods))},
            )
        if str(declaration.get("purpose") or "").strip().lower() == "oauth_authorization":
            query = _oauth_callback_query(request)
            if query is None:
                return _oauth_callback_response(
                    {"outcome": "restart_required"},
                    extension_id=extension_id,
                    project_id=str(registration.get("project_id") or ""),
                )
            if oauth_authorization_callback is None:
                return _oauth_callback_response(
                    {"outcome": "provider_unavailable"},
                    extension_id=extension_id,
                    project_id=str(registration.get("project_id") or ""),
                )
            try:
                callback_values = {
                    "extension_id": extension_id,
                    "callback_id": callback_id,
                    "registration": registration,
                    "extension": extension,
                    "callback_path": request.url.path,
                    **query,
                }
                if _is_async_callable(oauth_authorization_callback):
                    response = oauth_authorization_callback(**callback_values)
                else:
                    response = await concurrency.run_in_threadpool(
                        oauth_authorization_callback,
                        **callback_values,
                    )
                if inspect.isawaitable(response):
                    response = await response
                return _oauth_callback_response(
                    response,
                    extension_id=extension_id,
                    project_id=str(registration.get("project_id") or ""),
                )
            except Exception:
                logger.warning("Extension OAuth authorization callback failed")
                return _oauth_callback_response(
                    {"outcome": "restart_required"},
                    extension_id=extension_id,
                    project_id=str(registration.get("project_id") or ""),
                )
        try:
            authentication = await service.authenticate(
                request=request,
                registration=registration,
                declaration=declaration,
            )
        except CallbackBodyTooLarge:
            return _error("Request body exceeds size limit", 413)
        if not authentication.accepted:
            return _error(authentication.error, authentication.status_code)

        try:
            envelope = await service.build_request_envelope(
                request=request,
                registration=registration,
                declaration=declaration,
            )
        except CallbackBodyTooLarge:
            return _error("Request body exceeds size limit", 413)
        scope = request_scope(
            request,
            project_id=str(registration.get("project_id") or "").strip() or None,
            actor_id=f"{extension_id}_callback",
            default_actor="system",
        )
        payload, status_code = service.dispatch(
            extension=extension,
            declaration=declaration,
            envelope=envelope,
            scope=scope,
        )
        if hasattr(bl, "mark_extension_callback_registration_seen"):
            try:
                bl.mark_extension_callback_registration_seen(
                    registration_id=str(registration.get("id") or ""),
                    error="" if status_code < 400 else str(payload.get("error") or ""),
                )
            except Exception:
                logger.warning("Failed to update extension callback registration status")
        return JSONResponse(jsonable_encoder(payload), status_code=status_code)

    @router.get(
        "/webhook/extensions/{extension_id}/{callback_id}/{callback_token}",
        operation_id="get_extension_callback_webhook",
    )
    async def get_extension_callback_webhook(
        request: Request,
        extension_id: str,
        callback_id: str,
        callback_token: str,
    ):
        return await _dispatch_extension_callback_webhook(
            request=request,
            extension_id=extension_id,
            callback_id=callback_id,
            callback_token=callback_token,
        )

    @router.post(
        "/webhook/extensions/{extension_id}/{callback_id}/{callback_token}",
        operation_id="post_extension_callback_webhook",
    )
    async def post_extension_callback_webhook(
        request: Request,
        extension_id: str,
        callback_id: str,
        callback_token: str,
    ):
        return await _dispatch_extension_callback_webhook(
            request=request,
            extension_id=extension_id,
            callback_id=callback_id,
            callback_token=callback_token,
        )

    return router


__all__ = [
    "ExtensionCallbackConfigureRequest",
    "ExtensionCallbackRegistrationRequest",
    "ExtensionCallbackRotateRequest",
    "ExtensionCallbackStatusRequest",
    "create_extension_callbacks_router",
]
