"""API endpoints for model catalog management.

GET  /api/model-selection/options     — effective options for work forms
GET  /api/settings/model-catalog      — full catalog + visibility for settings (admin)
PUT  /api/settings/model-catalog      — save visibility policy (admin)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from core.application.auth.config import DEFAULT_ACCOUNT_ID


def create_api_model_catalog_router(
    *,
    get_logger_only,
    request_scope,
    require_authenticated_user,
    resolve_actor_username,
    auth_admin_roles,
    api_error_response,
) -> APIRouter:
    _ = request_scope
    router = APIRouter(tags=["model-catalog"])
    logger = logging.getLogger(__name__)

    def _conn() -> Any:
        bl = get_logger_only()
        conn = getattr(bl, "conn", None)
        if conn is None:
            raise HTTPException(status_code=503, detail="Database not available")
        return conn

    def _account_id(request: Request) -> str:
        auth_user = getattr(request.state, "auth_user", None)
        if isinstance(auth_user, dict):
            aid = str(auth_user.get("account_id") or "").strip()
            if aid:
                return aid
        return DEFAULT_ACCOUNT_ID

    def _load_model_selection_options(account_id: str) -> dict[str, Any]:
        from core.application.settings.model_catalog import get_effective_options_structured

        return get_effective_options_structured(
            account_id=account_id,
            conn=_conn(),
        )

    def _load_full_catalog_settings(account_id: str) -> dict[str, Any]:
        from core.application.settings.model_catalog import get_full_catalog_for_settings

        return get_full_catalog_for_settings(account_id=account_id, conn=_conn())

    def _save_model_catalog_settings(
        body: dict[str, Any], account_id: str, actor: str
    ) -> dict[str, str]:
        from core.application.settings.model_catalog import save_visibility_policy

        conn = _conn()
        if conn is None:
            raise HTTPException(status_code=503, detail="Database not available")
        save_visibility_policy(conn, body, account_id=account_id, actor=actor)
        return {"status": "saved"}

    @router.get(
        "/api/model-selection/options",
        responses={
            400: {"description": "Bad request"},
            403: {"description": "Forbidden"},
            422: {"description": "Validation error"},
            503: {"description": "Service unavailable"},
        },
    )
    async def model_selection_options(request: Request):
        """Effective provider/model options for work forms (cascading selects)."""
        try:
            account_id = _account_id(request)
            return await asyncio.to_thread(_load_model_selection_options, account_id)
        except Exception as exc:
            logger.warning("model_selection_options failed: %s", exc)
            raise HTTPException(status_code=503, detail="Model catalog unavailable") from exc

    @router.get(
        "/api/settings/model-catalog",
        responses={
            400: {"description": "Bad request"},
            403: {"description": "Forbidden"},
            422: {"description": "Validation error"},
            503: {"description": "Service unavailable"},
        },
    )
    async def get_model_catalog_settings(request: Request):
        """Full catalog state for the settings management page (admin only)."""
        try:
            require_authenticated_user(request, required_roles=auth_admin_roles)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="Admin role required") from exc

        try:
            account_id = _account_id(request)
            return await asyncio.to_thread(_load_full_catalog_settings, account_id)
        except HTTPException:
            raise
        except Exception as exc:
            return api_error_response(
                exc,
                request=request,
                fallback_message="Internal server error",
            )

    @router.put(
        "/api/settings/model-catalog",
        responses={
            400: {"description": "Bad request"},
            403: {"description": "Forbidden"},
            422: {"description": "Validation error"},
            503: {"description": "Service unavailable"},
        },
    )
    async def save_model_catalog_settings(request: Request):
        """Save visibility policy (admin only)."""
        try:
            require_authenticated_user(request, required_roles=auth_admin_roles)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="Admin role required") from exc

        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON body") from exc

        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Expected JSON object")

        account_id = _account_id(request)
        # resolve_actor_username(request, requested) — pass None to derive from auth session
        actor = resolve_actor_username(request, None)

        try:
            return await asyncio.to_thread(
                _save_model_catalog_settings,
                body,
                account_id,
                actor,
            )
        except (ValueError, TypeError, KeyError) as exc:
            # Validation / schema mismatch — return 422 (not 500)
            logger.warning("save_model_catalog_settings validation error: %s", exc)
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except HTTPException:
            raise
        except Exception as exc:
            return api_error_response(
                exc,
                request=request,
                fallback_message="Internal server error",
            )

    return router
