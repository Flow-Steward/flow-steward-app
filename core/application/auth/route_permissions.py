"""Which permission each HTTP route needs (ADR 0076 §4).

One ordered table: the first rule whose method and path match decides. A rule's
permission is a key of :mod:`core.application.auth.permissions`, or ``None`` for a
route any signed-in principal may call (its own account reads, the product shell)
and for the public protocol routes that never authenticate by role (sign-in, OAuth,
signed webhooks and artifact uploads; :func:`core.application.auth.enforce.is_public_auth_path`
still decides those).

A route no rule matches is unmapped. Reads end in a rule for every remaining API
read, the default every role had; writes have no such rule, so an unmapped write is
denied to everybody (closed by default) and the route-matrix test fails on it.

The table replaces, for people, the operator allowlist in ``operator_work.py``, the
admin-only patterns in ``config.AUTH_ADMIN_ONLY_PATTERNS``, the registry-read
patterns and the middleware's "a person's write needs admin" default. Until the
middleware reads it, ``tests/unit/test_route_permission_parity.py`` proves it gives
the same answers. Resource checks (account, workspace, project, workflow, ownership,
token allowlists) are not part of it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_SEG = r"[^/]+"
_READ = frozenset({"GET"})
_WRITE = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_ANY = _READ | _WRITE
#: HEAD and OPTIONS are reads, as in the middleware.
_SAFE_ALIASES = frozenset({"HEAD", "OPTIONS"})
#: Path segments under /api/extensions/ that are platform routes, not an extension id.
_RESERVED_EXTENSION_SEGMENTS = (
    "bootstrap|descriptors|install|installs|marketplace|releases|runtime|scan"
)


@dataclass(frozen=True, slots=True)
class RoutePermissionRule:
    """Requests with one of ``methods`` whose path fully matches ``pattern`` need ``permission``."""

    methods: frozenset[str]
    pattern: re.Pattern[str]
    permission: str | None


def _rule(methods: frozenset[str], pattern: str, permission: str | None) -> RoutePermissionRule:
    return RoutePermissionRule(methods, re.compile(pattern), permission)


ROUTE_PERMISSION_RULES: tuple[RoutePermissionRule, ...] = (
    # --- public protocol routes: authenticated by the protocol or a signed token ------
    _rule(
        _WRITE,
        r"/api/auth/(?:login|bootstrap|password-reset/(?:request|verify|confirm))",
        None,
    ),
    _rule(_ANY, r"/oauth/(?:authorize|token|revoke|consent)", None),
    _rule(_ANY, r"/\.well-known/oauth-authorization-server", None),
    _rule(_ANY, r"/webhook/(?:workflows|extensions|providers)/.+", None),
    _rule(_ANY, r"/api/extension-artifacts/.+", None),
    _rule(_READ, rf"/api/mcp/oauth/client-metadata/{_SEG}\.json", None),
    # --- the principal's own account ----------------------------------------------------
    _rule(_READ, r"/api/(?:auth/(?:me|introspect|tokens)|app/bootstrap)", None),
    _rule(_READ, r"/api/users/me/.+", None),
    _rule(_READ, r"/api/auth/(?:agent-access(?:/.*)?|oauth-grants)", "account.self"),
    _rule(_READ, rf"/api/oauth/authorizations/{_SEG}", "account.self"),
    _rule(
        frozenset({"POST"}),
        r"/api/auth/(?:logout|change-password|tokens|agent-access/tokens)",
        "account.self",
    ),
    _rule(
        frozenset({"POST"}),
        rf"/api/auth/(?:tokens|oauth-grants)/{_SEG}/revoke",
        "account.self",
    ),
    _rule(frozenset({"POST"}), rf"/api/oauth/authorizations/{_SEG}/decision", "account.self"),
    _rule(frozenset({"POST"}), r"/api/users/me/email-change/request", "account.self"),
    _rule(
        frozenset({"PATCH"}),
        r"/api/users/me/(?:profile|timezone|locale|date-time-format)",
        "account.profile.write",
    ),
    _rule(frozenset({"POST"}), r"/api/users/me/detect-timezone", "account.profile.write"),
    _rule(frozenset({"POST"}), r"/api/auth/switch-account", "account.profile.write"),
    # --- users, accounts, installation settings -----------------------------------------
    _rule(_ANY, r"/api/auth/(?:users|accounts|service-accounts)(?:/.*)?", "users.manage"),
    _rule(_ANY, r"/admin(?:/.*)?", "settings.manage"),
    _rule(_ANY, r"/api/settings/mcp-oauth(?:/.*)?", "settings.manage"),
    _rule(_ANY, r"/api/system/core-updates", "settings.manage"),
    _rule(_ANY, r"/api/(?:model-providers|llm-provider-extensions)(?:/.*)?", "settings.manage"),
    _rule(_WRITE, r"/api/(?:settings|system|security)(?:/.*)?", "settings.manage"),
    _rule(_READ, r"/api/security/posture", "registry.read"),
    _rule(_READ, r"/health/deep", "registry.read"),
    # --- the registry ---------------------------------------------------------------------
    _rule(
        _ANY,
        r"/api/(?:agents|catalog-categories|tools|skills|data-contracts|datasets|fields"
        r"|guardrails|mcp|preview|published-blocks)(?:/.*)?",
        "registry.manage",
    ),
    _rule(_WRITE, r"/api/(?:providers|changelog|registry)(?:/.*)?", "registry.manage"),
    _rule(
        _READ,
        r"/api/(?:providers|changelog)(?:/.*)?|/api/(?:model-selection/options|workload|runs)",
        "registry.read",
    ),
    _rule(_ANY, r"/api/workspaces(?:/.*)?", "workspaces.manage"),
    # --- extensions -----------------------------------------------------------------------
    _rule(
        _READ,
        rf"/api/extensions/(?!(?:{_RESERVED_EXTENSION_SEGMENTS})(?:/|$)){_SEG}(?:/compiled-manifest)?",
        "extensions.read",
    ),
    _rule(
        frozenset({"POST"}), rf"/api/extensions/{_SEG}/(?:action|query)/{_SEG}", "extensions.use"
    ),
    _rule(
        frozenset({"POST"}),
        rf"/api/projects/{_SEG}/extensions/{_SEG}/external-effects/{_SEG}/resolution",
        "extensions.use",
    ),
    _rule(_READ, r"/api/extensions(?:/.*)?", "registry.read"),
    _rule(_WRITE, r"/api/extensions(?:/.*)?", "extensions.manage"),
    # --- workflows ------------------------------------------------------------------------
    _rule(frozenset({"POST"}), rf"/api/workflows/{_SEG}/run", "workflows.run"),
    _rule(frozenset({"POST"}), r"/api/workflow-runs", "workflows.run"),
    _rule(frozenset({"POST"}), rf"/api/projects/{_SEG}/workflow-preview", "workflows.run"),
    _rule(_READ, r"/api/workflows(?:/.*)?", "registry.read"),
    _rule(_WRITE, r"/api/workflows(?:/.*)?", "workflows.manage"),
    _rule(_READ, r"/api/workflow-authoring/schema", "workflows.read"),
    _rule(
        _READ,
        rf"/api/projects/{_SEG}/workflow-authoring/(?:capabilities|capability|examples)",
        "workflows.read",
    ),
    # Validation changes nothing; every role could always run it.
    _rule(
        frozenset({"POST"}),
        rf"/api/projects/{_SEG}/workflow-authoring/validate",
        "workflows.read",
    ),
    _rule(
        frozenset({"POST"}),
        rf"/api/projects/{_SEG}/(?:workflow-authoring/drafts|workflow-imports)",
        "workflows.manage",
    ),
    # --- stored secrets (ADR 0057, ADR 0066 amendment) ----------------------------------
    _rule(_READ, r"/api/secrets(?:/.*)?", "secrets.read"),
    _rule(frozenset({"POST"}), r"/api/secrets", "secrets.value.write"),
    _rule(frozenset({"PATCH"}), rf"/api/secrets/{_SEG}", "secrets.value.write"),
    _rule(frozenset({"PUT"}), rf"/api/secrets/{_SEG}/value", "secrets.value.write"),
    _rule(frozenset({"DELETE"}), rf"/api/secrets/{_SEG}", "secrets.delete"),
    _rule(
        _WRITE,
        rf"/api/projects/{_SEG}/(?:workflows|connections)/{_SEG}/secret-bindings/{_SEG}(?:/value)?",
        "secrets.bind",
    ),
    _rule(
        frozenset({"POST"}),
        rf"/api/projects/{_SEG}/(?:(?:workflows|connections)/{_SEG}/secret-approval|secret-approvals)",
        "secrets.approve",
    ),
    _rule(
        _READ,
        rf"/api/projects/{_SEG}/(?:(?:workflows|connections)/{_SEG}/secret-approval"
        r"|secret-approvals|workflow-secret-approvals)",
        "secrets.read",
    ),
    # --- a project's workflows: configuration, except three reads -----------------------
    _rule(
        _READ,
        rf"/api/projects/{_SEG}/workflows/{_SEG}/(?:contract|validate|suggestions)",
        "workflows.read",
    ),
    _rule(_ANY, rf"/api/projects/{_SEG}/workflows(?:/.*)?", "workflows.manage"),
    _rule(_ANY, rf"/api/projects/{_SEG}/providers(?:/.*)?", "projects.write"),
    # --- projects -------------------------------------------------------------------------
    _rule(
        frozenset({"POST"}),
        rf"/api/projects/{_SEG}/notifications/dispatch",
        "notify.dispatch",
    ),
    _rule(
        _READ,
        rf"/api/projects/{_SEG}/notification-(?:routes|deliveries|overview)",
        "notify.read",
    ),
    _rule(_READ, rf"/api/(?:v2/)?projects/{_SEG}/jobs", "jobs.read"),
    _rule(
        _READ,
        rf"/api/projects/{_SEG}/workflow-(?:catalog|entrypoints)",
        "workflows.read",
    ),
    _rule(frozenset({"POST"}), rf"/api/(?:v2/)?projects/{_SEG}/jobs/bulk-delete", "jobs.manage"),
    _rule(_READ, r"/api/(?:v2/)?projects(?:/.*)?", "projects.read"),
    _rule(_WRITE, r"/api/(?:v2/)?projects(?:/.*)?", "projects.write"),
    # --- runs, approvals and jobs ---------------------------------------------------------
    _rule(frozenset({"POST"}), rf"/api/approvals/{_SEG}", "approvals.decide"),
    _rule(frozenset({"POST"}), rf"/api/jobs/{_SEG}/decision-overrides", "approvals.decide"),
    _rule(
        frozenset({"POST"}),
        rf"/api/jobs/{_SEG}/(?:cancel|restart|retry-step|acknowledge|unacknowledge)",
        "jobs.write",
    ),
    _rule(frozenset({"POST"}), r"/api/reports/generate", "reports.write"),
    _rule(_READ, r"/api/reports(?:/.*)?", "reports.read"),
    _rule(
        _READ,
        r"/api/(?:jobs|v2/jobs|approvals|dashboard|v2/dashboard|events|messages|watchdog|processes)"
        r"(?:/.*)?",
        "jobs.read",
    ),
    _rule(
        _WRITE,
        r"/api/(?:jobs|v2/jobs|approvals|events|watchdog|processes)(?:/.*)?",
        "jobs.manage",
    ),
    _rule(
        _WRITE,
        r"/api/(?:commands|trash|external-agents|reactions)(?:/.*)?",
        "platform.manage",
    ),
    # --- every other read: the default every role had -------------------------------------
    _rule(_READ, r"/api/.*", "platform.read"),
    # The product shell: pages whose data comes from the routes above.
    _rule(_READ, r"/(?!api/).*", None),
)


@dataclass(frozen=True, slots=True)
class RoutePermission:
    """The permission a request needs; ``permission is None`` means any signed-in principal."""

    permission: str | None
    rule: RoutePermissionRule


def route_permission(method: str, path: str) -> RoutePermission | None:
    """The permission ``method path`` needs, or ``None`` when no rule maps the route."""
    method_text = str(method or "").strip().upper()
    if method_text in _SAFE_ALIASES:
        method_text = "GET"
    path_text = str(path or "").strip()
    for rule in ROUTE_PERMISSION_RULES:
        if method_text in rule.methods and rule.pattern.fullmatch(path_text):
            return RoutePermission(rule.permission, rule)
    return None


def route_allowed(method: str, path: str, permissions: frozenset[str] | set[str]) -> bool:
    """Whether a holder of ``permissions`` may make the request; unmapped routes are denied."""
    match = route_permission(method, path)
    if match is None:
        return False
    return match.permission is None or match.permission in permissions


__all__ = [
    "ROUTE_PERMISSION_RULES",
    "RoutePermission",
    "RoutePermissionRule",
    "route_allowed",
    "route_permission",
]
