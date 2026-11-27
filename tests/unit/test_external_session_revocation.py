"""ADR 0083: the person rules of external sign-in are kept after sign-in, too.

A change of a person's global roles or account memberships, or disabling the person or
their account, revokes their sessions from external sign-in and the personal tokens of
a person who signs in externally. An administrator's session from external sign-in
manages only the account that issued it.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest

from core.application.auth.external_session_revocation import (
    accounts_changed,
    membership_account_ids,
    revoke_external_sign_in_grants,
    revoke_external_sign_in_grants_of_account,
    roles_changed,
)

USER = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
ACCOUNT = "33333333-3333-3333-3333-333333333333"


class _Cursor:
    """Records SQL; answers the storage probe, the account's people and role reads."""

    def __init__(
        self,
        *,
        storage: bool = True,
        account_users: list[str] | None = None,
        existing_roles: list[str] | None = None,
    ) -> None:
        self.storage = storage
        self.account_users = account_users or []
        self.existing_roles = existing_roles or []
        self.calls: list[tuple[str, Any]] = []
        self._last = ""

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def execute(self, sql: str, params: Any = ()) -> None:
        self._last = " ".join(str(sql).split())
        self.calls.append((self._last, params))

    def fetchone(self) -> Any:
        if "to_regclass('public.external_identities')" in self._last:
            return (self.storage,)
        if self._last.startswith("UPDATE user_account_memberships SET role_id"):
            # The role is set on the person's membership (ADR 0085).
            return ("membership",)
        return None

    def fetchall(self) -> list[Any]:
        if "FROM user_account_memberships m" in self._last:
            return [{"user_id": user_id} for user_id in self.account_users]
        if "JOIN public.accounts AS a ON a.id = m.account_id" in self._last:
            # The person's role in the account (the membership role reader).
            return [(role,) for role in self.existing_roles]
        return []

    def revocations(self) -> list[tuple[str, Any]]:
        return [
            (sql.split(" SET ")[0], params)
            for sql, params in self.calls
            if sql.startswith(("UPDATE personal_access_tokens AS t", "UPDATE auth_sessions"))
        ]


# --- the revocation itself ------------------------------------------------------------


def test_the_external_sessions_and_tokens_of_the_people_are_revoked() -> None:
    cur = _Cursor()

    assert revoke_external_sign_in_grants(cur, [USER, "", USER]) is True

    assert cur.revocations() == [
        ("UPDATE personal_access_tokens AS t", ([USER], "external")),
        ("UPDATE auth_sessions", ([USER], "external")),
    ]
    token_sql = next(sql for sql, _ in cur.calls if sql.startswith("UPDATE personal_access"))
    # Only people who sign in externally lose their tokens.
    assert "FROM external_identities ei WHERE ei.user_id = t.user_id" in token_sql
    session_sql = next(sql for sql, _ in cur.calls if sql.startswith("UPDATE auth_sessions"))
    # Local sessions are untouched.
    assert "AND auth_method = %s" in session_sql


def test_storage_without_external_sign_in_has_nothing_to_revoke() -> None:
    cur = _Cursor(storage=False)

    assert revoke_external_sign_in_grants(cur, [USER]) is False
    assert cur.revocations() == []


def test_disabling_an_account_revokes_its_peoples_external_sign_in() -> None:
    cur = _Cursor(account_users=[USER, OTHER])

    assert revoke_external_sign_in_grants_of_account(cur, ACCOUNT) is True

    people = next(params for sql, params in cur.calls if "FROM user_account_memberships m" in sql)
    assert people == (ACCOUNT, ACCOUNT)
    assert cur.revocations()[0][1] == ([USER, OTHER], "external")


@pytest.mark.parametrize(
    ("before", "after", "changed"),
    [
        (["viewer"], ["viewer"], False),
        (["Viewer"], ["viewer"], False),
        (["viewer"], ["operator"], True),
        (["viewer"], ["admin"], True),
        (None, ["viewer"], True),
    ],
)
def test_a_role_change_is_detected(before, after, changed) -> None:
    assert roles_changed(before, after) is changed


def test_unknown_memberships_count_as_a_change() -> None:
    assert membership_account_ids({"account_id": ACCOUNT}) is None
    assert accounts_changed(None, [ACCOUNT]) is True
    known = membership_account_ids(
        {"account_id": ACCOUNT, "account_memberships": [{"account_id": ACCOUNT}]}
    )
    assert accounts_changed(known, [ACCOUNT]) is False
    assert accounts_changed(known, [ACCOUNT, OTHER]) is True


# --- where Core changes what external sign-in checked ---------------------------------


class _Conn:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor
        self.conn = None
        self.transaction = None
        self.autocommit = True

    def cursor(self, *_args: Any, **_kwargs: Any) -> _Cursor:
        return self._cursor

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


def _update(cursor: _Cursor, *, existing: dict[str, Any], **changes: Any) -> None:
    from types import SimpleNamespace

    from core.application.auth.user_admin.repository import update_auth_user

    arguments: dict[str, Any] = {
        "display_name": None,
        "is_active": None,
        "password": None,
        "must_change_password": None,
        "roles": None,
        "requested_account_ids": None,
        "primary_account_id": None,
        "revoke_sessions": False,
    }
    arguments.update(changes)
    update_auth_user(
        SimpleNamespace(conn=_Conn(cursor), _table_exists=lambda _name: False),
        user_id=USER,
        existing_user=existing,
        **arguments,
    )


_EXISTING = {
    "username": "pat@acme.test",
    "roles": ["viewer"],
    "account_id": ACCOUNT,
    "account_memberships": [{"account_id": ACCOUNT}],
}


@pytest.mark.parametrize(
    "changes",
    [
        {"requested_account_ids": [ACCOUNT, OTHER], "primary_account_id": ACCOUNT},
        {"requested_account_ids": [OTHER], "primary_account_id": OTHER},
        {"is_active": False},
    ],
    ids=["membership-add", "membership-move", "disable"],
)
def test_a_membership_or_state_change_revokes_external_sign_in(changes) -> None:
    cursor = _Cursor()

    _update(cursor, existing=dict(_EXISTING), **changes)

    assert [params for _sql, params in cursor.revocations()] == [
        ([USER], "external"),
        ([USER], "external"),
    ]


@pytest.mark.parametrize("roles", [["operator"], ["admin"]], ids=["grant", "promotion"])
def test_a_role_change_revokes_external_sign_in_of_that_account_only(roles) -> None:
    """ADR 0085 (B): a role is per account, so is what its change revokes."""
    cursor = _Cursor(existing_roles=["viewer"])

    _update(cursor, existing=dict(_EXISTING), roles=roles, role_account_id=ACCOUNT)

    assert [params for _sql, params in cursor.revocations()] == [
        ([USER], ACCOUNT, "external"),
        ([USER], ACCOUNT, "external"),
    ]
    for sql, _params in cursor.revocations():
        assert sql in {"UPDATE personal_access_tokens AS t", "UPDATE auth_sessions"}
    assert all(
        "account_id::text = %s" in sql
        for sql, _ in cursor.calls
        if sql.startswith(("UPDATE personal_access_tokens AS t", "UPDATE auth_sessions"))
    )


def test_a_role_only_change_with_session_revocation_ends_that_accounts_access_only() -> None:
    """ADR 0085 (B): sessions, tokens and OAuth grants of the account; nothing else."""
    cursor = _Cursor(existing_roles=["viewer"])

    _update(
        cursor,
        existing=dict(_EXISTING),
        roles=["operator"],
        role_account_id=ACCOUNT,
        revoke_sessions=True,
    )

    revoking = [
        (sql, params)
        for sql, params in cursor.calls
        if sql.startswith(
            ("UPDATE auth_sessions", "UPDATE personal_access_tokens", "UPDATE oauth_grants")
        )
    ]
    assert [sql.split(" SET ")[0] for sql, _ in revoking] == [
        "UPDATE auth_sessions",
        "UPDATE personal_access_tokens",
    ]
    for sql, params in revoking:
        assert "CAST(account_id AS TEXT) = %s" in sql
        assert params == (USER, ACCOUNT)


@pytest.mark.parametrize(
    "changes",
    [
        {"display_name": "Pat"},
        {"roles": ["viewer"]},
        {"requested_account_ids": [ACCOUNT], "primary_account_id": ACCOUNT},
        {"is_active": True},
    ],
    ids=["profile", "same-role", "same-account", "enable"],
)
def test_a_change_that_keeps_the_rules_keeps_external_sessions(changes) -> None:
    cursor = _Cursor(existing_roles=["viewer"])

    _update(cursor, existing=dict(_EXISTING), **changes)

    assert cursor.revocations() == []


def test_a_module_role_grant_revokes_the_persons_external_sign_in(monkeypatch) -> None:
    from core.application.auth import external_sign_in_repository as repository
    from core.application.auth.external_sign_in_ports import ExternalSignInPort

    cursor = _Cursor(existing_roles=["viewer"])

    @contextmanager
    def _connection():
        yield _Conn(cursor)

    monkeypatch.setattr(repository, "user_in_account", lambda *_a, **_k: True)
    monkeypatch.setattr(repository, "external_person_refusal", lambda *_a, **_k: "")
    port = ExternalSignInPort(_connection)

    port.grant_roles(account_id=ACCOUNT, user_id=USER, roles=["operator"])
    assert cursor.revocations()[0][1] == ([USER], "external")

    unchanged = _Cursor(existing_roles=["operator"])
    cursor.__dict__.update(unchanged.__dict__)
    port.grant_roles(account_id=ACCOUNT, user_id=USER, roles=["viewer"])
    assert cursor.revocations() == []


def test_disable_account_revokes_inside_the_lifecycle_transaction() -> None:
    from core.application.auth.account_lifecycle import disable_account

    cursor = _Cursor(account_users=[USER])

    class _Store:
        conn = _Conn(cursor)

        @contextmanager
        def account_lifecycle_transaction(self):
            yield self.conn

        def set_account_active(self, account_id: str, *, is_active: bool):
            return {"account_id": account_id, "is_active": is_active}

    disable_account(_Store(), ACCOUNT)

    assert cursor.revocations()[0][1] == ([USER], "external")


# --- an external administrator manages the issuing account only -----------------------


def test_an_external_session_manages_only_its_own_account() -> None:
    from core.entrypoints.api.auth_users import admin_managed_account_ids

    person = {
        "account_id": ACCOUNT,
        "roles": ["admin"],
        "account_ids": [ACCOUNT, OTHER],
        "account_memberships": [
            {"account_id": ACCOUNT, "role": "admin"},
            {"account_id": OTHER, "role": "admin"},
        ],
    }

    assert admin_managed_account_ids({**person, "auth_source": "external"}) == [ACCOUNT]
    assert admin_managed_account_ids({**person, "auth_source": "external", "account_id": ""}) == []
    assert admin_managed_account_ids({**person, "auth_source": "local"}) == [ACCOUNT, OTHER]


def test_an_administrator_manages_only_accounts_where_they_are_admin() -> None:
    """Roles are held per account (ADR 0085): a membership alone is not reach."""
    from core.entrypoints.api.auth_users import admin_managed_account_ids

    person = {
        "account_id": ACCOUNT,
        "roles": ["admin"],
        "account_memberships": [
            {"account_id": ACCOUNT, "role": "admin"},
            {"account_id": OTHER, "role": "viewer"},
        ],
    }

    assert admin_managed_account_ids(person) == [ACCOUNT]
    # Acting in OTHER, where the person is a viewer: the request's roles are OTHER's.
    acting_in_other = {**person, "account_id": OTHER, "roles": ["viewer"]}
    assert admin_managed_account_ids(acting_in_other) == [ACCOUNT]
    # No membership carries admin and the acting role is not admin: nothing.
    assert (
        admin_managed_account_ids(
            {
                "account_id": OTHER,
                "roles": ["viewer"],
                "account_memberships": [{"account_id": OTHER, "role": "viewer"}],
            }
        )
        == []
    )
    # A personal token carries no memberships: its own account when its role is admin.
    assert admin_managed_account_ids({"account_id": ACCOUNT, "roles": ["admin"]}) == [ACCOUNT]


# --- ADR 0085, C4: an inviter who stops being an admin ends their invitations -----------


@pytest.mark.parametrize(
    "changes",
    [
        {"is_active": False},
        {"roles": ["viewer"]},
        {"requested_account_ids": [OTHER], "primary_account_id": OTHER},
    ],
    ids=["deactivated", "demoted", "removed-from-account"],
)
def test_losing_the_admin_role_cancels_the_persons_pending_invitations(changes) -> None:
    cursor = _Cursor(existing_roles=["admin"])

    _update(cursor, existing=dict(_EXISTING), **changes)

    cancelled = [
        params for sql, params in cursor.calls if sql.startswith("UPDATE account_invitations")
    ]
    assert cancelled == [(USER,)]
    sql = next(sql for sql, _ in cursor.calls if sql.startswith("UPDATE account_invitations"))
    assert "SET status = 'cancelled'" in sql and "AND NOT EXISTS" in sql


def test_a_profile_change_leaves_invitations_alone() -> None:
    cursor = _Cursor()

    _update(cursor, existing=dict(_EXISTING), display_name="Pat")

    assert not any(sql.startswith("UPDATE account_invitations") for sql, _ in cursor.calls)
