"""The company owner keeps the ultimate rights (ADR 0085, "Company owner").

Olivia decides for company A: she created it and is its owner. She invited
Fred, a freelancer, as an administrator. They fall out: whatever Fred tries, he
cannot remove, demote, deactivate, delete or lock Olivia out. Olivia removes Fred.
Ownership moves only when the person it goes to accepts it, and the installation
administrator can reassign it when the owner has lost access. On the disposable
audit database through Core's authentication middleware and auth routes (see
``tests/db/_companies.py``); every test runs in a transaction that is rolled back.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from core.application.auth.account_owner import (
    OWNER_PROTECTED_CODE,
    AccountOwnerProtectedError,
    is_account_owner,
)
from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.auth.external_sign_in_ports import ExternalSignInPort
from core.application.auth.membership_roles import set_membership_role
from core.application.auth.user_admin.command_service import UserAdminCommandService
from core.application.auth.user_admin.contracts import (
    UserActiveUpdateInput,
    UserOwnerReassignInput,
    UserPasswordUpdateInput,
    UserRolesUpdateInput,
)
from tests.db._companies import (
    World,
    client,
    companies_world,
    new_id,
    owner_of,
    person,
    session,
    username,
)

#: A password Fred would like to give Olivia.
NEW_PASSWORD = "a new password nobody knows"  # pragma: allowlist secret - a test value
MIGRATION = (
    Path(__file__).resolve().parents[2] / "db" / "migrations" / "20261127220000_account_owner.sql"
)


@dataclass(frozen=True)
class People:
    olivia: str  # created company A: its owner
    fred: str  # a freelancer, administrator of A (and of C, another client)
    otto: str  # an operator of A


@pytest.fixture
def world(disposable_db, disposable_db_conn) -> Iterator[World]:
    with companies_world(disposable_db.dsn, disposable_db_conn) as built:
        yield built


@pytest.fixture
def people(world: World) -> People:
    return People(
        olivia=person(world, "olivia", world.a.id, {world.a.id: "admin"}),
        fred=person(world, "fred", world.c.id, {world.a.id: "admin", world.c.id: "admin"}),
        otto=person(world, "otto", world.a.id, {world.a.id: "operator"}),
    )


def _browser(world: World, user_id: str, account_id: str):
    return client(world, cookie=session(world, user_id, account_id))


def _refused(response) -> None:
    assert response.status_code == 403, response.text
    assert response.json().get("code") == OWNER_PROTECTED_CODE, response.text


def _role(world: World, user_id: str, account_id: str) -> str:
    row = world.conn.execute(
        """SELECT r.key FROM user_account_memberships m LEFT JOIN roles r ON r.id = m.role_id
            WHERE m.user_id = %s AND m.account_id = %s""",
        (user_id, account_id),
    ).fetchone()
    return str(row[0] or "") if row else "<no membership>"


def _audited(world: World, action: str) -> list[dict]:
    return [entry for entry in world.audit if entry.get("action") == action]


# --- who becomes the owner ----------------------------------------------------------------


def test_the_first_administrator_of_a_company_is_its_owner(world: World, people: People) -> None:
    assert owner_of(world, world.a.id) == people.olivia
    # Fred became an administrator of A after Olivia: he does not own it.
    assert owner_of(world, world.c.id) == people.fred
    assert owner_of(world, world.b.id) == ""
    with world.conn.cursor() as cur:
        assert is_account_owner(cur, people.olivia, world.a.id)
        assert not is_account_owner(cur, people.fred, world.a.id)


def test_a_person_who_creates_their_own_company_is_its_owner(world: World) -> None:
    """The self-service sign-up's writes (``create_own_account``): account, person,
    membership, then the admin role through the one writer of membership roles."""
    account_id, user_id = new_id(), new_id()
    world.conn.execute(
        """INSERT INTO accounts (id, account_key, display_name, is_active)
           VALUES (%s, %s, 'Own company', true)""",
        (account_id, account_id),
    )
    world.conn.execute(
        "INSERT INTO users (id, username, password_hash, account_id) VALUES (%s, %s, 'x', %s)",
        (user_id, f"founder-{uuid.uuid4().hex[:8]}@example.invalid", account_id),
    )
    world.conn.execute(
        """INSERT INTO user_account_memberships (user_id, account_id, is_default)
           VALUES (%s, %s, true)""",
        (user_id, account_id),
    )
    assert owner_of(world, account_id) == ""
    with world.conn.cursor() as cur:
        set_membership_role(cur, user_id=user_id, account_id=account_id, role="admin")
    assert owner_of(world, account_id) == user_id


def test_the_upgrade_gives_each_company_its_earliest_active_administrator(
    world: World, people: People
) -> None:
    gone = person(world, "gone", world.a.id, {world.a.id: "admin"})
    world.conn.execute("UPDATE users SET is_active = false WHERE id = %s", (gone,))
    # Gone's membership is the oldest, Fred's the next; Olivia's the newest.
    for user_id, created in ((gone, "2026-01-01"), (people.fred, "2026-02-01")):
        world.conn.execute(
            """UPDATE user_account_memberships SET created_at = %s
                WHERE user_id = %s AND account_id = %s""",
            (created, user_id, world.a.id),
        )
    world.conn.execute(
        "UPDATE accounts SET owner_user_id = NULL WHERE id IN (%s, %s)",
        (world.a.id, world.c.id),
    )

    world.conn.execute(MIGRATION.read_text(encoding="utf-8"))
    assert owner_of(world, world.a.id) == people.fred
    assert owner_of(world, world.c.id) == people.fred

    # Running it again changes nothing, even when the order changed since.
    world.conn.execute(
        """UPDATE user_account_memberships SET created_at = '2025-01-01'
            WHERE user_id = %s AND account_id = %s""",
        (people.olivia, world.a.id),
    )
    world.conn.execute(MIGRATION.read_text(encoding="utf-8"))
    assert owner_of(world, world.a.id) == people.fred


# --- the freelancer falls out with the owner ----------------------------------------------


def test_a_freelancer_administrator_cannot_touch_the_owner(world: World, people: People) -> None:
    olivias = _browser(world, people.olivia, world.a.id)
    freds = _browser(world, people.fred, world.a.id)

    _refused(freds.delete(f"/api/auth/users/{people.olivia}/membership"))
    _refused(freds.put(f"/api/auth/users/{people.olivia}", json={"role": "viewer"}))
    _refused(freds.put(f"/api/auth/users/{people.olivia}", json={"role": "admin"}))
    _refused(freds.put(f"/api/auth/users/{people.olivia}", json={"is_active": False}))
    _refused(
        freds.put(
            f"/api/auth/users/{people.olivia}",
            json={"password": NEW_PASSWORD, "must_change_password": False},
        )
    )
    _refused(freds.post(f"/api/auth/users/{people.olivia}/force-password-reset"))
    _refused(freds.post(f"/api/auth/users/{people.olivia}/authenticator", json={"action": "reset"}))
    _refused(freds.delete(f"/api/auth/users/{people.olivia}"))
    # Nor can he give the company to himself.
    offered = freds.post("/api/auth/users/owner/transfer", json={"user_id": people.fred})
    assert offered.status_code == 403, offered.text
    taken = freds.post(
        f"/api/auth/accounts/{world.a.id}/owner",
        json={"user_id": people.fred, "reason": "mine now"},
    )
    assert taken.status_code == 403, taken.text

    # Olivia is untouched: owner, admin, active, signed in, same password.
    assert owner_of(world, world.a.id) == people.olivia
    assert _role(world, people.olivia, world.a.id) == "admin"
    active, password_hash, must_change = world.conn.execute(
        "SELECT is_active, password_hash, must_change_password FROM users WHERE id = %s",
        (people.olivia,),
    ).fetchone()
    assert (active, password_hash, must_change) == (True, "x", False)
    assert olivias.get("/api/projects").status_code == 200


def test_the_owner_demotes_and_removes_the_freelancer(world: World, people: People) -> None:
    olivias = _browser(world, people.olivia, world.a.id)

    demoted = olivias.put(f"/api/auth/users/{people.fred}", json={"role": "viewer"})
    assert demoted.status_code == 200, demoted.text
    removed = olivias.delete(f"/api/auth/users/{people.fred}/membership")
    assert removed.status_code == 200, removed.text

    assert _role(world, people.fred, world.a.id) == "<no membership>"
    # Fred keeps his own company.
    assert _role(world, people.fred, world.c.id) == "admin"
    assert owner_of(world, world.c.id) == people.fred


def test_the_owner_cannot_leave_or_be_demoted_before_transferring(
    world: World, people: People
) -> None:
    olivias = _browser(world, people.olivia, world.a.id)
    assert olivias.put(f"/api/auth/users/{people.olivia}", json={"role": "viewer"}).status_code in {
        403,
        409,
    }
    _refused(olivias.put(f"/api/auth/users/{people.olivia}", json={"is_active": False}))

    # The command line (the installation operator) is refused too.
    service = UserAdminCommandService(get_logger=lambda: world.store)
    with pytest.raises(AccountOwnerProtectedError):
        service.set_roles(
            UserRolesUpdateInput(user_id=people.olivia, role="viewer", role_account_id=world.a.id)
        )
    with pytest.raises(AccountOwnerProtectedError):
        service.set_active(UserActiveUpdateInput(user_id=people.olivia, is_active=False))
    with pytest.raises(AccountOwnerProtectedError):
        service.set_password(UserPasswordUpdateInput(user_id=people.olivia, password=NEW_PASSWORD))
    # A directory sync of a platform module never takes her role.
    with pytest.raises(ValueError, match="company owner"):
        _sync_port(world).remove_role(account_id=world.a.id, user_id=people.olivia)
    assert _role(world, people.olivia, world.a.id) == "admin"

    # She still changes her own sign-in.
    own = olivias.put(f"/api/auth/users/{people.olivia}", json={"must_change_password": True})
    assert own.status_code == 200, own.text


def _sync_port(world: World) -> ExternalSignInPort:
    @contextmanager
    def connection():
        with world.conn.transaction():
            yield world.conn

    return ExternalSignInPort(connection)


# --- transferring ownership ---------------------------------------------------------------


def test_a_transfer_waits_for_the_recipients_acceptance(world: World, people: People) -> None:
    olivias = _browser(world, people.olivia, world.a.id)
    freds = _browser(world, people.fred, world.a.id)

    # Only an active administrator can receive it; only the owner offers it.
    to_operator = olivias.post("/api/auth/users/owner/transfer", json={"user_id": people.otto})
    assert to_operator.status_code == 400, to_operator.text
    offered = olivias.post("/api/auth/users/owner/transfer", json={"user_id": people.fred})
    assert offered.status_code == 200, offered.text
    transfer_id = offered.json()["transfer"]["id"]

    # Nothing changed yet: Olivia owns A until Fred accepts.
    assert owner_of(world, world.a.id) == people.olivia
    state = freds.get("/api/auth/users/owner").json()
    assert (state["owner_user_id"], state["is_owner"], state["transfer_is_for_me"]) == (
        people.olivia,
        False,
        True,
    )
    # Nobody else answers it, and Olivia cannot accept for him.
    mine = olivias.post("/api/auth/users/owner/transfer/accept", json={"transfer_id": transfer_id})
    assert mine.status_code == 404, mine.text

    accepted = freds.post(
        "/api/auth/users/owner/transfer/accept", json={"transfer_id": transfer_id}
    )
    assert accepted.status_code == 200, accepted.text
    assert owner_of(world, world.a.id) == people.fred
    assert [
        e["detail"]["to_user_id"] for e in _audited(world, "account_owner_transfer_accept")
    ] == [people.fred]
    assert _audited(world, "account_owner_transfer_offer")

    # The answer closes it; and Olivia, no longer the owner, can now be demoted.
    again = freds.post("/api/auth/users/owner/transfer/accept", json={"transfer_id": transfer_id})
    assert again.status_code == 404
    demoted = freds.put(f"/api/auth/users/{people.olivia}", json={"role": "viewer"})
    assert demoted.status_code == 200, demoted.text


def test_a_declined_or_cancelled_transfer_keeps_the_owner(world: World, people: People) -> None:
    olivias = _browser(world, people.olivia, world.a.id)
    freds = _browser(world, people.fred, world.a.id)

    first = olivias.post("/api/auth/users/owner/transfer", json={"user_id": people.fred}).json()
    declined = freds.post(
        "/api/auth/users/owner/transfer/decline", json={"transfer_id": first["transfer"]["id"]}
    )
    assert declined.status_code == 200, declined.text
    assert owner_of(world, world.a.id) == people.olivia

    second = olivias.post("/api/auth/users/owner/transfer", json={"user_id": people.fred}).json()
    assert olivias.post("/api/auth/users/owner/transfer/cancel").status_code == 200
    late = freds.post(
        "/api/auth/users/owner/transfer/accept", json={"transfer_id": second["transfer"]["id"]}
    )
    assert late.status_code == 404, late.text
    assert owner_of(world, world.a.id) == people.olivia
    assert _audited(world, "account_owner_transfer_decline")
    assert _audited(world, "account_owner_transfer_cancel")


def test_a_transfer_to_someone_who_stops_being_admin_cannot_be_accepted(
    world: World, people: People
) -> None:
    olivias = _browser(world, people.olivia, world.a.id)
    offered = olivias.post("/api/auth/users/owner/transfer", json={"user_id": people.fred})
    transfer_id = offered.json()["transfer"]["id"]
    assert (
        olivias.put(f"/api/auth/users/{people.fred}", json={"role": "operator"}).status_code == 200
    )

    freds = _browser(world, people.fred, world.a.id)
    late = freds.post("/api/auth/users/owner/transfer/accept", json={"transfer_id": transfer_id})
    assert late.status_code in {403, 404}, late.text
    assert owner_of(world, world.a.id) == people.olivia


# --- recovery by the installation administrator -------------------------------------------


def test_the_installation_administrator_reassigns_a_lost_owner(
    world: World, people: People
) -> None:
    ivan = person(world, "ivan", DEFAULT_ACCOUNT_ID, {DEFAULT_ACCOUNT_ID: "admin"})
    ivans = _browser(world, ivan, DEFAULT_ACCOUNT_ID)

    # A reason is required, and a company administrator cannot use the recovery.
    missing = ivans.post(f"/api/auth/accounts/{world.a.id}/owner", json={"user_id": people.fred})
    assert missing.status_code == 422
    reassigned = ivans.post(
        f"/api/auth/accounts/{world.a.id}/owner",
        json={"user_id": people.otto, "reason": "Olivia lost access to her email"},
    )
    assert reassigned.status_code == 200, reassigned.text
    assert reassigned.json()["previous_owner_id"] == people.olivia
    assert reassigned.json()["promoted_to_admin"] is True
    assert owner_of(world, world.a.id) == people.otto
    assert _role(world, people.otto, world.a.id) == "admin"
    [entry] = _audited(world, "account_owner_reassign")
    assert entry["detail"]["reason"] == "Olivia lost access to her email"
    assert entry["detail"]["previous_owner_id"] == people.olivia

    # The command line does the same, by email address.
    store_audit: list[dict] = []
    service = UserAdminCommandService(
        get_logger=lambda: world.store,
        log_auth_audit_event_fn=lambda _bl, **kwargs: store_audit.append(kwargs),
    )
    result = service.reassign_owner(
        UserOwnerReassignInput(
            email=username(world, people.olivia),
            account_id=world.a.id,
            reason="Olivia is back",
        )
    )
    assert (result.owner_user_id, result.previous_owner_id) == (people.olivia, people.otto)
    assert owner_of(world, world.a.id) == people.olivia
    assert [entry["action"] for entry in store_audit] == ["account_owner_reassign"]


def test_recovery_needs_an_active_member_of_that_company(world: World, people: People) -> None:
    ivan = person(world, "ivan", DEFAULT_ACCOUNT_ID, {DEFAULT_ACCOUNT_ID: "admin"})
    stranger = person(world, "stranger", world.b.id, {world.b.id: "admin"})
    ivans = _browser(world, ivan, DEFAULT_ACCOUNT_ID)

    refused = ivans.post(
        f"/api/auth/accounts/{world.a.id}/owner",
        json={"user_id": stranger, "reason": "wrong company"},
    )
    assert refused.status_code == 400, refused.text
    assert owner_of(world, world.a.id) == people.olivia
