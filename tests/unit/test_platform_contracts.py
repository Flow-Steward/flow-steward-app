from __future__ import annotations

import logging

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.settings.platform_contracts import (
    ENTITLEMENT_SOURCE_CAPABILITY,
    FEATURE_NOT_ENTITLED_CODE,
    CommunityAuthorizationService,
    CommunityExtensionGovernancePolicy,
    CoreEntitlementService,
    EntitlementSourceRefusedError,
    FeatureNotEntitledError,
    InDatabaseSecretProvider,
    build_platform_contracts,
)


class _Source:
    """An entitlement source with fixed answers per account."""

    def __init__(
        self,
        features: dict[str, set[str]] | None = None,
        limits: dict[str, dict[str, int]] | None = None,
    ) -> None:
        self._features = features or {}
        self._limits = limits or {}
        self.calls = 0

    def features_for(self, account_id: str) -> set[str]:
        self.calls += 1
        return set(self._features.get(account_id, set()))

    def limits_for(self, account_id: str) -> dict[str, int]:
        self.calls += 1
        return dict(self._limits.get(account_id, {}))


class _BrokenSource:
    def features_for(self, account_id: str) -> set[str]:
        raise RuntimeError(f"entitlement server down for {account_id}")

    def limits_for(self, account_id: str) -> dict[str, int]:
        raise RuntimeError(f"entitlement server down for {account_id}")


def test_build_platform_contracts_exposes_expected_keys() -> None:
    contracts = build_platform_contracts()
    assert {
        "authorization_service",
        "entitlement_service",
        "audit_sink",
        "secret_provider",
        "extension_governance_policy",
    } <= set(contracts)
    assert contracts["entitlement_service"].is_entitled("acc-1", "feature_c") is False


def test_community_authorization_preserves_admin_only_settings_behavior() -> None:
    authz = CommunityAuthorizationService()
    assert authz.can_view_settings({"account_id": DEFAULT_ACCOUNT_ID, "roles": ["admin"]}) is True
    assert authz.can_view_settings({"roles": ["operator"]}) is False
    assert authz.can_manage_users({"roles": ["viewer"]}) is False
    assert authz.is_allowed("authenticated", auth_user={"id": 1}) is True


def test_nothing_is_entitled_and_no_limit_is_set_without_a_source() -> None:
    service = CoreEntitlementService()

    assert service.is_entitled("acc-1", "feature_c") is False
    assert service.is_entitled("acc-1", "unknown_feature") is False
    assert service.limit("acc-1", "max_projects") is None
    assert dict(service.snapshot("acc-1")) == {}


def test_a_source_entitles_features_per_account() -> None:
    service = CoreEntitlementService()
    service.register_entitlement_source(_Source({"acc-entitled": {"Feature_A "}}))

    assert service.is_entitled("acc-entitled", "feature_a") is True
    assert service.is_entitled("acc-entitled", " FEATURE_A") is True
    assert service.is_entitled("acc-plain", "feature_a") is False
    assert dict(service.snapshot("acc-entitled")) == {"feature_a": True}
    assert dict(service.snapshot("acc-plain")) == {}


def test_two_accounts_get_different_answers_from_the_source() -> None:
    service = CoreEntitlementService()
    service.register_entitlement_source(_Source({"acc-a": {"feature_b"}, "acc-b": {"feature_a"}}))

    assert service.is_entitled("acc-a", "feature_b") is True
    assert service.is_entitled("acc-a", "feature_a") is False
    assert service.is_entitled("acc-b", "feature_a") is True
    assert service.is_entitled("acc-b", "feature_b") is False


def test_an_anonymous_request_is_never_entitled() -> None:
    source = _Source({"": {"feature_a"}}, {"": {"max_projects": 5}})
    service = CoreEntitlementService()
    service.register_entitlement_source(source)

    assert service.is_entitled("", "feature_a") is False
    assert service.limit("  ", "max_projects") is None
    assert dict(service.snapshot("")) == {}
    assert source.calls == 0


def test_the_limit_is_the_value_the_source_gives() -> None:
    service = CoreEntitlementService()
    service.register_entitlement_source(
        _Source(limits={"acc-1": {"Max_Projects": 20, "max_runs": 3}, "acc-2": {"max_projects": 1}})
    )

    assert service.limit("acc-1", "max_projects") == 20
    assert service.limit("acc-1", "MAX_RUNS") == 3
    assert service.limit("acc-1", "max_users") is None
    assert service.limit("acc-2", "max_projects") == 1


def test_a_second_source_is_refused_so_nothing_can_raise_a_limit_beside_it() -> None:
    service = CoreEntitlementService()
    service.register_entitlement_source(_Source({"acc-1": {"a"}}, {"acc-1": {"max_projects": 2}}))

    with pytest.raises(EntitlementSourceRefusedError, match="exactly one entitlement source"):
        service.register_entitlement_source(
            _Source({"acc-1": {"b"}}, {"acc-1": {"max_projects": 99}})
        )

    assert service.is_entitled("acc-1", "b") is False
    assert service.limit("acc-1", "max_projects") == 2
    assert len(service.loading_checkpoint()) == 1


def test_only_a_module_declaring_the_capability_registers_while_modules_load() -> None:
    service = CoreEntitlementService()

    with (
        service.platform_module_scope("acme.notes", ()),
        pytest.raises(EntitlementSourceRefusedError, match=r"'acme\.notes' does not declare"),
    ):
        service.register_entitlement_source(_Source({"acc-1": {"notes"}}))
    with (
        service.platform_module_scope(None, ()),
        pytest.raises(EntitlementSourceRefusedError, match="without a platform module identity"),
    ):
        service.register_entitlement_source(_Source({"acc-1": {"notes"}}))
    assert service.loading_checkpoint() == ()

    with service.platform_module_scope("acme.source", [ENTITLEMENT_SOURCE_CAPABILITY]):
        service.register_entitlement_source(_Source({"acc-1": {"notes"}}))

    assert service.source_owner == "acme.source"
    assert service.is_entitled("acc-1", "notes") is True
    # The scope ended: a later registration is attributed to nobody and refused as a second.
    with pytest.raises(EntitlementSourceRefusedError, match=r"by 'acme\.source'"):
        service.register_entitlement_source(_Source({"acc-1": {"other"}}))


def test_a_declared_module_cannot_register_a_second_source_either() -> None:
    service = CoreEntitlementService()

    with service.platform_module_scope("acme.source", [ENTITLEMENT_SOURCE_CAPABILITY]):
        service.register_entitlement_source(_Source({"acc-1": {"a"}}))
        with pytest.raises(EntitlementSourceRefusedError):
            service.register_entitlement_source(_Source({"acc-1": {"b"}}))

    assert service.is_entitled("acc-1", "b") is False


def test_a_sealed_service_refuses_every_registration() -> None:
    service = CoreEntitlementService()
    service.seal()

    with pytest.raises(EntitlementSourceRefusedError, match="loading has finished"):
        service.register_entitlement_source(_Source({"acc-1": {"a"}}))
    assert service.is_entitled("acc-1", "a") is False


def test_a_rolled_back_source_frees_the_slot_for_the_owner() -> None:
    service = CoreEntitlementService()
    checkpoint = service.loading_checkpoint()
    with service.platform_module_scope("acme.source", [ENTITLEMENT_SOURCE_CAPABILITY]):
        service.register_entitlement_source(_Source({"acc-1": {"a"}}))

    service.restore_loading_checkpoint(checkpoint)

    assert service.source_owner is None
    assert service.is_entitled("acc-1", "a") is False


def test_the_capability_name_matches_the_module_loader() -> None:
    from core.infrastructure.platform_modules.loader import (
        ENTITLEMENT_SOURCE_CAPABILITY as LOADER_CAPABILITY,
    )

    assert ENTITLEMENT_SOURCE_CAPABILITY == LOADER_CAPABILITY == "entitlement_source"


def test_a_raising_source_fails_closed_and_is_logged_once(caplog) -> None:
    service = CoreEntitlementService()
    service.register_entitlement_source(_BrokenSource())

    with caplog.at_level(logging.WARNING):
        assert service.is_entitled("acc-1", "feature_a") is False
        # A failure is never "no limit": None would widen to the instance default.
        assert service.limit("acc-1", "max_projects") == 0
        assert service.limit("acc-1", "max_users") == 0
        assert dict(service.snapshot("acc-1")) == {}
        assert service.is_entitled("acc-1", "feature_a") is False

    failures = [record for record in caplog.records if "_BrokenSource" in record.getMessage()]
    assert len(failures) == 1
    # The exception text may carry account data; only its type is logged.
    assert "entitlement server down" not in caplog.text


@pytest.mark.parametrize(
    ("features", "limits"),
    [
        ("feature_a", {}),
        ({1}, {}),
        (set(), {"max_projects": True}),
        (set(), {"max_projects": -1}),
        (set(), {"max_projects": "10"}),
        (set(), [("max_projects", 1)]),
    ],
)
def test_a_source_answering_with_an_invalid_shape_fails_closed(features, limits) -> None:
    class _Malformed:
        def features_for(self, account_id: str):
            _ = account_id
            return features

        def limits_for(self, account_id: str):
            _ = account_id
            return limits

    service = CoreEntitlementService()
    service.register_entitlement_source(_Malformed())

    if features != set():
        assert service.is_entitled("acc-1", "a") is False
    else:
        assert service.limit("acc-1", "max_projects") == 0


def test_a_failing_source_narrows_a_limit_below_the_instance_default() -> None:
    service = CoreEntitlementService()
    service.register_entitlement_source(_BrokenSource())

    # Distinguishable from "no source defines it" (None) and closed.
    assert service.limit("acc-1", "max_projects") == 0
    assert service.is_entitled("acc-1", "feature_a") is False
    assert dict(service.snapshot("acc-1")) == {}


def test_without_a_source_or_a_defining_source_the_limit_stays_unset() -> None:
    service = CoreEntitlementService()
    assert service.limit("acc-1", "max_projects") is None

    service.register_entitlement_source(_Source(limits={"acc-1": {"max_runs": 3}}))
    assert service.limit("acc-1", "max_projects") is None
    assert service.limit("acc-2", "max_runs") is None


def test_registering_the_same_source_twice_is_a_no_op() -> None:
    source = _Source({"acc-1": {"feature_a"}})
    service = CoreEntitlementService()

    service.register_entitlement_source(source)
    service.register_entitlement_source(source)
    service.is_entitled("acc-1", "feature_a")

    assert source.calls == 1


def test_an_object_without_the_source_methods_is_refused() -> None:
    service = CoreEntitlementService()

    with pytest.raises(TypeError):
        service.register_entitlement_source(object())


def test_entitlement_state_cannot_be_written_from_outside() -> None:
    service = CoreEntitlementService()
    service.register_entitlement_source(_Source({"acc-1": {"feature_a"}}))

    snapshot = service.snapshot("acc-1")
    with pytest.raises(TypeError):
        snapshot["feature_b"] = True  # type: ignore[index]
    with pytest.raises(AttributeError):
        service.feature_flags = {"feature_b": True}  # type: ignore[attr-defined]
    assert not hasattr(service, "feature_flags")
    assert isinstance(service._sources, tuple)
    assert service.is_entitled("acc-1", "feature_b") is False


def test_build_platform_contracts_accepts_no_entitlement_overrides() -> None:
    overrides = {"entitlement_overrides": {"feature_a": True}}
    with pytest.raises(TypeError):
        build_platform_contracts(**overrides)

    service = build_platform_contracts()["entitlement_service"]
    assert service.is_entitled("acc-1", "feature_a") is False


def test_feature_not_entitled_error_carries_the_code_and_feature() -> None:
    error = FeatureNotEntitledError(" Feature_A ")

    assert error.status_code == 403
    assert error.payload["code"] == FEATURE_NOT_ENTITLED_CODE == "feature_not_entitled"
    assert error.payload["details"] == {"feature": "feature_a"}


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


def test_a_degraded_service_reports_a_missing_feature_as_unknown() -> None:
    service = CoreEntitlementService()
    service.register_entitlement_source(_Source({"acc-1": {"feature_a"}}))
    assert service.entitlement_status("acc-1", "feature_b") is False

    service.mark_loading_degraded("acme.accounts 1.2.0")
    service.mark_loading_degraded("acme.accounts 1.2.0")

    assert service.loading_degraded == ("acme.accounts 1.2.0",)
    # The source still grants definitely; a missing grant is no longer definite.
    assert service.entitlement_status("acc-1", "feature_a") is True
    assert service.entitlement_status("acc-1", "feature_b") is None
    # is_entitled keeps denying: degraded never widens access.
    assert service.is_entitled("acc-1", "feature_b") is False
    assert service.entitlement_status("", "feature_b") is False


def test_a_degraded_service_without_sources_answers_unknown_not_false() -> None:
    service = CoreEntitlementService()
    service.mark_loading_degraded("acme.accounts 1.2.0")

    assert service.entitlement_status("acc-1", "approval_journal") is None
    assert service.is_entitled("acc-1", "approval_journal") is False
    assert service.snapshot("acc-1") == {}
