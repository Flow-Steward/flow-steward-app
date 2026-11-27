"""The company owner (ADR 0085, "Company owner"): the predicate and the refusals.

A scripted cursor stands in for the database: it knows which account each person
owns and records every statement. The PostgreSQL scenarios are in
``tests/db/test_company_owner_postgres.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.application.auth import account_owner
from core.application.auth.account_owner import (
    OWNER_PROTECTED_CODE,
    AccountOwnerProtectedError,
    is_account_owner,
    require_owner_consent,
    require_owner_kept,
)
from core.application.auth.external_sign_in_admin import ExternalSignInAdmin
from core.application.auth.membership_roles import revoke_account_access, set_membership_role
from core.application.auth.user_admin import account_members, repository
from core.entrypoints.api.auth_account_owner_routes import register_account_owner_routes

OWNER = "11111111-1111-1111-1111-111111111111"
FREELANCER = "22222222-2222-2222-2222-222222222222"
COMPANY = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
INSTALLATION = "00000000-0000-0000-0000-000000000002"
IDENTITY = "cccccccc-cccc-cccc-cccc-cccccccccccc"


class _Cursor:
    """Answers the owner queries from ``owners`` ({account: owner}); records the rest."""

    def __init__(self, owners: dict[str, str] | None = None, *, identity_user: str = "") -> None:
        self.owners = dict(owners or {})
        self.identity_user = identity_user
        self.calls: list[tuple[str, Any]] = []
        self.rowcount = 1
        self._last = ""
        self._params: Any = None

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_exc: Any) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self._last = " ".join(str(sql).split())
        self._params = params
        self.calls.append((self._last, params))

    def fetchone(self) -> Any:
        if self._last.startswith("SELECT CAST(owner_user_id AS TEXT) AS owner_user_id"):
            owner = self.owners.get(self._params[0])
            return (owner,) if owner else None
        if self._last.startswith(
            "SELECT CAST(user_id AS TEXT) AS user_id FROM external_identities"
        ):
            return (self.identity_user,) if self.identity_user else None
        if "to_regclass" in self._last:
            return (False,)
        return ("row",)

    def fetchall(self) -> list[Any]:
        if self._last.startswith("SELECT CAST(id AS TEXT) AS id FROM accounts"):
            user = self._params[0]
            return [(account,) for account, owner in sorted(self.owners.items()) if owner == user]
        return []

    def wrote(self, prefix: str) -> bool:
        return any(sql.startswith(prefix) for sql, _ in self.calls)


# --- the predicate ----------------------------------------------------------------------


def test_the_predicate_names_the_owner_of_that_company_only() -> None:
    cur = _Cursor({COMPANY: OWNER})

    assert is_account_owner(cur, OWNER, COMPANY) is True
    assert is_account_owner(cur, FREELANCER, COMPANY) is False
    assert is_account_owner(cur, OWNER, OTHER) is False
    assert is_account_owner(cur, "", COMPANY) is False


def test_the_owner_is_kept_whoever_asks() -> None:
    cur = _Cursor({COMPANY: OWNER})

    with pytest.raises(AccountOwnerProtectedError) as refused:
        require_owner_kept(cur, user_id=OWNER)
    assert refused.value.status_code == 403
    assert refused.value.payload["code"] == OWNER_PROTECTED_CODE
    assert refused.value.account_ids == [COMPANY]
    with pytest.raises(AccountOwnerProtectedError):
        require_owner_kept(cur, user_id=OWNER, account_ids=[COMPANY])
    # Another company of the owner, or a company the change keeps, is not protected.
    require_owner_kept(cur, user_id=OWNER, account_ids=[OTHER])
    require_owner_kept(cur, user_id=OWNER, kept_account_ids=[COMPANY, OTHER])
    with pytest.raises(AccountOwnerProtectedError):
        require_owner_kept(cur, user_id=OWNER, kept_account_ids=[OTHER])
    # Somebody who owns nothing is never refused.
    require_owner_kept(cur, user_id=FREELANCER)


def test_only_the_owner_changes_their_own_sign_in() -> None:
    cur = _Cursor({COMPANY: OWNER})

    require_owner_consent(cur, user_id=OWNER, actor_user_id=OWNER)
    require_owner_consent(cur, user_id=FREELANCER, actor_user_id=OWNER)
    for actor in (FREELANCER, ""):
        with pytest.raises(AccountOwnerProtectedError):
            require_owner_consent(cur, user_id=OWNER, actor_user_id=actor)


# --- the paths every change passes through ----------------------------------------------


def test_no_writer_of_a_membership_role_demotes_the_owner() -> None:
    cur = _Cursor({COMPANY: OWNER})

    for role in ("operator", "viewer", None):
        with pytest.raises(AccountOwnerProtectedError):
            set_membership_role(cur, user_id=OWNER, account_id=COMPANY, role=role)
    assert not cur.wrote("UPDATE user_account_memberships")
    # The admin role is kept, and the owner's role elsewhere may change.
    assert set_membership_role(cur, user_id=OWNER, account_id=COMPANY, role="admin") == ["admin"]
    assert set_membership_role(cur, user_id=OWNER, account_id=OTHER, role="viewer") == ["viewer"]


def test_nobody_ends_the_owners_access_to_their_company() -> None:
    cur = _Cursor({COMPANY: OWNER})

    with pytest.raises(AccountOwnerProtectedError):
        revoke_account_access(cur, user_id=OWNER, account_id=COMPANY, reason="x")
    assert not cur.wrote("UPDATE auth_sessions")
    revoke_account_access(cur, user_id=OWNER, account_id=OTHER, reason="x")
    assert cur.wrote("UPDATE auth_sessions")


def test_the_owner_is_not_removed_from_their_company(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(account_members, "require_other_admins", lambda *_a, **_k: None)
    monkeypatch.setattr(
        account_members,
        "_person",
        lambda _cur, user_id: {"id": user_id, "username": "o@x.test", "account_id": COMPANY},
    )
    monkeypatch.setattr(account_members, "_member_accounts", lambda *_a: [COMPANY, OTHER])
    cur = _Cursor({COMPANY: OWNER})

    with pytest.raises(AccountOwnerProtectedError):
        account_members.remove_member(
            cur, user_id=OWNER, account_id=COMPANY, actor_user_id=FREELANCER
        )
    assert not cur.wrote("DELETE FROM user_account_memberships")


@pytest.mark.parametrize(
    ("change", "refused_for_the_owner_too"),
    [
        ({"deactivated": True}, True),
        ({"role_account": COMPANY}, True),
        ({"kept_accounts": [OTHER]}, True),
        ({"sign_in_changed": True}, False),
    ],
)
def test_the_user_update_keeps_the_owner_and_their_sign_in(
    change: dict[str, Any], refused_for_the_owner_too: bool
) -> None:
    arguments: dict[str, Any] = {
        "deactivated": False,
        "role_account": None,
        "kept_accounts": None,
        "sign_in_changed": False,
        **change,
    }
    cur = _Cursor({COMPANY: OWNER})

    for actor in (FREELANCER, None):
        with pytest.raises(AccountOwnerProtectedError):
            repository._require_owner_rules(cur, user_id=OWNER, actor_user_id=actor, **arguments)
    if refused_for_the_owner_too:
        with pytest.raises(AccountOwnerProtectedError):
            repository._require_owner_rules(cur, user_id=OWNER, actor_user_id=OWNER, **arguments)
    else:
        repository._require_owner_rules(cur, user_id=OWNER, actor_user_id=OWNER, **arguments)
    # A change of anyone else passes.
    repository._require_owner_rules(cur, user_id=FREELANCER, actor_user_id=OWNER, **arguments)


def test_only_the_owner_requires_their_own_password_reset() -> None:
    cur = _Cursor({COMPANY: OWNER})
    bl = SimpleNamespace(conn=SimpleNamespace(cursor=lambda: cur, commit=lambda: None))

    with pytest.raises(AccountOwnerProtectedError):
        repository.force_password_reset(bl, user_id=OWNER, actor_user_id=FREELANCER)
    with pytest.raises(AccountOwnerProtectedError):
        repository.force_password_reset(bl, user_id=OWNER)
    assert not cur.wrote("UPDATE users SET must_change_password")
    repository.force_password_reset(bl, user_id=OWNER, actor_user_id=OWNER)
    assert cur.wrote("UPDATE users SET must_change_password")


def _identity_admin(cur: _Cursor, audit: list[dict[str, Any]]) -> ExternalSignInAdmin:
    class _Connection:
        def __enter__(self) -> Any:
            return SimpleNamespace(cursor=lambda: cur)

        def __exit__(self, *_exc: Any) -> bool:
            return False

    return ExternalSignInAdmin(connection=_Connection, audit=lambda **kwargs: audit.append(kwargs))


def _admin_scope(user_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        principal_type="admin",
        token_scopes=frozenset(),
        account_id=COMPANY,
        actor_name="someone",
        user_id=user_id,
    )


def test_only_the_owner_unlinks_their_own_sign_in_identity() -> None:
    audit: list[dict[str, Any]] = []
    cur = _Cursor({COMPANY: OWNER}, identity_user=OWNER)
    service = _identity_admin(cur, audit)

    with pytest.raises(AccountOwnerProtectedError):
        service.unlink_identity(_admin_scope(FREELANCER), IDENTITY)
    assert not cur.wrote("DELETE FROM external_identities")
    assert service.unlink_identity(_admin_scope(OWNER), IDENTITY)["deleted"] is True
    assert [entry["action"] for entry in audit] == ["external_identity_unlink"]


def test_a_directory_sync_never_takes_the_owners_role(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.application.auth import external_sign_in_repository, membership_roles
    from core.application.auth.external_sign_in_ports import ExternalSignInPort

    cur = _Cursor({COMPANY: OWNER})

    class _Connection:
        def __enter__(self) -> Any:
            return SimpleNamespace(cursor=lambda: cur)

        def __exit__(self, *_exc: Any) -> bool:
            return False

    monkeypatch.setattr(external_sign_in_repository, "user_in_account", lambda *_a, **_k: True)
    monkeypatch.setattr(membership_roles, "require_other_admins", lambda *_a, **_k: None)
    port = ExternalSignInPort.__new__(ExternalSignInPort)
    port._connection = _Connection  # type: ignore[attr-defined]

    with pytest.raises(ValueError, match="company owner"):
        port.remove_role(account_id=COMPANY, user_id=OWNER)
    assert not cur.wrote("UPDATE user_account_memberships")


def test_the_predicate_reads_no_cached_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """The owner changes take effect at once: the predicate queries every time."""
    cur = _Cursor({COMPANY: OWNER})
    assert is_account_owner(cur, OWNER, COMPANY)
    cur.owners[COMPANY] = FREELANCER
    assert not is_account_owner(cur, OWNER, COMPANY)
    assert is_account_owner(cur, FREELANCER, COMPANY)
    monkeypatch.setattr(account_owner, "account_owner_id", lambda *_a: "")
    assert not is_account_owner(cur, FREELANCER, COMPANY)


# --- the routes ---------------------------------------------------------------------------


class _Ownership:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def state(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("state", kwargs))
        return {"owner_user_id": OWNER, "is_owner": kwargs["viewer_id"] == OWNER}

    def offer(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("offer", kwargs))
        return {"status": "pending"}

    def cancel(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("cancel", kwargs))
        return {"status": "cancelled"}

    def decide(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("decide", kwargs))
        return {"status": "accepted" if kwargs["accept"] else "declined"}

    def reassign(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("reassign", kwargs))
        return {"status": "reassigned"}


def _client(user: dict[str, Any], service: _Ownership) -> TestClient:
    router = APIRouter()

    def _member_admin(_request: Request) -> dict[str, Any] | JSONResponse:
        if "admin" not in user.get("roles", []):
            return JSONResponse({"error": "admin-required"}, status_code=403)
        return dict(user)

    register_account_owner_routes(
        router,
        member_admin=_member_admin,
        ownership_service=service,  # type: ignore[arg-type]
        service_error=lambda exc: JSONResponse({"error": str(exc)}, status_code=500),
    )
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _session(user_id: str, account_id: str = COMPANY) -> dict[str, Any]:
    return {
        "id": user_id,
        "username": f"{user_id[:4]}@x.test",
        "roles": ["admin"],
        "account_id": account_id,
        "principal_type": "admin",
        "auth_source": "local",
    }


def test_the_owner_offers_and_the_recipient_answers_in_the_acting_company() -> None:
    service = _Ownership()
    owners = _client(_session(OWNER), service)
    assert owners.get("/api/auth/users/owner").json()["is_owner"] is True
    assert owners.post("/api/auth/users/owner/transfer", json={"user_id": FREELANCER}).json() == {
        "status": "pending"
    }
    assert owners.post("/api/auth/users/owner/transfer/cancel").status_code == 200
    recipients = _client(_session(FREELANCER), service)
    answer = recipients.post(
        "/api/auth/users/owner/transfer/accept", json={"transfer_id": IDENTITY}
    )
    assert answer.json() == {"status": "accepted"}
    declined = recipients.post(
        "/api/auth/users/owner/transfer/decline", json={"transfer_id": IDENTITY}
    )
    assert declined.json() == {"status": "declined"}

    offer = next(kwargs for name, kwargs in service.calls if name == "offer")
    assert offer == {
        "account_id": COMPANY,
        "owner_id": OWNER,
        "to_user_id": FREELANCER,
        "actor": "1111@x.test",
    }
    decide = [kwargs for name, kwargs in service.calls if name == "decide"]
    assert [(item["user_id"], item["accept"]) for item in decide] == [
        (FREELANCER, True),
        (FREELANCER, False),
    ]


def test_a_token_never_transfers_or_accepts_ownership() -> None:
    service = _Ownership()
    token = {**_session(OWNER), "principal_type": "personal_access_token", "auth_source": "pat"}
    api = _client(token, service)

    assert (
        api.post("/api/auth/users/owner/transfer", json={"user_id": FREELANCER}).status_code == 403
    )
    assert (
        api.post(
            "/api/auth/users/owner/transfer/accept", json={"transfer_id": IDENTITY}
        ).status_code
        == 403
    )
    assert not [name for name, _ in service.calls if name != "state"]


def test_only_the_installation_administrator_reassigns_an_owner() -> None:
    service = _Ownership()
    body = {"user_id": FREELANCER, "reason": "The owner lost access"}

    company_admin = _client(_session(OWNER), service)
    refused = company_admin.post(f"/api/auth/accounts/{COMPANY}/owner", json=body)
    assert refused.status_code == 403
    assert refused.json()["code"] == "installation_admin_required"
    installation = _client(_session(FREELANCER, INSTALLATION), service)
    assert installation.post(f"/api/auth/accounts/{COMPANY}/owner", json=body).json() == {
        "status": "reassigned"
    }
    assert (
        installation.post(
            f"/api/auth/accounts/{COMPANY}/owner", json={"user_id": FREELANCER}
        ).status_code
        == 422
    )
    assert [kwargs for name, kwargs in service.calls if name == "reassign"] == [
        {
            "account_id": COMPANY,
            "to_user_id": FREELANCER,
            "reason": "The owner lost access",
            "actor": "2222@x.test",
        }
    ]


# --- the owner is refused first, whatever the request says --------------------------------


def _command_service(cur: _Cursor, cleared: list[str]) -> Any:
    from core.application.auth.user_admin.command_service import UserAdminCommandService

    owner = {"id": OWNER, "username": "o@x.test", "account_id": COMPANY, "account_ids": [COMPANY]}
    bl = SimpleNamespace(conn=SimpleNamespace(cursor=lambda: cur))
    return UserAdminCommandService(
        get_logger=lambda: bl,
        auth_tables_available_fn=lambda _bl: True,
        get_user_by_id_fn=lambda *_args: dict(owner),
        log_auth_audit_event_fn=lambda *_args, **_kwargs: None,
        auth_login_clear_user_failures_fn=lambda username, *, bl=None: cleared.append(username),
        repository=SimpleNamespace(),  # never reached
    )


@pytest.mark.parametrize(
    "change",
    [
        {"password": "short"},  # pragma: allowlist secret
        {"display_name": "Former owner"},
        {"must_change_password": True},
        {"is_active": False},
        {"role": "viewer", "role_account_id": COMPANY},
    ],
)
def test_a_change_to_the_owner_is_refused_before_its_input_is_checked(
    change: dict[str, Any],
) -> None:
    from core.application.auth.user_admin.contracts import UserUpdateInput

    service = _command_service(_Cursor({COMPANY: OWNER}), [])
    with pytest.raises(AccountOwnerProtectedError):
        service.update_user(UserUpdateInput(user_id=OWNER, actor_user_id=FREELANCER, **change))


def test_only_the_owner_clears_their_own_sign_in_lockout() -> None:
    from core.application.auth.user_admin.contracts import UserSelectorInput

    cleared: list[str] = []
    service = _command_service(_Cursor({COMPANY: OWNER}), cleared)
    for actor in (FREELANCER, None):
        with pytest.raises(AccountOwnerProtectedError):
            service.unlock_user(UserSelectorInput(user_id=OWNER), actor_user_id=actor)
    assert cleared == []
