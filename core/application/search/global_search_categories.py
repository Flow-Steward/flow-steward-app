from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field

from core.application.auth.principal import principal_type_has_minimum_role
from core.application.management.errors import ServiceValidationError
from core.application.management.scope import ScopeContext

DEFAULT_GLOBAL_SEARCH_CATEGORY = "all"


@dataclass(frozen=True, slots=True)
class PublicReleaseEntityFamily:
    """One public release family and its explicit global-search disposition."""

    search_group: str
    label: str
    minimum_role: str


_FAMILY_LABELS: tuple[tuple[str, str, str], ...] = (
    ("jobs", "Jobs / workflow runs", "viewer"),
    ("events", "Events", "viewer"),
    ("runtime_messages", "Runtime messages", "viewer"),
    ("workflows", "Workflows", "operator"),
    ("agents", "Agents", "admin"),
    ("tools", "Tools", "admin"),
    ("skills", "Skills", "admin"),
    ("mcp_connectors", "MCP connectors", "admin"),
    ("projects", "Projects", "viewer"),
    ("extensions", "Extensions", "operator"),
    ("guardrails", "Guardrails", "admin"),
    ("data_contracts", "Data contracts", "admin"),
    ("fields", "Fields", "admin"),
    ("published_blocks", "Published blocks", "admin"),
    ("datasets", "Datasets", "admin"),
    ("saved_queries", "Saved queries", "admin"),
    ("data_connections", "Data connections", "admin"),
    ("model_providers", "Model providers", "admin"),
    ("model_routes", "Model routes", "admin"),
    ("users", "Users", "admin"),
    ("workspaces", "Workspaces", "admin"),
)

# Canonical release-core search registry. Adding a public family requires an
# explicit search group here, so category=all cannot drift from a second list.
PUBLIC_RELEASE_ENTITY_FAMILIES: dict[str, PublicReleaseEntityFamily] = {
    family_id: PublicReleaseEntityFamily(
        search_group=family_id,
        label=label,
        minimum_role=minimum_role,
    )
    for family_id, label, minimum_role in _FAMILY_LABELS
}


def searchable_release_entity_families() -> dict[str, PublicReleaseEntityFamily]:
    return dict(PUBLIC_RELEASE_ENTITY_FAMILIES)


def supported_global_search_result_groups() -> tuple[str, ...]:
    return tuple(family.search_group for family in PUBLIC_RELEASE_ENTITY_FAMILIES.values())


def _category_to_groups() -> dict[str, tuple[str, ...]]:
    all_groups = supported_global_search_result_groups()
    return {"all": all_groups, **{group: (group,) for group in all_groups}}


def global_search_category_options() -> tuple[dict[str, str], ...]:
    options = [{"value": "all", "label": "Everything"}]
    options.extend(
        {"value": family.search_group, "label": family.label}
        for family in PUBLIC_RELEASE_ENTITY_FAMILIES.values()
    )
    return tuple(options)


def global_search_group_labels() -> dict[str, str]:
    return {family.search_group: family.label for family in PUBLIC_RELEASE_ENTITY_FAMILIES.values()}


class SearchCategoryOption(BaseModel):
    value: str
    label: str


class GlobalSearchConfiguration(BaseModel):
    default_category: str
    categories: list[SearchCategoryOption]
    group_labels: dict[str, str]
    filters: dict[str, list[SearchCategoryOption]] = Field(default_factory=dict)


def global_search_configuration(scope: ScopeContext) -> GlobalSearchConfiguration:
    """Publish only categories supported by the caller's existing search policy."""
    principal_type = str(scope.principal_type or "user").strip().lower() or "user"
    families = [
        family
        for family in PUBLIC_RELEASE_ENTITY_FAMILIES.values()
        if principal_type_has_minimum_role(principal_type, family.minimum_role)
    ]
    return GlobalSearchConfiguration(
        default_category=DEFAULT_GLOBAL_SEARCH_CATEGORY,
        categories=[
            SearchCategoryOption(value=DEFAULT_GLOBAL_SEARCH_CATEGORY, label="Everything"),
            *[
                SearchCategoryOption(value=family.search_group, label=family.label)
                for family in families
            ],
        ],
        group_labels={family.search_group: family.label for family in families},
    )


def supported_global_search_categories() -> tuple[str, ...]:
    return tuple(_category_to_groups().keys())


def global_search_category_groups(category: str | None) -> tuple[str, tuple[str, ...]]:
    normalized = str(category or "").strip().lower() or DEFAULT_GLOBAL_SEARCH_CATEGORY
    groups = _category_to_groups().get(normalized)
    if groups is None:
        raise ServiceValidationError(
            "Unsupported search category",
            payload={
                "error": "Unsupported search category",
                "supported_categories": list(supported_global_search_categories()),
            },
        )
    return normalized, groups


__all__ = [
    "DEFAULT_GLOBAL_SEARCH_CATEGORY",
    "PUBLIC_RELEASE_ENTITY_FAMILIES",
    "GlobalSearchConfiguration",
    "PublicReleaseEntityFamily",
    "global_search_category_groups",
    "global_search_category_options",
    "global_search_configuration",
    "global_search_group_labels",
    "searchable_release_entity_families",
    "supported_global_search_categories",
    "supported_global_search_result_groups",
]
