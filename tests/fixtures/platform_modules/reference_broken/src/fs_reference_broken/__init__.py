"""Test-only platform module whose registration fails half way (ADR 0082).

``register`` mounts a route under the module's own API tree, contributes a route
rule and an entitlement source, and then reads a port the container does not
offer, so it raises. ``register_control`` registers the same source and raises.
Core must skip the module, roll back everything it registered and keep running.
"""

from collections.abc import Mapping
from typing import Any

FEATURE = "reference_broken"


class _GrantEverything:
    def features_for(self, _account_id: str) -> tuple[str, ...]:
        return (FEATURE,)

    def limits_for(self, _account_id: str) -> dict[str, int]:
        return {}


def register(app: Any, container: Any) -> None:
    """App role entrypoint: registers a route, a rule and a source, then fails."""
    prefix = f"/api/modules/{container.metadata['platform_module']['id']}"

    def list_items() -> dict[str, Any]:
        return {"items": []}

    app.add_api_route(f"{prefix}/items", list_items, methods=["GET"])
    container.registries["module_route_permissions"].register(
        methods=("GET",), pattern="items", permission="jobs.read"
    )
    container.service_factories["entitlement_service"]().register_entitlement_source(
        _GrantEverything()
    )
    container.missing_port.read()  # raises AttributeError: the port does not exist


def register_control(context: Mapping[str, Any]) -> None:
    """Control role entrypoint: registers the source, then fails."""
    context["entitlement_service"].register_entitlement_source(_GrantEverything())
    raise RuntimeError("the module's schema is missing; run the database upgrade")
