"""Roles per account (ADR 0085): resolution, reach and the absence of global roles.

The PostgreSQL proof (two accounts, switching, tokens, limits, last administrator)
is tests/db/test_roles_per_account_postgres.py.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.application.auth import resolve, serve_auth_runtime
from core.application.auth.membership_roles import (
    LastAccountAdminError,
    account_role_keys,
    keep_one_role,
    require_other_admins,
)
from core.application.auth.user_admin.command_service import _require_update_access
from core.application.auth.user_admin.contracts import UserUpdateInput
from core.application.management.errors import ServiceForbiddenError
from core.infrastructure.db.membership_role_reader import (
    MEMBERSHIP_ROLE_SQL,
    membership_role_keys,
)

ROOT = Path(__file__).resolve().parents[2]
ACCOUNT_A = "aaaaaaaa-0000-4000-8000-00000000000a"
ACCOUNT_B = "bbbbbbbb-0000-4000-8000-00000000000b"
PERSON = "11111111-0000-4000-8000-000000000001"
ADMIN = "22222222-0000-4000-8000-000000000002"


class _Cursor:
    def __init__(self, rows: list[Any] | None = None, one: Any = None) -> None:
        self.rows = rows or []
        self.one = one
        self.calls: list[tuple[str, Any]] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self.calls.append((" ".join(str(sql).split()), params))

    def fetchall(self) -> list[Any]:
        return list(self.rows)

    def fetchone(self) -> Any:
        return self.one


# --- one resolver --------------------------------------------------------------------


def test_the_reader_asks_for_the_membership_of_the_account_and_an_active_account() -> None:
    sql = " ".join(MEMBERSHIP_ROLE_SQL.split())
    assert "FROM public.user_account_memberships AS m" in sql
    assert "JOIN public.accounts AS a ON a.id = m.account_id AND a.is_active" in sql
    assert "user_roles" not in sql

    cursor = _Cursor(rows=[("admin",), {"key": "admin"}])
    assert membership_role_keys(cursor, PERSON, ACCOUNT_A) == ["admin"]
    assert cursor.calls[0][1] == (PERSON, ACCOUNT_A)


@pytest.mark.parametrize("account", ["", None, "not-a-uuid"])
def test_no_account_means_no_role_without_a_query(account: Any) -> None:
    cursor = _Cursor(rows=[("admin",)])

    assert membership_role_keys(cursor, PERSON, account) == []
    assert cursor.calls == []
    bl = SimpleNamespace(conn=SimpleNamespace(cursor=lambda: cursor))
    assert account_role_keys(bl, PERSON, account) == []
    assert resolve.get_user_roles(bl, PERSON, account) == []
    assert serve_auth_runtime.get_user_roles(bl, PERSON, account) == []
    # The old two-argument call names no account: no role, never the installation's.
    assert resolve.get_user_roles(bl, PERSON) == []


def test_every_principal_resolver_reads_the_acting_accounts_role() -> None:
    session = (ROOT / "core/application/auth/resolve.py").read_text(encoding="utf-8")
    assert session.count('get_user_roles(bl, str(payload["id"]), payload.get("account_id"))') == 3
    serve = (ROOT / "core/application/auth/serve_auth_runtime.py").read_text(encoding="utf-8")
    assert serve.count('get_user_roles_fn(bl, str(payload["id"]), payload.get("account_id"))') == 2
    pat = (ROOT / "core/infrastructure/db/logger_extensions_accounts_pat_mixin.py").read_text(
        encoding="utf-8"
    )
    assert 'membership_role_keys(cur, row.get("user_id"), account_id)' in pat
    assert "_default_account_id" not in pat
    oauth = (ROOT / "core/application/auth/inbound_oauth_repository.py").read_text(encoding="utf-8")
    assert "rm.user_id = g.user_id AND rm.account_id = g.account_id" in oauth


def test_a_personal_token_without_an_account_is_refused() -> None:
    from core.infrastructure.db.logger_extensions_accounts_pat_mixin import (
        PlatformLoggerPersonalAccessTokensMixin,
    )

    row = {
        "pat_id": "1",
        "pat_account_id": "",
        "user_id": PERSON,
        "revoked": False,
        "expires_at": None,
        "is_active": True,
    }
    cursor = _Cursor(one=row)

    class _Store(PlatformLoggerPersonalAccessTokensMixin):
        conn = SimpleNamespace(cursor=lambda **_kwargs: cursor)

        def _table_exists(self, _table: str) -> bool:
            return True

    assert _Store().resolve_pat("fs_pat_token") is None
    assert not any("user_account_memberships" in sql for sql, _ in cursor.calls)


def test_no_code_reads_or_writes_a_global_role_row() -> None:
    """``user_roles`` holds scoped rows only; a person's role is the membership's."""
    global_predicate = re.compile(
        r"workspace_id IS NULL\s+AND\s+(?:\w+\.)?project_id IS NULL", re.IGNORECASE
    )
    offenders = []
    for path in sorted((ROOT / "core").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "user_roles" in text and global_predicate.search(text):
            offenders.append(str(path.relative_to(ROOT)))
        if re.search(r"INSERT INTO (?:public\.)?user_roles \(user_id, role_id\)\s", text):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


# --- last administrator ----------------------------------------------------------------


class _AdminsCursor(_Cursor):
    def __init__(self, *, roles: dict[str, str], others: int) -> None:
        super().__init__()
        self.roles = roles
        self.others = others
        self._last = ""

    def execute(self, sql: str, params: Any = None) -> None:
        super().execute(sql, params)
        self._last = " ".join(str(sql).split())

    def fetchall(self) -> list[Any]:
        if "r.key AS role FROM user_account_memberships" in self._last:
            return list(self.roles.items())
        return []

    def fetchone(self) -> Any:
        return (self.others,)


def test_the_last_administrator_check_is_per_account() -> None:
    cursor = _AdminsCursor(roles={ACCOUNT_A: "admin", ACCOUNT_B: "viewer"}, others=0)

    with pytest.raises(LastAccountAdminError) as refused:
        require_other_admins(cursor, user_id=PERSON, account_ids=[ACCOUNT_A, ACCOUNT_B])
    assert refused.value.account_id == ACCOUNT_A
    # Only accounts the person administers are counted.
    counts = [params for sql, params in cursor.calls if "COUNT(*) AS total" in sql]
    assert counts == [(PERSON, ACCOUNT_A)]

    # B is not administered by the person: nothing to keep there.
    require_other_admins(cursor, user_id=PERSON, account_ids=[ACCOUNT_B])
    require_other_admins(
        _AdminsCursor(roles={ACCOUNT_A: "admin"}, others=1),
        user_id=PERSON,
        account_ids=[ACCOUNT_A],
    )


def test_one_role_is_kept_per_membership() -> None:
    assert keep_one_role(["viewer", "Operator", " "]) == "operator"
    assert keep_one_role(["auditor"]) is None
    assert keep_one_role([]) is None


# --- reach of an account administrator -------------------------------------------------


def _target(*accounts: str) -> dict[str, Any]:
    return {
        "id": PERSON,
        "account_id": accounts[0],
        "account_ids": list(accounts),
        "account_memberships": [{"account_id": account} for account in accounts],
    }


def test_an_account_administrator_changes_the_role_in_their_account_only() -> None:
    target = _target(ACCOUNT_A, ACCOUNT_B)
    role_only = UserUpdateInput(user_id=PERSON, role="operator", actor_user_id=ADMIN)

    # Admin of B only: the role in B, although the person also lives in A.
    _require_update_access(target, role_only, ACCOUNT_B, [ACCOUNT_B])
    # Not the role in A.
    with pytest.raises(ServiceForbiddenError):
        _require_update_access(target, role_only, ACCOUNT_A, [ACCOUNT_B])
    # Nor anything else about a person who also lives elsewhere.
    with pytest.raises(ServiceForbiddenError):
        _require_update_access(
            target,
            UserUpdateInput(user_id=PERSON, is_active=False, actor_user_id=ADMIN),
            None,
            [ACCOUNT_B],
        )
    # Nor the role of someone who is not a member of the administrator's account.
    with pytest.raises(ServiceForbiddenError):
        _require_update_access(_target(ACCOUNT_A), role_only, ACCOUNT_B, [ACCOUNT_B])


def test_a_person_never_changes_their_own_role() -> None:
    target = _target(ACCOUNT_A)

    with pytest.raises(ServiceForbiddenError, match="your own role"):
        _require_update_access(
            target,
            UserUpdateInput(user_id=PERSON, role="admin", actor_user_id=PERSON),
            ACCOUNT_A,
            [ACCOUNT_A],
        )
    # The CLI names no acting person and no reach: unchanged.
    _require_update_access(target, UserUpdateInput(user_id=PERSON, role="admin"), None, None)
