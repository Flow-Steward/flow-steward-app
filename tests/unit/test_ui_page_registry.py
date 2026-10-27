from __future__ import annotations

from core.application.reports.ui_page_registry import (
    register_core_settings_pages,
    register_page,
    register_settings_page,
    resolve_registered_navigation_sections,
    resolve_registered_pages,
    resolve_registered_settings_pages,
    resolve_registered_shell_paths,
)
from core.application.settings.platform_contracts import CommunityEntitlementService


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
        auth_user={"roles": ["admin"]},
        include_core=True,
    )
    assert "service_accounts" in {row["page_key"] for row in visible_settings}
    assert "identity_integrations" not in {row["page_key"] for row in visible_settings}

    visible_nav = resolve_registered_navigation_sections(
        registries,
        edition="community",
        auth_user={"roles": ["admin"]},
    )
    assert [row["page_key"] for row in visible_nav["system"]] == ["extensions"]

    private_nav = resolve_registered_navigation_sections(
        registries,
        edition="private",
        auth_user={"roles": ["admin"]},
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
        auth_user={"roles": ["admin"]},
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
        page_key="audit_export",
        title="Audit Export",
        path="/settings/audit-export",
        nav_section="settings_hub",
        render_mode="platform_page",
        required_permission="view_settings",
        required_entitlement="audit_export",
        edition_visibility=("community", "private"),
        core_page=False,
        sidebar_nav=False,
        settings_tab=True,
        sort_order=120,
    )

    admin = {"roles": ["admin"]}
    community_entitlements = CommunityEntitlementService()
    visible_without_flag = resolve_registered_settings_pages(
        registries,
        edition="community",
        auth_user=admin,
        include_core=False,
        entitlement_service=community_entitlements,
    )
    assert visible_without_flag == []

    visible_with_flag = resolve_registered_settings_pages(
        registries,
        edition="community",
        auth_user=admin,
        include_core=False,
        entitlement_service=CommunityEntitlementService(feature_flags={"audit_export": True}),
    )
    assert [row["page_key"] for row in visible_with_flag] == ["audit_export"]


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
        auth_user={"roles": ["admin"]},
    )
    assert community_pages == []

    private_pages = resolve_registered_pages(
        registries,
        edition="private",
        auth_user={"roles": ["admin"]},
    )
    assert private_pages[0]["key"] == "private_fixture"
    assert private_pages[0]["label"] == "Private Fixture"
    assert private_pages[0]["component_key"] == "private.fixture"
    assert private_pages[0]["nav_order"] == 20

    private_nav = resolve_registered_navigation_sections(
        registries,
        edition="private",
        auth_user={"roles": ["admin"]},
    )
    assert private_nav["private_tools"][0]["path"] == "/private/fixture"
    assert resolve_registered_shell_paths(registries) == ("/private/fixture",)
