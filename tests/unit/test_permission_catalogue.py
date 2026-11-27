"""ADR 0076: the permission catalogue, the system-role matrix and effective permissions."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import pytest

from core.application.auth import permissions as catalogue
from core.application.auth.effective_permissions import (
    NOT_ROLE_BASED,
    PERSON,
    PERSONAL_TOKEN,
    PermissionResource,
    effective_permissions,
    load_role_permissions,
)
from core.application.auth.permissions import (
    ALL_PERMISSION_KEYS,
    OPERATOR_PERMISSIONS,
    PERMISSIONS,
    SYSTEM_ROLE_DESCRIPTIONS,
    SYSTEM_ROLE_PERMISSIONS,
    VIEWER_PERMISSIONS,
    keep_highest_system_role,
    token_grants_permission,
)
from core.application.auth.principal import ResolvedPrincipal
from core.application.auth.role_assignments import (
    RoleAssignment,
    ScopedRoleAssignmentError,
    replace_user_role_assignments,
    require_global_assignments,
)
from core.application.auth.scope_contracts import EXACT_ONLY_SCOPES
from core.application.management.errors import ServiceValidationError

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "db" / "migrations" / "20261120090000_role_permissions.sql"


# --- the catalogue ----------------------------------------------------------------


def test_keys_are_unique_and_in_the_taxonomy_format() -> None:
    keys = [permission.key for permission in catalogue._CATALOGUE]

    assert len(keys) == len(set(keys)) == len(PERMISSIONS)
    for key in keys:
        assert catalogue.PERMISSION_KEY_RE.fullmatch(key), key
        assert PERMISSIONS[key].description.strip(), key


def test_exact_match_permissions_are_granted_only_by_literal_scopes() -> None:
    for permission in PERMISSIONS.values():
        if not permission.exact_match:
            continue
        assert all("*" not in scope for scope in permission.token_scopes), permission.key
        assert not token_grants_permission(permission, {"*:*"})
        area = permission.key.split(".", 1)[0]
        assert not token_grants_permission(permission, {f"{area}:*"})


def test_the_secret_permissions_follow_adr_0057() -> None:
    for key in ("secrets.value.write", "secrets.delete", "secrets.bind", "secrets.approve"):
        permission = PERMISSIONS[key]
        assert permission.exact_match
        assert set(permission.token_scopes) <= EXACT_ONLY_SCOPES
        assert not token_grants_permission(permission, {"admin:all", "secrets:*", "*:write"})
    assert token_grants_permission(PERMISSIONS["secrets.value.write"], {"secrets:write"})
    assert token_grants_permission(PERMISSIONS["secrets.bind"], {"secrets:bind"})
    # Approving a configuration is never a token's.
    assert not token_grants_permission(
        PERMISSIONS["secrets.approve"], {"secrets:write", "secrets:bind", "secrets.approve"}
    )


def test_non_exact_permissions_follow_the_scope_wildcards() -> None:
    jobs_read = PERMISSIONS["jobs.read"]

    for scopes in ({"jobs.read"}, {"jobs:*"}, {"*:read"}, {"read:*"}, {"admin:all"}):
        assert token_grants_permission(jobs_read, scopes), scopes
    assert not token_grants_permission(jobs_read, {"projects.read"})
    assert not token_grants_permission(jobs_read, set())
    assert token_grants_permission(PERMISSIONS["workflows.run"], {"workflows.write"})
    assert token_grants_permission(PERMISSIONS["notify.dispatch"], {"notify:dispatch"})


def test_configuration_permissions_need_admin_all_from_a_token() -> None:
    for key in ("workflows.manage", "users.manage", "settings.manage", "registry.manage"):
        assert token_grants_permission(PERMISSIONS[key], {"admin:all"})
        assert not token_grants_permission(PERMISSIONS[key], {"command:*", "*:manage"})


# --- the system-role matrix -------------------------------------------------------


def test_the_matrix_is_a_ladder_inside_the_catalogue() -> None:
    assert VIEWER_PERMISSIONS < OPERATOR_PERMISSIONS < ALL_PERMISSION_KEYS
    # admin holds everything implicitly and is not stored as a list.
    assert set(SYSTEM_ROLE_PERMISSIONS) == {"operator", "viewer"}


def test_the_viewer_only_reads_and_manages_its_own_sign_in() -> None:
    for key in VIEWER_PERMISSIONS - {"account.self"}:
        assert key.endswith(".read"), key


def test_the_operator_does_no_configuration() -> None:
    assert not {key for key in OPERATOR_PERMISSIONS if key.endswith(".manage")}
    assert not OPERATOR_PERMISSIONS & {
        "projects.write",
        "secrets.bind",
        "secrets.delete",
        "secrets.approve",
    }


def _migration_rows() -> dict[str, set[str]]:
    sql = MIGRATION.read_text(encoding="utf-8")
    values = sql[sql.index("INSERT INTO public.role_permissions") : sql.index(") AS granted")]
    rows: dict[str, set[str]] = {}
    for role, key in re.findall(r"\('(viewer|operator|admin)', '([a-z_.]+)'\)", values):
        rows.setdefault(role, set()).add(key)
    return rows


def test_the_migration_writes_exactly_the_matrix() -> None:
    assert _migration_rows() == {role: set(keys) for role, keys in SYSTEM_ROLE_PERMISSIONS.items()}


def test_the_migration_and_the_seed_describe_the_system_roles_as_the_code_does() -> None:
    sql = " ".join(MIGRATION.read_text(encoding="utf-8").split())
    seed = json.loads((ROOT / "db" / "seeds" / "lookup_seed.json").read_text(encoding="utf-8"))

    for role, description in SYSTEM_ROLE_DESCRIPTIONS.items():
        assert f"'{description}'" in sql, role
    assert {row["key"]: row["description"] for row in seed["roles"]} == SYSTEM_ROLE_DESCRIPTIONS
    assert all(row["is_system"] is True for row in seed["roles"])


def test_keep_highest_system_role() -> None:
    assert keep_highest_system_role(["viewer", "Admin", "operator"]) == ["admin"]
    assert keep_highest_system_role(["viewer", "auditor", "operator"]) == ["auditor", "operator"]
    assert keep_highest_system_role([]) == []


# --- effective permissions --------------------------------------------------------


def _person(*roles: str, source: str = "local") -> dict[str, Any]:
    return {"id": "u1", "roles": list(roles), "auth_source": source, "principal_type": "user"}


def _token(*roles: str, scopes: list[str], source: str = "pat") -> dict[str, Any]:
    return {**_person(*roles, source=source), "scopes": scopes}


def _effective(principal: Any, resource: PermissionResource | None = None, **kwargs: Any):
    return effective_permissions(
        principal, resource, role_permissions=SYSTEM_ROLE_PERMISSIONS, **kwargs
    )


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("admin", ALL_PERMISSION_KEYS),
        ("operator", OPERATOR_PERMISSIONS),
        ("viewer", VIEWER_PERMISSIONS),
        # A person without a system role was always checked as a viewer.
        ("nobody", VIEWER_PERMISSIONS),
    ],
)
@pytest.mark.parametrize("source", ["local", "external"])
def test_a_person_holds_the_permissions_of_their_role(role, expected, source) -> None:
    granted = _effective(_person(role, source=source))

    assert granted.kind == PERSON
    assert granted.permissions == expected


def test_the_highest_system_role_decides() -> None:
    assert _effective(_person("viewer", "operator", "admin")).permissions == ALL_PERMISSION_KEYS
    assert _effective(_person("viewer", "operator")).permissions == OPERATOR_PERMISSIONS


def test_a_resolved_principal_and_a_scope_context_shape_are_accepted() -> None:
    principal = ResolvedPrincipal(id="u1", roles=["operator"], auth_source="local")

    assert _effective(principal).permissions == OPERATOR_PERMISSIONS
    scope_like = {"principal_type": "viewer", "auth_source": "external"}
    assert _effective(scope_like).permissions == VIEWER_PERMISSIONS


def test_a_token_narrower_than_its_owner_keeps_only_what_its_scopes_grant() -> None:
    granted = _effective(_token("operator", scopes=["jobs.read"]))

    assert granted.kind == PERSONAL_TOKEN
    assert granted.permissions == {"jobs.read"}


def test_a_token_wider_than_its_owner_is_capped_at_the_owner() -> None:
    viewer = _effective(_token("viewer", scopes=["admin:all", "secrets:write", "secrets:bind"]))
    operator = _effective(_token("operator", scopes=["admin:all"]))

    assert viewer.permissions == VIEWER_PERMISSIONS
    # admin:all reaches no exact-match permission it does not list.
    assert operator.permissions == OPERATOR_PERMISSIONS - {"secrets.value.write"}


def test_an_admin_token_gets_exact_permissions_only_from_literal_scopes() -> None:
    granted = _effective(_token("admin", scopes=["admin:all"])).permissions

    assert "users.manage" in granted
    assert {"secrets.value.write", "secrets.delete", "secrets.bind", "secrets.approve"}.isdisjoint(
        granted
    )
    with_secrets = _effective(_token("admin", scopes=["secrets:write"])).permissions
    assert with_secrets == {"secrets.value.write", "secrets.delete"}


@pytest.mark.parametrize("source", ["pat", "oauth", "token"])
def test_every_personal_token_source_is_capped(source) -> None:
    granted = _effective(_token("operator", scopes=["read:*"], source=source))

    assert granted.kind == PERSONAL_TOKEN
    assert granted.permissions == {
        key for key in OPERATOR_PERMISSIONS if token_grants_permission(PERMISSIONS[key], {"read:*"})
    }
    assert "jobs.write" not in granted.permissions


@pytest.mark.parametrize(
    "principal",
    [
        {"roles": ["operator"], "auth_source": "machine", "scopes": ["admin:all"]},
        {"roles": ["operator"], "principal_type": "service_account", "scopes": ["command:*"]},
        {"roles": ["admin"], "auth_source": "mcp_dev", "principal_type": "system_actor"},
        {"principal_type": "system_actor", "auth_source": "oauth_callback"},
        {"roles": [], "auth_source": "project_connection"},
        None,
    ],
)
def test_service_accounts_and_runtime_are_not_role_based(principal) -> None:
    granted = _effective(principal)

    assert granted.kind == NOT_ROLE_BASED
    assert granted.role_based is False
    assert granted.permissions == frozenset()


def test_an_unknown_stored_key_is_ignored_and_logged(caplog) -> None:
    stored = {"operator": {"jobs.read", "jobs.teleport"}}

    with caplog.at_level(logging.WARNING):
        granted = effective_permissions(_person("operator"), role_permissions=stored)

    assert granted.permissions == {"jobs.read"}
    assert "jobs.teleport" in caplog.text


def test_permissions_come_from_the_stored_rows_not_from_code() -> None:
    stored = {"operator": {"jobs.read"}, "viewer": set()}

    assert effective_permissions(_person("operator"), role_permissions=stored).permissions == {
        "jobs.read"
    }
    assert effective_permissions(_person("viewer"), role_permissions=stored).permissions == set()
    # admin needs no rows.
    assert effective_permissions(_person("admin"), role_permissions={}).permissions == (
        ALL_PERMISSION_KEYS
    )


def test_a_matching_scoped_assignment_above_the_global_role_decides_for_that_resource() -> None:
    assignments = [
        RoleAssignment("viewer"),
        RoleAssignment("operator", project_id="p1"),
        RoleAssignment("admin", project_id="p1", workflow_id="wf-1"),
        RoleAssignment("admin", workspace_id="w9"),
    ]
    person = _person("viewer")

    def granted(resource: PermissionResource | None) -> frozenset[str]:
        return _effective(person, resource, assignments=assignments).permissions

    assert granted(None) == VIEWER_PERMISSIONS
    assert granted(PermissionResource(project_id="p2")) == VIEWER_PERMISSIONS
    assert granted(PermissionResource(project_id="p1")) == OPERATOR_PERMISSIONS
    assert granted(PermissionResource(project_id="p1", workflow_id="wf-2")) == (
        OPERATOR_PERMISSIONS
    )
    assert granted(PermissionResource(project_id="p1", workflow_id="wf-1")) == ALL_PERMISSION_KEYS
    assert granted(PermissionResource(workspace_id="w9", project_id="p2")) == ALL_PERMISSION_KEYS


def test_a_lower_scoped_role_never_lowers_the_global_one() -> None:
    assignments = [RoleAssignment("operator"), RoleAssignment("viewer", project_id="p1")]

    assert (
        _effective(
            _person("operator"), PermissionResource(project_id="p1"), assignments=assignments
        ).permissions
        == OPERATOR_PERMISSIONS
    )


def test_a_role_outside_the_ladder_adds_its_stored_permissions() -> None:
    stored = {**SYSTEM_ROLE_PERMISSIONS, "auditor": {"registry.read"}}

    granted = effective_permissions(_person("viewer", "auditor"), role_permissions=stored)

    assert granted.permissions == VIEWER_PERMISSIONS | {"registry.read"}


# --- loading and assigning --------------------------------------------------------


class _Cursor:
    def __init__(self, rows: list[Any] | None = None, *, one: Any = None) -> None:
        self.rows = rows or []
        self.one = one
        self.statements: list[tuple[str, Any]] = []

    def fetchone(self) -> Any:
        return self.one

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_exc: Any) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self.statements.append((" ".join(sql.split()), params))

    def fetchall(self) -> list[Any]:
        return self.rows


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _Cursor:
        return self._cursor


def test_role_permissions_are_read_in_one_query() -> None:
    cursor = _Cursor(
        [
            ("operator", "jobs.read"),
            {"key": "Viewer", "permission_key": "jobs.read"},
            ("operator", "jobs.write"),
        ]
    )

    loaded = load_role_permissions(_Connection(cursor))

    assert loaded == {"operator": {"jobs.read", "jobs.write"}, "viewer": {"jobs.read"}}
    assert len(cursor.statements) == 1
    assert "FROM role_permissions" in cursor.statements[0][0]


@pytest.mark.parametrize(
    "assignment",
    [
        RoleAssignment("operator", workspace_id="w1"),
        RoleAssignment("operator", project_id="p1"),
        RoleAssignment("operator", project_id="p1", workflow_id="wf-1"),
    ],
)
def test_a_scoped_assignment_is_refused_before_anything_is_written(assignment) -> None:
    cursor = _Cursor()

    with pytest.raises(ScopedRoleAssignmentError) as refused:
        replace_user_role_assignments(
            cursor,
            user_id="u1",
            account_id="a1",
            assignments=[RoleAssignment("viewer"), assignment],
        )

    assert isinstance(refused.value, ServiceValidationError)
    assert "whole account" in str(refused.value)
    assert cursor.statements == []
    with pytest.raises(ScopedRoleAssignmentError):
        require_global_assignments([assignment])


def test_assigning_roles_keeps_the_highest_system_role_on_the_membership() -> None:
    """The role is held on the account membership (ADR 0085), never in user_roles."""
    cursor = _Cursor(one=("membership-1",))

    stored = replace_user_role_assignments(
        cursor,
        user_id="u1",
        account_id="a1",
        assignments=[RoleAssignment("viewer"), RoleAssignment("Operator"), RoleAssignment(" ")],
    )

    assert stored == ["operator"]
    [update] = cursor.statements
    assert update[0].startswith("UPDATE user_account_memberships SET role_id")
    assert update[1] == ("operator", "u1", "a1")
    assert "user_roles" not in update[0]


def test_assigning_no_roles_leaves_the_membership_without_a_role() -> None:
    cursor = _Cursor(one=("membership-1",))

    assert (
        replace_user_role_assignments(cursor, user_id="u1", account_id="a1", assignments=[]) == []
    )
    [update] = cursor.statements
    assert update[1] == (None, "u1", "a1")


def test_assigning_a_role_without_a_membership_is_refused() -> None:
    with pytest.raises(LookupError):
        replace_user_role_assignments(
            _Cursor(), user_id="u1", account_id="a1", assignments=[RoleAssignment("viewer")]
        )
