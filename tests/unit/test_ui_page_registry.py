from __future__ import annotations

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.reports.ui_page_registry import (
    PageRegistrySealedError,
    page_registration_scope,
    page_registries_sealed,
    register_core_settings_pages,
    register_page,
    register_settings_page,
    resolve_registered_navigation_sections,
    resolve_registered_pages,
    resolve_registered_settings_pages,
    resolve_registered_shell_paths,
    seal_page_registries,
)
from core.application.settings.platform_contracts import CoreEntitlementService


class _AccountFeatures:
    """An entitlement source that lists features per account."""

    def __init__(self, features_by_account: dict[str, set[str]]) -> None:
        self._features_by_account = features_by_account

    def features_for(self, account_id: str) -> set[str]:
        return set(self._features_by_account.get(account_id, set()))

    def limits_for(self, account_id: str) -> dict[str, int]:
        _ = account_id
        return {}


def _entitlements(features_by_account: dict[str, set[str]] | None = None) -> CoreEntitlementService:
    service = CoreEntitlementService()
    if features_by_account:
        service.register_entitlement_source(_AccountFeatures(features_by_account))
    return service


def test_register_core_settings_pages_covers_public_settings_surface() -> None:
    registries = {"settings_pages": {}, "navigation_items": {}}

    register_core_settings_pages(registries)

    assert {
        "settings_home",
        "general",
        "security",
        "retention",
        "notifications",
        "service_accounts",
        "model_providers",
        "account",
        "users",
    } <= set(registries["settings_pages"])
    assert "extensions" not in registries["settings_pages"]
    assert "assistant" not in registries["settings_pages"]
    assert "identity" not in registries["settings_pages"]
    assert "autoindex" not in registries["settings_pages"]


def test_registered_pages_hide_private_only_entries_in_community() -> None:
    registries = {"settings_pages": {}, "navigation_items": {}}
    register_core_settings_pages(registries)
    register_settings_page(
        registries,
        page_key="identity_integrations",
        title="Identity Integrations",
        path="/settings/identity",
        nav_section="system",
        render_mode="platform_page",
        required_permission="can_view_private_settings_page",
        edition_visibility=("private",),
        core_page=False,
        sidebar_nav=True,
        settings_tab=False,
        sort_order=90,
        nav_label="Identity Integrations",
        nav_icon="settings",
        current_pages=("identity_integrations",),
    )

    visible_settings = resolve_registered_settings_pages(
        registries,
        edition="community",
        auth_user={"account_id": DEFAULT_ACCOUNT_ID, "roles": ["admin"]},
        include_core=True,
    )
    assert "service_accounts" in {row["page_key"] for row in visible_settings}
    assert "identity_integrations" not in {row["page_key"] for row in visible_settings}

    visible_nav = resolve_registered_navigation_sections(
        registries,
        edition="community",
        auth_user={"account_id": DEFAULT_ACCOUNT_ID, "roles": ["admin"]},
    )
    assert [row["page_key"] for row in visible_nav["system"]] == ["extensions"]

    private_nav = resolve_registered_navigation_sections(
        registries,
        edition="private",
        auth_user={"account_id": DEFAULT_ACCOUNT_ID, "roles": ["admin"]},
    )
    assert [row["page_key"] for row in private_nav["system"]] == [
        "extensions",
        "identity_integrations",
    ]


def test_registered_navigation_sections_preserve_current_account_visibility() -> None:
    registries = {"settings_pages": {}, "navigation_items": {}}
    register_core_settings_pages(registries)

    admin_nav = resolve_registered_navigation_sections(
        registries,
        edition="community",
        auth_user={"account_id": DEFAULT_ACCOUNT_ID, "roles": ["admin"]},
    )
    assert "extensions" in {row["page_key"] for row in admin_nav["system"]}

    viewer_nav = resolve_registered_navigation_sections(
        registries,
        edition="community",
        auth_user={"roles": ["viewer"]},
        include_core=True,
    )
    assert [row["page_key"] for row in viewer_nav["account"]] == ["account"]

    auth_disabled_nav = resolve_registered_navigation_sections(
        registries,
        edition="community",
        auth_user=None,
        include_core=True,
    )
    assert "settings_home" in {row["page_key"] for row in auth_disabled_nav["system"]}
    assert "account" in auth_disabled_nav
    assert "account" not in {row["page_key"] for row in auth_disabled_nav["account"]}


def test_registered_pages_can_use_custom_authorization_service() -> None:
    class _AllowAllAuthz:
        def is_allowed(self, _permission: str, *, auth_user=None) -> bool:
            _ = auth_user
            return True

    registries = {"settings_pages": {}, "navigation_items": {}}
    register_core_settings_pages(registries)

    default_visible = resolve_registered_settings_pages(
        registries,
        edition="community",
        auth_user={"roles": ["viewer"]},
        include_core=True,
    )
    assert default_visible == []

    override_visible = resolve_registered_settings_pages(
        registries,
        edition="community",
        auth_user={"roles": ["viewer"]},
        include_core=True,
        authorization_service=_AllowAllAuthz(),
    )
    visible_keys = {row["page_key"] for row in override_visible}
    assert "service_accounts" in visible_keys
    assert "extensions" not in visible_keys


def test_registered_pages_can_require_entitlement_flags() -> None:
    registries = {"settings_pages": {}, "navigation_items": {}}
    register_settings_page(
        registries,
        page_key="feature_a",
        title="Audit Export",
        path="/settings/audit-export",
        nav_section="settings_hub",
        render_mode="platform_page",
        required_permission="view_settings",
        required_entitlement="feature_a",
        edition_visibility=("community", "private"),
        core_page=False,
        sidebar_nav=False,
        settings_tab=True,
        sort_order=120,
    )

    admin = {"roles": ["admin"], "account_id": "acc-1"}
    visible_without_flag = resolve_registered_settings_pages(
        registries,
        edition="community",
        auth_user=admin,
        include_core=False,
        entitlement_service=_entitlements(),
    )
    assert visible_without_flag == []

    visible_with_flag = resolve_registered_settings_pages(
        registries,
        edition="community",
        auth_user=admin,
        include_core=False,
        entitlement_service=_entitlements({"acc-1": {"feature_a"}}),
    )
    assert [row["page_key"] for row in visible_with_flag] == ["feature_a"]


def test_generic_registered_react_pages_are_resolved_for_private_mode() -> None:
    registries = {"settings_pages": {}, "registered_pages": {}, "navigation_items": {}}
    register_page(
        registries,
        key="private_fixture",
        label="Private Fixture",
        path="/private/fixture",
        component_key="private.fixture",
        icon="extension",
        nav_section="private_tools",
        nav_order=20,
        sidebar_nav=True,
        required_permission="can_view_private_settings_page",
        edition_visibility=("private",),
    )

    community_pages = resolve_registered_pages(
        registries,
        edition="community",
        auth_user={"account_id": DEFAULT_ACCOUNT_ID, "roles": ["admin"]},
    )
    assert community_pages == []

    private_pages = resolve_registered_pages(
        registries,
        edition="private",
        auth_user={"account_id": DEFAULT_ACCOUNT_ID, "roles": ["admin"]},
    )
    assert private_pages[0]["key"] == "private_fixture"
    assert private_pages[0]["label"] == "Private Fixture"
    assert private_pages[0]["component_key"] == "private.fixture"
    assert private_pages[0]["nav_order"] == 20

    private_nav = resolve_registered_navigation_sections(
        registries,
        edition="private",
        auth_user={"account_id": DEFAULT_ACCOUNT_ID, "roles": ["admin"]},
    )
    assert private_nav["private_tools"][0]["path"] == "/private/fixture"
    assert resolve_registered_shell_paths(registries) == ("/private/fixture",)


def _resource_admin_surface(**resource_overrides) -> dict:
    resource = {
        "key": "records",
        "title": "Records",
        "list_path": "/api/modules/fixture/records",
        "item_path": "/api/modules/fixture/records/{id}",
        "id_field": "id",
        "fields": [{"key": "name", "label": "Name", "type": "text"}],
    }
    resource.update(resource_overrides)
    return {"kind": "resource_admin", "resources": [resource]}


_MODULE = "acme.ledger"


def _module_surface(**resource_overrides) -> dict:
    """A resource_admin surface whose resources live in the module's own API tree."""
    return _resource_admin_surface(
        **{
            "list_path": "/api/modules/acme.ledger/records",
            "item_path": "/api/modules/acme.ledger/records/{id}",
            **resource_overrides,
        }
    )


@pytest.mark.parametrize(("given", "expected"), [(None, True), (True, True), (False, False)])
def test_resource_admin_allow_delete_is_false_only_when_said(given, expected) -> None:
    registries = {"settings_pages": {}, "registered_pages": {}, "navigation_items": {}}
    overrides = {} if given is None else {"allow_delete": given}
    with page_registration_scope(module_id=_MODULE):
        register_page(
            registries,
            key="module_records",
            label="Module Records",
            path="/modules/records",
            sidebar_nav=True,
            control_surface=_module_surface(**overrides),
        )

    pages = resolve_registered_pages(
        registries,
        edition="community",
        auth_user={"account_id": DEFAULT_ACCOUNT_ID, "roles": ["admin"]},
    )
    assert pages[0]["control_surface"]["resources"][0]["allow_delete"] is expected


@pytest.mark.parametrize(("given", "expected"), [(None, True), (True, True), (False, False)])
def test_resource_admin_allow_edit_is_false_only_when_said(given, expected) -> None:
    registries = {"settings_pages": {}, "registered_pages": {}, "navigation_items": {}}
    overrides = {} if given is None else {"allow_edit": given}
    with page_registration_scope(module_id=_MODULE):
        register_page(
            registries,
            key="module_records",
            label="Module Records",
            path="/modules/records",
            sidebar_nav=True,
            control_surface=_module_surface(**overrides),
        )

    pages = resolve_registered_pages(
        registries,
        edition="community",
        auth_user={"account_id": DEFAULT_ACCOUNT_ID, "roles": ["admin"]},
    )
    assert pages[0]["control_surface"]["resources"][0]["allow_edit"] is expected


def test_keyless_resource_admin_page_enters_registered_pages_and_shell_paths() -> None:
    registries = {"settings_pages": {}, "registered_pages": {}, "navigation_items": {}}
    register_core_settings_pages(registries)
    with page_registration_scope(module_id=_MODULE):
        register_page(
            registries,
            key="module_records",
            label="Module Records",
            path="/modules/records",
            sidebar_nav=True,
            control_surface=_module_surface(),
        )

    pages = resolve_registered_pages(
        registries,
        edition="community",
        auth_user={"account_id": DEFAULT_ACCOUNT_ID, "roles": ["admin"]},
    )
    assert [page["page_key"] for page in pages] == ["module_records"]
    assert pages[0]["component_key"] == ""
    assert pages[0]["control_surface"]["kind"] == "resource_admin"
    assert (
        pages[0]["control_surface"]["resources"][0]["list_path"]
        == "/api/modules/acme.ledger/records"
    )
    # The source stamp stays on the server; resolved records never carry it.
    assert "source_module" not in pages[0]
    assert registries["registered_pages"]["module_records"]["source_module"] == _MODULE
    assert "/modules/records" in resolve_registered_shell_paths(registries)


def test_page_without_control_surface_or_component_stays_out_of_registered_pages() -> None:
    registries = {"settings_pages": {}, "registered_pages": {}, "navigation_items": {}}
    register_page(registries, key="plain", label="Plain", path="/plain", sidebar_nav=True)

    assert "plain" not in registries["registered_pages"]
    assert "plain" in registries["navigation_items"]
    assert resolve_registered_shell_paths(registries) == ()


@pytest.mark.parametrize(
    "overrides",
    [
        {"list_path": "//evil.example/api/records"},
        {"list_path": "https://evil.example/api/records"},
        {"list_path": "/private/records"},
        {"list_path": "/api/../admin"},
        {"list_path": "/api/records?all=1"},
        {"list_path": "/api/records#top"},
        {"list_path": "/api/records/{id}"},
        {"list_path": ""},
        {"item_path": "//evil.example/api/records/{id}"},
        {"item_path": "/api/records/{id"},
        {"item_path": "/api/records/{}"},
        {"item_path": "/api/records/{a-b}"},
        {"id_field": ""},
        {"title": ""},
        {"key": ""},
        {"fields": []},
        {"fields": [{"key": "name", "type": "html"}]},
        {"fields": [{"label": "No key", "type": "text"}]},
    ],
)
def test_invalid_resource_admin_surface_is_refused(overrides) -> None:
    registries = {"settings_pages": {}, "registered_pages": {}, "navigation_items": {}}

    with pytest.raises(ValueError):
        register_page(
            registries,
            key="module_records",
            label="Module Records",
            path="/modules/records",
            control_surface=_resource_admin_surface(**overrides),
        )

    assert "module_records" not in registries["registered_pages"]


def test_resource_admin_surface_requires_a_resource() -> None:
    with pytest.raises(ValueError):
        register_page(
            {},
            key="module_records",
            path="/modules/records",
            control_surface={"kind": "resource_admin", "resources": []},
        )


def test_unknown_control_surface_kind_without_component_is_refused() -> None:
    with pytest.raises(ValueError):
        register_page(
            {},
            key="module_dashboard",
            path="/modules/dashboard",
            control_surface={"kind": "dashboard", "widgets": []},
        )


def test_unknown_control_surface_kind_with_component_is_kept() -> None:
    registries: dict = {}
    record = register_page(
        registries,
        key="module_dashboard",
        path="/modules/dashboard",
        component_key="private.fixture",
        control_surface={"kind": "dashboard", "widgets": []},
    )

    assert record["control_surface"] == {"kind": "dashboard", "widgets": []}
    assert "module_dashboard" in registries["registered_pages"]


@pytest.mark.parametrize(
    ("page_key", "path"),
    [
        ("general", "/modules/general"),
        ("users", "/modules/users"),
        ("module_general", "/settings/general"),
        ("module_settings", "/settings/"),
        ("module_account", "/settings/account"),
    ],
)
def test_registration_after_core_cannot_replace_core_pages(page_key: str, path: str) -> None:
    registries = {"settings_pages": {}, "registered_pages": {}, "navigation_items": {}}
    register_core_settings_pages(registries)
    core_general = registries["settings_pages"]["general"]

    with pytest.raises(ValueError):
        register_page(
            registries,
            key=page_key,
            path=path,
            settings_tab=True,
            control_surface=_resource_admin_surface(),
        )

    assert registries["settings_pages"]["general"] is core_general
    assert page_key not in registries["registered_pages"]


def test_core_pages_register_twice_and_hide_their_marker() -> None:
    registries = {"settings_pages": {}, "registered_pages": {}, "navigation_items": {}}
    register_core_settings_pages(registries)
    register_core_settings_pages(registries)

    rows = resolve_registered_settings_pages(
        registries,
        edition="community",
        auth_user={"account_id": DEFAULT_ACCOUNT_ID, "roles": ["admin"]},
        include_core=True,
    )
    assert "general" in {row["page_key"] for row in rows}
    assert not any(field.startswith("_") for row in rows for field in row)


def test_keyless_resource_admin_page_still_requires_its_entitlement() -> None:
    registries = {"settings_pages": {}, "registered_pages": {}, "navigation_items": {}}
    with page_registration_scope(module_id=_MODULE):
        register_page(
            registries,
            key="module_records",
            label="Module Records",
            path="/modules/records",
            required_entitlement="module_records",
            control_surface=_module_surface(),
        )
    admin = {"roles": ["admin"], "account_id": "acc-1"}

    hidden = resolve_registered_pages(
        registries,
        auth_user=admin,
        entitlement_service=_entitlements(),
    )
    assert hidden == []

    visible = resolve_registered_pages(
        registries,
        auth_user=admin,
        entitlement_service=_entitlements({"acc-1": {"module_records"}}),
    )
    assert [row["page_key"] for row in visible] == ["module_records"]


def _register_entitled_module_page(registries: dict) -> None:
    register_page(
        registries,
        key="module_ledger",
        label="Module Ledger",
        path="/modules/ledger",
        component_key="module.ledger",
        sidebar_nav=True,
        required_permission="authenticated",
        required_entitlement="Module_Ledger",
    )


def test_entitled_page_is_hidden_for_an_account_without_the_feature() -> None:
    registries = {"settings_pages": {}, "registered_pages": {}, "navigation_items": {}}
    _register_entitled_module_page(registries)
    entitlements = _entitlements({"acc-entitled": {"module_ledger"}})
    other_account = {"roles": ["admin"], "account_id": "acc-plain"}

    assert (
        resolve_registered_pages(
            registries, auth_user=other_account, entitlement_service=entitlements
        )
        == []
    )
    assert (
        resolve_registered_navigation_sections(
            registries, auth_user=other_account, entitlement_service=entitlements
        )
        == {}
    )


def test_entitled_page_is_visible_for_an_account_with_the_feature() -> None:
    registries = {"settings_pages": {}, "registered_pages": {}, "navigation_items": {}}
    _register_entitled_module_page(registries)
    entitlements = _entitlements({"acc-entitled": {"module_ledger"}})
    entitled_account = {"roles": ["viewer"], "account_id": "acc-entitled"}

    pages = resolve_registered_pages(
        registries, auth_user=entitled_account, entitlement_service=entitlements
    )
    navigation = resolve_registered_navigation_sections(
        registries, auth_user=entitled_account, entitlement_service=entitlements
    )

    assert [row["page_key"] for row in pages] == ["module_ledger"]
    assert [row["page_key"] for row in navigation["system"]] == ["module_ledger"]


def test_entitled_page_is_hidden_for_an_anonymous_request() -> None:
    registries = {"settings_pages": {}, "registered_pages": {}, "navigation_items": {}}
    register_page(
        registries,
        key="module_public",
        label="Module Public",
        path="/modules/public",
        component_key="module.public",
        required_permission="public",
        required_entitlement="module_public",
    )
    entitled_everywhere = _entitlements({"": {"module_public"}, "acc-1": {"module_public"}})

    assert (
        resolve_registered_pages(
            registries, auth_user=None, entitlement_service=entitled_everywhere
        )
        == []
    )
    assert [
        row["page_key"]
        for row in resolve_registered_pages(
            registries,
            auth_user={"roles": ["viewer"], "account_id": "acc-1"},
            entitlement_service=entitled_everywhere,
        )
    ] == ["module_public"]


def _register_any_of_page(registries: dict, required: object) -> dict:
    return register_page(
        registries,
        key="module_kinds",
        label="Module Kinds",
        path="/modules/kinds",
        component_key="module.kinds",
        sidebar_nav=True,
        required_permission="authenticated",
        required_entitlement=required,
    )


def test_page_with_a_list_of_entitlements_opens_for_any_of_them() -> None:
    registries = {"settings_pages": {}, "registered_pages": {}, "navigation_items": {}}
    record = _register_any_of_page(registries, ["acme.base", " Acme.Extra ", "acme.base"])
    assert record["required_entitlement"] == ["acme.base", "Acme.Extra"]
    entitlements = _entitlements(
        {"acc-base": {"acme.base"}, "acc-extra": {"acme.extra"}, "acc-none": {"other"}}
    )

    def visible(account: str) -> list[str]:
        return [
            row["page_key"]
            for row in resolve_registered_pages(
                registries,
                auth_user={"roles": ["viewer"], "account_id": account},
                entitlement_service=entitlements,
            )
        ]

    assert visible("acc-base") == ["module_kinds"]
    assert visible("acc-extra") == ["module_kinds"]
    assert visible("acc-none") == []
    assert (
        resolve_registered_pages(registries, auth_user=None, entitlement_service=entitlements) == []
    )
    assert (
        resolve_registered_navigation_sections(
            registries,
            auth_user={"roles": ["viewer"], "account_id": "acc-none"},
            entitlement_service=entitlements,
        )
        == {}
    )


@pytest.mark.parametrize(
    "required", [[], (), ["acme.base", ""], ["acme.base", 3], {"acme.base": True}, 7]
)
def test_page_refuses_a_malformed_entitlement_list(required: object) -> None:
    registries = {"settings_pages": {}, "registered_pages": {}, "navigation_items": {}}
    with pytest.raises(ValueError, match="required_entitlement"):
        _register_any_of_page(registries, required)


# --- pages are bound to the identity Core loaded them under (ADR 0082) ------------


def _empty_registries() -> dict:
    return {"settings_pages": {}, "registered_pages": {}, "navigation_items": {}}


def test_core_pages_carry_no_source_module() -> None:
    registries = _empty_registries()
    register_core_settings_pages(registries)

    record = register_page(
        registries, key="core_tool", path="/core-tool", component_key="core.tool"
    )

    assert "source_module" not in record
    assert all(
        "source_module" not in row for registry in registries.values() for row in registry.values()
    )


def test_module_scope_stamps_pages_and_is_reset_afterwards() -> None:
    registries = _empty_registries()

    with page_registration_scope(module_id=_MODULE) as scope:
        inside = register_page(
            registries, key="ledger_home", path="/ledger", component_key="ledger.home"
        )
    outside = register_page(
        registries, key="core_after", path="/core-after", component_key="core.after"
    )

    assert inside["source_module"] == _MODULE
    assert registries["registered_pages"]["ledger_home"]["source_module"] == _MODULE
    assert scope.page_keys == ["ledger_home"]
    assert "source_module" not in outside


def test_module_scope_is_reset_when_register_raises() -> None:
    registries = _empty_registries()

    with (
        pytest.raises(RuntimeError, match="module failed"),
        page_registration_scope(module_id=_MODULE),
    ):
        register_page(registries, key="ledger_home", path="/ledger", component_key="ledger.home")
        raise RuntimeError("module failed")
    after = register_page(registries, key="core_after", path="/core-after", component_key="c.a")

    assert "source_module" not in after


@pytest.mark.parametrize("forged", [{"source_module": "acme.reports"}, {"_core_definition": True}])
def test_register_page_refuses_a_forged_source(forged: dict) -> None:
    registries = _empty_registries()

    with pytest.raises(TypeError):
        register_page(registries, key="forged", path="/forged", component_key="f.p", **forged)
    with page_registration_scope(module_id=_MODULE), pytest.raises(TypeError):
        register_page(registries, key="forged", path="/forged", component_key="f.p", **forged)

    assert "forged" not in registries["registered_pages"]


def test_a_surface_field_cannot_set_the_source_module() -> None:
    registries = _empty_registries()
    surface = _module_surface()
    surface["source_module"] = "acme.reports"

    with page_registration_scope(module_id=_MODULE):
        record = register_page(
            registries, key="ledger_records", path="/ledger/records", control_surface=surface
        )

    assert record["source_module"] == _MODULE


def test_host_rendered_page_from_an_unverified_module_is_refused() -> None:
    registries = _empty_registries()

    with page_registration_scope(module_id=None), pytest.raises(ValueError, match="host renderer"):
        register_page(
            registries,
            key="unverified_records",
            path="/unverified/records",
            control_surface=_resource_admin_surface(),
        )

    assert "unverified_records" not in registries["registered_pages"]
    assert "unverified_records" not in registries["navigation_items"]


def test_an_unverified_module_may_still_register_a_compiled_page() -> None:
    registries = _empty_registries()

    with page_registration_scope(module_id=None):
        record = register_page(
            registries, key="private_page", path="/private/page", component_key="private.page"
        )

    assert "source_module" not in record
    assert "private_page" in registries["registered_pages"]


def test_core_may_register_a_host_rendered_page_on_any_api_path() -> None:
    registries = _empty_registries()

    record = register_page(
        registries,
        key="core_records",
        path="/core/records",
        control_surface=_resource_admin_surface(
            list_path="/api/records", item_path="/api/records/{id}"
        ),
    )

    assert record["control_surface"]["resources"][0]["list_path"] == "/api/records"


@pytest.mark.parametrize(
    "overrides",
    [
        {"list_path": "/api/records"},
        {"list_path": "/api/modules/acme.reports/records"},
        {"list_path": "/api/modules/acme.ledgerx/records"},
        {"list_path": "/api/modules/acme.ledger"},
        {"list_path": "/api/modules/acme.ledger/"},
        {"item_path": "/api/modules/acme.reports/records/{id}"},
        {"item_path": "/api/settings/{id}"},
    ],
)
def test_module_resource_paths_are_confined_to_its_own_tree(overrides: dict) -> None:
    registries = _empty_registries()

    with page_registration_scope(module_id=_MODULE), pytest.raises(ValueError):
        register_page(
            registries,
            key="ledger_records",
            path="/ledger/records",
            control_surface=_module_surface(**overrides),
        )

    assert "ledger_records" not in registries["registered_pages"]


# --- item_path carries exactly one trailing token ---------------------------------


@pytest.mark.parametrize(
    "item_path",
    [
        "/api/modules/acme.ledger/records",
        "/api/modules/acme.ledger/records/{id}/history",
        "/api/modules/acme.ledger/{kind}/{id}",
        "/api/modules/acme.ledger/records/item-{id}",
        "/api/modules/acme.ledger/records/{id}.json",
    ],
)
def test_item_path_needs_one_token_as_its_final_segment(item_path: str) -> None:
    registries: dict = {}
    with (
        page_registration_scope(module_id=_MODULE),
        pytest.raises(ValueError, match="final segment"),
    ):
        register_page(
            registries,
            key="module_records",
            path="/modules/records",
            control_surface=_module_surface(item_path=item_path),
        )
    assert "module_records" not in registries.get("registered_pages", {})


def test_item_path_with_one_trailing_token_is_kept() -> None:
    registries: dict = {}
    with page_registration_scope(module_id=_MODULE):
        record = register_page(
            registries,
            key="module_records",
            path="/modules/records",
            control_surface=_module_surface(item_path="/api/modules/acme.ledger/records/{id}"),
        )
    assert record["control_surface"]["resources"][0]["item_path"] == (
        "/api/modules/acme.ledger/records/{id}"
    )


# --- the composition root seals the registries -------------------------------------


def _core_registries() -> dict:
    registries = {"settings_pages": {}, "registered_pages": {}, "navigation_items": {}}
    register_core_settings_pages(registries)
    return registries


def test_unscoped_registration_before_the_seal_is_core() -> None:
    registries = _core_registries()
    record = register_page(
        registries,
        key="core_tools",
        path="/tools",
        control_surface=_resource_admin_surface(
            list_path="/api/tools", item_path="/api/tools/{id}"
        ),
    )
    assert "source_module" not in registries["registered_pages"]["core_tools"]
    assert record["control_surface"]["resources"][0]["list_path"] == "/api/tools"


@pytest.mark.parametrize("scope_module", [None, _MODULE, "unscoped"])
def test_every_registration_after_the_seal_is_refused(scope_module) -> None:
    registries = _core_registries()
    seal_page_registries(registries)
    before = {name: dict(registries[name]) for name in registries}

    def _register(target):
        return register_page(
            target, key="late_page", path="/late", component_key="late.page", sidebar_nav=True
        )

    if scope_module == "unscoped":
        with pytest.raises(PageRegistrySealedError, match="'late_page'"):
            _register(registries)
    else:
        # A module that kept its top-level copy of the registries is refused too.
        with (
            page_registration_scope(module_id=scope_module),
            pytest.raises(PageRegistrySealedError, match="'late_page'"),
        ):
            _register(dict(registries))

    assert {name: dict(registries[name]) for name in registries} == before


def test_seal_also_refuses_core_settings_pages_and_is_idempotent() -> None:
    registries = _core_registries()
    assert not page_registries_sealed(registries)
    seal_page_registries(registries)
    seal_page_registries(registries)

    assert page_registries_sealed(registries)
    with pytest.raises(PageRegistrySealedError):
        register_core_settings_pages(registries)
    with pytest.raises(PageRegistrySealedError, match="'late_settings'"):
        register_settings_page(
            registries,
            page_key="late_settings",
            title="Late",
            path="/settings/late",
            nav_section="settings_hub",
            render_mode="react_shell",
        )
    # Sealing one registry set never affects another.
    assert not page_registries_sealed(_core_registries())


# --- no owner replaces another owner's page -----------------------------------------


def _module_page(registries: dict, module_id: str, *, key: str, path: str) -> dict:
    with page_registration_scope(module_id=module_id):
        return register_page(
            registries,
            key=key,
            path=path,
            sidebar_nav=True,
            control_surface=_resource_admin_surface(
                list_path=f"/api/modules/{module_id}/records",
                item_path=f"/api/modules/{module_id}/records/{{id}}",
            ),
        )


@pytest.mark.parametrize(
    ("key", "path"),
    [("ledger_records", "/modules/reports"), ("reports_records", "/modules/ledger")],
)
def test_a_module_cannot_replace_another_module_page(key: str, path: str) -> None:
    registries = _core_registries()
    _module_page(registries, "acme.ledger", key="ledger_records", path="/modules/ledger")
    original = registries["registered_pages"]["ledger_records"]

    with pytest.raises(ValueError, match=r"platform module 'acme\.ledger'"):
        _module_page(registries, "acme.reports", key=key, path=path)

    assert registries["registered_pages"]["ledger_records"] is original
    assert registries["registered_pages"]["ledger_records"]["source_module"] == "acme.ledger"


def test_a_module_cannot_replace_an_unmarked_core_page_and_core_cannot_replace_a_module_page() -> (
    None
):
    registries = _core_registries()
    register_page(registries, key="core_tools", path="/tools", component_key="core.tools")
    _module_page(registries, _MODULE, key="ledger_records", path="/modules/ledger")

    with pytest.raises(ValueError, match="belongs to Core"):
        _module_page(registries, _MODULE, key="core_tools", path="/modules/tools")
    with pytest.raises(ValueError, match=r"platform module 'acme\.ledger'"):
        register_page(registries, key="ledger_records", path="/x", component_key="core.x")

    assert registries["registered_pages"]["core_tools"]["component_key"] == "core.tools"
    assert registries["registered_pages"]["ledger_records"]["source_module"] == _MODULE


def test_a_module_may_replace_its_own_page_and_the_extensions_entry() -> None:
    registries = _core_registries()
    _module_page(registries, _MODULE, key="ledger_records", path="/modules/ledger")
    _module_page(registries, _MODULE, key="ledger_records", path="/modules/ledger-v2")
    with page_registration_scope(module_id=_MODULE):
        register_page(
            registries,
            page_key="extensions",
            path="/extensions",
            component_key="acme.extensions",
            sidebar_nav=True,
        )

    assert registries["registered_pages"]["ledger_records"]["path"] == "/modules/ledger-v2"
    assert registries["navigation_items"]["extensions"]["source_module"] == _MODULE


# --- resource_report: a read-only host-rendered table ------------------------------


def _report_surface(**overrides) -> dict:
    surface = {
        "kind": "resource_report",
        "list_path": "/api/modules/acme.ledger/journal",
        "items_key": "items",
        "total_key": "total",
        "columns": [
            {"key": "decided_at", "label": "Decided at", "type": "datetime"},
            {"key": "action", "label": "Action", "type": "badge"},
            {"key": "job_id", "label": "Job", "type": "link", "link_path": "/jobs/{job_id}"},
        ],
        "filters": [
            {"key": "workflow_id", "label": "Workflow", "type": "text"},
            {"key": "date_from", "label": "Date from", "type": "date"},
            {"key": "action", "label": "Action", "type": "select", "options": ["approve"]},
        ],
        "page_size": 50,
        "empty_text": "Nothing recorded yet.",
    }
    surface.update(overrides)
    return surface


def _register_report(registries: dict, surface: dict) -> dict:
    with page_registration_scope(module_id=_MODULE):
        return register_page(
            registries, key="ledger_journal", path="/ledger/journal", control_surface=surface
        )


def test_resource_report_kind_is_accepted_without_a_component() -> None:
    registries = _empty_registries()

    record = _register_report(registries, _report_surface())

    surface = record["control_surface"]
    assert record["component_key"] == ""
    assert surface["kind"] == "resource_report"
    assert surface["list_path"] == "/api/modules/acme.ledger/journal"
    assert (surface["items_key"], surface["total_key"], surface["page_size"]) == (
        "items",
        "total",
        50,
    )
    assert [column["type"] for column in surface["columns"]] == ["datetime", "badge", "link"]
    assert surface["columns"][2]["link_path"] == "/jobs/{job_id}"
    assert surface["filters"][2]["options"] == [{"value": "approve", "label": "approve"}]
    assert surface["empty_text"] == "Nothing recorded yet."
    assert "ledger_journal" in registries["registered_pages"]
    assert "/ledger/journal" in resolve_registered_shell_paths(registries)


def test_resource_report_defaults_its_keys_and_page_size() -> None:
    surface = _report_surface()
    for key in ("items_key", "total_key", "page_size", "filters", "empty_text"):
        surface.pop(key)

    record = _register_report(_empty_registries(), surface)

    report = record["control_surface"]
    assert (report["items_key"], report["total_key"], report["page_size"]) == ("items", "total", 50)
    assert report["filters"] == []


@pytest.mark.parametrize("column_type", ["html", "markdown", "number", "image"])
def test_resource_report_refuses_an_unknown_column_type(column_type: str) -> None:
    surface = _report_surface(columns=[{"key": "a", "label": "A", "type": column_type}])

    with pytest.raises(ValueError, match="unknown type"):
        _register_report(_empty_registries(), surface)


@pytest.mark.parametrize(
    "link_path",
    [
        "https://evil.example/jobs/{job_id}",
        "//evil.example/jobs/{job_id}",
        "javascript:alert(1)",
        "jobs/{job_id}",
        "/jobs/../admin",
        "/api/jobs/{job_id}",
        "/api",
        "/jobs/{job_id}?tab=1",
        "/jobs/{job_id}#top",
        "/jobs/{job_id",
        "/jobs/{}",
        "\\\\evil.example\\jobs",
        "",
    ],
)
def test_resource_report_refuses_a_link_outside_the_app(link_path: str) -> None:
    surface = _report_surface(
        columns=[{"key": "job_id", "label": "Job", "type": "link", "link_path": link_path}]
    )

    with pytest.raises(ValueError):
        _register_report(_empty_registries(), surface)


def test_resource_report_link_needs_a_target_and_only_links_carry_one() -> None:
    with pytest.raises(ValueError, match="link_path or link_key"):
        _register_report(
            _empty_registries(),
            _report_surface(columns=[{"key": "job_id", "label": "Job", "type": "link"}]),
        )
    with pytest.raises(ValueError, match="sets a link"):
        _register_report(
            _empty_registries(),
            _report_surface(
                columns=[{"key": "a", "label": "A", "type": "text", "link_path": "/jobs/{a}"}]
            ),
        )
    record = _register_report(
        _empty_registries(),
        _report_surface(columns=[{"key": "a", "label": "A", "type": "link", "link_key": "href"}]),
    )
    assert record["control_surface"]["columns"][0]["link_key"] == "href"


def _job_link(**extra: str) -> dict:
    return {"key": "job_id", "label": "Job", "type": "link", "link_path": "/jobs/{job_id}", **extra}


def test_resource_report_link_if_turns_a_row_link_off_with_a_marker() -> None:
    record = _register_report(
        _empty_registries(),
        _report_surface(
            columns=[
                _job_link(
                    link_if="job_exists",
                    link_missing_label="Deleted",
                    link_missing_label_key="jobs.bulk.deleted",
                )
            ]
        ),
    )

    [column] = record["control_surface"]["columns"]
    assert (column["link_if"], column["link_missing_label"], column["link_missing_label_key"]) == (
        "job_exists",
        "Deleted",
        "jobs.bulk.deleted",
    )
    plain = _register_report(_empty_registries(), _report_surface(columns=[_job_link()]))
    assert "link_if" not in plain["control_surface"]["columns"][0]


@pytest.mark.parametrize(
    ("column", "message"),
    [
        (_job_link(link_if="job-exists"), "link_if must be an identifier"),
        (_job_link(link_missing_label="Deleted"), "without link_if"),
        (_job_link(link_missing_label_key="jobs.bulk.deleted"), "without link_if"),
        (
            _job_link(link_if="job_exists", link_missing_label_key="jobs.bulk.deleted"),
            "without link_missing_label",
        ),
        (
            {"key": "a", "label": "A", "type": "text", "link_if": "job_exists"},
            "sets a link",
        ),
        (
            {"key": "a", "label": "A", "type": "text", "link_missing_label": "Deleted"},
            "sets a link",
        ),
    ],
)
def test_resource_report_refuses_a_misdeclared_link_if(column: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _register_report(_empty_registries(), _report_surface(columns=[column]))


@pytest.mark.parametrize(
    "list_path",
    [
        "/api/records",
        "/api/modules/acme.reports/journal",
        "/api/modules/acme.ledgerx/journal",
        "/api/modules/acme.ledger",
        "/api/modules/acme.ledger/journal?limit=5",
        "/api/modules/acme.ledger/journal/{id}",
        "//evil.example/api/modules/acme.ledger/journal",
        "",
    ],
)
def test_resource_report_list_path_stays_in_the_module_tree(list_path: str) -> None:
    registries = _empty_registries()

    with pytest.raises(ValueError):
        _register_report(registries, _report_surface(list_path=list_path))

    assert "ledger_journal" not in registries["registered_pages"]


@pytest.mark.parametrize("page_size", [0, -1, 501, 10_000, True, "50", 2.5, None])
def test_resource_report_page_size_is_bounded(page_size) -> None:
    with pytest.raises(ValueError, match="page_size"):
        _register_report(_empty_registries(), _report_surface(page_size=page_size))


@pytest.mark.parametrize("page_size", [1, 500])
def test_resource_report_page_size_bounds_are_inclusive(page_size: int) -> None:
    record = _register_report(_empty_registries(), _report_surface(page_size=page_size))
    assert record["control_surface"]["page_size"] == page_size


@pytest.mark.parametrize(
    "overrides",
    [
        {"item_path": "/api/modules/acme.ledger/journal/{id}"},
        {"resources": []},
        {"fields": []},
        {"columns": []},
        {"columns": "decided_at"},
        {"columns": [{"key": "a", "label": "A"}, {"key": "a", "label": "Again"}]},
        {"columns": [{"key": "a"}]},
        {"columns": [{"key": "a b", "label": "A"}]},
        {"filters": [{"key": "limit", "label": "Limit"}]},
        {"filters": [{"key": "offset", "label": "Offset"}]},
        {"filters": [{"key": "f", "label": "F", "type": "number_range"}]},
        {"filters": [{"key": "f", "label": "F", "type": "select"}]},
        {"filters": [{"key": "f", "label": "F", "type": "text", "options": ["a"]}]},
        {"filters": [{"key": "f", "label": "F"}, {"key": "f", "label": "F again"}]},
        {"items_key": "items.list"},
        {"total_key": "a b"},
    ],
)
def test_invalid_resource_report_surface_is_refused(overrides: dict) -> None:
    registries = _empty_registries()

    with pytest.raises(ValueError):
        _register_report(registries, _report_surface(**overrides))

    assert "ledger_journal" not in registries["registered_pages"]


# --- status_document: one status object and one uploaded text document ---------------


def _status_surface(**overrides) -> dict:
    surface = {
        "kind": "status_document",
        "status_path": "/api/modules/acme.ledger/status",
        "upload_path": "/api/modules/acme.ledger/document",
        "fields": [
            {"key": "state", "label": "State", "type": "badge"},
            {"key": "holder", "label": "Holder"},
            {"key": "valid_until", "label": "Valid until", "type": "datetime"},
        ],
        "upload_label": "Document",
        "accept": ".txt,.json",
    }
    surface.update(overrides)
    return surface


def _register_status(registries: dict, surface: dict, **page) -> dict:
    with page_registration_scope(module_id=_MODULE):
        return register_page(
            registries,
            key="ledger_status",
            path="/settings/ledger-status",
            settings_tab=True,
            control_surface=surface,
            **page,
        )


def test_status_document_kind_is_accepted_without_a_component() -> None:
    registries = _empty_registries()

    record = _register_status(registries, _status_surface(status_key="status"))

    surface = record["control_surface"]
    assert record["component_key"] == ""
    assert surface["kind"] == "status_document"
    assert surface["status_path"] == "/api/modules/acme.ledger/status"
    assert surface["upload_path"] == "/api/modules/acme.ledger/document"
    assert surface["status_key"] == "status"
    assert [field["type"] for field in surface["fields"]] == ["badge", "text", "datetime"]
    assert surface["accept"] == ".txt,.json"
    assert "ledger_status" in registries["registered_pages"]
    assert "ledger_status" in registries["settings_pages"]


def test_status_document_defaults_its_upload_field_and_size() -> None:
    surface = _status_surface()
    surface.pop("accept")
    surface.pop("upload_label")

    document = _register_status(_empty_registries(), surface)["control_surface"]

    assert document["upload_field"] == "document"
    assert document["max_bytes"] == 65_536
    assert document["accept"] == ""
    assert document["upload_label"] == ""
    assert "status_key" not in document


@pytest.mark.parametrize(
    "overrides",
    [
        {"status_path": "/api/records/status"},
        {"status_path": "/api/modules/acme.reports/status"},
        {"status_path": "/api/modules/acme.ledger/status?full=1"},
        {"status_path": "/api/modules/acme.ledger/{id}"},
        {"status_path": "//evil.example/api/modules/acme.ledger/status"},
        {"status_path": ""},
        {"upload_path": "/api/modules/acme.ledgerx/document"},
        {"upload_path": "https://evil.example/api/modules/acme.ledger/document"},
        {"upload_path": ""},
        {"fields": []},
        {"fields": "state"},
        {"fields": [{"key": "state"}]},
        {"fields": [{"key": "a b", "label": "A"}]},
        {"fields": [{"key": "state", "label": "State", "type": "html"}]},
        {"fields": [{"key": "a", "label": "A"}, {"key": "a", "label": "Again"}]},
        {"upload_field": "document.text"},
        {"status_key": "status.inner"},
        {"max_bytes": 0},
        {"max_bytes": 1_048_577},
        {"max_bytes": "100"},
        {"max_bytes": True},
        {"accept": "javascript:alert(1)"},
        {"accept": ".txt;.json"},
        {"resources": []},
        {"columns": []},
        {"list_path": "/api/modules/acme.ledger/status"},
    ],
)
def test_invalid_status_document_surface_is_refused(overrides: dict) -> None:
    registries = _empty_registries()

    with pytest.raises(ValueError):
        _register_status(registries, _status_surface(**overrides))

    assert "ledger_status" not in registries["registered_pages"]
    assert "ledger_status" not in registries["settings_pages"]


def test_an_installation_admin_status_document_follows_the_installation_admin_predicate() -> None:
    from core.application.settings.platform_contracts import build_platform_contracts

    registries = _empty_registries()
    _register_status(registries, _status_surface(), required_permission="installation_admin")
    service = build_platform_contracts(
        installation_admin_predicate=lambda user: (
            "admin" in user.get("roles", []) and user.get("account_id") == "default"
        )
    )["authorization_service"]

    def keys(user: dict) -> set[str]:
        return {
            page["page_key"]
            for page in resolve_registered_settings_pages(
                registries, auth_user=user, authorization_service=service
            )
        }

    assert "ledger_status" in keys({"roles": ["admin"], "account_id": "default"})
    assert "ledger_status" not in keys({"roles": ["admin"], "account_id": "acc-other"})
    assert "ledger_status" not in keys({"roles": ["operator"], "account_id": "default"})


# --- page permissions (ADR 0082 §3b) -------------------------------------------------


@pytest.mark.parametrize("permission", ["admin", "settings.manage", "installation.manage"])
def test_a_page_with_a_permission_core_does_not_know_is_refused(permission: str) -> None:
    registries = {"settings_pages": {}, "registered_pages": {}, "navigation_items": {}}

    with (
        page_registration_scope(module_id=_MODULE),
        pytest.raises(ValueError, match="does not know") as refused,
    ):
        register_page(
            registries,
            key="module_records",
            label="Module Records",
            path="/modules/records",
            required_permission=permission,
            control_surface=_module_surface(),
        )

    assert "view_settings" in str(refused.value)
    assert "installation_admin" in str(refused.value)
    assert "module_records" not in registries["registered_pages"]


def test_every_page_permission_is_one_the_authorization_service_decides() -> None:
    from core.application.reports.ui_page_registry import PAGE_PERMISSIONS
    from core.application.settings.platform_contracts import CommunityAuthorizationService

    admin = {"account_id": DEFAULT_ACCOUNT_ID, "roles": ["admin"]}
    service = CommunityAuthorizationService()

    # An admin passes every name the service knows; an unknown name answers False.
    assert all(service.is_allowed(name, auth_user=admin) for name in PAGE_PERMISSIONS)
    assert service.is_allowed("admin", auth_user=admin) is False


def _installation_page_registries() -> dict:
    registries = {"settings_pages": {}, "registered_pages": {}, "navigation_items": {}}
    with page_registration_scope(module_id=_MODULE):
        register_page(
            registries,
            key="module_accounts",
            label="Accounts",
            path="/settings/module-accounts",
            settings_tab=True,
            required_permission="installation_admin",
            control_surface=_module_surface(),
        )
    return registries


def test_an_installation_admin_page_follows_the_installation_admin_predicate() -> None:
    from core.application.settings.platform_contracts import build_platform_contracts

    registries = _installation_page_registries()
    installation_admin = {"roles": ["admin"], "account_id": "default"}
    account_admin = {"roles": ["admin"], "account_id": "acc-other"}
    operator = {"roles": ["operator"], "account_id": "default"}

    def keys(user: dict, authorization_service) -> set[str]:
        return {
            page["page_key"]
            for page in resolve_registered_settings_pages(
                registries, auth_user=user, authorization_service=authorization_service
            )
        }

    # Without a predicate the admin check decides.
    plain = build_platform_contracts()["authorization_service"]
    assert "module_accounts" in keys(installation_admin, plain)
    assert "module_accounts" in keys(account_admin, plain)
    assert "module_accounts" not in keys(operator, plain)

    # With one, an administrator of another account no longer sees the page.
    decided = build_platform_contracts(
        installation_admin_predicate=lambda user: (
            "admin" in user.get("roles", []) and user.get("account_id") == "default"
        )
    )["authorization_service"]
    assert "module_accounts" in keys(installation_admin, decided)
    assert "module_accounts" not in keys(account_admin, decided)
    assert "module_accounts" not in keys(operator, decided)


def test_a_raising_installation_admin_predicate_hides_the_page() -> None:
    from core.application.settings.platform_contracts import CommunityAuthorizationService

    def _broken(_user):
        raise RuntimeError("accounts unavailable")

    service = CommunityAuthorizationService(installation_admin_predicate=_broken)

    assert (
        service.can_administer_installation({"account_id": DEFAULT_ACCOUNT_ID, "roles": ["admin"]})
        is False
    )
    assert (
        service.is_allowed(
            "installation_admin", auth_user={"account_id": DEFAULT_ACCOUNT_ID, "roles": ["admin"]}
        )
        is False
    )
