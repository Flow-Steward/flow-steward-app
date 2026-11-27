"""ADR 0047 decision 15: one access-preset contract for consent and the task 4 wizard (Z11)."""

from __future__ import annotations

from core.application.auth import agent_access_presets as presets
from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.auth.inbound_oauth_policy import SUPPORTED_SCOPES


def _offered(requested: list[str]) -> dict[str, list[str]]:
    return {
        preset.id: presets.granted_preset_scopes(preset, requested)
        for preset in presets.offered_presets(requested)
    }


def test_the_two_presets_and_their_exact_scopes() -> None:
    assert presets.OPERATIONAL_SCOPES == (
        "projects.read",
        "jobs.read",
        "jobs.write",
        "reports.read",
        "reports.write",
        "workflows.read",
        "workflows.write",
    )
    assert presets.FULL_AUTHORING_SCOPES == ("admin:all",)
    supported = SUPPORTED_SCOPES
    assert supported == (*presets.OPERATIONAL_SCOPES, "admin:all")
    assert presets.agent_access_preset("operational") is presets.AGENT_ACCESS_PRESETS[0]
    assert presets.agent_access_preset("nope") is None


def test_full_authoring_is_offered_only_when_requested() -> None:
    both = _offered(["admin:all", *presets.OPERATIONAL_SCOPES])
    assert both == {
        "operational": list(presets.OPERATIONAL_SCOPES),
        "full_authoring": ["admin:all"],
    }
    assert _offered(list(presets.OPERATIONAL_SCOPES)) == {
        "operational": list(presets.OPERATIONAL_SCOPES)
    }


def test_a_narrow_request_is_never_widened() -> None:
    assert _offered(["jobs.read"]) == {"operational": ["jobs.read"]}


def test_admin_all_alone_still_offers_the_narrower_operational_preset() -> None:
    assert _offered(["admin:all"]) == {
        "operational": list(presets.OPERATIONAL_SCOPES),
        "full_authoring": ["admin:all"],
    }


# --- ADR 0076 §5: "as my role" ------------------------------------------------------


def test_the_role_preset_carries_each_system_role_with_the_fewest_scopes() -> None:
    from core.application.auth.permissions import (
        ALL_PERMISSION_KEYS,
        OPERATOR_PERMISSIONS,
        VIEWER_PERMISSIONS,
    )

    assert presets.role_preset_scopes(VIEWER_PERMISSIONS) == ("read:*", "registry.read")
    assert presets.role_preset_scopes(OPERATOR_PERMISSIONS) == (
        "read:*",
        "registry.read",
        "command:*",
    )
    assert presets.role_preset_scopes(ALL_PERMISSION_KEYS) == ("admin:all",)
    assert presets.role_preset_scopes(()) == ()


def test_the_role_preset_never_grants_a_secret_scope() -> None:
    from core.application.auth.permissions import ALL_PERMISSION_KEYS
    from core.application.auth.scope_contracts import EXACT_ONLY_SCOPES

    for permissions in (ALL_PERMISSION_KEYS, {"secrets.value.write", "jobs.read"}):
        assert EXACT_ONLY_SCOPES.isdisjoint(presets.role_preset_scopes(permissions))


# --- ADR 0076 §5: "as my role" on the consent screen ---------------------------------


def test_the_role_grant_is_the_role_intersected_with_the_request() -> None:
    from core.application.auth.permissions import (
        ALL_PERMISSION_KEYS,
        OPERATOR_PERMISSIONS,
        VIEWER_PERMISSIONS,
    )

    operational = list(presets.OPERATIONAL_SCOPES)
    viewer = presets.role_preset_grant(VIEWER_PERMISSIONS, operational)
    assert viewer.scopes == ("projects.read", "jobs.read", "reports.read", "workflows.read")
    assert viewer.narrower is True
    assert set(viewer.permissions) == {
        "jobs.read",
        "projects.read",
        "workflows.read",
        "reports.read",
    }
    # An operator holds everything the operational scopes carry: they are kept as asked.
    operator = presets.role_preset_grant(OPERATOR_PERMISSIONS, operational)
    assert operator.scopes == presets.OPERATIONAL_SCOPES
    assert operator.narrower is True
    admin = presets.role_preset_grant(ALL_PERMISSION_KEYS, operational)
    assert admin.scopes == presets.OPERATIONAL_SCOPES


def test_a_request_for_everything_gets_exactly_the_role() -> None:
    from core.application.auth.permissions import (
        ALL_PERMISSION_KEYS,
        OPERATOR_PERMISSIONS,
        VIEWER_PERMISSIONS,
    )

    for permissions in (VIEWER_PERMISSIONS, OPERATOR_PERMISSIONS, ALL_PERMISSION_KEYS):
        grant = presets.role_preset_grant(permissions, ["admin:all"])
        assert grant.scopes == presets.role_preset_scopes(permissions)
        assert grant.narrower is False
    # A scope the others cover is not repeated.
    operator = presets.role_preset_grant(OPERATOR_PERMISSIONS, ["jobs.read", "admin:all"])
    assert operator.scopes == ("read:*", "registry.read", "command:*")


def test_the_role_grant_never_exceeds_the_role_or_the_request() -> None:
    from core.application.auth.permissions import (
        OPERATOR_PERMISSIONS,
        VIEWER_PERMISSIONS,
    )

    assert presets.role_preset_grant(VIEWER_PERMISSIONS, ["jobs.write"]).scopes == ()
    assert presets.role_preset_grant(VIEWER_PERMISSIONS, []).scopes == ()
    for permissions in (VIEWER_PERMISSIONS, OPERATOR_PERMISSIONS):
        for requested in (["jobs.read"], ["jobs.write", "workflows.read"], ["admin:all"]):
            grant = presets.role_preset_grant(permissions, requested)
            carried = presets.permissions_of_scopes(grant.scopes)
            assert set(grant.permissions) <= set(permissions)
            assert set(grant.permissions) == carried & set(permissions)
            assert carried <= presets.permissions_of_scopes(requested)


def test_only_an_admin_chooses_the_access() -> None:
    assert (
        presets.may_choose_agent_access({"account_id": DEFAULT_ACCOUNT_ID, "roles": ["admin"]})
        is True
    )
    assert presets.may_choose_agent_access({"roles": ["operator", "viewer"]}) is False
    assert presets.may_choose_agent_access({"roles": "viewer"}) is False
    assert presets.may_choose_agent_access(None) is False
