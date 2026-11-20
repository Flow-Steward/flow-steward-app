"""ADR 0076 §6: the route table and the system-role matrix answer as the old rules do.

For every HTTP route the application registers and each of its methods, a person with
the admin, operator or viewer role in a browser session is allowed or refused by the
middleware today (operator allowlist, admin-only and registry-read patterns, "a
person's write needs admin"). The same decision derived from
``route_permissions`` and the matrix in ``permissions`` must be equal, so the
middleware can switch to the table without changing what any role may do.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from functools import cache

import pytest
from starlette.requests import Request
from starlette.responses import Response

from core.application.auth import auth_http_runtime
from core.application.auth.effective_permissions import effective_permissions
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
from core.application.auth.serve_auth_runtime import is_self_service_auth_write_route
from core.infrastructure.runtime.http_app_auth_runtime import (
    _is_admin_only_route,
    _is_registry_read_route,
)

ROLES = ("admin", "operator", "viewer")

#: Public routes that authenticate by their protocol or a signed token. Called with a
#: person's session cookie, the middleware still applies its role rules to them ("a
#: person's write needs admin", the /api/mcp admin-only pattern); the table names
#: them public. Their own handlers decide (a signed webhook, the sign-in form, the
#: connector's client metadata); a session adds nothing, so the role gate there is an
#: artefact of the order of checks, not a rule to carry over.
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


def _request(path: str, method: str) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(b"cookie", b"session=session-token")],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
            "root_path": "",
        }
    )


def _old_decision(path: str, method: str, role: str, *, public: bool) -> bool:
    """Today's middleware, for a person signed in with a browser session."""
    user = {"id": f"{role}-1", "username": role, "roles": [role], "auth_source": "local"}

    async def _call_next(_request: Request) -> Response:
        return Response(status_code=204)

    response = asyncio.run(
        auth_http_runtime.local_auth_middleware(
            _request(path, method),
            _call_next,
            local_auth_enabled_fn=lambda: True,
            external_auth_enabled=False,
            is_public_auth_path_fn=lambda _path: public,
            route_registered_fn=lambda _path, _method: True,
            request_principal_resolver_fn=lambda _request, _bl: None,
            normalize_live_principal_fn=lambda raw, **_kwargs: (
                ResolvedPrincipal(**raw) if isinstance(raw, dict) else None
            ),
            get_logger_only_fn=lambda: object(),
            auth_tables_available_fn=lambda _bl: True,
            get_user_by_session_token_fn=lambda _bl, _token: dict(user),
            local_auth_bootstrap_required_fn=lambda _bl=None: False,
            auth_account_tables_available_fn=lambda _bl: False,
            roles_intersect_fn=roles_intersect,
            is_admin_only_route_fn=lambda candidate: bool(_is_admin_only_route(candidate)),
            is_registry_read_route_fn=lambda candidate: bool(_is_registry_read_route(candidate)),
            is_self_service_auth_write_route_fn=is_self_service_auth_write_route,
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
        )
    )
    return response.status_code == 204


def _new_decision(path: str, method: str, role: str) -> bool:
    person = {"id": f"{role}-1", "roles": [role], "auth_source": "local"}
    granted = effective_permissions(person, role_permissions=SYSTEM_ROLE_PERMISSIONS)
    return route_allowed(method, path, granted.permissions)


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


def test_the_table_gives_every_system_role_todays_answer_on_every_route() -> None:
    mismatches: dict[tuple[str, str], dict[str, tuple[bool, bool]]] = {}
    for row in _routes():
        method, sample = row["method"], row["sample"]
        for role in ROLES:
            old = _old_decision(sample, method, role, public=row["public"])
            new = _new_decision(sample, method, role)
            if old != new:
                mismatches.setdefault((method, row["path"]), {})[role] = (old, new)

    unexplained = {
        key: value for key, value in mismatches.items() if key not in PUBLIC_ROUTE_EXCEPTIONS
    }
    assert unexplained == {}
    # The exceptions are exactly the documented ones, and each is a public route that
    # the old order of checks gave to the admin only.
    assert set(mismatches) == PUBLIC_ROUTE_EXCEPTIONS
    for decisions in mismatches.values():
        assert decisions == {"operator": (False, True), "viewer": (False, True)}


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
    # Every catalogue permission is checked by at least one route.
    assert named >= ALL_PERMISSION_KEYS


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
        ("PATCH", "/api/users/me/locale", "account.profile.write"),
        ("POST", "/api/auth/tokens", "account.self"),
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
