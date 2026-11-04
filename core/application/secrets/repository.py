"""SQL for catalog secrets, their bindings, approvals and audit rows (ADR 0057).

Every function takes a cursor from the caller's transaction. Values never pass
through here: they are written to `secret_refs` by the storage helpers and read by
the stored-secret reader.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

_SECRET_COLUMNS = (
    "id",
    "account_id",
    "name",
    "description",
    "storage_ref_key",
    "created_at",
    "created_by",
    "metadata_updated_at",
    "metadata_updated_by",
    "value_updated_at",
    "value_updated_by",
)


def _row(cur: Any, columns: tuple[str, ...]) -> dict[str, Any] | None:
    row = cur.fetchone()
    if row is None:
        return None
    if isinstance(row, dict):
        return {column: row.get(column) for column in columns}
    return dict(zip(columns, row, strict=False))


def _rows(cur: Any, columns: tuple[str, ...]) -> list[dict[str, Any]]:
    result = []
    for row in cur.fetchall() or []:
        if isinstance(row, dict):
            result.append({column: row.get(column) for column in columns})
        else:
            result.append(dict(zip(columns, row, strict=False)))
    return result


def project_scope(cur: Any, *, account_id: str, project_id: str) -> dict[str, Any] | None:
    cur.execute(
        """
        SELECT id::text, workspace_id::text
          FROM projects
         WHERE id::text = %s AND account_id::text = %s AND deleted_at IS NULL
        """,
        (str(project_id), str(account_id)),
    )
    return _row(cur, ("project_id", "workspace_id"))


def insert_secret(
    cur: Any,
    *,
    account_id: str,
    name: str,
    description: str,
    actor: str,
    storage_ref_key: str | None = None,
) -> dict[str, Any]:
    secret_id = str(uuid.uuid4())
    cur.execute(
        """
        INSERT INTO catalog_secrets AS s (
            id, account_id, name, description, storage_ref_key,
            created_by, metadata_updated_by, value_updated_by
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING s.id, s.account_id, s.name, s.description, s.storage_ref_key,
               s.created_at, s.created_by, s.metadata_updated_at, s.metadata_updated_by,
               s.value_updated_at, s.value_updated_by
        """,
        (
            secret_id,
            account_id,
            name,
            description,
            storage_ref_key or f"catalog_secret:{secret_id}",
            actor,
            actor,
            actor,
        ),
    )
    row = _row(cur, _SECRET_COLUMNS)
    assert row is not None
    return row


def get_secret(
    cur: Any, *, account_id: str, secret_id: str, for_update: bool = False
) -> dict[str, Any] | None:
    if for_update:
        cur.execute(
            """
            SELECT s.id, s.account_id, s.name, s.description, s.storage_ref_key,
               s.created_at, s.created_by, s.metadata_updated_at, s.metadata_updated_by,
               s.value_updated_at, s.value_updated_by
              FROM catalog_secrets AS s
             WHERE s.id::text = %s AND s.account_id::text = %s
             FOR UPDATE
            """,
            (str(secret_id), str(account_id)),
        )
    else:
        cur.execute(
            """
            SELECT s.id, s.account_id, s.name, s.description, s.storage_ref_key,
               s.created_at, s.created_by, s.metadata_updated_at, s.metadata_updated_by,
               s.value_updated_at, s.value_updated_by
              FROM catalog_secrets AS s
             WHERE s.id::text = %s AND s.account_id::text = %s
            """,
            (str(secret_id), str(account_id)),
        )
    return _row(cur, _SECRET_COLUMNS)


def list_secrets(cur: Any, *, account_id: str) -> list[dict[str, Any]]:
    """Every secret of the installation, with how many places use it."""
    cur.execute(
        """
        SELECT s.id, s.account_id, s.name, s.description, s.storage_ref_key,
           s.created_at, s.created_by, s.metadata_updated_at, s.metadata_updated_by,
           s.value_updated_at, s.value_updated_by,
               (SELECT count(*) FROM catalog_secret_bindings AS b WHERE b.secret_id = s.id)
          FROM catalog_secrets AS s
         WHERE s.account_id::text = %s
         ORDER BY lower(s.name), s.id
        """,
        (str(account_id),),
    )
    return _rows(cur, (*_SECRET_COLUMNS, "usage_count"))


def update_metadata(
    cur: Any, *, account_id: str, secret_id: str, name: str, description: str, actor: str
) -> dict[str, Any] | None:
    cur.execute(
        """
        UPDATE catalog_secrets AS s
           SET name = %s, description = %s,
               metadata_updated_at = now(), metadata_updated_by = %s
         WHERE s.id::text = %s AND s.account_id::text = %s
        RETURNING s.id, s.account_id, s.name, s.description, s.storage_ref_key,
               s.created_at, s.created_by, s.metadata_updated_at, s.metadata_updated_by,
               s.value_updated_at, s.value_updated_by
        """,
        (name, description, actor, str(secret_id), str(account_id)),
    )
    return _row(cur, _SECRET_COLUMNS)


def touch_value(cur: Any, *, account_id: str, secret_id: str, actor: str) -> None:
    cur.execute(
        """
        UPDATE catalog_secrets
           SET value_updated_at = now(), value_updated_by = %s
         WHERE id::text = %s AND account_id::text = %s
        """,
        (actor, str(secret_id), str(account_id)),
    )


def delete_secret(cur: Any, *, account_id: str, secret_id: str, storage_ref_key: str) -> None:
    """Delete the catalog row, then its stored value; the bindings FK refuses a used secret."""
    cur.execute(
        "DELETE FROM catalog_secrets WHERE id::text = %s AND account_id::text = %s",
        (str(secret_id), str(account_id)),
    )
    cur.execute(
        "DELETE FROM secret_refs WHERE account_id::text = %s AND ref_key = %s",
        (str(account_id), storage_ref_key),
    )


_BINDING_COLUMNS = (
    "id",
    "secret_id",
    "consumer_kind",
    "project_connection_id",
    "connection_name",
    "workflow_id",
    "workflow_name",
    "project_id",
    "project_name",
    "field_name",
    "created_at",
    "created_by",
    "data_connection_id",
    "mcp_connector_id",
    "llm_provider_install_id",
    "consumer_name",
)


def bindings_for_secret(cur: Any, *, account_id: str, secret_id: str) -> list[dict[str, Any]]:
    """Every consumer of a secret, named, so a person can find and remove each binding."""
    cur.execute(
        """
        SELECT b.id, b.secret_id, b.consumer_kind, b.project_connection_id,
               c.display_name AS connection_name, b.workflow_id,
               w.display_name AS workflow_name,
               COALESCE(c.project_id, b.consumer_project_id)::text AS project_id,
               p.name AS project_name, b.field_name, b.created_at, b.created_by,
               b.data_connection_id, b.mcp_connector_id, b.llm_provider_install_id,
               COALESCE(
                   d.display_name,
                   m.display_name,
                   (SELECT NULLIF(ls.display_label, '') FROM llm_provider_sources AS ls
                     WHERE ls.extension_install_id = b.llm_provider_install_id
                     ORDER BY ls.created_at LIMIT 1),
                   b.llm_provider_install_id
               ) AS consumer_name
          FROM catalog_secret_bindings AS b
          LEFT JOIN project_connections AS c ON c.connection_id = b.project_connection_id
          LEFT JOIN registry_workflows AS w ON w.workflow_id = b.workflow_id
          LEFT JOIN workspace_data_connections AS d ON d.connection_id = b.data_connection_id
          LEFT JOIN mcp_connectors AS m ON m.connector_id = b.mcp_connector_id
          LEFT JOIN projects AS p
            ON p.id = COALESCE(c.project_id, b.consumer_project_id, m.project_id)
         WHERE b.secret_id::text = %s AND b.account_id::text = %s
         ORDER BY b.consumer_kind, b.project_connection_id, b.workflow_id,
                  b.data_connection_id, b.mcp_connector_id, b.llm_provider_install_id,
                  b.field_name
        """,
        (str(secret_id), str(account_id)),
    )
    return _rows(cur, _BINDING_COLUMNS)


def connection_bindings(cur: Any, *, account_id: str, connection_id: str) -> dict[str, str]:
    """field -> secret id for one connection."""
    cur.execute(
        """
        SELECT field_name, secret_id::text
          FROM catalog_secret_bindings
         WHERE project_connection_id = %s AND account_id::text = %s
        """,
        (str(connection_id), str(account_id)),
    )
    return {str(field): str(secret_id) for field, secret_id in cur.fetchall() or []}


def bind_connection_field(
    cur: Any, *, account_id: str, secret_id: str, connection_id: str, field_name: str, actor: str
) -> None:
    cur.execute(
        """
        INSERT INTO catalog_secret_bindings (
            account_id, secret_id, consumer_kind, project_connection_id, field_name, created_by
        ) VALUES (%s, %s, 'project_connection', %s, %s, %s)
        ON CONFLICT (project_connection_id, field_name)
        DO UPDATE SET secret_id = EXCLUDED.secret_id,
                      created_at = now(),
                      created_by = EXCLUDED.created_by
        """,
        (str(account_id), str(secret_id), str(connection_id), field_name, actor),
    )


def unbind_connection_field(
    cur: Any, *, account_id: str, connection_id: str, field_name: str
) -> bool:
    cur.execute(
        """
        DELETE FROM catalog_secret_bindings
         WHERE project_connection_id = %s AND field_name = %s AND account_id::text = %s
        """,
        (str(connection_id), field_name, str(account_id)),
    )
    return bool(getattr(cur, "rowcount", 0))


def delete_binding(cur: Any, *, account_id: str, binding_id: Any) -> None:
    """Remove one binding by its id (used when its secret is deleted)."""
    cur.execute(
        "DELETE FROM catalog_secret_bindings WHERE id::text = %s AND account_id::text = %s",
        (str(binding_id), str(account_id)),
    )


def lock_connection(
    cur: Any, *, account_id: str, connection_id: str, for_update: bool = True
) -> dict[str, Any] | None:
    columns = (
        "connection_id",
        "account_id",
        "project_id",
        "extension_id",
        "extension_install_id",
        "connection_kind",
        "connection_type",
        "runtime_mode",
        "remote_base_url",
        "connection_config",
        "connection_routes",
        "ingress_config",
        "workflow_mappings",
        "secret_refs",
        "credential_revision",
        "display_name",
    )
    if for_update:
        cur.execute(
            """
            SELECT connection_id, account_id::text, project_id::text, extension_id, extension_install_id,
                   connection_kind, connection_type, runtime_mode, remote_base_url, connection_config,
                   connection_routes, ingress_config, workflow_mappings, secret_refs,
                   credential_revision, display_name
              FROM project_connections
             WHERE connection_id = %s AND account_id::text = %s
             FOR UPDATE
            """,
            (str(connection_id), str(account_id)),
        )
    else:
        cur.execute(
            """
            SELECT connection_id, account_id::text, project_id::text, extension_id, extension_install_id,
                   connection_kind, connection_type, runtime_mode, remote_base_url, connection_config,
                   connection_routes, ingress_config, workflow_mappings, secret_refs,
                   credential_revision, display_name
              FROM project_connections
             WHERE connection_id = %s AND account_id::text = %s
            """,
            (str(connection_id), str(account_id)),
        )
    return _row(cur, columns)


def write_connection_secret_refs(
    cur: Any, *, connection_id: str, secret_refs: dict[str, Any], bump_revision: bool
) -> None:
    cur.execute(
        """
        UPDATE project_connections
           SET secret_refs = %s::jsonb,
               credential_revision = credential_revision + CASE WHEN %s THEN 1 ELSE 0 END,
               updated_at = now()
         WHERE connection_id = %s
        """,
        (json.dumps(secret_refs, sort_keys=True), bool(bump_revision), str(connection_id)),
    )


def install_release(cur: Any, *, install_id: str) -> dict[str, Any]:
    """The release an extension install currently runs, with its descriptor digest."""
    if not str(install_id or "").strip():
        return {"release_id": "", "descriptor_sha256": ""}
    cur.execute(
        """
        SELECT i.release_id, COALESCE(r.descriptor_sha256, '')
          FROM extension_installs AS i
          LEFT JOIN extension_releases AS r
            ON r.release_id = i.release_id AND r.extension_id = i.extension_id
         WHERE i.install_id = %s
        """,
        (str(install_id),),
    )
    return _row(cur, ("release_id", "descriptor_sha256")) or {
        "release_id": "",
        "descriptor_sha256": "",
    }


_APPROVAL_COLUMNS = (
    "fingerprint",
    "projection_jsonb",
    "approved_by",
    "approved_by_kind",
    "approved_at",
)


def connection_approval(cur: Any, *, account_id: str, connection_id: str) -> dict[str, Any] | None:
    cur.execute(
        """
        SELECT fingerprint, projection_jsonb, approved_by, approved_by_kind, approved_at
          FROM catalog_secret_approvals
         WHERE project_connection_id = %s AND account_id::text = %s
        """,
        (str(connection_id), str(account_id)),
    )
    return _row(cur, _APPROVAL_COLUMNS)


def record_connection_approval(
    cur: Any,
    *,
    account_id: str,
    connection_id: str,
    fingerprint: str,
    projection: dict[str, Any],
    actor: str,
    actor_kind: str,
) -> None:
    cur.execute(
        """
        INSERT INTO catalog_secret_approvals (
            account_id, consumer_kind, project_connection_id, fingerprint, projection_jsonb,
            approved_by, approved_by_kind
        ) VALUES (%s, 'project_connection', %s, %s, %s::jsonb, %s, %s)
        ON CONFLICT (project_connection_id)
        DO UPDATE SET fingerprint = EXCLUDED.fingerprint,
                      projection_jsonb = EXCLUDED.projection_jsonb,
                      approved_by = EXCLUDED.approved_by,
                      approved_by_kind = EXCLUDED.approved_by_kind,
                      approved_at = now()
        """,
        (
            str(account_id),
            str(connection_id),
            fingerprint,
            json.dumps(projection, sort_keys=True),
            actor,
            actor_kind,
        ),
    )


def record_audit(
    cur: Any,
    *,
    account_id: str,
    command_type: str,
    actor_kind: str,
    actor_id: str,
    target: dict[str, Any],
    payload: dict[str, Any] | None = None,
    status: str = "executed",
) -> None:
    """One `command_audit_log` row. Callers pass identifiers and outcomes, never values."""
    cur.execute(
        """
        INSERT INTO command_audit_log (
            command_id, command_type, account_id, actor_type, actor_id,
            target_jsonb, payload_jsonb, status
        ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
        """,
        (
            f"secret:{uuid.uuid4()}",
            command_type,
            str(account_id),
            actor_kind,
            actor_id,
            json.dumps(target, sort_keys=True, default=str),
            json.dumps(payload or {}, sort_keys=True, default=str),
            status,
        ),
    )
