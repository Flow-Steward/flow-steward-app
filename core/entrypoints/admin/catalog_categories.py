"""
Admin API route — the category list shared by agents, tools and skills.
"""

from __future__ import annotations

from fastapi import APIRouter

from core.application.management.query_models import CatalogCategoryView
from core.application.registry.catalog_categories import catalog_category_options

router = APIRouter(prefix="/api", tags=["catalog-categories"])


@router.get("/catalog-categories", response_model=list[CatalogCategoryView])
def list_catalog_categories():
    """List the categories an agent, tool or skill can be filed under."""
    return catalog_category_options()


__all__ = ["router"]
