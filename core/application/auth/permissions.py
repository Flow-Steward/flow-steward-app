"""The permission catalogue and the system-role matrix (ADR 0076).

This module is the single list of what a role can be allowed to do. A permission is
a key ``<area>.<action>`` in the token-scope taxonomy (``jobs.read``,
``projects.write``, ...), a description, and how a token earns it:

* a non-exact permission is granted to a token by its own key through the wildcard
  rules of :func:`core.application.auth.scope_contracts.scope_granted`
  (``jobs:*``, ``*:read``, ``*:*``, ``admin:all``) or by one of its listed
  ``token_scopes``, matched the same way (``read:*``, ``command:*``);
* an exact-match permission is granted only by a listed scope held literally. No
  wildcard reaches it; ``admin:all`` reaches it only where it is listed. The secret
  catalog keys follow ADR 0057: ``secrets:write`` and ``secrets:bind`` are never
  implied, and approving a configuration is never a token's.

What each role holds is data in the database (``role_permissions``). The lists here
are what the system roles hold: the migration that creates the table writes them,
and the parity test proves they give the same answers as the rules they replace.
``admin`` holds the whole catalogue implicitly, so a new permission reaches it with
no migration; ``operator`` and ``viewer`` receive one only through a migration that
adds it to their list.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from core.application.auth.scope_contracts import scope_granted

ADMIN_ROLE = "admin"
OPERATOR_ROLE = "operator"
VIEWER_ROLE = "viewer"

#: The system roles, lowest first: a person's highest one decides (ADR 0066, 0076 §7).
SYSTEM_ROLE_LEVELS: dict[str, int] = {VIEWER_ROLE: 1, OPERATOR_ROLE: 2, ADMIN_ROLE: 3}

#: ``<area>.<action>`` with an optional sub-area: ``jobs.read``, ``secrets.value.write``.
PERMISSION_KEY_RE = re.compile(r"^[a-z][a-z_]*(?:\.[a-z][a-z_]*){1,2}$")

_READ_FAMILY = "read:*"
_COMMAND_FAMILY = "command:*"
_ADMIN_ALL = "admin:all"
_SECRETS_WRITE = "secrets:write"  # pragma: allowlist secret - a scope name
_SECRETS_BIND = "secrets:bind"  # pragma: allowlist secret - a scope name


@dataclass(frozen=True, slots=True)
class Permission:
    """One thing a role may be allowed to do."""

    key: str
    description: str
    #: Granted to a token only by a listed scope held literally (ADR 0057 style).
    exact_match: bool = False
    #: Scopes besides the key's own wildcard family that grant it to a token.
    token_scopes: tuple[str, ...] = ()


_CATALOGUE: tuple[Permission, ...] = (
    # --- the person's own account -------------------------------------------------
    Permission(
        "account.self",
        "Manage your own sign-in: sign out, switch between your companies, change your "
        "password and email, your personal tokens, OAuth grants and AI agent connection",
        token_scopes=(_READ_FAMILY, _COMMAND_FAMILY),
    ),
    Permission(
        "account.profile.write",
        "Change your own profile, time zone, language and date format",
        token_scopes=(_COMMAND_FAMILY,),
    ),
    # --- reading ------------------------------------------------------------------
    Permission(
        "jobs.read",
        "Read jobs, their results, files, logs and approvals",
        token_scopes=(_READ_FAMILY,),
    ),
    Permission(
        "projects.read",
        "Read projects, their connections, schedules and history",
        token_scopes=(_READ_FAMILY,),
    ),
    Permission(
        "workflows.read",
        "Read the workflows of a project, their contracts and validation",
        token_scopes=("registry.read", _READ_FAMILY),
    ),
    Permission(
        "reports.read",
        "Read workflow reports",
        token_scopes=(_READ_FAMILY,),
    ),
    Permission(
        "notify.read",
        "Read a project's notification routes and deliveries",
        token_scopes=(_READ_FAMILY,),
    ),
    Permission(
        "secrets.read",
        "Read stored secrets' names, metadata and where they are used, never their values",
        token_scopes=(_READ_FAMILY,),
    ),
    Permission(
        "extensions.read",
        "Read an installed extension's details",
        token_scopes=("registry.read",),
    ),
    Permission(
        "platform.read",
        "Read settings, security findings, monitoring, search and the trash",
        token_scopes=(_READ_FAMILY,),
    ),
    Permission(
        "registry.read",
        "Read the workflow registry, its history, extensions, providers and workload",
    ),
    # --- the work -------------------------------------------------------------------
    Permission(
        "workflows.run",
        "Start a workflow and preview which workflow a submission would start",
        token_scopes=("workflows.write", _COMMAND_FAMILY),
    ),
    Permission(
        "approvals.decide",
        "Decide approvals and override decisions of a run",
        token_scopes=("jobs.write", _COMMAND_FAMILY),
    ),
    Permission(
        "jobs.write",
        "Cancel, restart, retry a step of, acknowledge and unacknowledge jobs",
        token_scopes=(_COMMAND_FAMILY,),
    ),
    Permission(
        "reports.write",
        "Generate a workflow report",
        token_scopes=(_COMMAND_FAMILY,),
    ),
    Permission(
        "notify.dispatch",
        "Send a notification through a route the admin configured",
        token_scopes=("notify:dispatch", _COMMAND_FAMILY),
    ),
    Permission(
        "extensions.use",
        "Run an extension page's actions and queries and resolve an external effect",
        token_scopes=("projects.write", _COMMAND_FAMILY),
    ),
    Permission(
        "secrets.value.write",
        "Create a stored secret, replace its value and edit its metadata",
        exact_match=True,
        token_scopes=(_SECRETS_WRITE,),
    ),
    # --- configuring the installation ----------------------------------------------
    Permission(
        "projects.write",
        "Create, change and delete projects, their connections, schedules, routes and policy",
        token_scopes=(_COMMAND_FAMILY,),
    ),
    Permission(
        "workspaces.manage",
        "Create, change and archive workspaces",
        exact_match=True,
        token_scopes=(_ADMIN_ALL,),
    ),
    Permission(
        "workflows.manage",
        "Author, import, change and delete workflows, their triggers and project bindings",
        exact_match=True,
        token_scopes=(_ADMIN_ALL,),
    ),
    Permission(
        "registry.manage",
        "Change agents, tools, skills, data contracts, datasets, MCP connectors and providers",
        exact_match=True,
        token_scopes=(_ADMIN_ALL,),
    ),
    Permission(
        "extensions.manage",
        "Install, configure, enable and remove extensions",
        exact_match=True,
        token_scopes=(_ADMIN_ALL,),
    ),
    Permission(
        "jobs.manage",
        "Delete jobs, replay a step, redrive an event and recover or stop workers",
        exact_match=True,
        token_scopes=(_ADMIN_ALL,),
    ),
    Permission(
        "secrets.bind",
        "Choose which stored secret a connection or workflow uses, or remove that choice",
        exact_match=True,
        token_scopes=(_SECRETS_BIND,),
    ),
    Permission(
        "secrets.delete",
        "Delete a stored secret together with every place it is used",
        exact_match=True,
        token_scopes=(_SECRETS_WRITE,),
    ),
    Permission(
        "secrets.approve",
        "Approve a configuration's use of stored secrets; only a person signed in",
        exact_match=True,
    ),
    Permission(
        "users.manage",
        "Manage users, accounts and service accounts",
        exact_match=True,
        token_scopes=(_ADMIN_ALL,),
    ),
    Permission(
        "settings.manage",
        "Change installation settings, security policy, model providers and updates",
        exact_match=True,
        token_scopes=(_ADMIN_ALL,),
    ),
    Permission(
        "platform.manage",
        "Run generic commands, restore or purge the trash, and take agent and reaction intake",
        exact_match=True,
        token_scopes=(_ADMIN_ALL,),
    ),
)

#: Every permission, by key. The only list of permissions in Core.
PERMISSIONS: dict[str, Permission] = {permission.key: permission for permission in _CATALOGUE}
ALL_PERMISSION_KEYS: frozenset[str] = frozenset(PERMISSIONS)

#: What a viewer holds today: every read a person makes, and their own sign-in.
VIEWER_PERMISSIONS: frozenset[str] = frozenset(
    {
        "account.self",
        "jobs.read",
        "projects.read",
        "workflows.read",
        "reports.read",
        "notify.read",
        "secrets.read",
        "extensions.read",
        "platform.read",
    }
)
#: What an operator holds today: the viewer's reads, the registry read, and the work.
OPERATOR_PERMISSIONS: frozenset[str] = VIEWER_PERMISSIONS | frozenset(
    {
        "registry.read",
        "account.profile.write",
        "workflows.run",
        "approvals.decide",
        "jobs.write",
        "reports.write",
        "notify.dispatch",
        "extensions.use",
        "secrets.value.write",
    }
)

#: The stored lists of the system roles. ``admin`` is absent on purpose: it holds the
#: whole catalogue implicitly and stores no rows.
SYSTEM_ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    OPERATOR_ROLE: OPERATOR_PERMISSIONS,
    VIEWER_ROLE: VIEWER_PERMISSIONS,
}

#: Descriptions the migration stores on the system roles.
SYSTEM_ROLE_DESCRIPTIONS: dict[str, str] = {
    ADMIN_ROLE: "Configures the installation: users, extensions, projects, workflows, "
    "connections and secrets. Holds every permission.",
    OPERATOR_ROLE: "Does the work the admin configured: starts workflows, decides "
    "approvals, controls jobs and replaces secret values.",
    VIEWER_ROLE: "Reads history, results and files. Makes no changes.",
}


def role_level(role: str) -> int:
    """The ladder position of a system role; 0 for any other role."""
    return SYSTEM_ROLE_LEVELS.get(str(role or "").strip().lower(), 0)


def highest_system_role(roles: Iterable[str]) -> str | None:
    """The highest system role among ``roles``, or ``None`` when there is none."""
    ranked = [str(role or "").strip().lower() for role in roles if role_level(role)]
    return max(ranked, key=role_level) if ranked else None


def keep_highest_system_role(roles: Iterable[str]) -> list[str]:
    """``roles`` with one system role per scope: the highest, other roles kept (§7)."""
    normalized: list[str] = []
    for role in roles:
        text = str(role or "").strip().lower()
        if text and text not in normalized:
            normalized.append(text)
    best = highest_system_role(normalized)
    return [role for role in normalized if not role_level(role) or role == best]


def token_grants_permission(permission: Permission, token_scopes: Iterable[str]) -> bool:
    """Whether a token holding ``token_scopes`` is granted ``permission``."""
    scopes = {
        str(scope or "").strip().lower() for scope in token_scopes if str(scope or "").strip()
    }
    if not scopes:
        return False
    if permission.exact_match:
        return any(scope in scopes for scope in permission.token_scopes)
    return scope_granted(scopes, permission.key) or any(
        scope_granted(scopes, scope) for scope in permission.token_scopes
    )


__all__ = [
    "ADMIN_ROLE",
    "ALL_PERMISSION_KEYS",
    "OPERATOR_PERMISSIONS",
    "OPERATOR_ROLE",
    "PERMISSIONS",
    "PERMISSION_KEY_RE",
    "SYSTEM_ROLE_DESCRIPTIONS",
    "SYSTEM_ROLE_LEVELS",
    "SYSTEM_ROLE_PERMISSIONS",
    "VIEWER_PERMISSIONS",
    "VIEWER_ROLE",
    "Permission",
    "highest_system_role",
    "keep_highest_system_role",
    "role_level",
    "token_grants_permission",
]
