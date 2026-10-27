from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

from core.application.extensions.oauth_uninstall_cleanup import (
    cleanup_extension_oauth_configurations,
)
from core.application.extensions.oauth_uninstall_inventory import (
    collect_oauth_configuration_inventory,
)
from core.application.extensions.oauth_uninstall_lifecycle import (
    GenericOAuthUninstallLifecycle,
)
from core.application.extensions.uninstall_models import (
    RemovalTransactionState,
    UninstallTarget,
)


class _Store:
    def __init__(self, cursor: MagicMock) -> None:
        self._cursor = cursor

    @staticmethod
    def _table_exists(table_name: str) -> bool:
        return table_name in {
            "extension_oauth_configurations",
            "extension_oauth_authorization_states",
            "secret_refs",
        }

    @contextmanager
    def cursor(self):
        yield self._cursor


def test_uninstall_inventory_is_exact_install_extension_account_and_redacted() -> None:
    cursor = MagicMock()
    cursor.fetchall.return_value = [
        (
            "configuration-1",
            "Primary",
            {
                "refs": {
                    "client_secret": ("extension_oauth_configuration:configuration-1:client_secret")
                }
            },
        )
    ]

    items = collect_oauth_configuration_inventory(
        _Store(cursor),
        account_id="account-1",
        extension_id="acme.oauth-extension",
        install_id="install-1",
        include_consumer_accounts=False,
    )

    sql, params = cursor.execute.call_args.args
    assert "extension_install_id = %s AND extension_id = %s" in sql
    assert "account_id::text = %s" in sql
    assert params == ("install-1", "acme.oauth-extension", "account-1")
    assert items == [
        {
            "kind": "oauth_configuration",
            "id": "configuration-1",
            "display_name": "Primary",
            "count": 1,
        },
        {
            "kind": "oauth_configuration_credentials",
            "id": "configuration-1",
            "count": 1,
        },
    ]
    assert "client_secret" not in repr(items)


def test_uninstall_cleanup_deletes_only_deterministically_owned_configuration_secrets() -> None:
    cursor = MagicMock()
    cursor.fetchall.return_value = [
        (
            "configuration-1",
            "account-1",
            {
                "refs": {
                    "client_secret": (
                        "extension_oauth_configuration:configuration-1:client_secret"
                    ),
                    "crafted": "foreign:must-survive",
                }
            },
        )
    ]
    cursor.rowcount = 1
    context = SimpleNamespace(
        bl=_Store(cursor), account_id="account-1", extension_id="acme.oauth-extension"
    )
    state = RemovalTransactionState(
        install_id="install-1",
        release_id="release-1",
        remove_shared_runtime_package=True,
        affected_state_persisted=False,
    )
    removed: list[str] = []

    cleanup_extension_oauth_configurations(
        context,
        SimpleNamespace(include_consumer_accounts=False),
        state,
        cursor,
        removed,
        record_removed=lambda _cur, values, kind: values.append(kind),
    )

    calls = cursor.execute.call_args_list
    secret_delete = next(call for call in calls if "DELETE FROM secret_refs" in call.args[0])
    assert secret_delete.args[1] == (
        "account-1",
        "extension_oauth_configuration:configuration-1:",
        "extension_oauth_configuration:configuration-1:",
    )
    assert "left(ref_key, length(%s)) = %s" in secret_delete.args[0]
    assert "foreign:must-survive" not in repr(calls)
    scoped_deletes = [
        call for call in calls if "extension_oauth_" in call.args[0] and "DELETE" in call.args[0]
    ]
    assert all(
        "extension_install_id = %s AND extension_id = %s" in call.args[0] for call in scoped_deletes
    )
    assert removed == [
        "oauth_authorization_states",
        "oauth_configurations",
        "oauth_configuration_credentials",
    ]


def test_non_oauth_install_does_not_inherit_status_from_global_table_existence() -> None:
    transaction_conn = MagicMock()
    cursor = transaction_conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = []
    bl = MagicMock()
    bl._table_exists.return_value = True
    bl.get_extension_install_for_operator.return_value = {
        "extension_id": "acme.non-oauth",
        "release_id": "release-1",
        "compiled_manifest": {"extension_id": "acme.non-oauth"},
    }

    @contextmanager
    def transaction(_bl):
        yield transaction_conn

    prepared = GenericOAuthUninstallLifecycle(lifecycle_transaction=transaction).prepare(
        SimpleNamespace(
            bl=bl,
            account_id="account-1",
            extension_id="acme.non-oauth",
            confirmed_plan={"install_id": "install-1"},
        ),
        UninstallTarget({}, "install-1", "release-1", False),
        validate_locked_plan=lambda *_args: None,
    )

    assert prepared.owns_oauth is False
    assert prepared.revocations == ()
