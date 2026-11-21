from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.entrypoints.api.catalog_search import create_catalog_search_router


def test_catalog_search_finds_entries_across_sources_without_a_project(monkeypatch) -> None:
    from core.application.registry import catalog_search

    def index(*items):
        return SimpleNamespace(items=items)

    monkeypatch.setattr(
        catalog_search,
        "load_extension_catalog",
        lambda: index(
            SimpleNamespace(
                extension_id="example.shop",
                name="Shopify",
                version="1.0.0",
                summary="Read Shopify orders",
                description="",
                categories=("commerce",),
                tags=("store",),
                capabilities=("data_source",),
                setup_summary="Link a store",
                release_zip_sha256="a" * 64,
                publication_status="published",
                verification_status="verified",
            )
        ),
    )
    monkeypatch.setattr(
        catalog_search,
        "load_mcp_server_catalog",
        lambda: index(
            SimpleNamespace(
                item_id="example.shop",
                title="Shopify server",
                server_version="",
                summary="Shopify administration",
                description="",
                keywords=("store",),
                category="commerce",
                remotes=(),
                status="published",
            )
        ),
    )
    monkeypatch.setattr(
        catalog_search,
        "load_workflow_template_catalog",
        lambda: index(
            SimpleNamespace(
                item_id="shop-orders",
                name="Import orders",
                version="1.0.0",
                summary="Import Shopify orders",
                description="",
                integrations=("shopify",),
                tags=(),
                categories=(),
                setup_requirements=(),
                publication_status="published",
                verification_status="verified",
                importable=True,
            )
        ),
    )
    for domain in ("extension", "mcp_server", "workflow_template"):
        monkeypatch.setattr(
            catalog_search,
            f"{domain}_catalog_availability",
            lambda _index: {"available": True, "stale": False},
        )
    monkeypatch.setattr(catalog_search, "catalog_release_compatible", lambda *_a, **_kw: True)

    app = FastAPI()
    app.include_router(create_catalog_search_router())
    response = TestClient(app).get("/api/catalog/search", params={"query": "Shopify"})

    assert response.status_code == 200
    data = response.json()
    assert data["match_count"] == 3
    assert [(item["kind"], item["detail_url"]) for item in data["items"]] == [
        ("extension", "/extensions?find=example.shop#marketplace"),
        ("mcp_server", "/registry/mcp?tab=catalog&mcp_entry=example.shop"),
        ("workflow_template", "/registry/workflows?tab=catalog&tpl=shop-orders"),
    ]
    assert data["items"][0]["setup_summary"] == "Link a store"
    assert data["items"][1]["operations_status"] == "requires_connection"


def test_catalog_search_distinguishes_unavailable_sources_from_zero_matches(monkeypatch) -> None:
    from core.application.registry import catalog_search

    def unavailable():
        raise catalog_search.McpServerCatalogError(
            "Catalog is unavailable", code="mcp_catalog_unavailable"
        )

    monkeypatch.setattr(catalog_search, "load_mcp_server_catalog", unavailable)
    app = FastAPI()
    app.include_router(create_catalog_search_router())
    client = TestClient(app)

    response = client.get("/api/catalog/search", params={"kind": "mcp_server", "query": "shop"})

    assert response.status_code == 200
    assert response.json() == {
        "items": [],
        "match_count": 0,
        "catalogs": {"mcp_server": {"available": False, "reason": "mcp_catalog_unavailable"}},
    }
    assert client.get("/api/catalog/search", params={"query": "  "}).status_code == 422
