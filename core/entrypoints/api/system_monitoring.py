"""Admin-only monitoring summary APIs."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from core.application.management import ServiceError
from core.application.monitoring.queries import ComponentsProjection
from core.application.monitoring.system import (
    build_monitoring_meta,
    build_monitoring_summary_payload,
    build_storage_monitoring_payload,
    cleanup_orphan_storage_scope,
    query_runtime_components,
)
from core.application.reports.system_diagnostic_report import collect_system_diagnostic_report
from core.entrypoints.admin import deps
from core.entrypoints.api.error_safety import safe_error_response
from core.entrypoints.api.report_download import (
    REPORT_DOWNLOAD_TOKEN_PATTERN,
    report_download_response,
    valid_report_download_token,
)
from core.infrastructure.config.version import APP_VERSION
from core.infrastructure.extension_runtime.runtime_target import extension_runtime_target

_MESSAGE_DATABASE_NOT_CONFIGURED = "Database not configured"

logger = logging.getLogger(__name__)


class StorageOrphanCleanupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: str = Field(default="")
    scope: str = Field(default="")
    confirm: str = Field(default="")


def create_api_system_monitoring_router(
    *,
    local_auth_enabled: Callable[[], bool],
    external_auth_enabled: Callable[[], bool] | bool,
    require_authenticated_user: Callable[..., dict[str, Any]],
    auth_admin_roles: set[str] | list[str] | tuple[str, ...],
    components_query: Callable[[Any], ComponentsProjection | None] = query_runtime_components,
    list_extensions: Callable[[Request], Any] | None = None,
) -> APIRouter:
    router = APIRouter(tags=["api-system-monitoring"])

    def _external_auth_enabled() -> bool:
        if callable(external_auth_enabled):
            return bool(external_auth_enabled())
        return bool(external_auth_enabled)

    def _api_auth_guard(request: Request):
        if local_auth_enabled() or _external_auth_enabled():
            try:
                require_authenticated_user(request, required_roles=auth_admin_roles)
            except PermissionError:
                return JSONResponse({"error": "Admin role required"}, status_code=403)
        return None

    @router.get("/api/system/monitoring/summary")
    async def monitoring_summary_api(request: Request):
        auth_failure = _api_auth_guard(request)
        if auth_failure is not None:
            return auth_failure

        runtime_target = extension_runtime_target()
        bl = deps.get_logger_only()
        if not bl:
            return JSONResponse(
                {
                    "status": "not_ready",
                    "ready": False,
                    "checks": {
                        "postgres": {"ok": False, "error": _MESSAGE_DATABASE_NOT_CONFIGURED}
                    },
                    "meta": {"extension_runtime_target": runtime_target},
                    "summary": {"checks_total": 1, "checks_ok": 0, "checks_failed": 1},
                    "components": None,
                    "time": datetime.now(UTC).isoformat(),
                },
                status_code=503,
            )

        components = components_query(bl)
        readiness = deps.compute_readiness(bl)
        meta = build_monitoring_meta(
            bl,
            app_version=APP_VERSION,
            env_flag=deps.env_flag,
            get_ready_queue_depth=deps.get_ready_queue_depth,
            local_auth_enabled=deps.local_auth_enabled,
            external_auth_enabled=_external_auth_enabled,
        )
        meta["extension_runtime_target"] = runtime_target
        payload = build_monitoring_summary_payload(
            readiness=readiness,
            meta=meta,
            components=components,
        )
        # A produced report is a successful response, even when what it reports
        # is unhealthy. Mirroring the readiness probe's 503 here made the
        # monitoring dashboard fail its own fetch exactly when the deployment
        # was unhealthy: the browser client rejects non-2xx, so the page fell
        # back to empty defaults and showed "Failed: 0" while checks were
        # failing. The unhealthy state is carried by `ready` and `status` in the
        # body. 503 means the stored report could not be produced, not that
        # its observed components are unhealthy or stale.
        payload.setdefault("components", None)
        return JSONResponse(
            payload, status_code=200 if components is not None and components.available else 503
        )

    @router.get("/api/system/monitoring/storage")
    async def monitoring_storage_api(request: Request):
        auth_failure = _api_auth_guard(request)
        if auth_failure is not None:
            return auth_failure

        bl = deps.get_logger_only()
        if not bl:
            return JSONResponse(
                {
                    "generated_at": datetime.now(UTC).isoformat(),
                    "backend_matrix": {},
                    "summary": {},
                    "memory": {"available": False, "error": _MESSAGE_DATABASE_NOT_CONFIGURED},
                    "vector": {"available": False},
                },
                status_code=503,
            )
        try:
            return JSONResponse(build_storage_monitoring_payload(bl))
        except Exception as exc:
            return safe_error_response(
                logger,
                exc,
                message="Storage monitoring failed",
            )

    @router.post("/api/system/monitoring/storage/orphans/cleanup")
    async def monitoring_storage_orphan_cleanup_api(
        request: Request,
        payload: StorageOrphanCleanupRequest,
    ):
        auth_failure = _api_auth_guard(request)
        if auth_failure is not None:
            return auth_failure

        bl = deps.get_logger_only()
        if not bl:
            return JSONResponse(
                {
                    "generated_at": datetime.now(UTC).isoformat(),
                    "error": _MESSAGE_DATABASE_NOT_CONFIGURED,
                },
                status_code=503,
            )
        try:
            return JSONResponse(
                cleanup_orphan_storage_scope(
                    bl,
                    backend=payload.backend,
                    scope=payload.scope,
                    confirm=payload.confirm,
                )
            )
        except ServiceError as exc:
            return JSONResponse(exc.payload, status_code=exc.status_code)
        except Exception as exc:
            return safe_error_response(
                logger,
                exc,
                message="Storage cleanup failed",
            )

    @router.get("/api/system/diagnostics/report")
    async def system_diagnostic_report_api(
        request: Request,
        download_token: Annotated[
            str | None,
            Query(min_length=16, max_length=64, pattern=REPORT_DOWNLOAD_TOKEN_PATTERN),
        ] = None,
    ):
        """Download the redacted installation report an operator attaches to a bug report."""
        auth_failure = _api_auth_guard(request)
        if auth_failure is not None:
            return auth_failure

        bl = deps.get_logger_only()
        if not bl:
            return JSONResponse({"error": _MESSAGE_DATABASE_NOT_CONFIGURED}, status_code=503)

        def _extensions() -> Any:
            if list_extensions is None:
                raise RuntimeError("Extension listing is not wired into this API")
            return list_extensions(request)

        def _render() -> bytes:
            report = collect_system_diagnostic_report(
                source="api",
                store=bl,
                list_extensions=_extensions,
                readiness=lambda: deps.compute_readiness(bl),
            )
            return json.dumps(report, ensure_ascii=False, default=str, indent=2).encode("utf-8")

        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        filename = f"flow-steward-diagnostics-{stamp}.json"
        if download_token and valid_report_download_token(download_token):
            return report_download_response(
                request, filename=filename, download_token=download_token, render=_render
            )
        try:
            body = await asyncio.to_thread(_render)
        except Exception as exc:
            return safe_error_response(logger, exc, message="Diagnostic report failed")
        return JSONResponse(
            json.loads(body),
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    return router


__all__ = ["StorageOrphanCleanupRequest", "create_api_system_monitoring_router"]
