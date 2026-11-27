"""ADR 0076 section 8 (2026-11-26): the auth middleware applies a person's project limit.

A person (or a personal token of a person) with a project scope limit reaches only
the limit's projects and holds in each the lower of the global and the scoped role.
These tests run the real middleware: on every route of the application for a person
without a limit (exactly the frozen ADR 0076 answers), and on every write route for a
limited person (a project in the path is checked per project; a write that names its
project elsewhere is let through only when its service checks the project, every
other project write fails closed).
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from starlette.requests import Request
from starlette.responses import Response

from core.application.auth import auth_http_runtime
from core.application.auth.permissions import (
    ALL_PERMISSION_KEYS,
    OPERATOR_PERMISSIONS,
    SYSTEM_ROLE_PERMISSIONS,
    VIEWER_PERMISSIONS,
)
from core.application.auth.principal import ResolvedPrincipal
from core.application.auth.project_scope_limit import (
    NO_PROJECT_ID,
    PROJECT_ACTING_PERMISSIONS,
    SERVICE_CHECKED_PROJECT_WRITES,
    is_service_checked_project_write,
    project_id_from_path,
)
from core.application.auth.request_principal_runtime import roles_intersect
from core.application.auth.route_permissions import route_permission
from core.application.auth.scoped_assignments import ScopeLimit
from core.application.management.scope import scope_from_principal
from core.infrastructure.runtime.http_app_auth_runtime import _is_registry_read_route
from tests.unit.test_route_permission_parity import (
    ALL_SCOPES,
    PUBLIC_ROUTE_EXCEPTIONS,
    _request,
    _routes,
    _snapshot,
)

PERSON_ID = "6a1c2b3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
#: The person is an operator in X, a viewer in Y and has nothing in Z.
PROJECT_X = "11111111-aaaa-4aaa-8aaa-111111111111"
PROJECT_Y = "22222222-bbbb-4bbb-8bbb-222222222222"
PROJECT_Z = "33333333-cccc-4ccc-8ccc-333333333333"
LIMIT = ScopeLimit(
    project_roles={PROJECT_X: frozenset({"operator"}), PROJECT_Y: frozenset({"viewer"})}
)
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Every write whose project is named in the body, a job, an approval or a report and
#: whose service checks the permission in that project. A route added here needs a
#: service test in ``test_scoped_roles_services.py``; a route missing here is refused
#: to a limited person.
EXPECTED_SERVICE_CHECKED_WRITES = frozenset(
    {
        ("POST", "/api/approvals/{thread_id}"),
        ("POST", "/api/jobs/{job_id}/decision-overrides"),
        ("POST", "/api/jobs/{job_id}/acknowledge"),
        ("POST", "/api/jobs/{job_id}/unacknowledge"),
        ("POST", "/api/jobs/{job_id}/cancel"),
        ("POST", "/api/jobs/{job_id}/restart"),
        ("POST", "/api/jobs/{job_id}/retry-step"),
        ("POST", "/api/workflow-runs"),
        ("POST", "/api/workflows/{workflow_id}/run"),
        ("POST", "/api/reports/generate"),
        ("POST", "/api/extensions/{extension_id}/action/{action_id}"),
        ("POST", "/api/extensions/{extension_id}/query/{query_id}"),
    }
)


def _person(kind: str = "session", role: str = "operator", **extra) -> dict:
    user = {"id": PERSON_ID, "username": "pat-person", "roles": [role]}
    if kind == "session":
        user["auth_source"] = "local"
    elif kind == "external":
        user["auth_source"] = "external"
    else:
        user.update(auth_source="pat", scopes=list(ALL_SCOPES))
    user.update(extra)
    return user


def run_middleware(
    path: str,
    method: str,
    user: dict,
    *,
    loader=None,
    public: bool = False,
    stored: dict | None = None,
) -> tuple[int, object]:
    """The real middleware once; ``(status, request state)``. Token scopes always pass."""
    external = user.get("auth_source") == "external"
    bearer = user.get("auth_source") in {"pat", "machine"}
    seen: dict = {}

    async def _call_next(request: Request) -> Response:
        seen["state"] = request.state
        return Response(status_code=204)

    original = auth_http_runtime.principal_has_required_scopes
    auth_http_runtime.principal_has_required_scopes = lambda *_args, **_kwargs: True
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
                normalize_live_principal_fn=lambda raw, **_kwargs: (
                    ResolvedPrincipal(**raw) if isinstance(raw, dict) else None
                ),
                get_logger_only_fn=lambda: object(),
                auth_tables_available_fn=lambda _bl: True,
                get_user_by_session_token_fn=lambda _bl, _token: dict(user),
                local_auth_bootstrap_required_fn=lambda _bl=None: False,
                auth_account_tables_available_fn=lambda _bl: False,
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
                load_role_permissions_fn=lambda _bl: (
                    SYSTEM_ROLE_PERMISSIONS if stored is None else stored
                ),
                contributed_route_rules_fn=lambda: (),
                load_scope_limit_fn=loader,
            )
        )
    finally:
        auth_http_runtime.principal_has_required_scopes = original
    return response.status_code, seen.get("state")


def _limited(_bl, _account_id, _user_id):
    return LIMIT


def _allowed(path: str, method: str, user: dict, **kwargs) -> bool:
    status, _state = run_middleware(path, method, user, **kwargs)
    return status == 204


# --- a person without a limit row: exactly today's answers -----------------------------


@pytest.mark.parametrize("kind", ["session", "external", "pat"])
def test_a_person_without_a_limit_gets_the_frozen_answer_on_every_route(kind) -> None:
    snapshot = _snapshot()
    calls: list[tuple[str, str]] = []

    def _no_limit(_bl, account_id, user_id):
        calls.append((account_id, user_id))
        return

    mismatches = {}
    rows = [row for row in _routes() if f"{row['method']} {row['path']}" in snapshot]
    assert len(rows) >= 0.95 * len(snapshot)
    for row in rows:
        key = f"{row['method']} {row['path']}"
        allowed = ""
        for role in ("admin", "operator", "viewer"):
            if _allowed(
                row["sample"],
                row["method"],
                _person(kind, role),
                loader=_no_limit,
                public=row["public"],
            ):
                allowed += role[0]
        expected = snapshot[key][kind]
        if (row["method"], row["path"]) in PUBLIC_ROUTE_EXCEPTIONS:
            expected = "aov"
        if allowed != expected:
            mismatches[key] = (expected, allowed)

    assert mismatches == {}
    # Non-vacuity: the limit was read for the operator and the viewer, never for admin.
    assert calls
    assert {user_id for _account, user_id in calls} == {PERSON_ID}


def test_a_person_without_a_limit_carries_no_project_fields() -> None:
    status, state = run_middleware(
        "/api/jobs", "GET", _person(), loader=lambda _bl, _account, _user: None
    )

    assert status == 204
    assert "project_permissions" not in state.auth_user
    assert state.auth_user["allowed_project_ids"] == []
    scope = scope_from_principal(state.auth_user)
    assert scope.project_permissions is None
    assert scope.allowed_project_ids == frozenset()


def test_an_admin_is_never_limited_and_has_nothing_read() -> None:
    def _must_not_read(*_args):
        raise AssertionError("an admin's limit must not be read")

    status, state = run_middleware(
        f"/api/projects/{PROJECT_Z}/workflow-preview",
        "POST",
        _person(role="admin"),
        loader=_must_not_read,
    )

    assert status == 204
    assert "project_permissions" not in state.auth_user


def test_a_service_account_has_no_limit_read() -> None:
    def _must_not_read(*_args):
        raise AssertionError("a service account's limit must not be read")

    machine = {
        "id": PERSON_ID,
        "username": "svc",
        "roles": ["operator"],
        "auth_source": "machine",
        "principal_type": "service_account",
        "scopes": list(ALL_SCOPES),
    }
    status, _state = run_middleware("/api/jobs", "GET", machine, loader=_must_not_read)

    assert status == 204


# --- fail closed -------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["session", "pat"])
def test_a_limit_that_cannot_be_read_refuses_the_request(kind) -> None:
    def _broken(_bl, _account_id, _user_id):
        raise RuntimeError("relation scoped_role_users does not exist")

    status, _state = run_middleware("/api/jobs", "GET", _person(kind), loader=_broken)

    assert status == 503


def test_the_production_middleware_reads_the_stored_limit() -> None:
    """The lifecycle wiring passes the stored reader; without it limits never apply."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    source = (root / "core/infrastructure/runtime/http_app_lifecycle.py").read_text(
        encoding="utf-8"
    )

    assert "load_scope_limit_fn=_auth_http_runtime.read_stored_scope_limit" in source
    assert auth_http_runtime.read_stored_scope_limit.__module__ == (
        "core.application.auth.project_scope_limit"
    )


def test_the_stored_reader_refuses_without_a_connection() -> None:
    with pytest.raises(RuntimeError):
        auth_http_runtime.read_stored_scope_limit(object(), "account", PERSON_ID)


# --- the role matrix: operator in X, viewer in Y, nothing in Z ---------------------------


def test_the_limited_person_carries_projects_and_per_project_permissions() -> None:
    status, state = run_middleware("/api/jobs", "GET", _person(), loader=_limited)

    assert status == 204
    assert state.auth_user["allowed_project_ids"] == sorted([PROJECT_X, PROJECT_Y])
    assert state.auth_principal.allowed_project_ids == sorted([PROJECT_X, PROJECT_Y])
    assert state.auth_user["project_permissions"] == {
        PROJECT_X: sorted(OPERATOR_PERMISSIONS),
        PROJECT_Y: sorted(VIEWER_PERMISSIONS),
    }
    # The global role stays as it is: it is the ceiling, not the answer per project.
    assert state.auth_user["permissions"] == sorted(OPERATOR_PERMISSIONS)
    scope = scope_from_principal(state.auth_user)
    assert scope.allowed_project_ids == frozenset({PROJECT_X, PROJECT_Y})
    assert scope.project_permissions == {
        PROJECT_X: OPERATOR_PERMISSIONS,
        PROJECT_Y: VIEWER_PERMISSIONS,
    }


@pytest.mark.parametrize(
    ("project", "expected"),
    [(PROJECT_X, 204), (PROJECT_Y, 403), (PROJECT_Z, 403)],
)
def test_launching_is_allowed_in_x_refused_in_y_and_out_of_reach_in_z(project, expected) -> None:
    status, _state = run_middleware(
        f"/api/projects/{project}/workflow-preview", "POST", _person(), loader=_limited
    )

    assert status == expected


@pytest.mark.parametrize(
    ("project", "expected"),
    [(PROJECT_X, 204), (PROJECT_Y, 204), (PROJECT_Z, 403)],
)
def test_reading_is_allowed_in_x_and_y_and_out_of_reach_in_z(project, expected) -> None:
    status, _state = run_middleware(f"/api/projects/{project}", "GET", _person(), loader=_limited)

    assert status == expected


def test_a_global_viewer_scoped_as_operator_stays_a_viewer() -> None:
    """Narrowing: the lower of the two roles applies, never the scoped one alone."""
    status, state = run_middleware(
        f"/api/projects/{PROJECT_X}/workflow-preview",
        "POST",
        _person(role="viewer"),
        loader=_limited,
    )

    assert status == 403
    _status, state = run_middleware("/api/jobs", "GET", _person(role="viewer"), loader=_limited)
    assert state.auth_user["project_permissions"][PROJECT_X] == sorted(VIEWER_PERMISSIONS)


def test_a_limit_without_projects_reaches_no_project() -> None:
    empty = ScopeLimit(project_roles={})

    status, state = run_middleware(
        "/api/jobs", "GET", _person(), loader=lambda _bl, _account, _user: empty
    )

    assert status == 204
    assert state.auth_user["allowed_project_ids"] == [NO_PROJECT_ID]
    assert state.auth_user["project_permissions"] == {}
    scope = scope_from_principal(state.auth_user)
    # Not empty: an empty allowlist would mean "every project".
    assert scope.allowed_project_ids == frozenset({NO_PROJECT_ID})
    assert scope.project_permissions == {}
    status, _state = run_middleware(
        f"/api/projects/{PROJECT_X}", "GET", _person(), loader=lambda *_args: empty
    )
    assert status == 403


# --- a personal token never reaches past its owner ---------------------------------------


def test_a_token_reaches_only_projects_it_and_its_owner_both_reach() -> None:
    token = _person("pat", allowed_project_ids=[PROJECT_X, PROJECT_Z])

    _status, state = run_middleware("/api/jobs", "GET", token, loader=_limited)

    assert state.auth_user["allowed_project_ids"] == [PROJECT_X]
    assert set(state.auth_user["project_permissions"]) == {PROJECT_X}
    assert run_middleware(f"/api/projects/{PROJECT_Z}", "GET", token, loader=_limited)[0] == 403
    assert run_middleware(f"/api/projects/{PROJECT_Y}", "GET", token, loader=_limited)[0] == 403


def test_a_token_without_a_project_list_gets_its_owners_projects() -> None:
    token = _person("pat")

    _status, state = run_middleware("/api/jobs", "GET", token, loader=_limited)

    assert state.auth_user["allowed_project_ids"] == sorted([PROJECT_X, PROJECT_Y])


def test_a_token_naming_only_other_projects_reaches_none() -> None:
    token = _person("pat", allowed_project_ids=[PROJECT_Z])

    _status, state = run_middleware("/api/jobs", "GET", token, loader=_limited)

    assert state.auth_user["allowed_project_ids"] == [NO_PROJECT_ID]
    assert state.auth_user["project_permissions"] == {}


def test_a_token_holds_in_a_project_only_what_its_scopes_grant() -> None:
    token = _person("pat", scopes=["read:*"])

    _status, state = run_middleware("/api/jobs", "GET", token, loader=_limited)

    held = set(state.auth_user["project_permissions"][PROJECT_X])
    assert "jobs.read" in held
    assert "workflows.run" not in held
    assert held <= OPERATOR_PERMISSIONS


# --- every write route of the application -------------------------------------------------


def _sample(path: str, project_id: str) -> str:
    return path.replace("{project_id}", project_id).replace("{", "").replace("}", "")


def _write_rows() -> list[dict]:
    rows = [row for row in _routes() if row["method"] in _WRITE_METHODS and not row["public"]]
    assert len(rows) > 150  # non-vacuity: the real application's writes
    return rows


@pytest.mark.parametrize(
    "stored",
    [
        SYSTEM_ROLE_PERMISSIONS,
        # A role table where the operator holds everything: every route then passes the
        # global check, so the limit's own decision is what is compared.
        {**SYSTEM_ROLE_PERMISSIONS, "operator": ALL_PERMISSION_KEYS},
    ],
    ids=["system-roles", "operator-holds-everything"],
)
def test_every_project_write_route_is_checked_for_its_project(stored) -> None:
    viewer_keys = frozenset(stored["viewer"])
    wrong: dict[str, tuple] = {}
    project_routes = 0
    for row in _write_rows():
        if (
            "{project_id}" not in row["path"]
            or project_id_from_path(_sample(row["path"], PROJECT_X)) is None
        ):
            continue
        project_routes += 1
        method = row["method"]
        match = route_permission(method, _sample(row["path"], PROJECT_X))
        permission = match.permission if match is not None else None
        unlimited = _allowed(_sample(row["path"], PROJECT_X), method, _person(), stored=stored)
        in_x = _allowed(
            _sample(row["path"], PROJECT_X), method, _person(), stored=stored, loader=_limited
        )
        in_y = _allowed(
            _sample(row["path"], PROJECT_Y), method, _person(), stored=stored, loader=_limited
        )
        in_z = _allowed(
            _sample(row["path"], PROJECT_Z), method, _person(), stored=stored, loader=_limited
        )
        expected_y = unlimited and (permission is None or permission in viewer_keys)
        if (in_x, in_y, in_z) != (unlimited, expected_y, False):
            wrong[f"{method} {row['path']}"] = (permission, unlimited, in_x, in_y, in_z)

    assert project_routes > 40
    assert wrong == {}


@pytest.mark.parametrize(
    "stored",
    [SYSTEM_ROLE_PERMISSIONS, {**SYSTEM_ROLE_PERMISSIONS, "operator": ALL_PERMISSION_KEYS}],
    ids=["system-roles", "operator-holds-everything"],
)
def test_a_write_naming_its_project_elsewhere_passes_only_when_its_service_checks_it(
    stored,
) -> None:
    """A missed route fails closed: only the pinned service-checked writes pass."""
    wrong: dict[str, tuple] = {}
    let_through: set[tuple[str, str]] = set()
    operator_work: set[tuple[str, str]] = set()
    for row in _write_rows():
        if project_id_from_path(_sample(row["path"], PROJECT_X)) is not None:
            continue
        method, sample = row["method"], row["sample"]
        match = route_permission(method, sample)
        permission = match.permission if match is not None else None
        unlimited = _allowed(sample, method, _person(), stored=stored)
        limited = _allowed(sample, method, _person(), stored=stored, loader=_limited)
        acts_on_projects = permission in PROJECT_ACTING_PERMISSIONS
        if acts_on_projects and limited:
            let_through.add((method, row["path"]))
        if acts_on_projects and unlimited:
            operator_work.add((method, row["path"]))
        expected = unlimited and (
            not acts_on_projects or (method, row["path"]) in EXPECTED_SERVICE_CHECKED_WRITES
        )
        if limited != expected:
            wrong[f"{method} {row['path']}"] = (permission, unlimited, limited)

    assert wrong == {}
    assert let_through == EXPECTED_SERVICE_CHECKED_WRITES
    if stored is SYSTEM_ROLE_PERMISSIONS:
        # Every write an operator does on a project, outside a project path, is checked
        # by its service: a new one must be added to the list and checked, or it is
        # refused to a limited operator who should be able to do it.
        assert operator_work == EXPECTED_SERVICE_CHECKED_WRITES


def test_the_service_checked_list_names_real_routes_and_only_them() -> None:
    routes = {(row["method"], row["path"]) for row in _routes()}
    assert routes >= EXPECTED_SERVICE_CHECKED_WRITES
    for method, path in EXPECTED_SERVICE_CHECKED_WRITES:
        sample = path.replace("{", "").replace("}", "")
        assert is_service_checked_project_write(method, sample), (method, path)
        match = route_permission(method, sample)
        assert match is not None and match.permission in PROJECT_ACTING_PERMISSIONS
    checked = {
        (method, row["path"])
        for row in _routes()
        for method, pattern, _permission in SERVICE_CHECKED_PROJECT_WRITES
        if row["method"] == method and pattern.match(row["sample"])
    }
    assert checked == EXPECTED_SERVICE_CHECKED_WRITES


def test_a_project_path_that_is_not_a_project_id_is_not_read_as_one() -> None:
    assert project_id_from_path("/api/v2/projects/sections") is None
    assert project_id_from_path(f"/api/v2/projects/{PROJECT_X}/jobs") == PROJECT_X
    assert project_id_from_path(f"/api/projects/{PROJECT_X.upper()}") == PROJECT_X
    assert project_id_from_path("/api/projectsx/" + str(uuid.uuid4())) is None
