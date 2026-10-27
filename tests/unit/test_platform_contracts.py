from __future__ import annotations

from core.application.settings.platform_contracts import (
    CommunityAuthorizationService,
    CommunityEntitlementService,
    CommunityExtensionGovernancePolicy,
    InDatabaseSecretProvider,
    build_platform_contracts,
)


def test_build_platform_contracts_exposes_expected_keys() -> None:
    contracts = build_platform_contracts()
    assert {
        "authorization_service",
        "entitlement_service",
        "audit_sink",
        "secret_provider",
        "extension_governance_policy",
    } <= set(contracts)
    assert contracts["entitlement_service"].is_enabled("external_sign_in") is False


def test_community_authorization_preserves_admin_only_settings_behavior() -> None:
    authz = CommunityAuthorizationService()
    assert authz.can_view_settings({"roles": ["admin"]}) is True
    assert authz.can_view_settings({"roles": ["operator"]}) is False
    assert authz.can_manage_users({"roles": ["viewer"]}) is False
    assert authz.is_allowed("authenticated", auth_user={"id": 1}) is True


def test_community_entitlement_service_supports_explicit_overrides() -> None:
    entitlements = CommunityEntitlementService(
        feature_flags={"external_sign_in": True, "audit_export": False}
    )
    assert entitlements.is_enabled("external_sign_in") is True
    assert entitlements.is_enabled("audit_export") is False
    assert entitlements.is_enabled("unknown_feature") is False


def test_extension_governance_and_secret_provider_defaults_are_safe_noops() -> None:
    policy = CommunityExtensionGovernancePolicy()
    assert policy.allow_extension_install(extension_id="sample-extension") == (True, "")
    assert policy.allow_remote_base_url(remote_base_url="https://example.com") == (True, "")
    assert policy.allow_domain(domain="example.com") == (True, "")

    provider = InDatabaseSecretProvider()
    assert (
        provider.save_provider_secret(
            object(),
            provider_key="demo",
            secret_key="token",
            secret_value="value",
        )
        is False
    )
    assert (
        provider.get_provider_secret(
            object(),
            provider_key="demo",
            secret_key="token",
        )
        == ""
    )
