"""Catalog secrets in consumers that read a stored value by its storage key (ADR 0057).

Data connections, MCP connectors and LLM provider installs keep a map of
``field -> storage key`` and read the value behind each key when they run. Choosing
a catalog secret for one of their fields puts the secret's own storage key in that
map, so a value replaced in Registry → Secrets reaches them at their next use, and
records a binding, so the Secrets page lists the use and a delete plan names it.

Only an administrator or operator configures these consumers, and the value never
leaves the host: the consumer reads it at its own execution boundary, as it reads a
value typed into the same field. They have no separate approval step.
"""

from __future__ import annotations

from typing import Any

from core.application.management.errors import ServiceNotFoundError, ServiceValidationError
from core.application.management.scope import ScopeContext
from core.application.secrets import repository
from core.application.secrets.authority import actor_identity, require_secret_bind

#: consumer kind -> the binding column that names the consumer
KINDS: dict[str, str] = {
    "data_connection": "data_connection_id",
    "mcp_connector": "mcp_connector_id",
    "llm_provider": "llm_provider_install_id",
}

# Fixed statements per kind: no SQL is assembled at run time.
_UPSERT = {
    "data_connection": """
        INSERT INTO catalog_secret_bindings (
            account_id, secret_id, consumer_kind, data_connection_id, field_name, created_by
        ) VALUES (%s, %s, 'data_connection', %s, %s, %s)
        ON CONFLICT (data_connection_id, field_name)
        DO UPDATE SET secret_id = EXCLUDED.secret_id,
                      created_at = now(), created_by = EXCLUDED.created_by
    """,
    "mcp_connector": """
        INSERT INTO catalog_secret_bindings (
            account_id, secret_id, consumer_kind, mcp_connector_id, field_name, created_by
        ) VALUES (%s, %s, 'mcp_connector', %s, %s, %s)
        ON CONFLICT (mcp_connector_id, field_name)
        DO UPDATE SET secret_id = EXCLUDED.secret_id,
                      created_at = now(), created_by = EXCLUDED.created_by
    """,
    "llm_provider": """
        INSERT INTO catalog_secret_bindings (
            account_id, secret_id, consumer_kind, llm_provider_install_id, field_name, created_by
        ) VALUES (%s, %s, 'llm_provider', %s, %s, %s)
        ON CONFLICT (llm_provider_install_id, field_name)
        DO UPDATE SET secret_id = EXCLUDED.secret_id,
                      created_at = now(), created_by = EXCLUDED.created_by
    """,
}
_DELETE = {
    "data_connection": """
        DELETE FROM catalog_secret_bindings
         WHERE consumer_kind = 'data_connection' AND data_connection_id = %s
           AND field_name = %s AND account_id::text = %s
    """,
    "mcp_connector": """
        DELETE FROM catalog_secret_bindings
         WHERE consumer_kind = 'mcp_connector' AND mcp_connector_id = %s
           AND field_name = %s AND account_id::text = %s
    """,
    "llm_provider": """
        DELETE FROM catalog_secret_bindings
         WHERE consumer_kind = 'llm_provider' AND llm_provider_install_id = %s
           AND field_name = %s AND account_id::text = %s
    """,
}
_SELECT = {
    "data_connection": """
        SELECT field_name, secret_id::text FROM catalog_secret_bindings
         WHERE consumer_kind = 'data_connection' AND data_connection_id = %s AND account_id::text = %s
    """,
    "mcp_connector": """
        SELECT field_name, secret_id::text FROM catalog_secret_bindings
         WHERE consumer_kind = 'mcp_connector' AND mcp_connector_id = %s AND account_id::text = %s
    """,
    "llm_provider": """
        SELECT field_name, secret_id::text FROM catalog_secret_bindings
         WHERE consumer_kind = 'llm_provider' AND llm_provider_install_id = %s AND account_id::text = %s
    """,
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _audit(cur: Any, scope: ScopeContext, command: str, target: dict[str, Any]) -> None:
    kind, actor = actor_identity(scope)
    repository.record_audit(
        cur,
        account_id=scope.account_id,
        command_type=f"secret.{command}",
        actor_kind=kind,
        actor_id=actor,
        target=target,
        payload={},
    )


def _kind(kind: str) -> str:
    if kind not in KINDS:
        raise ServiceValidationError(f"Unknown secret consumer kind '{kind}'")
    return kind


def bound_fields(cur: Any, *, account_id: str, kind: str, consumer_id: str) -> dict[str, str]:
    """field -> catalog secret id for one consumer."""
    cur.execute(_SELECT[_kind(kind)], (_text(consumer_id), _text(account_id)))
    rows = cur.fetchall() or []
    return {
        _text(row[0] if not isinstance(row, dict) else row["field_name"]): _text(
            row[1] if not isinstance(row, dict) else row["secret_id"]
        )
        for row in rows
    }


def bind(
    cur: Any,
    scope: ScopeContext,
    *,
    kind: str,
    consumer_id: str,
    field: str,
    secret_id: str,
) -> str:
    """Bind one field to a catalog secret; return the storage key the consumer must keep."""
    require_secret_bind(scope)
    field_name = _text(field)
    if not field_name or len(field_name) > 200 or field_name.startswith("_oauth_"):
        raise ServiceValidationError("field: a credential field name is required")
    secret = repository.get_secret(
        cur, account_id=scope.account_id, secret_id=_text(secret_id), for_update=True
    )
    if not secret:
        raise ServiceNotFoundError("Secret not found")
    _kind_name, actor = actor_identity(scope)
    cur.execute(
        _UPSERT[_kind(kind)],
        (scope.account_id, secret["id"], _text(consumer_id), field_name, actor),
    )
    _audit(
        cur,
        scope,
        "bind",
        {
            "secret_id": secret["id"],
            "consumer_kind": kind,
            "consumer_id": _text(consumer_id),
            "field": field_name,
        },
    )
    return _text(secret["storage_ref_key"])


def unbind(cur: Any, *, account_id: str, kind: str, consumer_id: str, field: str) -> bool:
    cur.execute(_DELETE[_kind(kind)], (_text(consumer_id), _text(field), _text(account_id)))
    return bool(getattr(cur, "rowcount", 0))


def is_catalog_storage_key(cur: Any, *, account_id: str, ref_key: str) -> bool:
    cur.execute(
        "SELECT 1 FROM catalog_secrets WHERE account_id::text = %s AND storage_ref_key = %s",
        (_text(account_id), _text(ref_key)),
    )
    return cur.fetchone() is not None


#: kind -> SQL that removes one field from the consumer's own storage-key map
_RELEASE = {
    "data_connection": """
        UPDATE workspace_data_connections
           SET secret_refs_jsonb = jsonb_set(
                   COALESCE(secret_refs_jsonb, '{}'::jsonb), '{refs}',
                   COALESCE(secret_refs_jsonb->'refs', '{}'::jsonb) - %s)
         WHERE connection_id = %s AND account_id::text = %s
    """,
    "mcp_connector": """
        UPDATE mcp_connectors
           SET header_secret_refs_jsonb = CASE
                   WHEN (COALESCE(header_secret_refs_jsonb->'refs', '{}'::jsonb) - %s) = '{}'::jsonb
                   THEN '{}'::jsonb
                   ELSE jsonb_build_object(
                       'refs', COALESCE(header_secret_refs_jsonb->'refs', '{}'::jsonb) - %s)
               END
         WHERE connector_id = %s AND account_id::text = %s
    """,
}


def release(cur: Any, scope: ScopeContext, usage: dict[str, Any]) -> None:
    """Before a confirmed delete: the consumer stops using the secret and keeps no key to it."""
    kind = _kind(_text(usage.get("consumer_kind")))
    consumer_id = _text(usage.get(KINDS[kind]))
    field = _text(usage.get("field_name"))
    if kind == "data_connection":
        cur.execute(_RELEASE[kind], (field, consumer_id, scope.account_id))
    elif kind == "mcp_connector":
        cur.execute(_RELEASE[kind], (field, field, consumer_id, scope.account_id))
    elif kind == "llm_provider":
        from core.application.settings.llm_provider_catalog_refs import release_catalog_ref

        release_catalog_ref(
            cur, account_id=scope.account_id, install_id=consumer_id, secret_name=field
        )
    unbind(cur, account_id=scope.account_id, kind=kind, consumer_id=consumer_id, field=field)


__all__ = [
    "KINDS",
    "bind",
    "bound_fields",
    "is_catalog_storage_key",
    "release",
    "unbind",
]
