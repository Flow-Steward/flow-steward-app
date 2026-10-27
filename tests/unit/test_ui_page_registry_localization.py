"""First-party settings metadata follows the user's interface language."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from core.application.reports.ui_page_registry import (
    register_core_settings_pages,
    register_page,
    resolve_registered_navigation_sections,
    resolve_registered_pages,
    resolve_registered_settings_pages,
)


def test_settings_metadata_uses_lithuanian_profile_without_mutating_registration() -> None:
    registries: dict[str, Any] = {}
    register_core_settings_pages(registries)
    original = deepcopy(registries)

    pages = resolve_registered_settings_pages(
        registries, auth_user={"roles": ["admin"], "locale": "lt-LT"}, include_core=True
    )

    assert {page["page_key"]: page["title"] for page in pages} == {
        "general": "Bendra",
        "security": "Saugumas",
        "retention": "Saugojimas",
        "notifications": "Pranešimai",
        "service_accounts": "Tarnybinės paskyros",
        "model_providers": "Modelių tiekėjai",
    }
    assert registries == original
    english_pages = resolve_registered_settings_pages(
        registries, auth_user={"roles": ["admin"], "locale": "en"}, include_core=True
    )
    assert english_pages[0]["title"] == "General"


def test_core_navigation_metadata_localizes_all_visible_label_fields() -> None:
    registries: dict[str, Any] = {}
    register_core_settings_pages(registries)

    sections = resolve_registered_navigation_sections(
        registries, auth_user={"roles": ["admin"], "locale": "lt"}, include_core=True
    )

    pages = {page["page_key"]: page for rows in sections.values() for page in rows}
    for page_key, expected_label in {
        "settings_home": "Nustatymai",
        "account": "Naudotojo nustatymai",
        "users": "Naudotojai ir prieiga",
    }.items():
        assert pages[page_key]["title"] == expected_label
        assert pages[page_key]["label"] == expected_label
        assert pages[page_key]["nav_label"] == expected_label


def test_registered_core_page_metadata_uses_profile_locale() -> None:
    registries: dict[str, Any] = {}
    register_core_settings_pages(registries)
    page = registries["settings_pages"]["general"]
    page["component_key"] = "settings.general"
    registries["registered_pages"]["general"] = page

    pages = resolve_registered_pages(
        registries, auth_user={"roles": ["admin"], "locale": "lt"}, include_core=True
    )

    assert pages[0]["title"] == "Bendra"
    assert pages[0]["label"] == "Bendra"


def test_extension_supplied_titles_remain_operator_content() -> None:
    registries: dict[str, Any] = {}
    register_page(
        registries,
        page_key="general",
        title="General",
        path="/settings/vendor-general",
        component_key="vendor.general",
        settings_tab=True,
        sidebar_nav=True,
    )

    user = {"locale": "lt"}
    settings = resolve_registered_settings_pages(registries, auth_user=user)
    navigation = resolve_registered_navigation_sections(registries, auth_user=user)
    pages = resolve_registered_pages(registries, auth_user=user)

    assert settings[0]["title"] == "General"
    assert navigation["system"][0]["nav_label"] == "General"
    assert pages[0]["label"] == "General"


def test_first_party_extensions_metadata_localizes_without_changing_default_visibility() -> None:
    registries: dict[str, Any] = {}
    register_core_settings_pages(registries)

    sections = resolve_registered_navigation_sections(
        registries, auth_user={"roles": ["admin"], "locale": "lt"}
    )

    assert [page["page_key"] for page in sections["system"]] == ["extensions"]
    extensions = sections["system"][0]
    assert extensions["title"] == "Plėtiniai"
    assert extensions["label"] == "Plėtiniai"
    assert extensions["nav_label"] == "Plėtiniai"
    assert extensions["core_page"] is False
    assert not any(field.startswith("_") for field in extensions)
    english = resolve_registered_navigation_sections(
        registries, auth_user={"roles": ["admin"], "locale": "en"}
    )
    assert english["system"][0]["title"] == "Extensions"


@pytest.mark.parametrize("title", ["Extensions", "Vendor-provided extensions"])
@pytest.mark.parametrize("core_page", [False, True])
def test_same_key_extension_replacement_does_not_inherit_core_translation(
    title: str, core_page: bool
) -> None:
    registries: dict[str, Any] = {}
    register_core_settings_pages(registries)
    register_page(
        registries,
        page_key="extensions",
        title=title,
        label="Vendor tools",
        path="/extensions",
        component_key="vendor.extensions",
        sidebar_nav=True,
        core_page=core_page,
    )

    user = {"roles": ["admin"], "locale": "lt"}
    navigation = resolve_registered_navigation_sections(
        registries, auth_user=user, include_core=True
    )
    pages = resolve_registered_pages(registries, auth_user=user, include_core=True)

    extensions = next(page for page in navigation["system"] if page["page_key"] == "extensions")
    assert extensions["title"] == title
    assert extensions["nav_label"] == "Vendor tools"
    assert pages[0]["title"] == title
    assert pages[0]["label"] == "Vendor tools"
