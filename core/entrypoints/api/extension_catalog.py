"""Read-only extension catalog routes plus the catalog install action.

The Marketplace tab keeps its existing API surface; these endpoints only add
the static catalog listing next to it. There is no create, delete, publish,
moderation, or administration endpoint here. The install action also updates
an older install to the newer catalog release, through the same archive
installer an uploaded ZIP uses.

Every handler here is a plain ``def``, not ``async def``, on purpose. The work
behind them — the outbound catalog fetch, the install-store read, and the
archive install — is synchronous and can take tens of seconds. Declared
``async`` it would run on the event loop and stall every other request in the
process; declared sync, Starlette runs it in a worker thread.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse, Response

from core.application.extensions.extension_catalog import (
    ExtensionCatalogError,
    extension_catalog_availability,
    extension_catalog_icon,
)
from core.application.extensions.extension_catalog_install import (
    ExtensionCatalogInstallService,
)
from core.application.management.errors import ServiceError


class ExtensionCatalogInstallRequest(BaseModel):
    """One catalog entry to install, by logical id and the version offered."""

    extension_id: str = Field(min_length=1, max_length=200)
    #: The version and digest the card showed. Listing and install can be
    #: served by two processes with independent caches, and a republished
    #: version keeps its number while changing its bytes, so the client pins
    #: both and the backend refuses when either has moved.
    version: str = Field(min_length=1, max_length=64)
    release_sha256: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")
    approved_browser_authority_impact_hash: str = Field(
        default="", max_length=64, pattern=r"^(?:|[a-f0-9]{64})$"
    )


def create_extension_catalog_router(
    *,
    get_extension_install_store: Callable[[], Any],
    extension_binding_service: Callable[[], Any],
    request_scope: Callable[[Request], Any],
    service_error_response: Callable[..., Any],
    api_error_response: Callable[..., Any],
    reload_extension_registries: Callable[[], None] | None = None,
    get_extension_runtime_store: Callable[[], Any] | None = None,
) -> APIRouter:
    router = APIRouter(tags=["extensions"])

    def _service() -> ExtensionCatalogInstallService:
        return ExtensionCatalogInstallService(
            get_install_store=get_extension_install_store,
            get_binding_service=extension_binding_service,
            get_runtime_store=get_extension_runtime_store,
        )

    def _catalog_error_response(exc: ExtensionCatalogError) -> JSONResponse:
        return JSONResponse(exc.detail(), status_code=exc.status_code)

    @router.get("/api/extensions/catalog")
    def api_extension_catalog(request: Request):
        """List the cached catalog joined with this account's install state."""
        try:
            return _service().list_catalog(scope=request_scope(request))
        except ExtensionCatalogError as e:
            return _catalog_error_response(e)
        except ServiceError as e:
            return service_error_response(e, request)
        except Exception as e:
            return api_error_response(e, request=request)

    @router.get("/api/extensions/catalog/availability")
    def api_extension_catalog_availability(request: Request):
        """Report catalog availability without failing when it is unusable."""
        del request
        return extension_catalog_availability()

    @router.get("/api/extensions/catalog/{extension_id}/icon")
    def api_extension_catalog_icon(request: Request, extension_id: str):
        """The entry's logo, verified against the index digest and served from here."""
        del request
        try:
            data = extension_catalog_icon(extension_id)
        except ExtensionCatalogError as e:
            return _catalog_error_response(e)
        return Response(
            content=data,
            media_type="image/png",
            headers={
                "Cache-Control": "private, max-age=86400",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'",
            },
        )

    @router.post("/api/extensions/catalog/install")
    def api_extension_catalog_install(request: Request, body: ExtensionCatalogInstallRequest):
        """Fetch, verify, and install (or update to) one catalog release via the installer."""
        try:
            payload = _service().install_from_catalog(
                body.extension_id,
                version=body.version,
                release_sha256=body.release_sha256,
                scope=request_scope(request),
                approved_browser_authority_impact_hash=(
                    body.approved_browser_authority_impact_hash
                ),
            )
            if reload_extension_registries is not None:
                reload_extension_registries()
        except ExtensionCatalogError as e:
            return _catalog_error_response(e)
        except ServiceError as e:
            return service_error_response(e, request)
        except Exception as e:
            return api_error_response(e, request=request)
        return payload.dump()

    return router


__all__ = ["ExtensionCatalogInstallRequest", "create_extension_catalog_router"]
