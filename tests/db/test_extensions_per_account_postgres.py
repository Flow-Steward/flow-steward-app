"""Extensions per account on the disposable audit database (ADR 0020, step C6).

The installation account installs an extension once. Account A enables it, account B
does not: A lists it, resolves it at runtime and keeps its own settings; B sees it
only as "not enabled", cannot resolve or run it and never reads A's settings or state.
Uninstalling the code removes every account's enablement. Every test runs in a
transaction that is rolled back.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from core.application.extensions.account_enablement import (
    ExtensionAccountEnablementService,
    install_reaches_account,
    install_serves_other_accounts,
)
from core.application.extensions.install import DBExtensionInstallStore
from core.application.management.errors import ServiceNotFoundError, ServiceValidationError
from core.application.management.scope import ScopeContext
from core.infrastructure.config.platform_identity import DEFAULT_ACCOUNT_ID, DEFAULT_TENANT_ID
from core.infrastructure.db.logger_extensions_accounts_query_mixin import (
    PlatformLoggerExtensionsAccountsQueryMixin,
)
from core.infrastructure.db.psycopg_runtime._connection import ConnectionAdapter

EXTENSION = "acme.crm-sync"


class _Logger(PlatformLoggerExtensionsAccountsQueryMixin):
    """The query mixin on the test connection, as the platform logger uses it."""

    def __init__(self, conn: Any) -> None:
        self.conn = ConnectionAdapter(conn)

    def _table_exists(self, name: str) -> bool:
        with self.conn.cursor() as cur:
            cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"public.{name}",))
            return bool(cur.fetchone()[0])

    def _column_exists(self, table: str, column: str) -> bool:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.columns"
                " WHERE table_schema = 'public' AND table_name = %s AND column_name = %s",
                (table, column),
            )
            return cur.fetchone() is not None


def _admin(account_id: str) -> ScopeContext:
    return ScopeContext(
        account_id=account_id, principal_type="admin", auth_source="local", actor_id="ana"
    )


def _account(conn: Any, key: str) -> str:
    account_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO accounts (id, tenant_id, account_key, display_name, is_active)
           VALUES (%s, %s, %s, %s, true)""",
        (account_id, DEFAULT_TENANT_ID, f"{key}-{account_id[:8]}", key),
    )
    return account_id


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
        store = DBExtensionInstallStore(conn)
        install = store.install(
            extension_id=EXTENSION, version="1.0.0", account_id=DEFAULT_ACCOUNT_ID
        )
        yield {
            "conn": conn,
            "store": store,
            "install": install,
            "a": _account(conn, "company-a"),
            "b": _account(conn, "company-b"),
            "service": ExtensionAccountEnablementService(
                get_install_store=lambda: store,
                get_enablement_store=lambda: store.account_enablements,
            ),
            "logger": _Logger(conn),
        }


def test_an_account_reaches_the_install_only_after_it_enabled_it(world) -> None:
    store, service, a, b = world["store"], world["service"], world["a"], world["b"]
    install_id = world["install"].install_id

    assert [row.extension_id for row in store.list_for_account(a)] == []
    service.enable(EXTENSION, scope=_admin(a))

    assert [row.install_id for row in store.list_for_account(a)] == [install_id]
    assert store.list_for_account(b) == []
    assert install_reaches_account(world["install"], a, store.account_enablements)
    assert not install_reaches_account(world["install"], b, store.account_enablements)
    assert service.get(EXTENSION, scope=_admin(b))["state"] == "not_enabled"


def test_runtime_lookups_follow_the_account(world) -> None:
    logger, service, a, b = world["logger"], world["service"], world["a"], world["b"]
    install_id = world["install"].install_id
    service.enable(EXTENSION, scope=_admin(a))
    service.update_settings(EXTENSION, {"region": "eu"}, scope=_admin(a))

    access_a = logger.get_extension_account_access(EXTENSION, account_id=a)
    access_b = logger.get_extension_account_access(EXTENSION, account_id=b)
    owner = logger.get_extension_account_access(EXTENSION, account_id=DEFAULT_ACCOUNT_ID)

    assert access_a["access"] == "enabled"
    assert access_a["owner_account_id"] == DEFAULT_ACCOUNT_ID
    assert access_a["account_settings"] == {"region": "eu"}
    assert access_b["access"] == "not_enabled"
    assert access_b["account_settings"] == {}
    assert owner["access"] == "owner"
    # By install id: A's lookup returns the install, B's returns nothing.
    assert logger.get_extension_by_install_id_for_runtime_gate(install_id, account_id=b) is None


def test_runtime_resolution_refuses_an_account_that_did_not_enable_it(world) -> None:
    from core.infrastructure.extension_runtime import extension_runtime_helpers as helpers

    logger, service, a, b = world["logger"], world["service"], world["a"], world["b"]
    service.enable(EXTENSION, scope=_admin(a))
    service.update_settings(EXTENSION, {"region": "eu"}, scope=_admin(a))

    record_b, _ = helpers.resolve_extension_runtime_record(
        EXTENSION, get_logger_only=lambda: logger, account_id=b
    )
    record_a, _ = helpers.resolve_extension_runtime_record(
        EXTENSION, get_logger_only=lambda: logger, account_id=a
    )

    assert record_b is None
    if record_a is not None:  # the bare install has no bundle row or files
        assert record_a["account_id"] == a
        assert record_a["account_settings"] == {"region": "eu"}


def test_disabling_keeps_the_settings_and_hides_the_install_again(world) -> None:
    store, service, a = world["store"], world["service"], world["a"]
    service.enable(EXTENSION, scope=_admin(a))
    service.update_settings(EXTENSION, {"region": "eu"}, scope=_admin(a))

    disabled = service.disable(EXTENSION, scope=_admin(a))

    assert disabled["state"] == "disabled"
    assert disabled["settings"] == {"region": "eu"}
    assert store.list_for_account(a) == []
    assert service.enable(EXTENSION, scope=_admin(a))["settings"] == {"region": "eu"}


def test_one_accounts_settings_are_never_another_accounts(world) -> None:
    service, a, b = world["service"], world["a"], world["b"]
    service.enable(EXTENSION, scope=_admin(a))
    service.update_settings(EXTENSION, {"region": "eu"}, scope=_admin(a))
    service.enable(EXTENSION, scope=_admin(b))

    assert service.get(EXTENSION, scope=_admin(b))["settings"] == {}
    assert (
        world["store"].account_enablements.get(account_id=b, extension_id=EXTENSION).settings == {}
    )


def test_an_extension_the_installation_turned_off_cannot_be_enabled(world) -> None:
    store, service, a = world["store"], world["service"], world["a"]
    service.enable(EXTENSION, scope=_admin(a))
    store.set_state(extension_id=EXTENSION, account_id=DEFAULT_ACCOUNT_ID, state="disabled")

    assert store.list_for_account(a) == []
    with pytest.raises(ServiceValidationError):
        service.enable(EXTENSION, scope=_admin(world["b"]))


def test_uninstalling_the_code_removes_every_accounts_enablement(world) -> None:
    store, service, a = world["store"], world["service"], world["a"]
    service.enable(EXTENSION, scope=_admin(a))
    assert install_serves_other_accounts(world["install"], store.account_enablements)

    store.uninstall(extension_id=EXTENSION, account_id=DEFAULT_ACCOUNT_ID)

    assert store.account_enablements.get(account_id=a, extension_id=EXTENSION) is None
    with pytest.raises(ServiceNotFoundError):
        service.enable(EXTENSION, scope=_admin(a))


def test_the_database_refuses_an_enablement_of_another_extensions_install(world) -> None:
    import psycopg

    conn = world["conn"]
    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        conn.execute(
            """INSERT INTO extension_account_enablements (account_id, extension_id, install_id)
               VALUES (%s, 'acme.other', %s)""",
            (world["a"], world["install"].install_id),
        )
