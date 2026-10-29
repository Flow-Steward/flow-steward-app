from __future__ import annotations

from fastapi import APIRouter

from core.application.management.scope import normalize_scope

from .workflow_entrypoints_routes_execute import _register_workflow_entrypoint_execution_route
from .workflow_entrypoints_routes_providers import _register_workflow_provider_trigger_routes
from .workflow_entrypoints_routes_webhooks import _register_workflow_webhook_routes


def create_workflow_entrypoints_router(
    *,
    get_logger_only,
    get_trigger_store=None,
    request_scope=None,
    enqueue_task,
    redeliver_task=None,
    artifact_plane,
    logger,
    err_database_not_configured: str,
):
    router = APIRouter(tags=["workflow-entrypoints"])
    scope_resolver = request_scope or (lambda _request, **_kwargs: normalize_scope())
    ctx = {
        "router": router,
        "get_logger_only": get_logger_only,
        "get_trigger_store": get_trigger_store or get_logger_only,
        "scope_resolver": scope_resolver,
        "enqueue_task": enqueue_task,
        "redeliver_task": redeliver_task,
        "artifact_plane": artifact_plane,
        "logger": logger,
        "err_database_not_configured": err_database_not_configured,
    }
    _register_workflow_webhook_routes(ctx)
    _register_workflow_provider_trigger_routes(ctx)
    _register_workflow_entrypoint_execution_route(ctx)
    return router


__all__ = [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
