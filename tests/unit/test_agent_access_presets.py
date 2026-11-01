"""ADR 0047 decision 15: one access-preset contract for consent and the task 4 wizard (Z11)."""

from __future__ import annotations

from core.application.auth import agent_access_presets as presets
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
