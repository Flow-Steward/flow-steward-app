"""System retention settings page and API routes."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.runtime.entrypoint_db_adapters import (
    MAX_RETENTION_DAYS,
    MAX_REVISIONS_KEEP,
)


def _int_env(name: str, default: int, *, maximum: int = MAX_RETENTION_DAYS) -> int:
    raw = _runtime_env.get(name)
    try:
        value = int(raw) if raw is not None else int(default)
    except (TypeError, ValueError):
        value = int(default)
    return max(1, min(maximum, value))


def _retention_defaults() -> dict[str, int]:
    return {
        "action_logs_days": _int_env("FS_RETENTION_LOGS_DAYS", 30),
        "job_logs_days": _int_env("FS_RETENTION_MESSAGES_DAYS", 30),
        "trash_days": _int_env("FS_RETENTION_TRASH_DAYS", 30),
        "terminal_job_checkpoints_days": _int_env(
            "FS_RETENTION_TERMINAL_JOB_CHECKPOINTS_DAYS",
            30,
        ),
        "revisions_keep": _int_env("FS_RETENTION_REVISIONS_KEEP", 5, maximum=MAX_REVISIONS_KEEP),
    }


def _json_safe_retention_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return jsonable_encoder(
        payload,
        custom_encoder={datetime: lambda value: value.isoformat()},
    )


def create_settings_retention_router(
    *,
    require_authenticated_user: Callable[..., dict[str, Any]],
    auth_admin_roles: set[str],
    get_logger_only: Callable[[], Any],
    request_scope: Callable[..., Any],
    resolve_actor_username: Callable[..., str],
) -> APIRouter:
    router = APIRouter(tags=["settings-retention"])

    def _account_id_for_request(request: Request) -> str:
        try:
            scope = request_scope(request)
        except Exception:
            scope = None
        account_id = getattr(scope, "account_id", None)
        resolved = str(account_id or DEFAULT_ACCOUNT_ID).strip()
        return resolved or DEFAULT_ACCOUNT_ID

    def _resolve_payload(request: Request) -> tuple[dict[str, Any], bool]:
        defaults = _retention_defaults()
        account_id = _account_id_for_request(request)
        bl = get_logger_only()
        if bl and hasattr(bl, "get_retention_settings"):
            try:
                settings = bl.get_retention_settings(defaults=defaults, account_id=account_id)
                return dict(settings), True
            except Exception:
                return {**defaults, "account_id": account_id}, False
        return {**defaults, "account_id": account_id}, False

    def _ensure_admin_api(request: Request) -> dict[str, Any] | None:
        try:
            return require_authenticated_user(request, required_roles=auth_admin_roles)
        except PermissionError:
            return None

    @router.get("/api/settings/retention")
    async def api_retention_settings(request: Request):
        auth_user = _ensure_admin_api(request)
        if auth_user is None:
            return JSONResponse({"error": "Admin role required."}, status_code=403)
        del auth_user

        settings, available = _resolve_payload(request)
        status_code = 200 if available else 503
        return JSONResponse(
            _json_safe_retention_payload(
                {
                    "settings": settings,
                    "available": available,
                    "defaults": _retention_defaults(),
                }
            ),
            status_code=status_code,
        )

    @router.post("/api/settings/retention")
    async def api_save_retention_settings(request: Request):
        auth_user = _ensure_admin_api(request)
        if auth_user is None:
            return JSONResponse({"error": "Admin role required."}, status_code=403)
        del auth_user

        bl = get_logger_only()
        if not bl or not hasattr(bl, "save_retention_settings"):
            return JSONResponse({"error": "Database not configured."}, status_code=503)

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body."}, status_code=400)

        if not isinstance(body, dict):
            return JSONResponse({"error": "Invalid JSON body."}, status_code=400)

        actor = resolve_actor_username(request, "", default="system")
        account_id = _account_id_for_request(request)
        try:
            saved = bl.save_retention_settings(body, actor=actor, account_id=account_id)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except RuntimeError as exc:
            return JSONResponse({"error": str(exc)}, status_code=503)
        except Exception as exc:
            return JSONResponse(
                {"error": f"Failed to save retention settings: {exc}"}, status_code=500
            )

        return JSONResponse(
            _json_safe_retention_payload({"status": "saved", "settings": dict(saved)})
        )

    return router


__all__ = ["create_settings_retention_router"]
