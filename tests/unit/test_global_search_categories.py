from __future__ import annotations

import pytest

from core.application.management.errors import ServiceValidationError
from core.application.search.global_search_categories import (
    DEFAULT_GLOBAL_SEARCH_CATEGORY,
    PUBLIC_RELEASE_ENTITY_FAMILIES,
    global_search_category_groups,
    global_search_category_options,
    global_search_group_labels,
    searchable_release_entity_families,
    supported_global_search_categories,
    supported_global_search_result_groups,
)


def test_global_search_category_options_include_expected_entries() -> None:
    options = global_search_category_options()
    values = [str(item.get("value") or "") for item in options]
    assert values[0] == "all"
    assert "jobs" in values
    assert "events" in values
    assert "runtime_messages" in values
    assert "workflows" in values
    assert "kb" not in values
    assert "code" not in values
    assert "chats" not in values
    assert {
        "extensions",
        "guardrails",
        "data_contracts",
        "saved_queries",
        "model_providers",
        "model_routes",
        "users",
        "workspaces",
        "fields",
        "published_blocks",
    }.issubset(values)


def test_public_release_entity_registry_is_exhaustively_classified_for_search() -> None:
    searchable = searchable_release_entity_families()
    assert set(searchable) == set(supported_global_search_result_groups())
    assert set(PUBLIC_RELEASE_ENTITY_FAMILIES) == set(searchable)
    assert all(family.search_group == family_id for family_id, family in searchable.items())


def test_global_search_category_groups_returns_expected_mapping() -> None:
    category, groups = global_search_category_groups("jobs")
    assert category == "jobs"
    assert groups == ("jobs",)

    default_category, default_groups = global_search_category_groups("")
    assert default_category == DEFAULT_GLOBAL_SEARCH_CATEGORY
    assert default_groups


def test_global_search_category_groups_rejects_unknown_category() -> None:
    with pytest.raises(ServiceValidationError, match="Unsupported search category"):
        global_search_category_groups("unknown-lane")

    labels = global_search_group_labels()
    assert labels["jobs"] == "Jobs / workflow runs"
    assert labels["workflows"] == "Workflows"
    assert "all" in supported_global_search_categories()
    assert "jobs" in supported_global_search_categories()
    assert "workflows" in supported_global_search_categories()
