"""Account administration routes of secret backends (ADR 0066 amendment 2026-11-26).

Controllers only: authorization, entitlement and every rule live in
``core.application.secrets.backend_service``. No route returns a secret value.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from core.application.management import ServiceError


class SecretBackendConfigCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class SecretBackendConfigUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    config: dict[str, Any] | None = None
    enabled: bool | None = None


class SecretAdoptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    config_id: str = Field(min_length=1, max_length=64)
    locator: dict[str, Any]


class SecretMoveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config_id: str = Field(min_length=1, max_length=64)
    locator: dict[str, Any]
    #: Overwrite a secret already kept at that name in the storage. Default: refuse.
    replace: bool = False


def request_entitlement_service(request: Request) -> Any:
    """The app role's entitlement decider, or ``None`` without one (nothing is entitled)."""
    container = getattr(request.app.state, "runtime_container", None)
    registries = getattr(container, "registries", None)
    contracts = registries.get("platform_contracts") if isinstance(registries, Mapping) else None
    return contracts.get("entitlement_service") if isinstance(contracts, Mapping) else None


def register_secret_backend_routes(
    *,
    api_error_response: Callable[..., JSONResponse],
    get_logger: Callable[[], Any],
    request_scope: Callable[..., Any],
    router: APIRouter,
    service_error_response: Callable[..., JSONResponse],
) -> None:
    def _service(request: Request) -> Any:
        from core.application.secrets.backend_service import SecretBackendAdminService

        return SecretBackendAdminService(
            get_logger, entitlement_service=lambda: request_entitlement_service(request)
        )

    def _call(request: Request, action: Callable[[Any, Any], Any]) -> Any:
        try:
            return action(_service(request), request_scope(request, project_id=None))
        except ServiceError as e:
            return service_error_response(e)
        except Exception as e:
            return api_error_response(e)

    @router.get("/api/secret-backends")
    def api_secret_backends(request: Request):
        return _call(request, lambda service, scope: service.overview(scope))

    @router.post("/api/secret-backends/configs")
    async def api_secret_backend_config_create(
        request: Request, body: SecretBackendConfigCreateRequest
    ):
        return _call(
            request,
            lambda service, scope: service.create_config(
                scope,
                backend=body.backend,
                name=body.name,
                config=body.config,
                enabled=body.enabled,
            ),
        )

    @router.patch("/api/secret-backends/configs/{config_id}")
    async def api_secret_backend_config_update(
        request: Request, config_id: str, body: SecretBackendConfigUpdateRequest
    ):
        return _call(
            request,
            lambda service, scope: service.update_config(
                scope, config_id, name=body.name, config=body.config, enabled=body.enabled
            ),
        )

    @router.get("/api/secret-backends/configs/{config_id}/health")
    def api_secret_backend_config_health(request: Request, config_id: str):
        return _call(request, lambda service, scope: service.config_health(scope, config_id))

    @router.post("/api/secret-backends/adopt")
    async def api_secret_backend_adopt(request: Request, body: SecretAdoptRequest):
        return _call(
            request,
            lambda service, scope: service.adopt_secret(
                scope,
                name=body.name,
                description=body.description,
                config_id=body.config_id,
                locator=body.locator,
            ),
        )

    @router.post("/api/secret-backends/secrets/{secret_id}/move")
    async def api_secret_backend_move(request: Request, secret_id: str, body: SecretMoveRequest):
        return _call(
            request,
            lambda service, scope: service.move_to_backend(
                scope,
                secret_id,
                config_id=body.config_id,
                locator=body.locator,
                replace=body.replace,
            ),
        )

    @router.post("/api/secret-backends/secrets/{secret_id}/move-local")
    async def api_secret_backend_move_local(request: Request, secret_id: str):
        return _call(request, lambda service, scope: service.move_to_local(scope, secret_id))


__all__ = ["register_secret_backend_routes", "request_entitlement_service"]
