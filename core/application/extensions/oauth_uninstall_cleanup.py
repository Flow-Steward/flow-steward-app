from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .uninstall_inventory_queries import uninstall_inventory_row
from .uninstall_models import RemovalTransactionState, UninstallContext, UninstallTarget


def cleanup_extension_oauth_configurations(
    context: UninstallContext,
    target: UninstallTarget,
    state: RemovalTransactionState,
    cur: Any,
    removed_data: list[str],
    *,
    record_removed: Callable[..., None],
) -> None:
    bl = context.bl
    if (
        getattr(bl, "_table_exists", lambda *_args: False)("extension_oauth_configurations")
        is not True
    ):
        return
    scope_sql = "extension_install_id = %s AND extension_id = %s"
    params: list[Any] = [state.install_id, context.extension_id]
    if not target.include_consumer_accounts:
        scope_sql += " AND account_id::text = %s"
        params.append(context.account_id)
    cur.execute(
        f"SELECT configuration_id, account_id::text AS account_id, secret_refs FROM extension_oauth_configurations WHERE {scope_sql} FOR UPDATE",  # nosec B608
        tuple(params),
    )
    configurations = [
        uninstall_inventory_row(raw, ("configuration_id", "account_id", "secret_refs"))
        for raw in cur.fetchall() or []
    ]
    for configuration in configurations:
        configuration_id = str(configuration.get("configuration_id") or "").strip()
        account_id = str(configuration.get("account_id") or "").strip()
        if getattr(bl, "_table_exists", lambda *_args: False)("secret_refs") is True:
            owned_prefix = f"extension_oauth_configuration:{configuration_id}:"
            cur.execute(
                "DELETE FROM secret_refs WHERE account_id::text = %s AND left(ref_key, length(%s)) = %s",
                (account_id, owned_prefix, owned_prefix),
            )
    if (
        getattr(bl, "_table_exists", lambda *_args: False)("extension_oauth_authorization_states")
        is True
    ):
        cur.execute(
            f"DELETE FROM extension_oauth_authorization_states WHERE {scope_sql}",  # nosec B608
            tuple(params),
        )
        record_removed(cur, removed_data, "oauth_authorization_states")
    cur.execute(
        f"DELETE FROM extension_oauth_configurations WHERE {scope_sql}",  # nosec B608
        tuple(params),
    )
    record_removed(cur, removed_data, "oauth_configurations")
    if configurations:
        removed_data.append("oauth_configuration_credentials")
