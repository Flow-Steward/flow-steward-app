from __future__ import annotations

from typing import Any

from .uninstall_inventory_queries import uninstall_inventory_row


def collect_uninstall_connection_rows(
    bl: Any,
    *,
    account_id: str,
    extension_id: str,
    install_id: str,
    include_consumer_accounts: bool,
) -> list[dict[str, Any]]:
    cursor_factory = getattr(bl, "cursor", None)
    if not callable(cursor_factory):
        cursor_factory = getattr(getattr(bl, "conn", None), "cursor", None)
    if (
        include_consumer_accounts
        and callable(cursor_factory)
        and getattr(bl, "_table_exists", lambda *_args: False)("project_connections")
    ):
        with cursor_factory() as cur:
            cur.execute(
                "SELECT connection_id, account_id::text AS account_id, project_id::text AS project_id, display_name, secret_refs FROM project_connections WHERE extension_install_id = %s OR (account_id::text = %s AND extension_install_id IS NULL AND extension_id = %s) ORDER BY account_id, connection_id",
                (install_id, account_id, extension_id),
            )
            rows = cur.fetchall() or []
        columns = ("connection_id", "account_id", "project_id", "display_name", "secret_refs")
        return [uninstall_inventory_row(row, columns) for row in rows]
    if (
        getattr(bl, "conn", None) is not None
        and callable(cursor_factory)
        and getattr(bl, "_table_exists", lambda *_args: False)("project_connections")
    ):
        with cursor_factory() as cur:
            cur.execute(
                "SELECT connection_id, account_id::text AS account_id, project_id::text AS project_id, display_name, secret_refs FROM project_connections WHERE account_id::text = %s AND ((connection_kind = 'extension_runtime' AND extension_install_id = %s) OR (connection_kind = 'extension' AND (extension_install_id = %s OR (extension_install_id IS NULL AND extension_id = %s)))) ORDER BY connection_id",
                (account_id, install_id, install_id, extension_id),
            )
            rows = cur.fetchall() or []
        columns = ("connection_id", "account_id", "project_id", "display_name", "secret_refs")
        return [uninstall_inventory_row(row, columns) for row in rows]
    from core.infrastructure.db.extension_runtime_connection_store import (
        list_extension_runtime_connections,
    )

    return list_extension_runtime_connections(
        bl, account_id=account_id, install_id=install_id, enabled_only=False
    )
