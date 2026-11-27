"""First-party settings metadata follows the user's interface language."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.reports.ui_page_registry import (
    page_registration_scope,
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
        registries,
        auth_user={"account_id": DEFAULT_ACCOUNT_ID, "roles": ["admin"], "locale": "lt-LT"},
        include_core=True,
    )

    assert {page["page_key"]: page["title"] for page in pages} == {
        "general": "Bendra",
        "security": "Saugumas",
        "email": "El. paštas",
        "retention": "Saugojimas",
        "notifications": "Pranešimai",
        "service_accounts": "Tarnybinės paskyros",
        "model_providers": "Pokalbių modeliai",
        "secret_storage": "Paslapčių saugykla",
    }
    assert registries == original
    english_pages = resolve_registered_settings_pages(
        registries,
        auth_user={"account_id": DEFAULT_ACCOUNT_ID, "roles": ["admin"], "locale": "en"},
        include_core=True,
    )
    assert english_pages[0]["title"] == "General"
    assert (
        next(page for page in english_pages if page["page_key"] == "model_providers")["title"]
        == "Chat Models"
    )


def test_core_navigation_metadata_localizes_all_visible_label_fields() -> None:
    registries: dict[str, Any] = {}
    register_core_settings_pages(registries)

    sections = resolve_registered_navigation_sections(
        registries,
        auth_user={"account_id": DEFAULT_ACCOUNT_ID, "roles": ["admin"], "locale": "lt"},
        include_core=True,
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
        registries,
        auth_user={"account_id": DEFAULT_ACCOUNT_ID, "roles": ["admin"], "locale": "lt"},
        include_core=True,
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
        registries, auth_user={"account_id": DEFAULT_ACCOUNT_ID, "roles": ["admin"], "locale": "lt"}
    )

    assert [page["page_key"] for page in sections["system"]] == ["extensions"]
    extensions = sections["system"][0]
    assert extensions["title"] == "Plėtiniai"
    assert extensions["label"] == "Plėtiniai"
    assert extensions["nav_label"] == "Plėtiniai"
    assert extensions["core_page"] is False
    assert not any(field.startswith("_") for field in extensions)
    english = resolve_registered_navigation_sections(
        registries, auth_user={"account_id": DEFAULT_ACCOUNT_ID, "roles": ["admin"], "locale": "en"}
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

    user = {"account_id": DEFAULT_ACCOUNT_ID, "roles": ["admin"], "locale": "lt"}
    navigation = resolve_registered_navigation_sections(
        registries, auth_user=user, include_core=True
    )
    pages = resolve_registered_pages(registries, auth_user=user, include_core=True)

    extensions = next(page for page in navigation["system"] if page["page_key"] == "extensions")
    assert extensions["title"] == title
    assert extensions["nav_label"] == "Vendor tools"
    assert pages[0]["title"] == title
    assert pages[0]["label"] == "Vendor tools"


def _register_journal_report(registries: dict[str, Any]) -> None:
    with page_registration_scope(module_id="acme.journal"):
        register_page(
            registries,
            page_key="acme_journal",
            title="Journal",
            path="/acme/journal",
            control_surface={
                "kind": "resource_report",
                "list_path": "/api/modules/acme.journal/entries",
                "columns": [
                    {
                        "key": "workflow_name",
                        "label": "Workflow",
                        "label_key": "jobs.columns.workflow",
                    },
                    {"key": "note", "label": "Note"},
                    {
                        "key": "job_id",
                        "label": "Job",
                        "type": "link",
                        "link_path": "/jobs/{job_id}",
                        "link_if": "job_exists",
                        "link_missing_label": "Deleted",
                        "link_missing_label_key": "jobs.bulk.deleted",
                    },
                ],
                "filters": [
                    {
                        "key": "date_from",
                        "label": "Date from",
                        "label_key": "react.shared.filter_from",
                        "type": "date",
                    },
                    {
                        "key": "action",
                        "label": "Action",
                        "label_key": "security_overview.columns.action",
                        "type": "select",
                        "options": [
                            {
                                "value": "comment",
                                "label": "Comment",
                                "label_key": "jobs.detail.review.comment",
                            }
                        ],
                    },
                ],
            },
        )


def test_resource_report_labels_use_the_ui_catalogue_like_page_titles() -> None:
    registries: dict[str, Any] = {}
    _register_journal_report(registries)
    original = deepcopy(registries)

    [page] = resolve_registered_pages(registries, auth_user={"locale": "lt"})

    surface = page["control_surface"]
    assert [column["label"] for column in surface["columns"]] == ["Darbo eiga", "Note", "Job"]
    assert surface["columns"][2]["link_missing_label"] == "Ištrinta"
    assert [item["label"] for item in surface["filters"]] == ["Nuo", "Veiksmas"]
    assert surface["filters"][1]["options"][0]["label"] == "Komentaras"
    # The title is module content, like any module page title.
    assert page["title"] == "Journal"
    assert registries == original

    [english] = resolve_registered_pages(registries, auth_user={"locale": "en"})
    english_surface = english["control_surface"]
    assert [column["label"] for column in english_surface["columns"]] == [
        "Workflow",
        "Note",
        "Job",
    ]
    assert english_surface["columns"][2]["link_missing_label"] == "Deleted"
    assert [item["label"] for item in english_surface["filters"]] == ["Date from", "Action"]


def test_status_document_labels_use_the_ui_catalogue() -> None:
    registries: dict[str, Any] = {}
    with page_registration_scope(module_id="acme.status"):
        register_page(
            registries,
            page_key="acme_status",
            title="Status",
            path="/settings/acme-status",
            settings_tab=True,
            control_surface={
                "kind": "status_document",
                "status_path": "/api/modules/acme.status/status",
                "upload_path": "/api/modules/acme.status/document",
                "upload_label": "Look",
                "upload_label_key": "react.shared.appearance",
                "fields": [
                    {"key": "since", "label": "From", "label_key": "react.shared.filter_from"},
                    {"key": "state", "label": "State"},
                ],
            },
        )
    original = deepcopy(registries)

    [page] = resolve_registered_pages(registries, auth_user={"locale": "lt"})

    surface = page["control_surface"]
    assert [field["label"] for field in surface["fields"]] == ["Nuo", "State"]
    assert surface["upload_label"] == "Išvaizda"
    assert registries == original

    [english] = resolve_registered_pages(registries, auth_user={"locale": "en"})
    assert [field["label"] for field in english["control_surface"]["fields"]] == ["From", "State"]
    assert english["control_surface"]["upload_label"] == "Look"
