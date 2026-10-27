"""Read-only public status route for the admin maintenance banner."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter

from core.application.maintenance.contracts import MaintenanceStatusResult
from core.infrastructure.runtime.maintenance_http import runtime_maintenance_status


def create_system_maintenance_router(
    *,
    status_provider: Callable[[], object] = runtime_maintenance_status,
) -> APIRouter:
    router = APIRouter(tags=["system"])

    @router.get("/api/system/maintenance", response_model=MaintenanceStatusResult)
    async def system_maintenance_status() -> object:
        return status_provider()

    return router


__all__ = ["create_system_maintenance_router"]
