"""Thin CLI bindings for maintenance mode."""

from __future__ import annotations

from core.entrypoints.cli.platform.service_bindings.common import (
    RuntimeServices,
    ServiceFactory,
    await_maybe,
)


async def maintenance_status(runtime: RuntimeServices, _value: object, **_kwargs: object) -> object:
    return await await_maybe(runtime.maintenance_service.status())


async def maintenance_enable(runtime: RuntimeServices, _value: object, **_kwargs: object) -> object:
    return await await_maybe(
        runtime.maintenance_service.enable(
            owner_kind="manual",
            owner_id="manual-maintenance",
        )
    )


async def maintenance_disable(
    runtime: RuntimeServices, _value: object, **_kwargs: object
) -> object:
    return await await_maybe(runtime.maintenance_service.disable())


MAINTENANCE_SERVICE_FACTORIES: dict[str, ServiceFactory] = {
    "maintenance.status": maintenance_status,
    "maintenance.enable": maintenance_enable,
    "maintenance.disable": maintenance_disable,
}


__all__ = ["MAINTENANCE_SERVICE_FACTORIES"]
