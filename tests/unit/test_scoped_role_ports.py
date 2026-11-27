"""The scoped roles port refuses bad grants before it opens a connection.

Refusals that need no data (an unknown scope or field, a role other than operator
or viewer, a malformed id, a place listed twice, no account) are raised
before the port touches the database. Refusals that read rows (another account's
person or project, an administrator, unknown ids) and the writes themselves run on a
real database in ``tests/db/test_scoped_roles_port_postgres.py``.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest

from core.application.auth.scoped_assignments import SCOPED_ROLE_KEYS, ScopeLimit
from core.application.auth.scoped_role_ports import (
    ScopedRoleGrantInvalidError,
    ScopedRoleProjectNotFoundError,
    ScopedRolesPort,
    ScopedRoleUserNotFoundError,
)

ACCOUNT = "22222222-2222-4222-8222-222222222222"
USER = "33333333-3333-4333-8333-333333333333"
PROJECT = "44444444-4444-4444-8444-444444444444"


def _port() -> tuple[ScopedRolesPort, list[int]]:
    opened: list[int] = []

    @contextmanager
    def connection():
        opened.append(1)
        raise AssertionError("the port opened a connection for a request it must refuse")
        yield  # pragma: no cover

    return ScopedRolesPort(connection), opened


@pytest.mark.parametrize(
    ("grant", "message"),
    [
        ({"scope": "account", "id": PROJECT, "role": "viewer"}, "cannot be granted"),
        ({"scope": "", "id": PROJECT, "role": "viewer"}, "cannot be granted"),
        ({"project_id": PROJECT, "role": "viewer"}, "cannot be granted"),
        ({"scope": "project", "id": PROJECT, "role": "viewer", "workspace_id": "x"}, "Unknown"),
        ({"scope": "project", "id": PROJECT, "role": "viewer", "project_id": PROJECT}, "in id"),
        ({"scope": "workspace", "id": PROJECT, "role": "viewer", "project_id": PROJECT}, "in id"),
    ],
)
def test_an_unknown_scope_or_field_is_refused(grant: dict[str, Any], message: str) -> None:
    port, opened = _port()

    with pytest.raises(ScopedRoleGrantInvalidError, match=message):
        port.replace_user_grants(ACCOUNT, USER, [grant], created_by="alice")
    assert opened == []


@pytest.mark.parametrize("scope", ["workspace", "project", "workflow"])
@pytest.mark.parametrize("role", ["admin", "owner", "", "VIEWERS"])
def test_a_role_other_than_operator_or_viewer_is_refused(scope: str, role: str) -> None:
    port, opened = _port()
    grant = {"scope": scope, "id": PROJECT, "role": role}
    if scope == "workflow":
        grant.update(id="wf-1", project_id=PROJECT)

    with pytest.raises(ScopedRoleGrantInvalidError):
        port.replace_user_grants(ACCOUNT, USER, [grant], created_by="alice")
    assert opened == []


def test_only_operator_and_viewer_may_be_granted() -> None:
    assert frozenset({"operator", "viewer"}) == SCOPED_ROLE_KEYS


@pytest.mark.parametrize(
    "grants",
    [
        [
            {"scope": "project", "id": PROJECT, "role": "viewer"},
            {"scope": "project", "id": PROJECT, "role": "operator"},
        ],
        [
            {"scope": "workspace", "id": PROJECT, "role": "viewer"},
            {"scope": "workspace", "id": PROJECT, "role": "viewer"},
        ],
        [
            {"scope": "workflow", "id": "wf-1", "project_id": PROJECT, "role": "viewer"},
            {"scope": "workflow", "id": "wf-1", "project_id": PROJECT, "role": "operator"},
        ],
    ],
    ids=["project", "workspace", "workflow"],
)
def test_a_place_listed_twice_is_refused(grants: list[dict[str, Any]]) -> None:
    port, opened = _port()

    with pytest.raises(ValueError, match="more than once"):
        port.replace_user_grants(ACCOUNT, USER, grants, created_by="alice")
    assert opened == []


def test_one_workflow_in_two_projects_is_two_places() -> None:
    from core.application.auth.scoped_role_ports import _normalise_grants

    other = "55555555-5555-4555-8555-555555555555"
    grants = _normalise_grants(
        [
            {"scope": "workflow", "id": "wf-1", "project_id": PROJECT, "role": "viewer"},
            {"scope": "workflow", "id": "wf-1", "project_id": other, "role": "operator"},
            {"scope": "project", "id": PROJECT, "role": "viewer"},
        ]
    )

    assert [grant.view() for grant in grants] == [
        {"scope": "workflow", "id": "wf-1", "role": "viewer", "project_id": PROJECT},
        {"scope": "workflow", "id": "wf-1", "role": "operator", "project_id": other},
        {"scope": "project", "id": PROJECT, "role": "viewer"},
    ]


def test_malformed_ids_are_unknown_ids() -> None:
    port, opened = _port()

    with pytest.raises(ScopedRoleUserNotFoundError):
        port.replace_user_grants(ACCOUNT, "nobody", [], created_by="alice")
    for grant in (
        {"scope": "project", "id": "p-1", "role": "viewer"},
        {"scope": "workspace", "id": "w-1", "role": "viewer"},
        {"scope": "workflow", "id": "wf-1", "project_id": "p-1", "role": "viewer"},
        {"scope": "workflow", "id": "", "project_id": PROJECT, "role": "viewer"},
    ):
        with pytest.raises(ScopedRoleProjectNotFoundError):
            port.replace_user_grants(ACCOUNT, USER, [grant], created_by="alice")
    with pytest.raises(LookupError):
        port.lift_limit(ACCOUNT, "nobody", created_by="alice")
    assert opened == []


@pytest.mark.parametrize("account", ["", None, "  "])
def test_a_caller_without_an_account_is_refused(account: Any) -> None:
    port, opened = _port()

    with pytest.raises(LookupError):
        port.list_grants(account)
    with pytest.raises(LookupError):
        port.candidates(account)
    with pytest.raises(LookupError):
        port.replace_user_grants(account, USER, [], created_by="alice")
    assert opened == []


def test_the_account_is_positional_only() -> None:
    port, _ = _port()

    with pytest.raises(TypeError):
        port.list_grants(account_id=ACCOUNT)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        port.replace_user_grants(  # type: ignore[call-arg]
            account_id=ACCOUNT, user_id=USER, grants=[], created_by="alice"
        )


def test_a_scope_limit_lists_its_projects() -> None:
    limit = ScopeLimit(project_roles={PROJECT: frozenset({"viewer"})})

    assert limit.project_ids == frozenset({PROJECT})
    assert ScopeLimit().project_ids == frozenset()


def test_the_runtime_container_carries_the_port_slot() -> None:
    from core.infrastructure.runtime.app_factory import RuntimeContainer

    assert "scoped_roles" in RuntimeContainer.__dataclass_fields__


def test_the_composition_root_attaches_the_port_before_modules_load() -> None:
    from pathlib import Path

    from core.entrypoints.api import app_factory

    source = Path(app_factory.__file__).read_text(encoding="utf-8")
    attach = source.index("def _attach_module_ports(")
    assert "container.scoped_roles = ScopedRolesPort(connection)" in source[attach:]
