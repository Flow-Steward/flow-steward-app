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

The middleware decides every request of a person (browser session, external sign-in)
and of a person's own token with it: it replaced the operator allowlist, the
admin-only route list and the "a person's write needs admin" default.
``tests/unit/test_route_permission_parity.py`` proves against a frozen snapshot of
those rules that no role's answer changed. Resource checks (account, workspace,
project, workflow, ownership, token allowlists) are not part of it.

A rule marked ``admin_only`` is a route that was on the admin-only list. That list
still decides two rules ADR 0076 leaves as they are: the token scope a route needs
(``admin:all`` for these) and the role a service account needs (admin). The flag is
that list; the parity test proves it marks exactly the routes the list matched.

Platform modules (ADR 0082) add rules only through a
:class:`ModuleRouteRuleRegistrar` that Core binds to the module's loaded identity,
and only under their own prefix ``/api/modules/<module id>/``. Their rules are checked right before the generic
``/api/.*`` read rule, after every other Core rule, and name only permissions of
Core's catalogue. A module rule is admin-only exactly when its permission is
admin-only on some Core rule; own-account and interactive-only permissions are
refused. A module rule may also ask for an installation administrator
(``requires_installation_admin``); its permission must then be admin-only, and the
middleware additionally asks its installation-administrator predicate. With nothing
registered the decision is the table's alone.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from core.application.auth.permissions import ALL_PERMISSION_KEYS
from core.infrastructure.extension_runtime.package_identity import CANONICAL_EXTENSION_ID_RE

_SEG = r"[^/]+"
_READ = frozenset({"GET"})
_WRITE = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_ANY = _READ | _WRITE
#: HEAD and OPTIONS are reads, as in the middleware.
_SAFE_ALIASES = frozenset({"HEAD", "OPTIONS"})
#: Path segments under /api/extensions/ that are platform routes, not an extension id.
_RESERVED_EXTENSION_SEGMENTS = (
    "bootstrap|descriptors|install|install-requests|installs|marketplace|releases|runtime|scan"
)


@dataclass(frozen=True, slots=True)
class RoutePermissionRule:
    """Requests with one of ``methods`` whose path fully matches ``pattern`` need ``permission``."""

    methods: frozenset[str]
    pattern: re.Pattern[str]
    permission: str | None
    #: The route needs ``admin:all`` from a token and the admin role from a service
    #: account (the former admin-only route list).
    admin_only: bool = False
    #: The platform module that contributed the rule; ``None`` for Core's own rules.
    module_id: str | None = None
    #: A module rule that only an installation administrator may pass (ADR 0082). The
    #: middleware asks its installation-administrator predicate for such a route.
    requires_installation_admin: bool = False


def _rule(
    methods: frozenset[str], pattern: str, permission: str | None, *, admin_only: bool = False
) -> RoutePermissionRule:
    return RoutePermissionRule(methods, re.compile(pattern), permission, admin_only)


ROUTE_PERMISSION_RULES: tuple[RoutePermissionRule, ...] = (
    # --- public protocol routes: authenticated by the protocol or a signed token ------
    _rule(
        _WRITE,
        r"/api/auth/(?:login|bootstrap|password-reset/(?:request|verify|confirm))",
        None,
    ),
    _rule(_ANY, r"/oauth/(?:authorize|token|revoke|consent)", None),
    # External sign-in (ADR 0083): browser navigations authenticated by the sign-in method.
    _rule(_READ, r"/api/auth/external/(?:options|start|callback)", None),
    _rule(_ANY, r"/\.well-known/oauth-authorization-server", None),
    _rule(_ANY, r"/webhook/(?:workflows|extensions|providers)/.+", None),
    _rule(_ANY, r"/api/extension-artifacts/.+", None),
    # Under /api/mcp, so a service account still needs the admin role for it.
    _rule(_READ, rf"/api/mcp/oauth/client-metadata/{_SEG}\.json", None, admin_only=True),
    # --- the principal's own account ----------------------------------------------------
    _rule(
        _READ,
        r"/api/(?:auth/(?:me|introspect|tokens|multi-factor/status)|app/bootstrap)",
        None,
    ),
    _rule(_READ, r"/api/users/me/.+", None),
    _rule(_READ, r"/api/auth/(?:agent-access(?:/.*)?|oauth-grants)", "account.self"),
    _rule(_READ, rf"/api/oauth/authorizations/{_SEG}", "account.self"),
    _rule(
        frozenset({"POST"}),
        r"/api/auth/(?:logout|change-password|tokens|agent-access/tokens"
        r"|multi-factor/(?:enroll|verify|disable))",
        "account.self",
    ),
    _rule(
        frozenset({"POST"}),
        rf"/api/auth/(?:tokens|oauth-grants)/{_SEG}/revoke",
        "account.self",
    ),
    _rule(frozenset({"POST"}), rf"/api/oauth/authorizations/{_SEG}/decision", "account.self"),
    # ADR 0085: whoever opened an invitation link answers it, also a person with no role
    # anywhere yet (P5); the route refuses tokens and checks the link's token.
    _rule(frozenset({"POST"}), r"/api/auth/invitations/(?:preview|accept|decline)", None),
    _rule(frozenset({"POST"}), r"/api/users/me/email-change/request", "account.self"),
    _rule(
        frozenset({"PATCH"}),
        r"/api/users/me/(?:profile|timezone|locale|date-time-format)",
        # Every role changes its own profile, language, time zone and date format.
        "account.self",
    ),
    _rule(frozenset({"POST"}), r"/api/users/me/detect-timezone", "account.self"),
    # ADR 0085: every role moves between its companies, a viewer included.
    _rule(frozenset({"POST"}), r"/api/auth/switch-account", "account.self"),
    # --- users, accounts, installation settings -----------------------------------------
    _rule(
        _ANY,
        r"/api/auth/(?:users|accounts|service-accounts)(?:/.*)?",
        "users.manage",
        admin_only=True,
    ),
    _rule(_ANY, r"/admin(?:/.*)?", "settings.manage", admin_only=True),
    _rule(
        _ANY,
        r"/api/settings/mcp-oauth(?:/platform-clients(?:/.*)?)?",
        "settings.manage",
        admin_only=True,
    ),
    _rule(_ANY, r"/api/system/core-updates", "settings.manage", admin_only=True),
    _rule(
        _ANY,
        r"/api/settings/external-sign-in/identities(?:/[^/]+)?",
        "settings.manage",
        admin_only=True,
    ),
    _rule(
        _ANY,
        r"/api/(?:model-providers|llm-provider-extensions)(?:/.*)?",
        "settings.manage",
        admin_only=True,
    ),
    _rule(_WRITE, r"/api/(?:settings|system|security)(?:/.*)?", "settings.manage"),
    _rule(_READ, r"/api/security/posture", "registry.read"),
    _rule(_READ, r"/health/deep", "registry.read"),
    # --- the registry ---------------------------------------------------------------------
    _rule(
        _ANY,
        r"/api/(?:agents|catalog-categories|tools|skills|data-contracts|datasets|fields"
        r"|guardrails|mcp|preview|published-blocks)(?:/.*)?",
        "registry.manage",
        admin_only=True,
    ),
    # Searching the public extension and workflow catalogues.
    _rule(_READ, r"/api/catalog/search", "registry.manage", admin_only=True),
    _rule(_WRITE, r"/api/(?:providers|changelog|registry)(?:/.*)?", "registry.manage"),
    _rule(
        _READ,
        r"/api/(?:providers|changelog)(?:/.*)?|/api/(?:model-selection/options|workload|runs)",
        "registry.read",
    ),
    _rule(_ANY, r"/api/workspaces(?:/.*)?", "workspaces.manage", admin_only=True),
    # --- extensions -----------------------------------------------------------------------
    # Requests to install an extension (ADR 0084): an account administrator asks, the
    # installation administrator answers; the service decides which of the two a caller is.
    _rule(_ANY, r"/api/extensions/install-requests(?:/.*)?", "extensions.manage", admin_only=True),
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
    # Importing a descriptor from a local file or URL, installing from the catalogue
    # and minting a runtime token.
    _rule(
        _WRITE,
        r"/api/extensions/(?:descriptors/import/(?:file|url)|catalog/install|runtime/token)",
        "extensions.manage",
        admin_only=True,
    ),
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
    # Where an account's secret values are kept (ADR 0066 amendment 2026-11-26): the
    # account admin's; the service also requires a person signed in interactively.
    _rule(_ANY, r"/api/secret-backends(?:/.*)?", "settings.manage", admin_only=True),
    _rule(_READ, r"/api/secrets(?:/.*)?", "secrets.read"),
    _rule(frozenset({"POST"}), r"/api/secrets", "secrets.value.write"),
    _rule(frozenset({"PATCH"}), rf"/api/secrets/{_SEG}", "secrets.value.write"),
    _rule(frozenset({"PUT"}), rf"/api/secrets/{_SEG}/value", "secrets.value.write"),
    _rule(frozenset({"DELETE"}), rf"/api/secrets/{_SEG}", "secrets.delete"),
    _rule(
        _WRITE,
        rf"/api/projects/{_SEG}/(?:workflows|connections)/{_SEG}/secret-bindings/{_SEG}",
        "secrets.bind",
    ),
    # A value typed into a workflow's secret input becomes a catalog secret bound to it.
    _rule(
        _WRITE,
        rf"/api/projects/{_SEG}/(?:workflows|connections)/{_SEG}/secret-bindings/{_SEG}/value",
        "secrets.bind",
        admin_only=True,
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
    _rule(_ANY, rf"/api/projects/{_SEG}/workflows(?:/.*)?", "workflows.manage", admin_only=True),
    _rule(_ANY, rf"/api/projects/{_SEG}/providers(?:/.*)?", "projects.write", admin_only=True),
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
    # Killing a worker process and recovering a job the watchdog found.
    _rule(
        _WRITE,
        rf"/api/(?:processes/{_SEG}/kill|watchdog/jobs/{_SEG}/recover)",
        "jobs.manage",
        admin_only=True,
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


#: Where module rules are checked: right before the generic API read rule.
_API_READ_CATCH_ALL_INDEX = next(
    index for index, rule in enumerate(ROUTE_PERMISSION_RULES) if rule.pattern.pattern == r"/api/.*"
)
#: Every module API route lives under this prefix, followed by the module id.
MODULE_API_PREFIX = "/api/modules/"


def module_api_prefix(module_id: str) -> str:
    """The path prefix a platform module owns: ``/api/modules/<module id>/``."""
    return f"{MODULE_API_PREFIX}{module_id}/"


def is_module_api_path(path: str) -> bool:
    """Whether ``path`` lies in some platform module's API tree."""
    return str(path or "").strip().startswith(MODULE_API_PREFIX)


#: Permissions Core's table marks admin-only on at least one rule. A module rule that
#: names one is admin-only as well: a token then needs ``admin:all`` and a service
#: account the admin role, as on the strictest Core route with that permission. The
#: module does not choose; the permission decides.
MODULE_ADMIN_ONLY_PERMISSIONS: frozenset[str] = frozenset(
    rule.permission
    for rule in ROUTE_PERMISSION_RULES
    if rule.admin_only and rule.permission is not None
)
#: Permissions a module rule may not name, and why. Own-account permissions are the
#: signed-in principal's self-service (Core lets a local machine principal make such a
#: write with no role and maps an external sign-in to another permission), and
#: approving a configuration's secrets is decided only in a person's interactive
#: session, which the secret service checks, not the route table.
MODULE_REFUSED_PERMISSIONS: dict[str, str] = {
    "account.self": "is the principal's own-account self-service",
    "account.profile.write": "is the principal's own-account self-service",
    "secrets.approve": "is decided only in a person's interactive session",
}


def _has_dot_segment(path: str) -> bool:
    return any(segment in {".", ".."} for segment in path.split("/"))


def _contributed_match(
    method: str, path: str, contributed: Sequence[RoutePermissionRule]
) -> RoutePermission | None:
    # A contributed rule never decides a path outside the module tree, nor one with
    # dot segments, whatever its pattern says.
    if not path.startswith(MODULE_API_PREFIX) or _has_dot_segment(path):
        return None
    for rule in contributed:
        if method in rule.methods and rule.pattern.fullmatch(path):
            return RoutePermission(rule.permission, rule)
    return None


def route_permission(
    method: str, path: str, contributed: Sequence[RoutePermissionRule] = ()
) -> RoutePermission | None:
    """The permission ``method path`` needs, or ``None`` when no rule maps the route.

    ``contributed`` are module rules from :class:`ModuleRoutePermissionRegistry`;
    they are checked after every Core rule except the generic API read rule.
    """
    method_text = str(method or "").strip().upper()
    if method_text in _SAFE_ALIASES:
        method_text = "GET"
    path_text = str(path or "").strip()
    for index, rule in enumerate(ROUTE_PERMISSION_RULES):
        if index == _API_READ_CATCH_ALL_INDEX and contributed:
            match = _contributed_match(method_text, path_text, contributed)
            if match is not None:
                return match
        if method_text in rule.methods and rule.pattern.fullmatch(path_text):
            return RoutePermission(rule.permission, rule)
    return None


def route_admin_only(
    method: str, path: str, contributed: Sequence[RoutePermissionRule] = ()
) -> bool:
    """Whether the route was admin-only: a token needs ``admin:all``, a service account admin."""
    match = route_permission(method, path, contributed)
    return bool(match is not None and match.rule.admin_only)


def route_allowed(
    method: str,
    path: str,
    permissions: frozenset[str] | set[str],
    contributed: Sequence[RoutePermissionRule] = (),
) -> bool:
    """Whether a holder of ``permissions`` may make the request; unmapped routes are denied."""
    match = route_permission(method, path, contributed)
    if match is None:
        return False
    return match.permission is None or match.permission in permissions


class ModuleRoutePermissionRegistry:
    """Route permission rules that platform modules contribute (ADR 0082, section 2).

    A module registers a rule for its own API tree only. The pattern is relative to
    ``/api/modules/<module id>/`` and is compiled as a group behind that literal
    prefix, so it cannot match any other path. A rule names a permission of Core's
    catalogue; a module cannot invent a permission or make a route public. Whether
    the rule is admin-only follows from the permission
    (:data:`MODULE_ADMIN_ONLY_PERMISSIONS`), never from the module, and a permission of
    :data:`MODULE_REFUSED_PERMISSIONS` is refused. Rules can be added but not replaced
    or removed, except that Core's module loader rolls back the rules of a module
    whose loading failed (:meth:`restore_loading_checkpoint`).
    """

    __slots__ = ("_counts", "_lock", "_rules")

    def __init__(self) -> None:
        self._rules: tuple[RoutePermissionRule, ...] = ()
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()

    def register(
        self,
        module_id: str,
        *,
        methods: Iterable[str],
        pattern: str,
        permission: str,
        requires_installation_admin: bool = False,
    ) -> RoutePermissionRule:
        """Add one rule; raises ``ValueError`` when it would leave the module's tree.

        ``requires_installation_admin`` limits the route to an installation
        administrator. Its permission must be admin-only
        (:data:`MODULE_ADMIN_ONLY_PERMISSIONS`), so a person needs an admin-grade
        permission, a token ``admin:all`` and a service account the admin role
        (the existing admin check); the middleware then asks its
        installation-administrator predicate as well.
        """
        module = str(module_id or "").strip()
        if not CANONICAL_EXTENSION_ID_RE.fullmatch(module):
            raise ValueError(f"module id {module_id!r} is not a canonical package id")
        if isinstance(methods, str):
            methods = (methods,)
        method_set = frozenset(str(item or "").strip().upper() for item in methods)
        if not method_set or not method_set <= _ANY:
            raise ValueError(f"methods must be a non-empty subset of {sorted(_ANY)}")
        permission_key = str(permission or "").strip()
        if permission_key not in ALL_PERMISSION_KEYS:
            raise ValueError(f"permission {permission!r} is not in Core's permission catalogue")
        refused = MODULE_REFUSED_PERMISSIONS.get(permission_key)
        if refused is not None:
            raise ValueError(
                f"permission {permission_key!r} cannot guard a module route: it {refused}"
            )
        if not isinstance(requires_installation_admin, bool):
            raise ValueError("requires_installation_admin must be a bool")
        if requires_installation_admin and permission_key not in MODULE_ADMIN_ONLY_PERMISSIONS:
            raise ValueError(
                f"permission {permission_key!r} is not admin-only, so it cannot guard a "
                "route that requires an installation administrator"
            )
        relative = str(pattern or "")
        if not relative or relative.startswith("/"):
            raise ValueError("pattern must be relative to the module prefix, without a leading /")
        try:
            # Compiled alone first: a pattern with unbalanced groups could otherwise
            # close the wrapping group and add an alternative outside the prefix.
            re.compile(relative)
            compiled = re.compile(rf"{re.escape(module_api_prefix(module))}(?:{relative})")
        except re.error as exc:
            raise ValueError(f"pattern {pattern!r} is not a valid expression: {exc}") from exc
        rule = RoutePermissionRule(
            method_set,
            compiled,
            permission_key,
            admin_only=permission_key in MODULE_ADMIN_ONLY_PERMISSIONS,
            module_id=module,
            requires_installation_admin=requires_installation_admin,
        )
        with self._lock:
            self._rules = (*self._rules, rule)
            self._counts[module] = self._counts.get(module, 0) + 1
        return rule

    def rules(self) -> tuple[RoutePermissionRule, ...]:
        return self._rules

    def rule_count(self, module_id: str) -> int:
        """How many rules were registered under ``module_id``."""
        return self._counts.get(str(module_id or "").strip(), 0)

    def loading_checkpoint(self) -> tuple[tuple[RoutePermissionRule, ...], dict[str, int]]:
        """The registered rules, for the module loader's rollback."""
        with self._lock:
            return self._rules, dict(self._counts)

    def restore_loading_checkpoint(
        self, checkpoint: tuple[tuple[RoutePermissionRule, ...], Mapping[str, int]]
    ) -> None:
        """Drop every rule registered after ``checkpoint`` was taken.

        Only Core's module loader calls it, when a platform module fails to load.
        """
        rules, counts = checkpoint
        with self._lock:
            self._rules = tuple(rules)
            self._counts = dict(counts)


class ModuleRouteRuleRegistrar:
    """The route permission registration point one platform module receives.

    Core builds it with the identity under which it loaded the module, so the
    module cannot name another module's tree: ``register`` takes no module id.
    The shared registry is held only in a closure, not in an attribute. That is an
    interface boundary, not a sandbox: in-process code can always introspect.
    """

    __slots__ = ("_add",)

    def __init__(self, registry: ModuleRoutePermissionRegistry, module_id: str) -> None:
        module = str(module_id or "").strip()
        if not CANONICAL_EXTENSION_ID_RE.fullmatch(module):
            raise ValueError(f"module id {module_id!r} is not a canonical package id")

        def add(
            methods: Iterable[str],
            pattern: str,
            permission: str,
            requires_installation_admin: bool,
        ) -> RoutePermissionRule:
            return registry.register(
                module,
                methods=methods,
                pattern=pattern,
                permission=permission,
                requires_installation_admin=requires_installation_admin,
            )

        self._add = add

    def register(
        self,
        *,
        methods: Iterable[str],
        pattern: str,
        permission: str,
        requires_installation_admin: bool = False,
    ) -> RoutePermissionRule:
        """Add one rule under the loaded module's own ``/api/modules/<id>/`` tree."""
        return self._add(methods, pattern, permission, requires_installation_admin)


__all__ = [
    "MODULE_ADMIN_ONLY_PERMISSIONS",
    "MODULE_API_PREFIX",
    "MODULE_REFUSED_PERMISSIONS",
    "ROUTE_PERMISSION_RULES",
    "ModuleRoutePermissionRegistry",
    "ModuleRouteRuleRegistrar",
    "RoutePermission",
    "RoutePermissionRule",
    "is_module_api_path",
    "module_api_prefix",
    "route_admin_only",
    "route_allowed",
    "route_permission",
]
