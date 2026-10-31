"""Access presets an AI agent connection may receive (ADR 0047 decision 15).

One application-owned contract shared by the inbound OAuth consent screen and the
task 4 "connect an AI agent" wizard. The presets name existing scopes only; they
create no new scope and never raise a caller above its own authority (that check
stays :func:`core.application.auth.principal.enforce_token_scope_within_caller_authority`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from core.application.auth.principal import (
    TokenAuthorityError,
    enforce_token_scope_within_caller_authority,
)
from core.application.auth.scope_contracts import scope_granted

OPERATIONAL_PRESET_ID = "operational"
FULL_AUTHORING_PRESET_ID = "full_authoring"

OPERATIONAL_SCOPES: tuple[str, ...] = (
    "projects.read",
    "jobs.read",
    "jobs.write",
    "reports.read",
    "reports.write",
    "workflows.read",
    "workflows.write",
)
FULL_AUTHORING_SCOPES: tuple[str, ...] = ("admin:all",)


@dataclass(frozen=True, slots=True)
class AgentAccessPreset:
    """One named access level: an exact scope set and when it is offered."""

    id: str
    scopes: tuple[str, ...]
    #: Offered only when the client asked for at least one of its scopes.
    offered_only_when_requested: bool


AGENT_ACCESS_PRESETS: tuple[AgentAccessPreset, ...] = (
    AgentAccessPreset(
        id=OPERATIONAL_PRESET_ID,
        scopes=OPERATIONAL_SCOPES,
        offered_only_when_requested=False,
    ),
    AgentAccessPreset(
        id=FULL_AUTHORING_PRESET_ID,
        scopes=FULL_AUTHORING_SCOPES,
        offered_only_when_requested=True,
    ),
)

#: Every scope an agent connection can hold, in advertisement order.
AGENT_ACCESS_SCOPES: tuple[str, ...] = (*OPERATIONAL_SCOPES, *FULL_AUTHORING_SCOPES)


def agent_access_preset(preset_id: str) -> AgentAccessPreset | None:
    """Return the preset named ``preset_id``, or ``None`` for an unknown id."""
    normalized = str(preset_id or "").strip().lower()
    return next((preset for preset in AGENT_ACCESS_PRESETS if preset.id == normalized), None)


def granted_preset_scopes(preset: AgentAccessPreset, requested: list[str]) -> list[str]:
    """Scopes a preset grants for a request: preset ∩ requested, in preset order.

    A request is never widened: a client that asked for ``jobs.read`` receives
    ``jobs.read`` under the operational preset, not the whole preset. A request for
    ``admin:all`` covers every operational scope, so the narrower operational preset
    stays available as an alternative to full authoring.
    """
    wanted = {str(scope or "").strip().lower() for scope in requested if str(scope or "").strip()}
    return [scope for scope in preset.scopes if scope_granted(wanted, scope)]


def offered_presets(requested: list[str]) -> list[AgentAccessPreset]:
    """Presets the consent screen offers for a request, in contract order."""
    wanted = {str(scope or "").strip().lower() for scope in requested}
    offered: list[AgentAccessPreset] = []
    for preset in AGENT_ACCESS_PRESETS:
        if preset.offered_only_when_requested and not wanted.intersection(preset.scopes):
            continue
        if granted_preset_scopes(preset, requested):
            offered.append(preset)
    return offered


#: Reason a preset is shown but cannot be chosen: it exceeds the caller's authority.
EXCEEDS_AUTHORITY_REASON = "exceeds_authority"


def preset_offers(caller: Mapping[str, Any] | None, requested: list[str]) -> list[dict[str, Any]]:
    """The presets offered for ``requested``, each marked available for ``caller`` or not.

    A preset the caller could not mint stays listed with ``available: False`` and the
    reason, so the consent screen and the wizard show it disabled instead of hiding
    it. The ceiling is :func:`enforce_token_scope_within_caller_authority`, the same
    check every token issuance applies.
    """
    offers: list[dict[str, Any]] = []
    for preset in offered_presets(requested):
        scopes = granted_preset_scopes(preset, requested)
        try:
            enforce_token_scope_within_caller_authority(
                dict(caller) if caller is not None else None,
                requested_scopes=scopes,
                requested_project_ids=[],
                requested_workflow_ids=[],
            )
            available, reason = True, ""
        except TokenAuthorityError:
            available, reason = False, EXCEEDS_AUTHORITY_REASON
        offers.append(
            {
                "id": preset.id,
                "scopes": scopes,
                "available": available,
                "reason": reason,
                "subset_of_preset": len(scopes) < len(preset.scopes),
            }
        )
    return offers


__all__ = [
    "AGENT_ACCESS_PRESETS",
    "AGENT_ACCESS_SCOPES",
    "EXCEEDS_AUTHORITY_REASON",
    "FULL_AUTHORING_PRESET_ID",
    "FULL_AUTHORING_SCOPES",
    "OPERATIONAL_PRESET_ID",
    "OPERATIONAL_SCOPES",
    "AgentAccessPreset",
    "agent_access_preset",
    "granted_preset_scopes",
    "offered_presets",
    "preset_offers",
]
