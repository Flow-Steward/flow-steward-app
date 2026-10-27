"""Connection-scoped credential helpers.

Project connections store long-lived credentials as account-scoped secret refs.
The connection row keeps only the ref mapping; decrypted values are resolved by
host runtime code immediately before invoking a credentialed extension action.
"""

from __future__ import annotations

from typing import Any

from core.infrastructure.security import connection_secret_store as _secret_store

connection_secret_ref_key = _secret_store.connection_secret_ref_key
connection_secret_ref_map = _secret_store.connection_secret_ref_map
delete_connection_secret_refs = _secret_store.delete_connection_secret_refs
save_connection_secret = _secret_store.save_connection_secret
save_connection_secrets = _secret_store.save_connection_secrets


def secret_ref_names(secret_refs: Any) -> list[str]:
    return list(_secret_store.normalize_connection_secret_refs(secret_refs))


def resolve_connection_secrets(
    conn: Any,
    *,
    account_id: str,
    secret_refs: Any,
    include_internal: bool = False,
) -> dict[str, str]:
    refs = _secret_store.normalize_connection_secret_refs(secret_refs)
    if not include_internal:
        refs = {name: ref_key for name, ref_key in refs.items() if not name.startswith("_oauth_")}
    return _secret_store.resolve_connection_secrets(
        conn,
        account_id=account_id,
        secret_refs={"refs": refs},
    )
