"""
Health-check, readiness, and Prometheus metrics routes.
"""

from __future__ import annotations

import hmac
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from core.application.auth.enforce import require_roles
from core.application.monitoring.health_query import HealthCountsQueryService
from core.entrypoints.admin import deps
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.config.version import APP_VERSION
from core.infrastructure.monitoring.system_storage import SystemHealthCountsRepository
from core.infrastructure.observability.observability_metrics import (
    build_prometheus_metrics_payload,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])
health_counts_query = HealthCountsQueryService(repository=SystemHealthCountsRepository())

# ── Constants ───────────────────────────────────────────────────────────

_METRICS_TOKEN = str(_runtime_env.get("FS_METRICS_TOKEN", "") or "").strip()
_BUILD_SHA = str(_runtime_env.get("FS_BUILD_SHA", "") or "").strip()
_IMAGE_DIGEST = str(_runtime_env.get("FS_APP_IMAGE_DIGEST", "") or "").strip()


# ── Helpers ─────────────────────────────────────────────────────────────


def _metrics_request_authorized(request: Request) -> bool:
    if _METRICS_TOKEN:
        supplied = str(request.headers.get("x-metrics-token") or "").strip()
        if supplied and hmac.compare_digest(supplied, _METRICS_TOKEN):
            return True
        auth_header = str(request.headers.get("authorization") or "").strip()
        if auth_header.lower().startswith("bearer "):
            bearer = auth_header[7:].strip()
            if bearer and hmac.compare_digest(bearer, _METRICS_TOKEN):
                return True

    try:
        require_roles(getattr(request.state, "auth_user", None), {"admin", "operator"})
    except PermissionError:
        return False
    return True


def _operations_request_authorized(request: Request) -> bool:
    try:
        require_roles(getattr(request.state, "auth_user", None), {"admin", "operator"})
    except PermissionError:
        return False
    return True


def _live_health_payload() -> dict[str, str]:
    return {
        "status": "ok",
        "build_version": APP_VERSION,
        "build_sha": _BUILD_SHA,
        "image_digest": _IMAGE_DIGEST,
        "service": "platform-orchestrator",
        "time": datetime.now(UTC).isoformat(),
    }


# ── Routes ──────────────────────────────────────────────────────────────


@router.get("/health/live")
async def health_live():
    return _live_health_payload()


@router.get("/health/ready")
def health_ready():
    bl = deps.get_logger_only()
    if not bl:
        return JSONResponse(
            {
                "status": "not_ready",
                "service": "platform-orchestrator",
                "ready": False,
                "checks": {"postgres": {"ok": False, "error": "Database not configured"}},
            },
            status_code=503,
        )
    readiness = deps.compute_readiness(bl)
    payload = {
        "status": "ready" if bool(readiness.get("ready")) else "not_ready",
        "service": "platform-orchestrator",
        "ready": bool(readiness.get("ready")),
        "checks": readiness.get("checks") or {},
        "time": datetime.now(UTC).isoformat(),
    }
    return JSONResponse(payload, status_code=200 if payload["ready"] else 503)


@router.get("/health/deep")
async def health_deep(request: Request):
    if not _operations_request_authorized(request):
        return JSONResponse({"error": "Operator or admin role required"}, status_code=403)

    bl = deps.get_logger_only()
    if not bl:
        return JSONResponse(
            {
                "status": "not_ready",
                "service": "platform-orchestrator",
                "ready": False,
                "checks": {"postgres": {"ok": False, "error": "Database not configured"}},
                "meta": {},
            },
            status_code=503,
        )

    readiness = deps.compute_readiness(bl)
    meta: dict[str, Any] = {
        "version": APP_VERSION,
        "watchdog_enabled": deps.env_flag("FS_WATCHDOG_ENABLED", True),
        "watchdog_auto_apply": deps.env_flag("FS_WATCHDOG_AUTO_APPLY", True),
        "local_auth_enabled": deps.local_auth_enabled(),
        "external_auth_enabled": deps.external_auth_enabled(),
    }
    try:
        meta.update(health_counts_query.get_counts(bl))
    except Exception as e:
        meta["query_error"] = str(e)

    payload = {
        "status": "ready" if bool(readiness.get("ready")) else "not_ready",
        "service": "platform-orchestrator",
        "ready": bool(readiness.get("ready")),
        "checks": readiness.get("checks") or {},
        "meta": meta,
        "time": datetime.now(UTC).isoformat(),
    }
    return JSONResponse(payload, status_code=200 if payload["ready"] else 503)


@router.get("/metrics")
async def metrics(request: Request):
    if not _metrics_request_authorized(request):
        return JSONResponse({"error": "Metrics token required"}, status_code=401)

    bl = deps.get_logger_only()
    if not bl:
        return PlainTextResponse("", media_type="text/plain; version=0.0.4")

    payload = build_prometheus_metrics_payload(
        bl,
        compute_readiness=deps.compute_readiness,
        to_json_obj=deps.to_json_obj,
        get_ready_queue_depth=deps.get_ready_queue_depth,
    )
    return PlainTextResponse(payload, media_type="text/plain; version=0.0.4")


@router.get("/health")
async def health():
    return await health_live()


@router.get("/api/health")
async def api_health():
    return await health_live()
