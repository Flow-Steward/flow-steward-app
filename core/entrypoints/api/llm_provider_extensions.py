"""Admin HTTP adapter for account-scoped LLM provider extension workflows."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, concurrency

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.settings.llm_provider_extension_requests import (
    RefreshLlmProviderModelsRequest,
    SaveLlmProviderCredentialsRequest,
)
from core.application.settings.llm_provider_extension_use_cases import (
    LlmProviderExtensionError,
    LlmProviderExtensionUseCases,
)


def create_api_llm_provider_extensions_router(
    *,
    get_logger_only,
    require_authenticated_user,
    auth_admin_roles,
    api_error_response,
) -> APIRouter:
    router = APIRouter(
        tags=["llm-provider-extensions"],
        responses={
            403: {"description": "Administrator access required"},
            404: {"description": "Provider extension resource not found"},
        },
    )

    def account_id(request: Request) -> str:
        auth_user = getattr(request.state, "auth_user", None)
        if isinstance(auth_user, dict):
            value = str(auth_user.get("account_id") or "").strip()
            if value:
                return value
        return DEFAULT_ACCOUNT_ID

    def require_admin(request: Request) -> None:
        try:
            require_authenticated_user(request, required_roles=auth_admin_roles)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="Admin role required") from exc

    def use_cases() -> LlmProviderExtensionUseCases:
        try:
            logger = get_logger_only()
        except Exception:
            logger = None
        conn = getattr(logger, "conn", None) if logger is not None else None
        if conn is None:
            raise HTTPException(status_code=503, detail="Database not available")
        return LlmProviderExtensionUseCases(conn, get_logger_only=get_logger_only)

    async def invoke(request: Request, operation):
        try:
            return await operation() if callable(operation) else operation
        except LlmProviderExtensionError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        except HTTPException:
            raise
        except Exception as exc:
            return api_error_response(
                exc, request=request, fallback_message="Internal server error"
            )

    async def run_use_case(method_name: str, /, *args, **kwargs):
        def blocking_operation():
            return getattr(use_cases(), method_name)(*args, **kwargs)

        return await concurrency.run_in_threadpool(blocking_operation)

    @router.get(
        "/api/llm-provider-extensions",
        responses={
            403: {"description": "Administrator access required"},
            503: {"description": "Database not available"},
        },
    )
    async def list_extensions(request: Request):
        require_admin(request)

        async def operation():
            return await run_use_case("list_sources", account_id(request))

        return await invoke(request, operation)

    @router.post(
        "/api/llm-provider-extensions/{install_id}/credentials",
        responses={
            403: {"description": "Administrator access required"},
            503: {"description": "Database not available"},
        },
    )
    async def save_credentials(
        install_id: str, body: SaveLlmProviderCredentialsRequest, request: Request
    ):
        require_admin(request)

        async def operation():
            return await run_use_case(
                "save_credentials",
                install_id,
                account_id(request),
                secrets=body.secrets,
                connection_config=body.connection_config,
                remote_base_url=body.remote_base_url,
            )

        return await invoke(request, operation)

    @router.get(
        "/api/llm-provider-extensions/{install_id}/status",
        responses={
            403: {"description": "Administrator access required"},
            503: {"description": "Database not available"},
        },
    )
    async def connection_status(install_id: str, request: Request):
        """Saved provider secret names (never values) and the last check."""
        require_admin(request)

        async def operation():
            return await run_use_case("connection_status", install_id, account_id(request))

        return await invoke(request, operation)

    @router.post(
        "/api/llm-provider-extensions/{install_id}/refresh-models",
        responses={
            403: {"description": "Administrator access required"},
            503: {"description": "Database not available"},
        },
    )
    async def refresh_models(
        install_id: str, body: RefreshLlmProviderModelsRequest, request: Request
    ):
        require_admin(request)

        async def operation():
            return await run_use_case(
                "refresh_models",
                install_id,
                account_id(request),
                connection_config=body.connection_config,
            )

        return await invoke(request, operation)

    @router.post(
        "/api/llm-provider-extensions/sources/{source_id}/{action}",
        responses={
            403: {"description": "Administrator access required"},
            404: {"description": "Unknown source action"},
            503: {"description": "Database not available"},
        },
    )
    async def set_source_state(source_id: str, action: str, request: Request):
        require_admin(request)
        if action not in {"enable", "disable"}:
            raise HTTPException(status_code=404, detail="Unknown action")

        async def operation():
            return await run_use_case(
                "set_source_state", source_id, account_id(request), enabled=action == "enable"
            )

        return await invoke(request, operation)

    @router.post(
        "/api/llm-provider-extensions/models/{model_id}/{action}",
        responses={
            403: {"description": "Administrator access required"},
            404: {"description": "Unknown model action"},
            503: {"description": "Database not available"},
        },
    )
    async def set_model_state(model_id: str, action: str, request: Request):
        require_admin(request)
        if action not in {"enable", "disable"}:
            raise HTTPException(status_code=404, detail="Unknown action")

        async def operation():
            return await run_use_case(
                "set_model_state", model_id, account_id(request), enabled=action == "enable"
            )

        return await invoke(request, operation)

    return router
