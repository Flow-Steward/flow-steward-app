"""Access presets an AI agent connection may receive (ADR 0047 decision 15).

One application-owned contract shared by the inbound OAuth consent screen and the
task 4 "connect an AI agent" wizard. The presets name existing scopes only; they
create no new scope and never raise a caller above its own authority (that check
stays :func:`core.application.auth.principal.enforce_token_scope_within_caller_authority`).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from core.application.auth.permissions import (
    ADMIN_ROLE,
    PERMISSIONS,
    highest_system_role,
    token_grants_permission,
)
from core.application.auth.principal import (
    TokenAuthorityError,
    enforce_token_scope_within_caller_authority,
)
from core.application.auth.scope_contracts import EXACT_ONLY_SCOPES, scope_granted

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


# ---------------------------------------------------------------------------
# ADR 0076 §5: "as my role", the one access an operator or a viewer is given
# ---------------------------------------------------------------------------

ROLE_PRESET_ID = "my_role"
#: How long an "as my role" agent token lives. The server chooses it, not the person:
#: the longest an agent grant may last (``MAX_GRANT_EXPIRY_DAYS``), so a one-button
#: connection does not stop working within weeks.
ROLE_PRESET_EXPIRY_DAYS = 90
#: Token scope families, narrowest first. An "as my role" token holds the fewest of
#: them that carry its owner's permissions.
_ROLE_PRESET_CANDIDATE_SCOPES: tuple[str, ...] = (
    "read:*",
    "registry.read",
    "command:*",
    "admin:all",
)


def _grantable_by_a_preset(key: str) -> bool:
    """A permission a preset may put into a token: one a scope other than the secret
    catalog's exact scopes grants. ``secrets:write`` and ``secrets:bind`` are never
    implied (ADR 0057); only an admin's explicit choice puts them on a token."""
    permission = PERMISSIONS.get(key)
    if permission is None:
        return False
    if not permission.exact_match:
        return True
    return any(scope not in EXACT_ONLY_SCOPES for scope in permission.token_scopes)


def role_preset_scopes(permissions: Iterable[str]) -> tuple[str, ...]:
    """The token scopes that carry the owner's ``permissions`` to an agent token.

    The token's effective permissions (its owner's ∩ its scopes) are then exactly the
    owner's, except the secret catalog's exact-match permissions, which no preset
    grants. The scope check of every route still applies to the token.
    """
    wanted = {key for key in permissions if _grantable_by_a_preset(key)}
    chosen: list[str] = []
    for scope in _ROLE_PRESET_CANDIDATE_SCOPES:
        if any(
            not token_grants_permission(PERMISSIONS[key], chosen)
            and token_grants_permission(PERMISSIONS[key], [scope])
            for key in wanted
        ):
            chosen.append(scope)
    # Drop a scope the others already cover (``admin:all`` covers every family).
    for scope in list(chosen):
        rest = [item for item in chosen if item != scope]
        if all(token_grants_permission(PERMISSIONS[key], rest) for key in wanted):
            chosen = rest
    return tuple(chosen)


def permissions_of_scopes(scopes: Iterable[str]) -> frozenset[str]:
    """Every catalogue permission a token holding ``scopes`` is granted."""
    held = [str(scope or "").strip().lower() for scope in scopes if str(scope or "").strip()]
    if not held:
        return frozenset()
    return frozenset(
        key for key, permission in PERMISSIONS.items() if token_grants_permission(permission, held)
    )


@dataclass(frozen=True, slots=True)
class RoleGrant:
    """What "as my role" grants a client that asked for ``requested`` scopes."""

    #: The token scopes, never carrying more than the owner's role or the request.
    scopes: tuple[str, ...]
    #: The permissions those scopes carry for the owner, in catalogue order.
    permissions: tuple[str, ...]
    #: The client asked for less than the owner's role allows.
    narrower: bool


def role_preset_grant(permissions: Iterable[str], requested: Iterable[str]) -> RoleGrant:
    """The "as my role" access for a client that asked for ``requested`` (ADR 0076 §5).

    The role's access intersected with the request, in permissions: a requested scope
    is kept when everything it carries is within the owner's role, and a scope of the
    role preset is added when everything it carries was requested. So a viewer's client
    that asked for the operational scopes gets their read part, and a client that asked
    for ``admin:all`` gets the role preset. The result never carries a permission the
    request did not ask for (today's "subset of what the app asked for" rule) nor one
    the owner's role does not hold. A scope the others already cover is dropped.
    """
    owner = frozenset(key for key in permissions if _grantable_by_a_preset(key))
    wanted = [str(scope or "").strip().lower() for scope in requested if str(scope or "").strip()]
    requested_permissions = permissions_of_scopes(wanted)
    granted: list[str] = []
    for scope in wanted:
        carried = permissions_of_scopes([scope])
        if carried and carried <= owner and scope not in granted:
            granted.append(scope)
    for scope in role_preset_scopes(owner):
        carried = permissions_of_scopes([scope])
        if carried and carried <= requested_permissions and scope not in granted:
            granted.append(scope)
    target = permissions_of_scopes(granted)
    for scope in list(granted):
        rest = [item for item in granted if item != scope]
        if rest and permissions_of_scopes(rest) == target:
            granted = rest
    carried = owner & target
    return RoleGrant(
        scopes=tuple(granted),
        permissions=tuple(key for key in PERMISSIONS if key in carried),
        narrower=bool(owner - carried),
    )


#: An operator or a viewer sent a preset, an expiry or projects (ADR 0076 §5).
ADVANCED_OPTIONS_ADMIN_ONLY = "advanced_options_admin_only"
ADVANCED_OPTIONS_ADMIN_ONLY_MESSAGE = (
    "Your agent gets the access of your role; only an admin chooses the access, "
    "the expiry and the projects."
)


def may_choose_agent_access(user: Mapping[str, Any] | None) -> bool:
    """Whether the person chooses a preset, an expiry and projects: the admin only."""
    roles = (user or {}).get("roles") or []
    if isinstance(roles, str):
        roles = [roles]
    return highest_system_role(roles) == ADMIN_ROLE


#: Within this many days of its end an agent connection is "ending soon": the app warns
#: its owner and sends one email. A connection made for this long or less is not
#: reminded: its owner chose so short a life a moment ago.
AGENT_EXPIRY_REMINDER_DAYS = 7


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
    "ADVANCED_OPTIONS_ADMIN_ONLY",
    "ADVANCED_OPTIONS_ADMIN_ONLY_MESSAGE",
    "AGENT_ACCESS_PRESETS",
    "AGENT_ACCESS_SCOPES",
    "AGENT_EXPIRY_REMINDER_DAYS",
    "EXCEEDS_AUTHORITY_REASON",
    "FULL_AUTHORING_PRESET_ID",
    "FULL_AUTHORING_SCOPES",
    "OPERATIONAL_PRESET_ID",
    "OPERATIONAL_SCOPES",
    "ROLE_PRESET_EXPIRY_DAYS",
    "ROLE_PRESET_ID",
    "AgentAccessPreset",
    "RoleGrant",
    "agent_access_preset",
    "granted_preset_scopes",
    "may_choose_agent_access",
    "offered_presets",
    "permissions_of_scopes",
    "preset_offers",
    "role_preset_grant",
    "role_preset_scopes",
]
