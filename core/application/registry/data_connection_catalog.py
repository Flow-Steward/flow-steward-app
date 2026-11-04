"""Catalog secrets in workspace data connections (ADR 0057).

A data connection keeps ``secret_refs_jsonb = {"refs": {field: storage_key}}`` and
reads each value by its key when it connects. Choosing a catalog secret for a field
puts the secret's own storage key there and records the binding, so a value replaced
in Registry → Secrets reaches the connection at its next use and the Secrets page
lists the use. A value typed into a field later ends that field's binding.
"""

from __future__ import annotations

import json
from typing import Any

from core.application.secrets import reference_consumers

KIND = "data_connection"


def _refs(cur: Any, connection_id: str) -> dict[str, str]:
    cur.execute(
        "SELECT secret_refs_jsonb FROM workspace_data_connections WHERE connection_id = %s",
        (connection_id,),
    )
    row = cur.fetchone()
    value = (row.get("secret_refs_jsonb") if isinstance(row, dict) else (row or [None])[0]) or {}
    if isinstance(value, str):
        value = json.loads(value or "{}")
    refs = value.get("refs") if isinstance(value, dict) else None
    return {str(k): str(v) for k, v in (refs or {}).items()} if isinstance(refs, dict) else {}


def apply_secret_choices(
    conn: Any,
    *,
    scope: Any,
    connection_id: str,
    choices: dict[str, str],
    typed: set[str],
) -> None:
    """Bind chosen fields, unbind fields typed or cleared, and rewrite the refs map."""
    with conn.cursor() as cur:
        account_id = str(getattr(scope, "account_id", "") or "")
        bound = reference_consumers.bound_fields(
            cur, account_id=account_id, kind=KIND, consumer_id=connection_id
        )
        refs = _refs(cur, connection_id)
        for field in sorted(typed | {name for name, secret_id in choices.items() if not secret_id}):
            if field in bound:
                reference_consumers.unbind(
                    cur, account_id=account_id, kind=KIND, consumer_id=connection_id, field=field
                )
            if field not in typed:
                refs.pop(field, None)
        for field, secret_id in sorted(choices.items()):
            if not secret_id:
                continue
            refs[field] = reference_consumers.bind(
                cur,
                scope,
                kind=KIND,
                consumer_id=connection_id,
                field=field,
                secret_id=secret_id,
            )
        cur.execute(
            "UPDATE workspace_data_connections SET secret_refs_jsonb = %s::jsonb "
            "WHERE connection_id = %s",
            (json.dumps({"refs": refs} if refs else {}), connection_id),
        )


def secret_choices_by_connection(cur: Any, *, account_id: str, connection_ids: list[str]) -> dict:
    """connection id -> {field: catalog secret id}, for the editor to show the choices."""
    if not connection_ids:
        return {}
    cur.execute(
        """
        SELECT data_connection_id, field_name, secret_id::text
          FROM catalog_secret_bindings
         WHERE consumer_kind = 'data_connection' AND account_id::text = %s
           AND data_connection_id = ANY(%s)
        """,
        (str(account_id), list(connection_ids)),
    )
    choices: dict[str, dict[str, str]] = {}
    for row in cur.fetchall() or []:
        values = list(row.values()) if isinstance(row, dict) else list(row)
        choices.setdefault(str(values[0]), {})[str(values[1])] = str(values[2])
    return choices


__all__ = ["apply_secret_choices", "secret_choices_by_connection"]
