"""Thin project-scoped HTTP adapter for extension-owned OAuth configuration."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request, concurrency
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core.application.extensions.oauth_authorization import (
    OAuthAuthorizationService,
    OAuthAuthorizationStart,
)
from core.application.extensions.oauth_callback import OAuthCallbackService
from core.application.extensions.oauth_configuration import (
    OAuthConfigurationSave,
    OAuthConfigurationService,
)
from core.application.extensions.oauth_connection_lifecycle import OAuthConnectionLifecycleService
from core.application.management.errors import (
    ServiceError,
    ServiceNotFoundError,
    ServiceUnavailableError,
    ServiceValidationError,
)
from core.application.management.guards import require_project_access
from core.infrastructure.runtime.extension_oauth_db_adapters import (
    create_extension_oauth_authorization_service,
    create_extension_oauth_callback_service,
    create_extension_oauth_configuration_service,
    create_extension_oauth_connection_lifecycle_service,
)

_MESSAGE_INVALID_OAUTH_AUTHORIZATION_REQUEST = "Invalid OAuth authorization request"


class OAuthConfigurationMutationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    configuration_id: UUID | None = None
    display_name: str = Field(min_length=1, max_length=200)
    values: dict[str, str] = Field(default_factory=dict)
    secret_replacements: dict[str, str] = Field(default_factory=dict)
    expected_revision: int | None = Field(default=None, ge=1)
    confirmation_token: str | None = Field(default=None, max_length=256)

    def command(self) -> OAuthConfigurationSave:
        return OAuthConfigurationSave(
            configuration_id=self.configuration_id,
            display_name=self.display_name,
            values=self.values,
            secret_replacements=self.secret_replacements,
        )


class OAuthConfigurationDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    confirmation_token: str | None = Field(default=None, max_length=256)


class OAuthPostConnectRetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    configuration_id: UUID
    connection_type: str = Field(min_length=1, max_length=128)


class _JSONPairs(list[tuple[str, Any]]):
    pass


class _OAuthRequestTooLarge(Exception):
    pass


_MAX_OAUTH_REQUEST_BYTES = 64 * 1024


async def _read_bounded_request_body(request: Request) -> bytes:
    content_length = str(request.headers.get("content-length") or "").strip()
    if content_length.isdigit() and int(content_length) > _MAX_OAUTH_REQUEST_BYTES:
        raise _OAuthRequestTooLarge()
    cached = getattr(request, "_body", None)
    if isinstance(cached, (bytes, bytearray)):
        if len(cached) > _MAX_OAUTH_REQUEST_BYTES:
            raise _OAuthRequestTooLarge()
        return bytes(cached)
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > _MAX_OAUTH_REQUEST_BYTES:
            raise _OAuthRequestTooLarge()
        chunks.append(chunk)
    body = b"".join(chunks)
    request._body = body
    return body


def build_extension_oauth_callback_handler(logger: Any) -> Callable[..., Any]:
    service = create_extension_oauth_callback_service(logger)

    def handler(*, registration: dict[str, Any], extension: Any = None, **values: Any):
        _ = extension
        registration_scope = dict(registration)
        if not str(registration_scope.get("account_id") or "").strip():
            project_id = str(registration_scope.get("project_id") or "").strip()
            project = logger.get_project(project_id) if project_id else None
            if isinstance(project, dict):
                registration_scope["account_id"] = str(project.get("account_id") or "").strip()
        return service.handle_authorization_callback(
            registration=registration_scope,
            **values,
        )

    return handler


def create_extension_oauth_router(
    *,
    get_logger_only: Callable[[], Any],
    request_scope: Callable[..., Any],
    require_authenticated_user: Callable[..., dict],
    service_error_response: Callable[[Exception], JSONResponse],
    api_error_response: Callable[..., JSONResponse],
    service_factory: Callable[[Any], OAuthConfigurationService] | None = None,
    authorization_service_factory: Callable[[Any], OAuthAuthorizationService] | None = None,
    callback_service_factory: Callable[[Any], OAuthCallbackService] | None = None,
    connection_lifecycle_service_factory: Callable[[Any], OAuthConnectionLifecycleService]
    | None = None,
) -> APIRouter:
    router = APIRouter(tags=["extension-oauth"])
    build_service = service_factory or create_extension_oauth_configuration_service
    build_authorization_service = (
        authorization_service_factory or create_extension_oauth_authorization_service
    )
    build_callback_service = callback_service_factory or create_extension_oauth_callback_service
    build_connection_lifecycle_service = (
        connection_lifecycle_service_factory or create_extension_oauth_connection_lifecycle_service
    )

    def scoped(request: Request, project_id: str, *, operator: bool):
        try:
            require_authenticated_user(
                request,
                required_roles={"operator", "admin"} if operator else None,
            )
        except PermissionError as exc:
            status = 403 if operator else 401
            raise ServiceError(
                "Operator role required" if operator else "Authentication required",
                status_code=status,
            ) from exc
        logger = get_logger_only()
        if logger is None:
            raise ServiceUnavailableError("Database not available")
        project = logger.get_project(project_id) if hasattr(logger, "get_project") else None
        if not isinstance(project, dict):
            raise ServiceNotFoundError("Project not found")
        scope = request_scope(request, project_id=project_id)
        try:
            require_project_access(scope, project)
        except ServiceError as exc:
            raise ServiceNotFoundError("Project not found") from exc
        return logger, scope

    async def invoke(request: Request, operation: Callable[[], Any]):
        try:
            return await concurrency.run_in_threadpool(operation)
        except ServiceError as exc:
            return service_error_response(exc)
        except Exception as exc:
            return api_error_response(exc, request=request)

    def mutation(value: Any) -> OAuthConfigurationMutationRequest:
        try:
            return OAuthConfigurationMutationRequest.model_validate(value)
        except ValidationError as exc:
            raise ServiceValidationError(
                "Invalid OAuth configuration request",
                payload={"error": "invalid_request"},
            ) from exc

    async def oauth_json_body(
        request: Request,
        *,
        preserve_duplicate_intent_error: bool = False,
    ) -> tuple[dict[str, Any] | None, dict[str, str], bool]:
        def reject_nonstandard_constant(_value: str) -> None:
            raise ValueError("non-standard JSON constant")

        try:
            raw = await _read_bounded_request_body(request)
            parsed = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_JSONPairs,
                parse_constant=reject_nonstandard_constant,
            )
        except _OAuthRequestTooLarge:
            return None, {"error": "request_too_large"}, True
        except Exception:
            return None, {"error": "invalid_request"}, False

        def convert(value: Any, *, path: str) -> Any:
            if isinstance(value, _JSONPairs):
                result: dict[str, Any] = {}
                for raw_key, raw_value in value:
                    key = str(raw_key)
                    if key in result:
                        if preserve_duplicate_intent_error and path == "intent_inputs":
                            raise ServiceValidationError(
                                "Duplicate OAuth authorization input",
                                payload={
                                    "error": "duplicate_oauth_intent_input",
                                    "field": key,
                                },
                            )
                        raise ServiceValidationError(
                            _MESSAGE_INVALID_OAUTH_AUTHORIZATION_REQUEST,
                            payload={"error": "invalid_request"},
                        )
                    child_path = key if not path else f"{path}.{key}"
                    result[key] = convert(raw_value, path=child_path)
                return result
            if isinstance(value, list):
                return [convert(item, path=path) for item in value]
            return value

        try:
            converted = convert(parsed, path="")
        except ServiceValidationError as exc:
            return None, {str(key): str(value) for key, value in exc.payload.items()}, False
        return (
            (converted if isinstance(converted, dict) else None),
            {"error": "invalid_request"},
            False,
        )

    def invalid_request_response(payload: dict[str, str] | None = None) -> JSONResponse:
        return service_error_response(
            ServiceValidationError(
                "Invalid OAuth configuration request",
                payload=payload or {"error": "invalid_request"},
            )
        )

    def request_too_large_response() -> JSONResponse:
        return JSONResponse({"error": "request_too_large"}, status_code=413)

    def authorization(value: Any) -> OAuthAuthorizationStart:
        try:
            return OAuthAuthorizationStart.model_validate(value)
        except ValidationError as exc:
            raise ServiceValidationError(
                _MESSAGE_INVALID_OAUTH_AUTHORIZATION_REQUEST,
                payload={"error": "invalid_request"},
            ) from exc

    def retry_request(value: Any) -> OAuthPostConnectRetryRequest:
        try:
            return OAuthPostConnectRetryRequest.model_validate(value)
        except ValidationError as exc:
            raise ServiceValidationError(
                "Invalid OAuth post-connect retry request",
                payload={"error": "invalid_request"},
            ) from exc

    def delete_request(value: Any) -> OAuthConfigurationDeleteRequest:
        try:
            return OAuthConfigurationDeleteRequest.model_validate(value)
        except ValidationError as exc:
            raise ServiceValidationError(
                "Invalid OAuth configuration delete request",
                payload={"error": "invalid_request"},
            ) from exc

    route_prefix = "/api/projects/{project_id}/extensions/{install_id}/oauth/{provider_id}"

    @router.put(route_prefix + "/callback-registration")
    async def ensure_callback_registration(
        project_id: str,
        install_id: str,
        provider_id: str,
        request: Request,
    ):
        def operation():
            logger, scope = scoped(request, project_id, operator=True)
            return (
                build_authorization_service(logger)
                .ensure_redirect_uri(scope, install_id, provider_id)
                .model_dump(mode="json")
            )

        return await invoke(request, operation)

    @router.get(route_prefix + "/setup-status")
    async def provider_setup_status(
        project_id: str,
        install_id: str,
        provider_id: str,
        request: Request,
    ):
        def operation():
            logger, scope = scoped(request, project_id, operator=False)
            return (
                build_authorization_service(logger)
                .provider_setup_status(scope, install_id, provider_id)
                .model_dump(mode="json")
            )

        return await invoke(request, operation)

    @router.post(route_prefix + "/authorization")
    async def start_authorization(
        project_id: str,
        install_id: str,
        provider_id: str,
        request: Request,
    ):
        body, body_error, too_large = await oauth_json_body(
            request,
            preserve_duplicate_intent_error=True,
        )
        if too_large:
            return request_too_large_response()
        if body is None:
            return service_error_response(
                ServiceValidationError(
                    _MESSAGE_INVALID_OAUTH_AUTHORIZATION_REQUEST,
                    payload=body_error,
                )
            )

        def operation():
            logger, scope = scoped(request, project_id, operator=True)
            command = authorization(body)
            return (
                build_authorization_service(logger)
                .start_authorization(scope, install_id, provider_id, command)
                .model_dump(mode="json")
            )

        return await invoke(request, operation)

    @router.post(route_prefix + "/connections/{connection_id}/post-connect/retry")
    async def retry_post_connect(
        project_id: str,
        install_id: str,
        provider_id: str,
        connection_id: str,
        request: Request,
    ):
        body, body_error, too_large = await oauth_json_body(request)
        if too_large:
            return request_too_large_response()
        if body is None:
            return invalid_request_response(body_error)

        def operation():
            logger, scope = scoped(request, project_id, operator=True)
            parsed = retry_request(body)
            install = logger.get_extension_install_for_operator(
                install_id, account_id=scope.account_id
            )
            if not isinstance(install, dict):
                raise ServiceNotFoundError("Extension install not found")
            extension_id = str(install.get("extension_id") or "").strip()
            if not extension_id:
                raise ServiceNotFoundError("Extension install not found")
            return (
                build_callback_service(logger)
                .retry_post_connect(
                    tenant_id=str(scope.tenant_id or ""),
                    account_id=str(scope.account_id or ""),
                    project_id=project_id,
                    extension_install_id=install_id,
                    extension_id=extension_id,
                    provider_id=provider_id,
                    configuration_id=str(parsed.configuration_id),
                    connection_id=connection_id,
                    connection_type=parsed.connection_type,
                )
                .model_dump(mode="json", exclude_none=True)
            )

        return await invoke(request, operation)

    @router.post(
        route_prefix + "/configurations/{configuration_id}/connections/{connection_id}/disconnect"
    )
    async def disconnect_connection(
        project_id: str,
        install_id: str,
        provider_id: str,
        configuration_id: UUID,
        connection_id: str,
        request: Request,
    ):
        def operation():
            logger, scope = scoped(request, project_id, operator=True)
            return build_connection_lifecycle_service(logger).disconnect(
                scope,
                install_id,
                provider_id,
                str(configuration_id),
                connection_id,
            )

        return await invoke(request, operation)

    @router.get(route_prefix + "/configurations")
    async def list_configurations(
        project_id: str, install_id: str, provider_id: str, request: Request
    ):
        def operation():
            logger, scope = scoped(request, project_id, operator=False)
            return [
                row.model_dump(mode="json", exclude_none=True)
                for row in build_service(logger).list_configurations(scope, install_id, provider_id)
            ]

        return await invoke(request, operation)

    @router.post(route_prefix + "/configurations/impact")
    async def preview_configuration_change(
        project_id: str,
        install_id: str,
        provider_id: str,
        request: Request,
    ):
        body, body_error, too_large = await oauth_json_body(request)
        if too_large:
            return request_too_large_response()
        if body is None:
            return invalid_request_response(body_error)

        def operation():
            logger, scope = scoped(request, project_id, operator=True)
            parsed = mutation(body)
            return (
                build_service(logger)
                .preview_configuration_change(
                    scope,
                    install_id,
                    provider_id,
                    parsed.command(),
                    expected_revision=parsed.expected_revision,
                )
                .model_dump(mode="json", exclude_none=True)
            )

        return await invoke(request, operation)

    @router.put(route_prefix + "/configurations")
    async def save_configuration(
        project_id: str,
        install_id: str,
        provider_id: str,
        request: Request,
    ):
        body, body_error, too_large = await oauth_json_body(request)
        if too_large:
            return request_too_large_response()
        if body is None:
            return invalid_request_response(body_error)

        def operation():
            logger, scope = scoped(request, project_id, operator=True)
            parsed = mutation(body)
            return (
                build_service(logger)
                .save_configuration(
                    scope,
                    install_id,
                    provider_id,
                    parsed.command(),
                    expected_revision=parsed.expected_revision,
                    confirmation_token=parsed.confirmation_token,
                )
                .model_dump(mode="json", exclude_none=True)
            )

        return await invoke(request, operation)

    @router.post(route_prefix + "/configurations/{configuration_id}/delete-impact")
    async def preview_configuration_delete(
        project_id: str,
        install_id: str,
        provider_id: str,
        configuration_id: UUID,
        request: Request,
    ):
        body, body_error, too_large = await oauth_json_body(request)
        if too_large:
            return request_too_large_response()
        if body is None:
            return invalid_request_response(body_error)

        def operation():
            logger, scope = scoped(request, project_id, operator=True)
            parsed = delete_request(body)
            return (
                build_service(logger)
                .preview_configuration_delete(
                    scope,
                    install_id,
                    provider_id,
                    str(configuration_id),
                    expected_revision=parsed.expected_revision,
                )
                .model_dump(mode="json")
            )

        return await invoke(request, operation)

    @router.post(route_prefix + "/configurations/{configuration_id}/reconcile-release")
    async def reconcile_configuration_release(
        project_id: str,
        install_id: str,
        provider_id: str,
        configuration_id: UUID,
        request: Request,
    ):
        """Move a configuration left on an earlier extension release to the installed one."""

        def operation():
            logger, scope = scoped(request, project_id, operator=True)
            return (
                build_service(logger)
                .reconcile_release(scope, install_id, provider_id, str(configuration_id))
                .model_dump(mode="json")
            )

        return await invoke(request, operation)

    @router.delete(route_prefix + "/configurations/{configuration_id}")
    async def delete_configuration(
        project_id: str,
        install_id: str,
        provider_id: str,
        configuration_id: UUID,
        request: Request,
    ):
        body, body_error, too_large = await oauth_json_body(request)
        if too_large:
            return request_too_large_response()
        if body is None:
            return invalid_request_response(body_error)

        def operation():
            logger, scope = scoped(request, project_id, operator=True)
            parsed = delete_request(body)
            build_service(logger).delete_configuration(
                scope,
                install_id,
                provider_id,
                str(configuration_id),
                expected_revision=parsed.expected_revision,
                confirmation_token=parsed.confirmation_token,
            )
            return {"deleted": True}

        return await invoke(request, operation)

    return router


__all__ = [
    "OAuthConfigurationDeleteRequest",
    "OAuthConfigurationMutationRequest",
    "OAuthPostConnectRetryRequest",
    "build_extension_oauth_callback_handler",
    "create_extension_oauth_router",
]
