"""Extension install requests on the disposable audit database (ADR 0084).

Company A and company B ask the installation administrator for the same catalog
extension. The database keeps one pending request per account and extension, the
listing joins the company and the person, another account's request is answered as
missing, and approving after the install enables the extension for the asking company
only. Every test runs in a transaction that is rolled back.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from core.application.extensions.account_enablement import ExtensionAccountEnablementService
from core.application.extensions.install import DBExtensionInstallStore
from core.application.extensions.install_requests import (
    DBExtensionInstallRequestStore,
    ExtensionInstallRequestService,
)
from core.application.management.errors import (
    ServiceConflictError,
    ServiceNotFoundError,
)
from core.application.management.scope import ScopeContext
from core.infrastructure.config.platform_identity import DEFAULT_ACCOUNT_ID, DEFAULT_TENANT_ID

EXTENSION = "acme.crm-sync"


def _account(conn: Any, key: str) -> str:
    account_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO accounts (id, tenant_id, account_key, display_name, is_active)
           VALUES (%s, %s, %s, %s, true)""",
        (account_id, DEFAULT_TENANT_ID, f"{key}-{account_id[:8]}", key),
    )
    return account_id


def _person(conn: Any, name: str, account_id: str) -> str:
    user_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO users (id, username, password_hash, display_name, account_id)
           VALUES (%s, %s, 'x', %s, %s)""",
        (user_id, f"{name}-{user_id[:8]}@example.invalid", name, account_id),
    )
    return user_id


def _admin(account_id: str, user_id: str) -> ScopeContext:
    return ScopeContext(
        account_id=account_id,
        principal_type="admin",
        auth_source="local",
        actor_id=user_id,
        user_id=user_id,
    )


class _Audit:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def log_action(self, **values: Any) -> None:
        self.rows.append(values)


@pytest.fixture
def world(disposable_db_conn) -> Iterator[dict[str, Any]]:
    conn = disposable_db_conn
    with conn.transaction(force_rollback=True):
        conn.execute(
            """INSERT INTO accounts (id, tenant_id, account_key, display_name, is_active)
               VALUES (%s, %s, 'default', 'Default Account', true)
               ON CONFLICT DO NOTHING""",
            (DEFAULT_ACCOUNT_ID, DEFAULT_TENANT_ID),
        )
        installs = DBExtensionInstallStore(conn)
        requests = DBExtensionInstallRequestStore(conn)
        audit = _Audit()
        a = _account(conn, "Company A")
        b = _account(conn, "Company B")
        service = ExtensionInstallRequestService(
            get_store=lambda: requests,
            get_install_store=lambda: installs,
            get_enablement_service=lambda: ExtensionAccountEnablementService(
                get_install_store=lambda: installs,
                get_enablement_store=lambda: installs.account_enablements,
            ),
            catalog_name={EXTENSION: "CRM sync"}.get,
            get_audit_logger=lambda: audit,
        )
        yield {
            "conn": conn,
            "installs": installs,
            "requests": requests,
            "service": service,
            "audit": audit,
            "a": a,
            "b": b,
            "ann": _person(conn, "Ann", a),
            "ben": _person(conn, "Ben", b),
            "root": _person(conn, "Root", DEFAULT_ACCOUNT_ID),
        }


def test_two_companies_request_the_same_extension_and_see_only_their_own(world) -> None:
    service, a, b = world["service"], world["a"], world["b"]
    mine = service.create(EXTENSION, note="for orders", scope=_admin(a, world["ann"]))
    theirs = service.create(EXTENSION, scope=_admin(b, world["ben"]))

    assert [row["id"] for row in service.list_requests(scope=_admin(a, world["ann"]))["items"]] == [
        mine["id"]
    ]
    assert [row["id"] for row in service.list_requests(scope=_admin(b, world["ben"]))["items"]] == [
        theirs["id"]
    ]
    pending = service.list_requests(scope=_admin(DEFAULT_ACCOUNT_ID, world["root"]))["items"]
    by_id = {row["id"]: row for row in pending}
    assert set(by_id) == {mine["id"], theirs["id"]}
    assert by_id[mine["id"]]["account_name"] == "Company A"
    assert by_id[mine["id"]]["requested_by_name"] == "Ann"
    assert by_id[mine["id"]]["extension_name"] == "CRM sync"
    assert by_id[mine["id"]]["note"] == "for orders"


def test_the_database_keeps_one_pending_request_per_account_and_extension(world) -> None:
    service, a = world["service"], world["a"]
    service.create(EXTENSION, scope=_admin(a, world["ann"]))

    with pytest.raises(ServiceConflictError):
        service.create(EXTENSION, scope=_admin(a, world["ann"]))
    with pytest.raises(psycopg.errors.UniqueViolation), world["conn"].transaction():
        world["conn"].execute(
            """INSERT INTO extension_install_requests (account_id, extension_id)
               VALUES (%s, %s)""",
            (a, EXTENSION),
        )
    # A pending row cannot claim a decision time, an answered one must have one.
    with pytest.raises(psycopg.errors.CheckViolation), world["conn"].transaction():
        world["conn"].execute(
            """INSERT INTO extension_install_requests (account_id, extension_id, status)
               VALUES (%s, %s, 'approved')""",
            (a, "acme.other"),
        )


def test_another_account_cannot_cancel_a_request(world) -> None:
    service, a, b = world["service"], world["a"], world["b"]
    request = service.create(EXTENSION, scope=_admin(a, world["ann"]))

    with pytest.raises(ServiceNotFoundError):
        service.cancel(request["id"], scope=_admin(b, world["ben"]))
    assert world["requests"].get(request["id"]).status == "pending"
    cancelled = service.cancel(request["id"], scope=_admin(a, world["ann"]))
    assert cancelled["status"] == "cancelled"
    assert cancelled["decided_at"]


def test_approving_after_the_install_enables_only_the_asking_company(world) -> None:
    service, installs, a, b = world["service"], world["installs"], world["a"], world["b"]
    request = service.create(EXTENSION, scope=_admin(a, world["ann"]))
    other = service.create(EXTENSION, scope=_admin(b, world["ben"]))
    root = _admin(DEFAULT_ACCOUNT_ID, world["root"])

    with pytest.raises(ServiceConflictError):
        service.approve(request["id"], scope=root)
    assert world["requests"].get(request["id"]).status == "pending"

    install = installs.install(
        extension_id=EXTENSION, version="1.0.0", account_id=DEFAULT_ACCOUNT_ID
    )
    approved = service.approve(request["id"], scope=root)

    assert approved["status"] == "approved"
    assert world["requests"].get(request["id"]).decided_by == world["root"]
    assert [row.install_id for row in installs.list_for_account(a)] == [install.install_id]
    assert installs.list_for_account(b) == []
    assert world["requests"].get(other["id"]).status == "pending"
    declined = service.decline(other["id"], reason="Not for you", scope=root)
    assert declined["decline_reason"] == "Not for you"
    assert installs.list_for_account(b) == []


def test_deleting_an_account_removes_its_requests_and_a_person_leaves_the_record(world) -> None:
    service, a = world["service"], world["a"]
    request = service.create(EXTENSION, scope=_admin(a, world["ann"]))

    world["conn"].execute("DELETE FROM users WHERE id = %s", (world["ann"],))
    assert world["requests"].get(request["id"]).requested_by == ""
    world["conn"].execute("DELETE FROM user_account_memberships WHERE account_id = %s", (a,))
    world["conn"].execute("DELETE FROM accounts WHERE id = %s", (a,))
    assert world["requests"].get(request["id"]) is None
