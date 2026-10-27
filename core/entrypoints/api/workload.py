"""
Workload signal endpoint — GET /api/workload

Returns a recommendation (normal|throttle|shed|pause) based on current
system metrics: active jobs, queue depth, and DB connections.
"""

from __future__ import annotations

import contextlib
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core.application.auth.enforce import require_roles
from core.application.jobs.workload_service import get_workload_signal
from core.application.management.scope import scope_from_principal
from core.entrypoints.admin import deps

logger = logging.getLogger(__name__)

router = APIRouter(tags=["workload"])


@router.get("/api/workload")
async def workload_signal(request: Request):
    """Return the current workload recommendation and underlying metrics.

    The ``active_jobs`` metric is scoped to the caller's account_id so
    principals from different tenants cannot infer each other's activity.
    Queue depth and DB connection count remain global system metrics.
    """
    user = getattr(request.state, "auth_user", None)
    if isinstance(user, dict):
        try:
            require_roles(user, {"admin", "operator"})
        except PermissionError:
            return JSONResponse({"error": "Operator role required"}, status_code=403)

    bl = deps.get_logger_only()
    if bl is None:
        return JSONResponse(
            {"error": "Database not available"},
            status_code=503,
        )
    scope = scope_from_principal(user if isinstance(user, dict) else None)
    account_id = scope.account_id or None

    get_ready_queue_depth_fn = None
    with contextlib.suppress(AssertionError, AttributeError):
        get_ready_queue_depth_fn = deps.get_ready_queue_depth

    signal = get_workload_signal(
        bl,
        get_ready_queue_depth_fn=get_ready_queue_depth_fn,
        account_id=account_id,
    )
    return JSONResponse(signal)
