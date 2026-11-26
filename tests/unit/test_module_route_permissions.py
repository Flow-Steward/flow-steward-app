"""Route permission rules contributed by platform modules (ADR 0082, section 2)."""

from __future__ import annotations

import re

import pytest

from core.application.auth.permissions import ALL_PERMISSION_KEYS
from core.application.auth.route_permissions import (
    ROUTE_PERMISSION_RULES,
    ModuleRoutePermissionRegistry,
    ModuleRouteRuleRegistrar,
    module_api_prefix,
    route_admin_only,
    route_allowed,
    route_permission,
)

_MODULE = "acme.ledger"


def _registry_with_write_rule() -> ModuleRoutePermissionRegistry:
    registry = ModuleRoutePermissionRegistry()
    registry.register(
        _MODULE, methods=("POST", "DELETE"), pattern=r"exports(?:/[^/]+)?", permission="jobs.write"
    )
    return registry


def test_the_module_prefix_is_under_api_modules() -> None:
    assert module_api_prefix(_MODULE) == "/api/modules/acme.ledger/"


def test_an_empty_registry_leaves_core_decisions_unchanged() -> None:
    registry = ModuleRoutePermissionRegistry()

    assert registry.rules() == ()
    for method, path in [
        ("GET", "/api/modules/acme.ledger/exports"),
        ("POST", "/api/modules/acme.ledger/exports"),
        ("GET", "/api/jobs"),
        ("POST", "/api/projects"),
    ]:
        assert route_permission(method, path, registry.rules()) == route_permission(method, path)
    # A module write has no rule of its own: unmapped and denied to everybody.
    assert route_permission("POST", "/api/modules/acme.ledger/exports") is None
    assert route_permission("GET", "/api/modules/acme.ledger/exports").permission == (
        "platform.read"
    )


def test_core_table_is_not_changed_by_registration() -> None:
    before = tuple(ROUTE_PERMISSION_RULES)

    _registry_with_write_rule()

    assert tuple(ROUTE_PERMISSION_RULES) == before


def test_a_module_write_rule_maps_its_route() -> None:
    rules = _registry_with_write_rule().rules()

    match = route_permission("POST", "/api/modules/acme.ledger/exports/e-1", rules)

    assert match is not None
    assert match.permission == "jobs.write"
    assert match.rule.admin_only is False
    assert route_admin_only("DELETE", "/api/modules/acme.ledger/exports/e-1", rules) is False
    assert route_allowed("POST", "/api/modules/acme.ledger/exports", {"jobs.write"}, rules)
    assert not route_allowed("POST", "/api/modules/acme.ledger/exports", {"jobs.read"}, rules)


def test_a_module_read_rule_runs_before_the_catch_all() -> None:
    registry = ModuleRoutePermissionRegistry()
    registry.register(_MODULE, methods=("GET",), pattern=r"reports/.+", permission="reports.read")

    match = route_permission("GET", "/api/modules/acme.ledger/reports/r-1", registry.rules())
    unmatched = route_permission("GET", "/api/modules/acme.ledger/other", registry.rules())

    assert match.permission == "reports.read"
    assert unmatched.permission == "platform.read"
    assert unmatched.rule.pattern.pattern == r"/api/.*"


def test_a_core_rule_still_wins_over_a_module_rule() -> None:
    # A module rule can never shadow a Core route: it only matches its own tree.
    rules = _registry_with_write_rule().rules()

    assert route_permission("POST", "/api/projects", rules).permission == "projects.write"
    assert route_permission("GET", "/api/jobs", rules).permission == "jobs.read"


@pytest.mark.parametrize(
    "pattern",
    [
        r".*",
        r"x|/api/projects",
        r")|(/api/projects",
        r"(?:a)|.*/api/settings",
    ],
)
def test_a_rule_cannot_match_outside_the_module_prefix(pattern: str) -> None:
    registry = ModuleRoutePermissionRegistry()
    try:
        registry.register(_MODULE, methods=("POST",), pattern=pattern, permission="jobs.write")
    except ValueError:
        return
    rules = registry.rules()

    for path in (
        "/api/projects",
        "/api/settings/general",
        "/api/modules/acme.other/exports",
        "/api/modules/acme.ledgerx/exports",
        "/x|/api/projects",
    ):
        # A contributed rule must not change the decision for any path outside the
        # module tree: Core answers exactly as it does with nothing registered.
        assert route_permission("POST", path, rules) == route_permission("POST", path), path
    for rule in rules:
        assert rule.pattern.pattern.startswith(re.escape("/api/modules/acme.ledger/"))


def test_a_rule_never_matches_a_path_with_dot_segments() -> None:
    registry = ModuleRoutePermissionRegistry()
    registry.register(_MODULE, methods=("POST",), pattern=r".+", permission="jobs.write")
    rules = registry.rules()

    assert route_permission("POST", "/api/modules/acme.ledger/../../settings", rules) is None
    assert route_permission("POST", "/api/modules/acme.ledger/./x", rules) is None
    assert route_permission("POST", "/api/modules/acme.ledger/x", rules) is not None


@pytest.mark.parametrize(
    "pattern",
    [
        "",
        "/api/modules/acme.ledger/exports",
        "(unbalanced",
        "exports)",
    ],
)
def test_an_invalid_or_absolute_pattern_is_refused(pattern: str) -> None:
    with pytest.raises(ValueError):
        ModuleRoutePermissionRegistry().register(
            _MODULE, methods=("POST",), pattern=pattern, permission="jobs.write"
        )


@pytest.mark.parametrize("permission", ["ledger.export", "", "admin:all", "JOBS.WRITE"])
def test_an_unknown_permission_is_refused(permission: str) -> None:
    assert permission not in ALL_PERMISSION_KEYS
    with pytest.raises(ValueError):
        ModuleRoutePermissionRegistry().register(
            _MODULE, methods=("POST",), pattern="exports", permission=permission
        )


@pytest.mark.parametrize("module_id", ["", "Acme.Ledger", "ledger", "acme..ledger", "acme/ledger"])
def test_a_non_canonical_module_id_is_refused(module_id: str) -> None:
    with pytest.raises(ValueError):
        ModuleRoutePermissionRegistry().register(
            module_id, methods=("POST",), pattern="exports", permission="jobs.write"
        )


@pytest.mark.parametrize("methods", [(), ("TRACE",), ("POST", "CONNECT")])
def test_unknown_methods_are_refused(methods: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        ModuleRoutePermissionRegistry().register(
            _MODULE, methods=methods, pattern="exports", permission="jobs.write"
        )


def test_the_rules_view_is_immutable() -> None:
    registry = _registry_with_write_rule()

    rules = registry.rules()

    assert isinstance(rules, tuple)
    assert not hasattr(registry, "__dict__")


# --- the registration point a module receives is bound to its identity -------------


def test_a_scoped_registrar_registers_under_its_bound_module_only() -> None:
    registry = ModuleRoutePermissionRegistry()
    ledger = ModuleRouteRuleRegistrar(registry, "acme.ledger")
    reports = ModuleRouteRuleRegistrar(registry, "acme.reports")

    ledger.register(methods=("POST",), pattern=r".*", permission="jobs.write")
    reports.register(methods=("GET",), pattern="summary", permission="reports.read")
    rules = registry.rules()

    assert route_permission("POST", "/api/modules/acme.ledger/anything", rules).permission == (
        "jobs.write"
    )
    assert route_permission("POST", "/api/modules/acme.reports/summary", rules) is None
    assert route_permission("GET", "/api/modules/acme.reports/summary", rules).permission == (
        "reports.read"
    )
    assert registry.rule_count("acme.ledger") == 1
    assert registry.rule_count("acme.reports") == 1
    assert registry.rule_count("acme.notes") == 0


def test_a_scoped_registrar_takes_no_module_id() -> None:
    registrar = ModuleRouteRuleRegistrar(ModuleRoutePermissionRegistry(), "acme.ledger")

    with pytest.raises(TypeError):
        registrar.register(  # type: ignore[call-arg]
            "acme.reports", methods=("POST",), pattern="exports", permission="jobs.write"
        )
    with pytest.raises(TypeError):
        registrar.register(  # type: ignore[call-arg]
            module_id="acme.reports", methods=("POST",), pattern="exports", permission="jobs.write"
        )


def test_a_scoped_registrar_keeps_the_registry_checks() -> None:
    registrar = ModuleRouteRuleRegistrar(ModuleRoutePermissionRegistry(), "acme.ledger")

    with pytest.raises(ValueError):
        registrar.register(methods=("POST",), pattern="/api/projects", permission="jobs.write")
    with pytest.raises(ValueError):
        registrar.register(methods=("POST",), pattern="exports", permission="ledger.export")


def test_a_scoped_registrar_exposes_neither_the_registry_nor_its_rules() -> None:
    registry = ModuleRoutePermissionRegistry()
    registrar = ModuleRouteRuleRegistrar(registry, "acme.ledger")

    assert not hasattr(registrar, "__dict__")
    assert not hasattr(registrar, "rules")
    assert all(getattr(registrar, slot) is not registry for slot in registrar.__slots__)


@pytest.mark.parametrize("module_id", ["", "Acme.Ledger", "ledger"])
def test_a_scoped_registrar_needs_a_canonical_module_id(module_id: str) -> None:
    with pytest.raises(ValueError):
        ModuleRouteRuleRegistrar(ModuleRoutePermissionRegistry(), module_id)


# --- the real middleware reads the contributed rules ------------------------------


def test_the_middleware_maps_a_module_write_only_when_a_rule_is_registered() -> None:
    from tests.unit.test_route_permission_parity import _principal, middleware_decision

    rules = _registry_with_write_rule().rules()
    path = "/api/modules/acme.ledger/exports"

    operator_with_rule, _ = middleware_decision(
        path, "POST", _principal("session", "operator"), public=False, contributed=rules
    )
    viewer_with_rule, _ = middleware_decision(
        path, "POST", _principal("session", "viewer"), public=False, contributed=rules
    )
    operator_without_rule, _ = middleware_decision(
        path, "POST", _principal("session", "operator"), public=False
    )

    assert operator_with_rule is True
    assert viewer_with_rule is False
    assert operator_without_rule is False


# --- a module route is at least as strict as a Core route with its permission -------


def test_admin_only_follows_the_permission_as_core_pairs_them() -> None:
    from core.application.auth.route_permissions import (
        MODULE_ADMIN_ONLY_PERMISSIONS,
        MODULE_REFUSED_PERMISSIONS,
    )

    core_admin = {rule.permission for rule in ROUTE_PERMISSION_RULES if rule.admin_only}
    assert core_admin - {None} == MODULE_ADMIN_ONLY_PERMISSIONS
    # Core marks projects.write and secrets.bind admin-only on some rules and not on
    # others; a module rule takes the strict side.
    assert {"projects.write", "secrets.bind", "users.manage"} <= MODULE_ADMIN_ONLY_PERMISSIONS
    assert "jobs.write" not in MODULE_ADMIN_ONLY_PERMISSIONS
    registry = ModuleRoutePermissionRegistry()
    for permission in sorted(ALL_PERMISSION_KEYS - set(MODULE_REFUSED_PERMISSIONS)):
        rule = registry.register(_MODULE, methods=("POST",), pattern="probe", permission=permission)
        assert rule.admin_only is (permission in core_admin), permission
        assert rule.module_id == _MODULE


@pytest.mark.parametrize("permission", ["account.self", "account.profile.write", "secrets.approve"])
def test_self_service_and_interactive_permissions_are_refused(permission: str) -> None:
    with pytest.raises(ValueError, match="cannot guard a module route"):
        ModuleRoutePermissionRegistry().register(
            _MODULE, methods=("GET", "POST"), pattern="me", permission=permission
        )
    registrar = ModuleRouteRuleRegistrar(ModuleRoutePermissionRegistry(), _MODULE)
    with pytest.raises(ValueError, match="cannot guard a module route"):
        registrar.register(methods=("POST",), pattern="me", permission=permission)


def _matrix_rules() -> tuple:
    registry = ModuleRoutePermissionRegistry()
    registrar = ModuleRouteRuleRegistrar(registry, _MODULE)
    registrar.register(methods=("GET",), pattern="entries", permission="jobs.read")
    registrar.register(methods=("POST",), pattern="entries", permission="jobs.write")
    # Held by an operator, not by a viewer, and granted to no read:* token.
    registrar.register(methods=("GET",), pattern="catalogue", permission="registry.read")
    # Not admin-only on any Core rule, held by no operator.
    registrar.register(methods=("POST",), pattern="purge", permission="platform.manage")
    # Admin-grade: admin-only on Core's own route.
    registrar.register(methods=("GET", "POST"), pattern="members", permission="users.manage")
    return registry.rules()


#: (method, relative path) of the matrix: held/lacked/admin-grade/no rule, read and write.
_MATRIX = (
    ("GET", "entries"),  # jobs.read
    ("POST", "entries"),  # jobs.write
    ("GET", "catalogue"),  # registry.read
    ("POST", "purge"),  # platform.manage
    ("GET", "members"),  # users.manage, admin-only
    ("POST", "members"),  # users.manage, admin-only
    ("GET", "unknown"),  # no module rule: the generic platform.read rule
    ("POST", "unknown"),  # no module rule: unmapped, refused
)
_READ_COMMAND = ["read:*", "command:*"]


def _module_principal(kind: str, role: str, scopes: list[str] | None = None) -> dict:
    user: dict = {"id": f"{kind}-{role}", "username": f"{kind}-{role}", "roles": [role]}
    if kind == "session":
        user["auth_source"] = "local"
    elif kind == "external":
        user["auth_source"] = "external"
    elif kind == "pat":
        user.update(auth_source="pat", scopes=list(scopes or ()))
    elif kind == "oauth":
        user.update(auth_source="oauth", scopes=list(scopes or ()))
    elif kind == "service_account":
        user.update(
            auth_source="machine", principal_type="service_account", scopes=list(scopes or ())
        )
    elif kind == "system_actor":
        user.update(auth_source="mcp_dev", principal_type="system_actor")
    else:  # pragma: no cover - a typo in the matrix
        raise AssertionError(kind)
    return user


@pytest.mark.parametrize(
    ("kind", "role", "scopes", "expected"),
    [
        # Interactive people: the role's permissions.
        ("session", "viewer", None, "TFFFFFTF"),
        ("session", "operator", None, "TTTFFFTF"),
        ("session", "admin", None, "TTTTTTTF"),
        ("external", "operator", None, "TTTFFFTF"),
        # A person's token: the coarse scope, the catalogue grant and the owner's role.
        ("pat", "operator", _READ_COMMAND, "TTFFFFTF"),
        ("pat", "admin", _READ_COMMAND, "TTFFFFTF"),
        ("pat", "admin", ["admin:all"], "TTTTTTTF"),
        ("pat", "operator", ["read:*"], "TFFFFFTF"),
        ("oauth", "operator", _READ_COMMAND, "TTFFFFTF"),
        ("oauth", "admin", ["admin:all"], "TTTTTTTF"),
        # Service accounts: their role's permissions and their token's grant, no blanket rule.
        ("service_account", "viewer", _READ_COMMAND, "TFFFFFTF"),
        ("service_account", "operator", _READ_COMMAND, "TTFFFFTF"),
        ("service_account", "operator", [*_READ_COMMAND, "registry.read"], "TTTFFFTF"),
        ("service_account", "admin", _READ_COMMAND, "TTFFFFTF"),
        ("service_account", "admin", ["admin:all"], "TTTTTTTF"),
        # An internal machine principal without a token: its role's permissions.
        ("system_actor", "viewer", None, "TFFFFFTF"),
        ("system_actor", "operator", None, "TTTFFFTF"),
        ("system_actor", "admin", None, "TTTTTTTF"),
    ],
)
def test_every_principal_kind_needs_the_declared_permission_on_a_module_route(
    kind: str, role: str, scopes: list[str] | None, expected: str, monkeypatch
) -> None:
    from core.application.auth import auth_http_runtime
    from tests.unit.test_route_permission_parity import middleware_decision

    # The OAuth forward and route rules are ADR 0047's and tested on their own; here
    # an OAuth token is decided like any other token of a person.
    monkeypatch.setattr(auth_http_runtime, "_oauth_request_refusal", lambda *_a, **_k: None)
    rules = _matrix_rules()
    user = _module_principal(kind, role, scopes)
    answers = ""
    for method, relative in _MATRIX:
        allowed, _state = middleware_decision(
            f"{module_api_prefix(_MODULE)}{relative}",
            method,
            user,
            public=False,
            contributed=rules,
            check_scopes=True,
        )
        answers += "T" if allowed else "F"

    assert answers == expected


@pytest.mark.parametrize(
    ("kind", "scopes"),
    [
        ("service_account", ["admin:all"]),
        ("pat", ["admin:all"]),
        ("session", None),
        ("system_actor", None),
    ],
)
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_an_unmatched_module_write_is_refused_even_to_an_admin(
    kind: str, scopes: list[str] | None, method: str
) -> None:
    from tests.unit.test_route_permission_parity import middleware_decision

    allowed, _state = middleware_decision(
        "/api/modules/acme.ledger/entries/e-1",
        method,
        _module_principal(kind, "admin", scopes),
        public=False,
        contributed=_matrix_rules(),
        check_scopes=True,
    )

    assert allowed is False


@pytest.mark.parametrize(
    ("kind", "role", "scopes", "expected"),
    [
        ("service_account", "viewer", ["read:*"], True),
        ("service_account", "operator", ["command:*"], False),
        ("pat", "viewer", ["read:*"], True),
        ("pat", "operator", ["command:*"], False),
        ("oauth", "viewer", ["read:*"], True),
    ],
)
def test_an_unmatched_module_read_falls_to_the_generic_read_rule(
    kind: str, role: str, scopes: list[str], expected: bool, monkeypatch
) -> None:
    from core.application.auth import auth_http_runtime
    from tests.unit.test_route_permission_parity import middleware_decision

    monkeypatch.setattr(auth_http_runtime, "_oauth_request_refusal", lambda *_a, **_k: None)
    path = "/api/modules/acme.ledger/unknown"
    assert route_permission("GET", path, _matrix_rules()).rule.pattern.pattern == r"/api/.*"

    allowed, _state = middleware_decision(
        path,
        "GET",
        _module_principal(kind, role, scopes),
        public=False,
        contributed=_matrix_rules(),
        check_scopes=True,
    )

    assert allowed is expected


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/jobs"),
        ("POST", "/api/jobs/j1/cancel"),
        ("GET", "/api/auth/users"),
        ("POST", "/api/projects"),
        ("DELETE", "/api/secrets/s1"),
        ("POST", "/api/auth/logout"),
        ("POST", "/api/not-a-route"),
        ("GET", "/api/anything-else"),
    ],
)
def test_module_rules_do_not_change_any_core_decision(method: str, path: str) -> None:
    from tests.unit.test_route_permission_parity import _principal, middleware_decision

    rules = _matrix_rules()
    for kind in ("session", "external", "pat", "machine"):
        for role in ("admin", "operator", "viewer"):
            recorded_with: list = []
            recorded_without: list = []
            with_rules = middleware_decision(
                path,
                method,
                _principal(kind, role),
                public=False,
                recorded=recorded_with,
                contributed=rules,
            )[0]
            without_rules = middleware_decision(
                path, method, _principal(kind, role), public=False, recorded=recorded_without
            )[0]
            assert (with_rules, recorded_with) == (without_rules, recorded_without), (
                kind,
                role,
            )
