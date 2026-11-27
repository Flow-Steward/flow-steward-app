"""ADR 0076 §6: the route table answers every request as the old role rules did.

``tests/fixtures/auth/adr0076_route_role_decisions.json`` froze, before the old rules
were removed (the operator allowlist ``operator_work.py``, the admin-only route list
and "a person's write needs admin"), which system roles the middleware let through on
every route for a browser session, an external sign-in, a personal token and a service
account, and which token scopes each route needed. The middleware now decides people
and their tokens with ``route_permissions`` and the stored role permissions; these
tests run it on every route of the application and compare.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from functools import cache
from pathlib import Path

import pytest
from starlette.requests import Request
from starlette.responses import Response

from core.application.auth import auth_http_runtime
from core.application.auth.permissions import (
    ALL_PERMISSION_KEYS,
    SYSTEM_ROLE_PERMISSIONS,
)
from core.application.auth.principal import ResolvedPrincipal
from core.application.auth.request_principal_runtime import roles_intersect
from core.application.auth.route_permissions import (
    ROUTE_PERMISSION_RULES,
    route_allowed,
    route_permission,
)
from core.infrastructure.runtime.http_app_auth_runtime import _is_registry_read_route

ROLES = ("admin", "operator", "viewer")
KINDS = ("session", "external", "pat", "machine")
#: Every scope a route can ask for, so the role decision is what is compared.
ALL_SCOPES = ["admin:all", "command:*", "secrets:write", "secrets:bind", "read:*"]
SNAPSHOT = (
    Path(__file__).resolve().parents[1] / "fixtures" / "auth" / "adr0076_route_role_decisions.json"
)

#: Public routes that authenticate by their protocol or a signed token. Called with a
#: person's session cookie, the old middleware still applied its role rules to them ("a
#: person's write needs admin", the /api/mcp admin-only pattern); the table names
#: them public. Their own handlers decide (a signed webhook, the sign-in form, the
#: connector's client metadata); a session adds nothing, so the role gate there was an
#: artefact of the order of checks, not a rule carried over. Service accounts keep it.
PUBLIC_ROUTE_EXCEPTIONS = frozenset(
    {
        ("GET", "/api/mcp/oauth/client-metadata/{connector_id}.json"),
        ("POST", "/api/auth/login"),
        ("POST", "/api/auth/bootstrap"),
        ("POST", "/api/auth/password-reset/request"),
        ("POST", "/api/auth/password-reset/verify"),
        ("POST", "/api/auth/password-reset/confirm"),
        ("POST", "/webhook/workflows/{entry_key}"),
        ("POST", "/webhook/providers/{provider_key}"),
        ("POST", "/webhook/extensions/{extension_id}/{callback_id}/{callback_token}"),
        ("PUT", "/api/extension-artifacts/{kind}/{object_id}"),
    }
)

#: Own-account self-service writes a service account (``machine``) reached as a viewer
#: only because its bearer credential resolved through the local token path and was
#: taken for a person's session. The rule now reads the principal's own source
#: (ADR 0083 re-review, c63d3175f9), so a viewer service account is held to the
#: write-role rule like an external viewer already was ('ao'). Intended: a service
#: account has no password or e-mail, must not mint personal tokens, has no cookie
#: session to log out, and revokes its machine tokens through
#: /api/auth/service-accounts/{key}/tokens/{id}/revoke, not the personal-token route.
MACHINE_SELF_SERVICE_TIGHTENED = frozenset(
    {
        ("POST", "/api/auth/change-password"),
        ("POST", "/api/auth/logout"),
        ("POST", "/api/auth/tokens"),
        ("POST", "/api/auth/tokens/{token_id}/revoke"),
        ("POST", "/api/users/me/email-change/request"),
    }
)

#: Routes whose frozen answer was loosened on purpose, with the answer they give now.
#: Switching between one's companies (ADR 0085) needs ``account.self``, which every
#: role holds: a viewer in the acting company was stuck there while a person with no
#: role at all could switch (P5). A viewer also changes their own profile, language,
#: time zone and date format (owner decision, 2026-11-27). An external session keeps the
#: external own-account rule and a service account keeps the write-role rule.
LOOSENED_ON_PURPOSE: dict[tuple[str, str], dict[str, str]] = {
    ("POST", "/api/auth/switch-account"): {"session": "aov", "pat": "aov"},
    ("PATCH", "/api/users/me/profile"): {"session": "aov", "pat": "aov"},
    ("PATCH", "/api/users/me/timezone"): {"session": "aov", "pat": "aov"},
    ("PATCH", "/api/users/me/locale"): {"session": "aov", "pat": "aov"},
    ("PATCH", "/api/users/me/date-time-format"): {"session": "aov", "pat": "aov"},
    ("POST", "/api/users/me/detect-timezone"): {"session": "aov", "pat": "aov"},
}

_ROUTE_TABLE = """
import json
from core.entrypoints.api import app_factory
from core.entrypoints.api.app import app
rows = []
for route in app.routes:
    path = getattr(route, "path", None)
    for method in sorted(getattr(route, "methods", None) or ()):
        if path and method in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            sample = path.replace("{", "").replace("}", "")
            rows.append({
                "method": method,
                "path": path,
                "sample": sample,
                "public": bool(app_factory._is_public_auth_path(app, sample)),
            })
print(json.dumps(rows))
"""


@cache
def _routes() -> tuple[dict, ...]:
    environment = dict(os.environ, CONTROL_LOOPS_ENABLED="0", FS_MEMORY_VECTOR_BACKEND="null")
    listing = subprocess.run(
        [sys.executable, "-c", _ROUTE_TABLE],
        capture_output=True,
        text=True,
        timeout=300,
        env=environment,
        check=False,
    )
    assert listing.returncode == 0, listing.stderr[-2000:]
    rows = json.loads(listing.stdout.strip().splitlines()[-1])
    assert len(rows) > 500  # non-vacuity: the real application, not a stub
    return tuple(rows)


@cache
def _snapshot() -> dict[str, dict]:
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))["routes"]


def _request(path: str, method: str, *, bearer: bool) -> Request:
    headers = (
        [(b"authorization", b"Bearer some-token")]
        if bearer
        else [(b"cookie", b"session=session-token")]
    )
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": headers,
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
            "root_path": "",
        }
    )


def _principal(kind: str, role: str) -> dict:
    user = {"id": f"{role}-1", "username": role, "roles": [role]}
    if kind == "session":
        user["auth_source"] = "local"
    elif kind == "external":
        user["auth_source"] = "external"
    elif kind == "pat":
        user.update(auth_source="pat", scopes=list(ALL_SCOPES))
    else:
        user.update(
            auth_source="machine", principal_type="service_account", scopes=list(ALL_SCOPES)
        )
    return user


def middleware_decision(
    path: str,
    method: str,
    user: dict,
    *,
    public: bool,
    recorded: list | None = None,
    stored: dict | None = None,
    loader=None,
    contributed: tuple = (),
    check_scopes: bool = False,
    cookie_session: bool = False,
    real_normalize: bool = False,
    active_accounts: frozenset[str] | None = None,
) -> tuple[bool, object]:
    """Run the real middleware once; ``recorded`` collects the token scopes it required.

    ``contributed`` are module route rules (ADR 0082) the middleware reads per request.
    ``check_scopes`` keeps the real token scope check instead of recording and passing.
    """
    # ``cookie_session``: the principal arrives through the session cookie lookup (an
    # external sign-in's session included), not through the request resolver.
    external = user.get("auth_source") == "external" and not cookie_session
    bearer = user.get("auth_source") in {"pat", "machine"}
    seen: dict = {}

    async def _call_next(request: Request) -> Response:
        seen["state"] = request.state
        return Response(status_code=204)

    def _record(_user, required, **_kwargs):
        if recorded is not None:
            recorded.append(sorted(str(item) for item in required))
        return True

    normalize = (
        (lambda raw, **kwargs: _real_normalize_live_principal(raw, **kwargs))
        if real_normalize
        else (lambda raw, **_kwargs: ResolvedPrincipal(**raw) if isinstance(raw, dict) else None)
    )
    bl: object = object()
    if active_accounts is not None:
        bl = type(
            "_AccountsBl",
            (),
            {"is_account_active": staticmethod(lambda account_id: account_id in active_accounts)},
        )()

    original = auth_http_runtime.principal_has_required_scopes
    if not check_scopes:
        auth_http_runtime.principal_has_required_scopes = _record
    try:
        response = asyncio.run(
            auth_http_runtime.local_auth_middleware(
                _request(path, method, bearer=bearer),
                _call_next,
                local_auth_enabled_fn=lambda: True,
                external_auth_enabled=external,
                is_public_auth_path_fn=lambda _path: public,
                route_registered_fn=lambda _path, _method: True,
                request_principal_resolver_fn=lambda _request, _bl: dict(user),
                normalize_live_principal_fn=normalize,
                get_logger_only_fn=lambda: bl,
                auth_tables_available_fn=lambda _bl: True,
                get_user_by_session_token_fn=lambda _bl, _token: dict(user),
                local_auth_bootstrap_required_fn=lambda _bl=None: False,
                auth_account_tables_available_fn=lambda _bl: active_accounts is not None,
                roles_intersect_fn=roles_intersect,
                is_registry_read_route_fn=lambda candidate: bool(
                    _is_registry_read_route(candidate)
                ),
                is_self_service_auth_write_route_fn=lambda _path: False,
                csrf_request_valid_fn=lambda _request: True,
                auth_cookie_name="session",
                auth_unsafe_methods={"POST", "PUT", "PATCH", "DELETE"},
                auth_admin_roles={"admin"},
                auth_write_roles={"admin", "operator"},
                err_database_not_configured="Database not configured",
                err_auth_tables_not_available="Auth tables unavailable",
                err_authentication_required="Authentication required",
                err_account_disabled="Account is disabled",
                err_admin_role_required="Admin role required",
                err_operator_role_required="Operator role required",
                path_bootstrap="/bootstrap",
                path_login="/login",
                load_role_permissions_fn=loader
                or (lambda _bl: SYSTEM_ROLE_PERMISSIONS if stored is None else stored),
                contributed_route_rules_fn=lambda: contributed,
            )
        )
    finally:
        auth_http_runtime.principal_has_required_scopes = original
    return response.status_code == 204, seen.get("state")


def _real_normalize_live_principal(raw, *, default_auth_source: str):
    from core.application.auth.request_principal_runtime import normalize_live_principal
    from core.application.auth.resolve import normalize_principal

    return normalize_live_principal(
        raw,
        default_auth_source=default_auth_source,
        normalize_principal_fn=normalize_principal,
        resolved_principal_type=ResolvedPrincipal,
    )


def _decisions(row: dict, kind: str) -> tuple[str, list[str] | None]:
    allowed, scopes = "", None
    for role in ROLES:
        recorded: list = []
        ok, _state = middleware_decision(
            row["sample"],
            row["method"],
            _principal(kind, role),
            public=row["public"],
            recorded=recorded,
        )
        if ok:
            allowed += role[0]
        if role == "admin":
            scopes = recorded[0] if recorded else None
    return allowed, scopes


def _compared_routes() -> list[dict]:
    snapshot = _snapshot()
    rows = [row for row in _routes() if f"{row['method']} {row['path']}" in snapshot]
    # Non-vacuity: the snapshot still describes the application.
    assert len(rows) >= 0.95 * len(snapshot)
    return rows


@pytest.mark.parametrize("kind", KINDS)
def test_every_principal_kind_gets_the_frozen_answer_on_every_route(kind) -> None:
    snapshot = _snapshot()
    mismatches = {}
    for row in _compared_routes():
        key = f"{row['method']} {row['path']}"
        expected = snapshot[key][kind]
        if kind == "machine" and (row["method"], row["path"]) in MACHINE_SELF_SERVICE_TIGHTENED:
            assert expected == "aov", key  # the frozen answer this set documents
            expected = "ao"
        loosened = LOOSENED_ON_PURPOSE.get((row["method"], row["path"]), {})
        if kind in loosened:
            assert expected == "ao", key  # the frozen answer this map documents
            expected = loosened[kind]
        allowed, scopes = _decisions(row, kind)
        if (row["method"], row["path"]) in PUBLIC_ROUTE_EXCEPTIONS and kind != "machine":
            # The documented exceptions: every role reaches the public handler.
            if allowed != "aov":
                mismatches[key] = (expected, allowed)
        elif allowed != expected:
            mismatches[key] = (expected, allowed)
        if kind in {"pat", "machine"} and scopes != snapshot[key][f"{kind}_scopes"]:
            mismatches[f"{key} scopes"] = (snapshot[key][f"{kind}_scopes"], scopes)

    assert mismatches == {}


def test_the_admin_only_flag_marks_exactly_the_former_admin_only_routes() -> None:
    snapshot = _snapshot()
    wrong = {}
    for row in _compared_routes():
        key = f"{row['method']} {row['path']}"
        match = route_permission(row["method"], row["sample"])
        flagged = bool(match is not None and match.rule.admin_only)
        if flagged != snapshot[key]["admin_only"]:
            wrong[key] = flagged

    assert wrong == {}


def test_the_documented_exceptions_are_the_routes_whose_old_answer_was_admin_only() -> None:
    snapshot = _snapshot()
    for method, path in PUBLIC_ROUTE_EXCEPTIONS:
        assert snapshot[f"{method} {path}"]["session"] == "a"


def test_the_middleware_puts_the_effective_permissions_on_the_request() -> None:
    allowed, state = middleware_decision(
        "/api/jobs", "GET", _principal("session", "operator"), public=False
    )

    assert allowed
    assert state.effective_permissions.permissions == SYSTEM_ROLE_PERMISSIONS["operator"]
    assert state.auth_user["permissions"] == sorted(SYSTEM_ROLE_PERMISSIONS["operator"])


def test_a_personal_token_carries_its_owners_permissions_cut_to_its_scopes() -> None:
    token = {**_principal("pat", "operator"), "scopes": ["read:*"]}

    allowed, state = middleware_decision("/api/jobs", "GET", token, public=False)

    assert allowed
    assert "jobs.read" in state.effective_permissions.permissions
    assert "jobs.write" not in state.effective_permissions.permissions


def test_a_service_account_carries_no_role_permissions() -> None:
    allowed, state = middleware_decision(
        "/api/jobs", "GET", _principal("machine", "operator"), public=False
    )

    assert allowed
    assert getattr(state, "effective_permissions", None) is None
    assert "permissions" not in state.auth_user


def test_the_stored_role_permissions_decide_not_the_code_matrix() -> None:
    """The middleware reads ``role_permissions``: a row removed there is refused."""
    stored = {
        **SYSTEM_ROLE_PERMISSIONS,
        "operator": SYSTEM_ROLE_PERMISSIONS["operator"] - {"jobs.write"},
    }

    allowed, _state = middleware_decision(
        "/api/jobs/j1/cancel",
        "POST",
        _principal("session", "operator"),
        public=False,
        stored=stored,
    )

    assert allowed is False


def test_role_permissions_that_cannot_be_read_refuse_the_request() -> None:
    def _broken(_bl):
        raise RuntimeError("relation role_permissions does not exist")

    async def _call_next(_request: Request) -> Response:
        return Response(status_code=204)

    user = _principal("session", "operator")
    response = asyncio.run(
        auth_http_runtime.local_auth_middleware(
            _request("/api/jobs", "GET", bearer=False),
            _call_next,
            local_auth_enabled_fn=lambda: True,
            external_auth_enabled=False,
            is_public_auth_path_fn=lambda _path: False,
            route_registered_fn=lambda _path, _method: True,
            request_principal_resolver_fn=lambda _request, _bl: None,
            normalize_live_principal_fn=lambda raw, **_kwargs: ResolvedPrincipal(**raw),
            get_logger_only_fn=lambda: object(),
            auth_tables_available_fn=lambda _bl: True,
            get_user_by_session_token_fn=lambda _bl, _token: dict(user),
            local_auth_bootstrap_required_fn=lambda _bl=None: False,
            auth_account_tables_available_fn=lambda _bl: False,
            roles_intersect_fn=roles_intersect,
            is_registry_read_route_fn=lambda _path: False,
            is_self_service_auth_write_route_fn=lambda _path: False,
            csrf_request_valid_fn=lambda _request: True,
            auth_cookie_name="session",
            auth_unsafe_methods={"POST", "PUT", "PATCH", "DELETE"},
            auth_admin_roles={"admin"},
            auth_write_roles={"admin", "operator"},
            err_database_not_configured="Database not configured",
            err_auth_tables_not_available="Auth tables unavailable",
            err_authentication_required="Authentication required",
            err_account_disabled="Account is disabled",
            err_admin_role_required="Admin role required",
            err_operator_role_required="Operator role required",
            path_bootstrap="/bootstrap",
            path_login="/login",
            load_role_permissions_fn=_broken,
        )
    )

    assert response.status_code == 503


def test_an_unmapped_write_is_refused_even_to_an_admin() -> None:
    allowed, _state = middleware_decision(
        "/api/not-a-route", "POST", _principal("session", "admin"), public=False
    )

    assert allowed is False


def test_a_write_an_edition_contributes_as_own_account_needs_account_self() -> None:
    """The ``self_service_auth_write_paths`` registry still names own-account writes."""
    from core.application.auth.auth_http_runtime import _person_route_permission

    assert (
        _person_route_permission(
            "POST",
            "/api/private/me",
            interactive_source="local",
            contributed_own_account_write=True,
        )
        == "account.self"
    )
    # Open owner question: an external sign-in writes to its own account as an operator.
    assert (
        _person_route_permission(
            "POST",
            "/api/auth/logout",
            interactive_source="external",
            contributed_own_account_write=True,
        )
        == "account.profile.write"
    )


@pytest.mark.parametrize("path", ["/api/auth/tokens", "/api/auth/agent-access/tokens"])
def test_an_external_viewer_session_cookie_needs_the_external_own_account_permission(
    path: str,
) -> None:
    """The session an external sign-in issued resolves through the cookie lookup; its
    principal's ``external`` source, not the lookup's ``local``, picks the rule."""
    external_viewer = {"id": "v-1", "username": "v", "roles": ["viewer"]}
    external_viewer["auth_source"] = "external"

    allowed, _state = middleware_decision(
        path, "POST", external_viewer, public=False, cookie_session=True
    )

    assert allowed is False
    # A local viewer keeps account.self; an external operator holds the profile write.
    assert middleware_decision(path, "POST", _principal("session", "viewer"), public=False)[0]
    external_operator = {**external_viewer, "roles": ["operator"]}
    assert middleware_decision(path, "POST", external_operator, public=False, cookie_session=True)[
        0
    ]


def test_every_route_of_the_application_is_mapped() -> None:
    unmapped = sorted(
        f"{row['method']} {row['path']}"
        for row in _routes()
        if route_permission(row["method"], row["sample"]) is None
    )

    assert unmapped == []


def test_every_write_route_names_a_permission_unless_it_is_public() -> None:
    """ADR 0076 §6: a write without a permission fails here; only public routes have none."""
    missing = []
    for row in _routes():
        match = route_permission(row["method"], row["sample"])
        public = row["public"]
        if row["method"] != "GET" and match is not None and match.permission is None and not public:
            missing.append(f"{row['method']} {row['path']}")

    assert missing == []


def test_every_rule_is_used_by_a_route_or_documented() -> None:
    """A rule no registered route reaches is dead policy (``/admin`` has no route today)."""
    used = set()
    for row in _routes():
        match = route_permission(row["method"], row["sample"])
        if match is not None:
            used.add(id(match.rule))
    unused = [rule.pattern.pattern for rule in ROUTE_PERMISSION_RULES if id(rule) not in used]

    assert unused == [r"/admin(?:/.*)?"]


def test_every_permission_the_table_names_is_in_the_catalogue() -> None:
    named = {rule.permission for rule in ROUTE_PERMISSION_RULES if rule.permission is not None}

    assert named <= ALL_PERMISSION_KEYS
    # Every catalogue permission is checked by at least one route, except those the
    # middleware checks itself: account.profile.write gates an external sign-in
    # session's writes to its own account (auth_http_runtime), not a route.
    checked_outside_the_table = {"account.profile.write"}
    assert named | checked_outside_the_table >= ALL_PERMISSION_KEYS


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("POST", "/api/workflows/wf-1/run", "workflows.run"),
        ("POST", "/api/workflow-runs", "workflows.run"),
        ("POST", "/api/approvals/thread-1", "approvals.decide"),
        ("POST", "/api/jobs/j1/decision-overrides", "approvals.decide"),
        ("POST", "/api/jobs/j1/cancel", "jobs.write"),
        ("DELETE", "/api/jobs/j1", "jobs.manage"),
        ("POST", "/api/secrets", "secrets.value.write"),
        ("PUT", "/api/secrets/s1/value", "secrets.value.write"),
        ("DELETE", "/api/secrets/s1", "secrets.delete"),
        ("PUT", "/api/projects/p1/connections/c1/secret-bindings/api_key", "secrets.bind"),
        ("POST", "/api/projects/p1/workflows/wf-1/secret-approval", "secrets.approve"),
        ("GET", "/api/projects/p1/workflows/wf-1/contract", "workflows.read"),
        ("GET", "/api/projects/p1/workflows/wf-1/export", "workflows.manage"),
        ("GET", "/api/extensions/fixture.extension", "extensions.read"),
        ("GET", "/api/extensions/marketplace", "registry.read"),
        ("GET", "/api/extensions/install/compiled-manifest", "registry.read"),
        ("HEAD", "/api/jobs", "jobs.read"),
        ("PATCH", "/api/users/me/locale", "account.self"),
        ("POST", "/api/auth/tokens", "account.self"),
        ("POST", "/api/auth/switch-account", "account.self"),
        ("GET", "/api/auth/users", "users.manage"),
    ],
)
def test_route_permission_examples(method: str, path: str, expected: str) -> None:
    match = route_permission(method, path)

    assert match is not None
    assert match.permission == expected


def test_an_unmapped_write_is_refused_to_everybody() -> None:
    assert route_permission("POST", "/api/not-a-route") is None
    assert route_allowed("POST", "/api/not-a-route", ALL_PERMISSION_KEYS) is False


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, *_args):
        return None

    def fetchall(self):
        return self.rows


def test_the_middleware_reads_the_rows_and_falls_back_only_when_there_are_none(caplog) -> None:
    from types import SimpleNamespace

    from core.application.auth.auth_http_runtime import _stored_role_permissions

    def store(rows):
        return SimpleNamespace(conn=SimpleNamespace(cursor=lambda: _Rows(rows)))

    stored = _stored_role_permissions(store([{"key": "operator", "permission_key": "jobs.read"}]))
    assert stored == {"operator": frozenset({"jobs.read"})}
    assert _stored_role_permissions(store([])) == SYSTEM_ROLE_PERMISSIONS
    assert "holds no rows" in caplog.text


def test_an_admin_needs_no_stored_rows() -> None:
    """``admin`` holds the whole catalogue implicitly: nothing is read for it."""

    def _unreadable(_bl):
        raise AssertionError("an admin's request read role_permissions")

    allowed, state = middleware_decision(
        "/api/auth/users", "GET", _principal("session", "admin"), public=False, loader=_unreadable
    )
    assert allowed
    assert state.effective_permissions.permissions == ALL_PERMISSION_KEYS
