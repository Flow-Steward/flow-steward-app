"""resource_admin row actions (ADR 0082, 2026-11-27)."""

from __future__ import annotations

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.reports.ui_page_registry import (
    page_registration_scope,
    register_page,
    resolve_registered_pages,
)

MODULE = "acme.ledger"
REVOKE = {
    "key": "revoke",
    "label": "Revoke sessions",
    "method": "POST",
    "path_template": "/api/modules/acme.ledger/people/{id}/revoke",
}


def _registries() -> dict:
    return {"settings_pages": {}, "registered_pages": {}, "navigation_items": {}}


def _register(registries: dict, row_actions) -> None:
    resource = {
        "key": "people",
        "title": "People",
        "list_path": "/api/modules/acme.ledger/people",
        "item_path": "/api/modules/acme.ledger/people/{id}",
        "id_field": "id",
        "fields": [{"key": "name", "label": "Name", "type": "text"}],
    }
    if row_actions is not None:
        resource["row_actions"] = row_actions
    with page_registration_scope(module_id=MODULE):
        register_page(
            registries,
            key="module_people",
            label="People",
            path="/modules/people",
            sidebar_nav=True,
            control_surface={"kind": "resource_admin", "resources": [resource]},
        )


def _resolved_resource(registries: dict, locale: str | None = None) -> dict:
    pages = resolve_registered_pages(
        registries,
        edition="community",
        auth_user={"account_id": DEFAULT_ACCOUNT_ID, "roles": ["admin"]},
        locale=locale,
    )
    return pages[0]["control_surface"]["resources"][0]


def test_a_row_action_in_the_modules_tree_is_kept_with_post_as_its_method() -> None:
    registries = _registries()
    action = {key: value for key, value in REVOKE.items() if key != "method"}
    _register(
        registries,
        [
            {**action, "confirm": " Sign out everywhere? ", "success_text": "Revoked."},
            {
                "key": "resync",
                "label": "Resync",
                "path_template": "/api/modules/acme.ledger/people/{id}",
            },
        ],
    )

    resource = _resolved_resource(registries)

    assert resource["row_actions"] == [
        {
            "key": "revoke",
            "label": "Revoke sessions",
            "method": "POST",
            "path_template": "/api/modules/acme.ledger/people/{id}/revoke",
            "confirm": "Sign out everywhere?",
            "success_text": "Revoked.",
        },
        {
            "key": "resync",
            "label": "Resync",
            "method": "POST",
            "path_template": "/api/modules/acme.ledger/people/{id}",
        },
    ]


def test_a_resource_without_row_actions_is_unchanged() -> None:
    registries = _registries()
    _register(registries, None)

    assert "row_actions" not in _resolved_resource(registries)


@pytest.mark.parametrize(
    "change",
    [
        {"path_template": "/api/modules/acme.other/people/{id}/revoke"},
        {"path_template": "/api/admin/users/{id}/revoke"},
        {"path_template": "//evil.example/api/modules/acme.ledger/people/{id}"},
        {"path_template": "/api/modules/acme.ledger/people/../{id}"},
        {"path_template": "/api/modules/acme.ledger/people/{id}/revoke?all=1"},
        {"path_template": "/api/modules/acme.ledger/people/revoke"},
        {"path_template": "/api/modules/acme.ledger/{account}/{id}"},
        {"path_template": "/api/modules/acme.ledger/people/x{id}"},
        {"path_template": ""},
        {"method": "DELETE"},
        {"method": "GET"},
        {"label": ""},
        {"key": "Revoke"},
        {"key": ""},
        {"confirm": 3},
        {"target": "_blank"},
    ],
)
def test_a_row_action_core_cannot_render_safely_is_refused(change) -> None:
    registries = _registries()

    with pytest.raises(ValueError):
        _register(registries, [{**REVOKE, **change}])

    assert "module_people" not in registries["registered_pages"]


@pytest.mark.parametrize("row_actions", ["revoke", {"key": "revoke"}, [REVOKE, REVOKE], ["revoke"]])
def test_malformed_row_action_lists_are_refused(row_actions) -> None:
    with pytest.raises(ValueError):
        _register(_registries(), row_actions)


def test_row_action_texts_with_a_catalogue_key_are_translated(monkeypatch) -> None:
    from core.application.reports import ui_page_registry, ui_page_surfaces

    for module in (ui_page_registry, ui_page_surfaces):
        monkeypatch.setattr(
            module,
            "translate_ui_text",
            lambda key, *, locale, default="": f"{locale}:{key}",
        )
    registries = _registries()
    _register(
        registries,
        [
            {
                **REVOKE,
                "label_key": "acme.revoke",
                "confirm": "Sure?",
                "confirm_key": "acme.revoke_confirm",
            }
        ],
    )

    [action] = _resolved_resource(registries, locale="lt")["row_actions"]

    assert action["label"] == "lt:acme.revoke"
    assert action["confirm"] == "lt:acme.revoke_confirm"
