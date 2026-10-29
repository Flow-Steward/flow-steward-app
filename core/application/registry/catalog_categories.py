"""The one category list shared by agents, tools, skills and every catalog.

The workflow, extension and MCP server catalogs carry the same list in their
``categories.json``; a category is always one of these ids, never free text.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import AfterValidator

CATALOG_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("ai", "AI"),
    ("cms", "CMS"),
    ("crm", "CRM"),
    ("data", "Data"),
    ("developer-tools", "Developer tools"),
    ("documents", "Documents"),
    ("ecommerce", "E-commerce"),
    ("email", "Email"),
    ("finance", "Finance"),
    ("hr", "HR"),
    ("inventory", "Inventory"),
    ("it", "IT"),
    ("marketing", "Marketing"),
    ("media", "Media"),
    ("messaging", "Messaging"),
    ("operations", "Operations"),
    ("other", "Other"),
    ("productivity", "Productivity"),
    ("sales", "Sales"),
    ("support", "Support"),
)
CATALOG_CATEGORY_IDS: frozenset[str] = frozenset(item_id for item_id, _ in CATALOG_CATEGORIES)
#: What an entry without a category is filed under.
DEFAULT_CATALOG_CATEGORY = "other"


def catalog_category(value: Any) -> str:
    """Return the category id for ``value``; a blank value files under the default.

    Raises ``ValueError`` for anything outside the shared list.
    """
    normalized = str(value or "").strip().lower()
    if not normalized:
        return DEFAULT_CATALOG_CATEGORY
    if normalized not in CATALOG_CATEGORY_IDS:
        allowed = ", ".join(item_id for item_id, _ in CATALOG_CATEGORIES)
        raise ValueError(f"category must be one of: {allowed}")
    return normalized


#: A request field that only accepts a shared category id.
CatalogCategory = Annotated[str, AfterValidator(catalog_category)]


def known_catalog_category(value: Any) -> str:
    """Return ``value`` as a category id when it is one, else an empty string."""
    normalized = str(value or "").strip().lower()
    return normalized if normalized in CATALOG_CATEGORY_IDS else ""


def catalog_category_options() -> list[dict[str, str]]:
    return [{"id": item_id, "label": label} for item_id, label in CATALOG_CATEGORIES]


__all__ = [
    "CATALOG_CATEGORIES",
    "CATALOG_CATEGORY_IDS",
    "DEFAULT_CATALOG_CATEGORY",
    "CatalogCategory",
    "catalog_category",
    "catalog_category_options",
    "known_catalog_category",
]
