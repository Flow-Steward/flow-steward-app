"""Choice fields of host-rendered resource_admin pages (ADR 0082, section 6).

A module declares choices with human labels (or a same-module path that lists
them); the registry keeps every value it was given and never lets a choice reach
outside the module's own API tree.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from core.application.reports.ui_page_registry import (
    page_registration_scope,
    register_page,
    resolve_registered_pages,
)

_MODULE = "acme.ledger"
_PREFIX = f"/api/modules/{_MODULE}"


def _surface(*fields: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "resource_admin",
        "resources": [
            {
                "key": "records",
                "title": "Records",
                "list_path": f"{_PREFIX}/records",
                "item_path": f"{_PREFIX}/records/{{id}}",
                "id_field": "id",
                "fields": [{"key": "name", "label": "Name", "type": "text"}, *fields],
            }
        ],
    }


def _register(*fields: dict[str, Any]) -> dict[str, Any]:
    registries: dict[str, Any] = {}
    with page_registration_scope(module_id=_MODULE):
        register_page(
            registries,
            key="ledger_records",
            path="/ledger/records",
            control_surface=_surface(*fields),
        )
    return registries


def _fields(registries: dict[str, Any]) -> list[dict[str, Any]]:
    record = registries["registered_pages"]["ledger_records"]
    return record["control_surface"]["resources"][0]["fields"]


def test_select_options_keep_their_labels_and_text_options_label_themselves() -> None:
    registries = _register(
        {
            "key": "mode",
            "type": "select",
            "options": [
                "invite_only",
                {
                    "value": "jit",
                    "label": "Create people on first sign-in",
                    "description": "At once",
                },
            ],
        }
    )

    assert _fields(registries)[1]["options"] == [
        {"value": "invite_only", "label": "invite_only"},
        {"value": "jit", "label": "Create people on first sign-in", "description": "At once"},
    ]


def test_new_field_kinds_and_their_texts_are_accepted() -> None:
    registries = _register(
        {"key": "secret", "type": "secret", "help": "Stored encrypted."},
        {
            "key": "provider",
            "type": "reference",
            "required": True,
            "options_path": f"{_PREFIX}/provider-options",
            "empty_options_label": "Add a provider first.",
        },
        {
            "key": "roles",
            "type": "multi_choice",
            "options": [{"value": "viewer", "label": "Viewer"}],
            "default": ["viewer"],
        },
        {
            "key": "tenants",
            "type": "csv",
            "advanced": True,
            "visible_when": {"field": "name", "contains": "{"},
        },
    )

    fields = {field["key"]: field for field in _fields(registries)}
    assert fields["secret"]["type"] == "secret"
    assert fields["provider"]["options_path"] == f"{_PREFIX}/provider-options"
    assert "options" not in fields["provider"]
    assert fields["roles"]["options"] == [{"value": "viewer", "label": "Viewer"}]
    assert fields["tenants"]["visible_when"] == {"field": "name", "contains": "{"}


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ({"key": "mode", "type": "select"}, "requires options"),
        ({"key": "mode", "type": "select", "options": []}, "requires options"),
        ({"key": "mode", "type": "select", "options": ["a", "a"]}, "repeats"),
        ({"key": "mode", "type": "select", "options": [{"label": "No value"}]}, "without a value"),
        (
            {"key": "mode", "type": "select", "options_path": f"{_PREFIX}/options"},
            "declare it as a reference",
        ),
        ({"key": "who", "type": "reference"}, "requires options_path"),
        (
            {
                "key": "who",
                "type": "multi_choice",
                "options": ["a"],
                "options_path": f"{_PREFIX}/options",
            },
            "both options and options_path",
        ),
        ({"key": "note", "type": "text", "options": ["a"]}, "not a choice field"),
        ({"key": "note", "type": "text", "help": ["not", "text"]}, "help must be text"),
        (
            {"key": "note", "type": "text", "visible_when": {"field": "note", "contains": "x"}},
            "another field",
        ),
        (
            {"key": "note", "type": "text", "visible_when": {"field": "missing", "contains": "x"}},
            "unknown field",
        ),
    ],
)
def test_an_invalid_choice_field_is_refused(field: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _register(field)


@pytest.mark.parametrize(
    "options_path",
    [
        "/api/modules/acme.other/provider-options",
        "/api/users",
        "https://evil.example/options",
        "//evil.example/api/options",
        f"{_PREFIX}/../users",
        f"{_PREFIX}/{{id}}",
    ],
)
def test_reference_options_stay_in_the_modules_own_tree(options_path: str) -> None:
    with pytest.raises(ValueError):
        _register({"key": "who", "type": "reference", "options_path": options_path})


def test_report_select_filter_may_load_its_choices_from_the_module() -> None:
    registries: dict[str, Any] = {}
    surface = {
        "kind": "resource_report",
        "list_path": f"{_PREFIX}/journal",
        "columns": [{"key": "workflow_name", "label": "Workflow"}],
        "filters": [
            {
                "key": "workflow_id",
                "label": "Workflow",
                "type": "select",
                "options_path": f"{_PREFIX}/journal-workflows",
            }
        ],
    }
    with page_registration_scope(module_id=_MODULE):
        record = register_page(
            registries, key="ledger_journal", path="/ledger/journal", control_surface=surface
        )

    assert record["control_surface"]["filters"][0]["options_path"] == (
        f"{_PREFIX}/journal-workflows"
    )

    refused = deepcopy(surface)
    refused["filters"][0]["options_path"] = "/api/modules/acme.other/journal-workflows"
    with page_registration_scope(module_id=_MODULE), pytest.raises(ValueError):
        register_page({}, key="ledger_journal", path="/ledger/journal", control_surface=refused)


def test_resource_admin_labels_help_and_options_use_the_ui_catalogue() -> None:
    registries = _register(
        {
            "key": "since",
            "type": "select",
            "label": "From",
            "label_key": "react.shared.filter_from",
            "help": "Look",
            "help_key": "react.shared.appearance",
            "options": [
                {"value": "a", "label": "From", "label_key": "react.shared.filter_from"},
                {"value": "b", "label": "Plain"},
            ],
        }
    )
    original = deepcopy(registries)

    [page] = resolve_registered_pages(registries, auth_user={"locale": "lt"})

    field = page["control_surface"]["resources"][0]["fields"][1]
    assert (field["label"], field["help"]) == ("Nuo", "Išvaizda")
    assert [option["label"] for option in field["options"]] == ["Nuo", "Plain"]
    assert registries == original

    [english] = resolve_registered_pages(registries, auth_user={"locale": "en"})
    english_field = english["control_surface"]["resources"][0]["fields"][1]
    assert (english_field["label"], english_field["options"][0]["label"]) == ("From", "From")
