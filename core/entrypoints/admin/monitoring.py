"""
Admin API monitoring routes — runs, graph visualization, and registry validation.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request

from core.application.auth.enforce import require_roles
from core.application.monitoring.runs import MonitoringRunsService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["monitoring"])


def _require_operator_or_admin(request: Request) -> None:
    if not isinstance(getattr(request.state, "auth_user", None), dict):
        return
    try:
        require_roles(getattr(request.state, "auth_user", None), {"admin", "operator"})
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _get_registry():
    from core.application.registry.registry_manager import RegistryManager

    return RegistryManager.instance()


@router.get(
    "/validate",
    responses={
        403: {"description": "Forbidden"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def validate_registry(
    request: Request,
    reload: Annotated[bool, Query()] = False,
):
    """Validate registry integrity and return errors/warnings."""
    _require_operator_or_admin(request)
    registry = _get_registry()
    if reload:
        try:
            require_roles(getattr(request.state, "auth_user", None), {"admin"})
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        registry.reload_all()
    errors, warnings = registry.validate()
    stats = registry.get_stats()
    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "stats": stats,
    }


@router.post(
    "/registry/reload",
    responses={
        403: {"description": "Forbidden"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def reload_registry(request: Request):
    """Reload registry from DB (equivalent to SIGHUP)."""
    try:
        require_roles(getattr(request.state, "auth_user", None), {"admin"})
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    registry = _get_registry()
    registry.reload_all()
    stats = registry.get_stats()
    return {"status": "reloaded", "stats": stats}


@router.get(
    "/registry/stats",
    responses={
        403: {"description": "Forbidden"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def get_registry_stats(request: Request):
    """Get registry statistics."""
    _require_operator_or_admin(request)
    registry = _get_registry()
    return registry.get_stats()


@router.get(
    "/graph",
    responses={
        403: {"description": "Forbidden"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def get_graph_info(request: Request):
    """Get information about the current graph and node count."""
    _require_operator_or_admin(request)
    info = {}

    try:
        registry = _get_registry()
        agents = registry.agents.list_all()
        info["nodes"] = {
            "fixed": [
                "task_intake",
                "kb_intake",
                "orchestrator",
                "orchestrator_validation",
                "human_approval_gate",
            ],
            "agents": [a.agent_id for a in agents if a.enabled],
            "fixed_labels": {"kb_intake": "Project context"},
            "total": 5 + len([a for a in agents if a.enabled]),
        }
    except Exception:
        info["nodes"] = {"error": "Could not load registry"}

    return info


@router.get(
    "/runs",
    responses={
        403: {"description": "Forbidden"},
        500: {"description": "Internal server error"},
        503: {"description": "Service unavailable"},
    },
)
def list_runs(
    request: Request,
    project_name: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query()] = 20,
):
    """List recent job runs from the database."""
    try:
        require_roles(getattr(request.state, "auth_user", None), {"admin", "operator"})
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    try:
        return MonitoringRunsService().list_runs(
            project_name=project_name,
            status=status,
            limit=limit,
        )
    except RuntimeError as exc:
        if str(exc) == "Database not configured":
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
