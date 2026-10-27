"""Persistence helpers for project connection CRUD."""

from __future__ import annotations

import contextlib
import json
from datetime import datetime
from typing import Any


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _json_payload(value: Any) -> str:
    return json.dumps(value)


def _normalize_secret_refs_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        refs = value.get("refs")
        if isinstance(refs, dict):
            payload = dict(value)
            payload["refs"] = {
                str(key or "").strip(): str(ref or "").strip()
                for key, ref in refs.items()
                if str(key or "").strip() and str(ref or "").strip()
            }
            return payload
        return dict(value)
    if isinstance(value, list):
        refs = {}
        for item in value:
            secret_name = _normalize_text(item)
            if secret_name:
                refs[secret_name] = secret_name
        return {"refs": refs} if refs else {}
    return {}


def _normalize_tags(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    tags: list[str] = []
    for item in value:
        tag = _normalize_text(item)
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def _row_to_dict(row: Any, description: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        payload = dict(row)
    else:
        columns = [col[0] if hasattr(col, "__getitem__") else col.name for col in description or []]
        if not isinstance(row, (tuple, list)) or len(columns) != len(row):
            return {}
        payload = {str(columns[idx]): row[idx] for idx in range(len(columns))}

    json_columns = {
        "connection_config",
        "oauth_metadata",
        "secret_refs",
        "connection_routes",
        "ingress_config",
        "workflow_mappings",
        "tags",
        "oauth_requested_scopes",
        "oauth_granted_scopes",
    }
    for key, value in list(payload.items()):
        if key in json_columns and isinstance(value, str):
            with contextlib.suppress(Exception):
                payload[key] = json.loads(value)
        elif isinstance(value, datetime):
            payload[key] = value.timestamp()
    return payload


def insert_project_connection(db: Any, connection: Any) -> None:
    insert_query = "".join(
        [
            "INSERT INTO project_connections",
            " (connection_id, project_id, extension_id, account_id, display_name,",
            " connection_kind, connection_type, environment, credential_revision,",
            " extension_install_id, oauth_configuration_id, oauth_provider_id,",
            " oauth_extension_release_id, oauth_authorization_revision,",
            " oauth_requested_scopes, oauth_granted_scopes,",
            " oauth_provider_security_fingerprint,",
            " oauth_provider_revocation_fingerprint, runtime_mode, remote_base_url,",
            " health_status, runtime_status, last_seen_ok_at, last_error,",
            " enabled, connection_config, secret_refs, connection_routes,",
            " ingress_config, workflow_mappings, tags)",
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,",
            " %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s, %s,",
            " to_timestamp(%s), %s, %s, %s::jsonb, %s::jsonb, %s::jsonb,",
            " %s::jsonb, %s::jsonb, %s::jsonb)",
            " ON CONFLICT (connection_id)",
            " DO UPDATE SET",
            " project_id = EXCLUDED.project_id,",
            " extension_id = EXCLUDED.extension_id,",
            " account_id = EXCLUDED.account_id,",
            " display_name = EXCLUDED.display_name,",
            " connection_kind = EXCLUDED.connection_kind,",
            " connection_type = EXCLUDED.connection_type,",
            " environment = EXCLUDED.environment,",
            " credential_revision = EXCLUDED.credential_revision,",
            " extension_install_id = EXCLUDED.extension_install_id,",
            " oauth_configuration_id = EXCLUDED.oauth_configuration_id,",
            " oauth_provider_id = EXCLUDED.oauth_provider_id,",
            " oauth_extension_release_id = EXCLUDED.oauth_extension_release_id,",
            " oauth_authorization_revision = EXCLUDED.oauth_authorization_revision,",
            " oauth_requested_scopes = EXCLUDED.oauth_requested_scopes,",
            " oauth_granted_scopes = EXCLUDED.oauth_granted_scopes,",
            " oauth_provider_security_fingerprint =",
            " EXCLUDED.oauth_provider_security_fingerprint,",
            " oauth_provider_revocation_fingerprint =",
            " EXCLUDED.oauth_provider_revocation_fingerprint,",
            " runtime_mode = EXCLUDED.runtime_mode,",
            " remote_base_url = EXCLUDED.remote_base_url,",
            " health_status = EXCLUDED.health_status,",
            " runtime_status = EXCLUDED.runtime_status,",
            " last_seen_ok_at = EXCLUDED.last_seen_ok_at,",
            " last_error = EXCLUDED.last_error,",
            " enabled = EXCLUDED.enabled,",
            " connection_config = EXCLUDED.connection_config,",
            " secret_refs = EXCLUDED.secret_refs,",
            " connection_routes = EXCLUDED.connection_routes,",
            " ingress_config = EXCLUDED.ingress_config,",
            " workflow_mappings = EXCLUDED.workflow_mappings,",
            " tags = EXCLUDED.tags,",
            " updated_at = now()",
        ]
    )

    with db.cursor() as cur:
        cur.execute(
            insert_query,
            (
                connection.connection_id,
                connection.project_id,
                connection.extension_id,
                connection.account_id,
                _normalize_text(getattr(connection, "display_name", "")),
                connection.connection_kind,
                _normalize_text(getattr(connection, "connection_type", "")),
                _normalize_text(getattr(connection, "environment", "")),
                max(1, int(getattr(connection, "credential_revision", 1) or 1)),
                _normalize_text(connection.extension_install_id) or None,
                _normalize_text(getattr(connection, "oauth_configuration_id", None)) or None,
                _normalize_text(getattr(connection, "oauth_provider_id", None)) or None,
                _normalize_text(getattr(connection, "oauth_extension_release_id", None)) or None,
                getattr(connection, "oauth_authorization_revision", None),
                _json_payload(getattr(connection, "oauth_requested_scopes", [])),
                _json_payload(getattr(connection, "oauth_granted_scopes", [])),
                _normalize_text(getattr(connection, "oauth_provider_security_fingerprint", None))
                or None,
                _normalize_text(getattr(connection, "oauth_provider_revocation_fingerprint", None))
                or None,
                _normalize_text(connection.runtime_mode) or None,
                _normalize_text(connection.remote_base_url) or None,
                _normalize_text(connection.health_status) or None,
                _normalize_text(connection.runtime_status) or None,
                connection.last_seen_ok_at,
                _normalize_text(connection.last_error),
                connection.enabled,
                _json_payload(connection.connection_config),
                _json_payload(_normalize_secret_refs_payload(connection.secret_refs)),
                _json_payload(connection.connection_routes),
                _json_payload(connection.ingress_config),
                _json_payload(connection.workflow_mappings),
                _json_payload(_normalize_tags(getattr(connection, "tags", []))),
            ),
        )


def get_project_connection_row(db: Any, connection_id: str) -> dict[str, Any] | None:
    with db.cursor() as cur:
        cur.execute(
            """SELECT connection_id, project_id, extension_id, account_id,
                      display_name, connection_kind, connection_type, environment,
                      credential_revision, extension_install_id, oauth_configuration_id,
                      oauth_provider_id, oauth_extension_release_id,
                      oauth_authorization_revision, oauth_requested_scopes,
                      oauth_granted_scopes, oauth_provider_security_fingerprint,
                      oauth_provider_revocation_fingerprint, runtime_mode, remote_base_url,
                      health_status, runtime_status, last_seen_ok_at, last_error,
                      enabled, connection_config, secret_refs, connection_routes,
                      ingress_config, workflow_mappings, tags, created_at, updated_at
               FROM project_connections
               WHERE connection_id = %s""",
            (connection_id,),
        )
        row = cur.fetchone()
        return _row_to_dict(row, cur.description) if row else None


def get_project_connection_runtime_metadata(
    db: Any,
    connection_id: str,
) -> dict[str, Any] | None:
    """Return only non-secret connection metadata needed for runtime preflight."""
    with db.cursor() as cur:
        cur.execute(
            """SELECT connection_id, project_id, extension_id, account_id,
                      extension_install_id, connection_type, enabled,
                      oauth_configuration_id, oauth_provider_id,
                      oauth_extension_release_id, oauth_authorization_revision,
                      oauth_requested_scopes, oauth_granted_scopes,
                      oauth_provider_security_fingerprint,
                      oauth_provider_revocation_fingerprint,
                      health_status, runtime_status,
                      COALESCE(connection_config->'oauth', '{}'::jsonb) AS oauth_metadata
               FROM project_connections
               WHERE connection_id = %s""",
            (connection_id,),
        )
        row = cur.fetchone()
        return _row_to_dict(row, cur.description) if row else None


def get_project_connection_row_for_update(
    db: Any,
    connection_id: str,
) -> dict[str, Any] | None:
    """Lock and return one exact connection inside the caller transaction."""
    with db.cursor() as cur:
        cur.execute(
            """SELECT connection_id, project_id, extension_id, account_id,
                      display_name, connection_kind, connection_type, environment,
                      credential_revision, extension_install_id, oauth_configuration_id,
                      oauth_provider_id, oauth_extension_release_id,
                      oauth_authorization_revision, oauth_requested_scopes,
                      oauth_granted_scopes, oauth_provider_security_fingerprint,
                      oauth_provider_revocation_fingerprint, runtime_mode, remote_base_url,
                      health_status, runtime_status, last_seen_ok_at, last_error,
                      enabled, connection_config, secret_refs, connection_routes,
                      ingress_config, workflow_mappings, tags, created_at, updated_at
               FROM project_connections
               WHERE connection_id = %s
               FOR UPDATE""",
            (connection_id,),
        )
        row = cur.fetchone()
        return _row_to_dict(row, cur.description) if row else None


def list_project_connection_rows(db: Any, project_id: Any) -> list[dict[str, Any]]:
    with db.cursor() as cur:
        cur.execute(
            """SELECT connection_id, project_id, extension_id, account_id,
                      display_name, connection_kind, connection_type, environment,
                      credential_revision, extension_install_id, oauth_configuration_id,
                      oauth_provider_id, oauth_extension_release_id,
                      oauth_authorization_revision, oauth_requested_scopes,
                      oauth_granted_scopes, oauth_provider_security_fingerprint,
                      oauth_provider_revocation_fingerprint, runtime_mode, remote_base_url,
                      health_status, runtime_status, last_seen_ok_at, last_error,
                      enabled, connection_config, secret_refs, connection_routes,
                      ingress_config, workflow_mappings, tags, created_at, updated_at
               FROM project_connections
               WHERE project_id = %s
               ORDER BY display_name, created_at""",
            (project_id,),
        )
        return [_row_to_dict(row, cur.description) for row in cur.fetchall()]


def list_extension_connection_rows_for_update(
    db: Any,
    *,
    extension_id: str,
    connection_type: str,
) -> list[dict[str, Any]]:
    with db.cursor() as cur:
        cur.execute(
            """SELECT connection_id, project_id, extension_id, account_id,
                      display_name, connection_kind, connection_type, environment,
                      credential_revision, extension_install_id, oauth_configuration_id,
                      oauth_provider_id, oauth_extension_release_id,
                      oauth_authorization_revision, oauth_requested_scopes,
                      oauth_granted_scopes, oauth_provider_security_fingerprint,
                      oauth_provider_revocation_fingerprint, runtime_mode, remote_base_url,
                      health_status, runtime_status, last_seen_ok_at, last_error,
                      enabled, connection_config, secret_refs, connection_routes,
                      ingress_config, workflow_mappings, tags, created_at, updated_at
               FROM project_connections
               WHERE extension_id = %s
                 AND connection_type = %s
               ORDER BY connection_id
               FOR UPDATE""",
            (_normalize_text(extension_id), _normalize_text(connection_type)),
        )
        return [_row_to_dict(row, cur.description) for row in cur.fetchall()]


def lock_project_for_connection_update(db: Any, project_id: str) -> None:
    with db.cursor() as cur:
        cur.execute("SELECT id FROM projects WHERE id = %s FOR UPDATE", (str(project_id),))
        if not cur.fetchone():
            raise ValueError("Project no longer exists")


def list_project_extension_connection_rows_for_update(
    db: Any,
    *,
    project_id: str,
    extension_id: str,
    connection_type: str,
) -> list[dict[str, Any]]:
    with db.cursor() as cur:
        cur.execute(
            """SELECT connection_id, project_id, extension_id, account_id,
                      display_name, connection_kind, connection_type, environment,
                      credential_revision, extension_install_id, oauth_configuration_id,
                      oauth_provider_id, oauth_extension_release_id,
                      oauth_authorization_revision, oauth_requested_scopes,
                      oauth_granted_scopes, oauth_provider_security_fingerprint,
                      oauth_provider_revocation_fingerprint, runtime_mode, remote_base_url,
                      health_status, runtime_status, last_seen_ok_at, last_error,
                      enabled, connection_config, secret_refs, connection_routes,
                      ingress_config, workflow_mappings, tags, created_at, updated_at
               FROM project_connections
               WHERE project_id = %s
                 AND extension_id = %s
                 AND connection_type = %s
               ORDER BY connection_id
               FOR UPDATE""",
            (
                str(project_id),
                _normalize_text(extension_id),
                _normalize_text(connection_type),
            ),
        )
        return [_row_to_dict(row, cur.description) for row in cur.fetchall()]


def update_project_connection_row(db: Any, connection_id: str, updates: dict[str, Any]) -> None:
    set_clauses: list[str] = []
    values: list[Any] = []
    for key, value in updates.items():
        if key in (
            "connection_config",
            "secret_refs",
            "connection_routes",
            "ingress_config",
            "workflow_mappings",
            "tags",
            "oauth_requested_scopes",
            "oauth_granted_scopes",
        ):
            set_clauses.append(f"{key} = %s::jsonb")
            if key == "secret_refs":
                values.append(_json_payload(_normalize_secret_refs_payload(value)))
            elif key == "tags":
                values.append(_json_payload(_normalize_tags(value)))
            else:
                values.append(_json_payload(value))
        elif key == "last_seen_ok_at":
            if value is None:
                set_clauses.append("last_seen_ok_at = NULL")
            else:
                set_clauses.append("last_seen_ok_at = to_timestamp(%s)")
                values.append(float(value))
        else:
            set_clauses.append(f"{key} = %s")
            values.append(value)

    set_clauses.append("updated_at = now()")
    values.append(connection_id)
    update_query = "".join(
        [
            "UPDATE project_connections SET ",
            ", ".join(set_clauses),
            " WHERE connection_id = %s",
        ]
    )

    with db.cursor() as cur:
        cur.execute(update_query, values)


def delete_project_connection_row(db: Any, connection_id: str) -> None:
    with db.cursor() as cur:
        cur.execute("DELETE FROM project_connections WHERE connection_id = %s", (connection_id,))


def get_project_connection_row_for_extension_install(
    db: Any,
    *,
    project_id: Any,
    extension_install_id: str,
) -> dict[str, Any] | None:
    normalized_install_id = _normalize_text(extension_install_id)
    if not normalized_install_id:
        return None

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT connection_id, project_id, extension_id, account_id,
                   display_name, connection_kind, connection_type, environment,
                   credential_revision, extension_install_id, oauth_configuration_id,
                   oauth_provider_id, oauth_extension_release_id,
                   oauth_authorization_revision, oauth_requested_scopes,
                   oauth_granted_scopes, oauth_provider_security_fingerprint,
                   oauth_provider_revocation_fingerprint, runtime_mode, remote_base_url,
                   health_status, runtime_status, last_seen_ok_at, last_error,
                   enabled, connection_config, secret_refs, connection_routes,
                   ingress_config, workflow_mappings, tags, created_at, updated_at
            FROM project_connections
            WHERE project_id = %s
              AND connection_kind = 'extension_runtime'
              AND extension_install_id = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (project_id, normalized_install_id),
        )
        row = cur.fetchone()
        return _row_to_dict(row, cur.description) if row else None
