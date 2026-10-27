"""Route placement for the two catalog surfaces.

Both catalogs live one path segment below an existing `{id}` parameter route.
FastAPI matches in registration order, so `/catalog` only works while it is
registered first — and nothing about the code reads as if it depends on import
order. These pin it.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _admin_workflow_router_paths() -> list[tuple[str, frozenset[str]]]:
    import core.entrypoints.admin.workflows  # noqa: F401 - registers every route module
    from core.entrypoints.admin.workflows_common import router

    return [
        (str(route.path), frozenset(getattr(route, "methods", set()) or set()))
        for route in router.routes
    ]


def _first_index(paths: list[tuple[str, frozenset[str]]], path: str, method: str) -> int:
    for index, (candidate, methods) in enumerate(paths):
        if candidate == path and method in methods:
            return index
    raise AssertionError(f"{method} {path} is not registered")


def test_the_workflow_catalog_is_registered_before_the_workflow_id_route() -> None:
    paths = _admin_workflow_router_paths()

    catalog = _first_index(paths, "/api/workflows/catalog", "GET")
    by_id = _first_index(paths, "/api/workflows/{workflow_id}", "GET")

    assert catalog < by_id, "GET /api/workflows/catalog would be read as a workflow id"


def test_every_workflow_catalog_route_precedes_the_workflow_id_route() -> None:
    paths = _admin_workflow_router_paths()
    by_id = _first_index(paths, "/api/workflows/{workflow_id}", "GET")
    catalog_indexes = [
        index
        for index, (path, _methods) in enumerate(paths)
        if path.startswith("/api/workflows/catalog")
    ]

    assert catalog_indexes, "no workflow catalog routes are registered"
    assert max(catalog_indexes) < by_id


def _include_router_order() -> list[str]:
    """Names of the routers `register_api_routes` includes, in order."""
    source = (ROOT / "core/entrypoints/api/route_bindings.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    order: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "include_router":
            continue
        if not node.args:
            continue
        argument = node.args[0]
        if isinstance(argument, ast.Name):
            order.append(argument.id)
    return order


def test_the_extension_catalog_router_is_included_before_the_extension_id_routes() -> None:
    order = _include_router_order()

    assert "extension_catalog_router" in order
    assert order.index("extension_catalog_router") < order.index(
        "integrations_connectors_router"
    ), "GET /api/extensions/catalog would be read as an extension id"
    assert order.index("extension_catalog_router") < order.index("reactions_extensions_router")


def test_the_mcp_catalog_router_is_included_before_the_connector_id_routes() -> None:
    order = _include_router_order()

    assert "mcp_server_catalog_router" in order
    assert order.index("mcp_server_catalog_router") < order.index(
        "integrations_connectors_router"
    ), "GET /api/mcp/catalog would be read as a connector id"
