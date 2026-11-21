"""Read-only catalog search HTTP adapter."""

from __future__ import annotations

from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query

from core.application.registry.catalog_search import CatalogSelection, search_catalogs

_CATALOG_DETAIL_PATHS = {
    "extension": "/extensions?find={id}#marketplace",
    "mcp_server": "/registry/mcp?tab=catalog&mcp_entry={id}",
    "workflow_template": "/registry/workflows?tab=catalog&tpl={id}",
}


def create_catalog_search_router() -> APIRouter:
    router = APIRouter(tags=["catalogs"])

    @router.get("/api/catalog/search")
    def api_catalog_search(
        query: Annotated[str, Query(min_length=1, max_length=200)],
        kind: CatalogSelection = "all",
        limit: Annotated[int, Query(ge=1, le=25)] = 12,
    ):
        if not query.strip():
            raise HTTPException(status_code=422, detail="Enter a catalog search query.")
        result = search_catalogs(query, kind=kind, limit=limit)
        for item in result["items"]:
            item["detail_url"] = _CATALOG_DETAIL_PATHS[item["kind"]].format(
                id=quote(item["id"], safe="")
            )
        return result

    return router
