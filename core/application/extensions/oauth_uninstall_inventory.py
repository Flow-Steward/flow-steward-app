from __future__ import annotations

from typing import Any

from .uninstall_inventory_queries import uninstall_inventory_row


def collect_oauth_configuration_inventory(
    bl: Any,
    *,
    account_id: str,
    extension_id: str,
    install_id: str,
    include_consumer_accounts: bool,
) -> list[dict[str, Any]]:
    cursor_factory = getattr(bl, "cursor", None)
    if not callable(cursor_factory) or (
        getattr(bl, "_table_exists", lambda *_args: False)("extension_oauth_configurations")
        is not True
    ):
        return []
    scope = "extension_install_id = %s AND extension_id = %s"
    params: list[Any] = [install_id, extension_id]
    if not include_consumer_accounts:
        scope += " AND account_id::text = %s"
        params.append(account_id)
    with cursor_factory() as cur:
        cur.execute(
            f"SELECT configuration_id, display_name, secret_refs FROM extension_oauth_configurations WHERE {scope} ORDER BY configuration_id",  # nosec B608
            tuple(params),
        )
        rows = [
            uninstall_inventory_row(raw, ("configuration_id", "display_name", "secret_refs"))
            for raw in cur.fetchall() or []
        ]
    items: list[dict[str, Any]] = []
    for row in rows:
        configuration_id = str(row.get("configuration_id") or "").strip()
        refs = row.get("secret_refs")
        refs = refs if isinstance(refs, dict) else {}
        owned_refs = refs.get("refs") if isinstance(refs.get("refs"), dict) else {}
        items.append(
            {
                "kind": "oauth_configuration",
                "id": configuration_id,
                "display_name": str(row.get("display_name") or configuration_id),
                "count": 1,
            }
        )
        if owned_refs:
            items.append(
                {
                    "kind": "oauth_configuration_credentials",
                    "id": configuration_id,
                    "count": len(owned_refs),
                }
            )
    return items
