"""ProjectConnectionRepository persistence operations."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any

from core.infrastructure.db import psycopg_runtime
from core.infrastructure.db.platform_logger_common import (
    SQL_AND,
    SQL_ENABLED_TRUE,
    SQL_PROJECT_ID_EQ,
    _as_dict,
    _coerce_bool,
    _coerce_int,
    _default_account_id,
    _normalize_string_list,
)
from core.infrastructure.db.psycopg_runtime import sql

_MESSAGE_EXTENSION_ID_S = "extension_id = %s"
_MESSAGE_EXTENSION_INSTALL_ID_S = "extension_install_id = %s"


def _normalize_secret_refs_payload(value) -> dict:
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
        if all(not isinstance(item, (dict, list)) for item in value.values()):
            refs = {
                str(key or "").strip(): str(ref or "").strip()
                for key, ref in value.items()
                if str(key or "").strip() and str(ref or "").strip()
            }
            return {"refs": refs} if refs else {}
        return dict(value)
    if isinstance(value, list):
        refs = {}
        for item in value:
            secret_name = str(item or "").strip()
            if secret_name:
                refs[secret_name] = secret_name
        return {"refs": refs} if refs else {}
    return {}


def _normalize_tags(value) -> list[str]:
    return _normalize_string_list(value)


class ProjectConnectionRepositoryMixin:
    conn: Any
    _table_exists: Callable[[str], bool]
    _column_exists: Callable[[str, str], bool]

    def consume_project_connection_ingress_token(
        self,
        *,
        project_id: str,
        extension_id: str,
        account_id: str,
        connection_id: str,
        extension_install_id: str,
        token: str,
    ) -> bool:
        """Atomically remove one extension-owned pending ingress token."""
        normalized_token = str(token or "").strip()
        if not normalized_token:
            return False
        clauses = [
            "project_id = %s",
            _MESSAGE_EXTENSION_ID_S,
            "account_id = %s",
            "COALESCE(ingress_config->'pending_hitl_tokens', '{}'::jsonb) ? %s",
        ]
        params: list[Any] = [project_id, extension_id, account_id, normalized_token]
        if str(connection_id or "").strip():
            clauses.append("connection_id = %s")
            params.append(str(connection_id).strip())
        if str(extension_install_id or "").strip():
            clauses.append(_MESSAGE_EXTENSION_INSTALL_ID_S)
            params.append(str(extension_install_id).strip())
        params.append(normalized_token)
        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE project_connections
                   SET ingress_config = jsonb_set(
                         COALESCE(ingress_config, '{{}}'::jsonb),
                         '{{pending_hitl_tokens}}',
                         COALESCE(ingress_config->'pending_hitl_tokens', '{{}}'::jsonb) - %s,
                         true
                       ),
                       updated_at = now()
                 WHERE {" AND ".join(clauses)}
                RETURNING connection_id
                """,  # nosec B608 -- clauses are fixed internal predicates
                (params[-1], *params[:-1]),
            )
            return cur.fetchone() is not None

    @staticmethod
    def _normalize_project_connection_row(row: dict | None) -> dict:
        payload = dict(row or {})
        payload["account_id"] = (
            str(payload.get("account_id") or _default_account_id()).strip() or _default_account_id()
        )
        payload["connection_kind"] = (
            str(payload.get("connection_kind") or "extension").strip() or "extension"
        )
        extension_id = payload.get("extension_id")
        payload["extension_id"] = (
            str(extension_id).strip() if extension_id not in (None, "") else None
        )
        payload["display_name"] = str(payload.get("display_name") or "").strip()
        payload["extension_install_id"] = str(payload.get("extension_install_id") or "").strip()
        payload["connection_type"] = str(payload.get("connection_type") or "").strip()
        payload["environment"] = str(payload.get("environment") or "").strip()
        payload["credential_revision"] = _coerce_int(payload.get("credential_revision"), 1)
        payload["runtime_mode"] = str(payload.get("runtime_mode") or "").strip()
        payload["remote_base_url"] = str(payload.get("remote_base_url") or "").strip()
        payload["health_status"] = str(payload.get("health_status") or "").strip()
        payload["runtime_status"] = str(payload.get("runtime_status") or "").strip()
        if hasattr(payload.get("last_seen_ok_at"), "timestamp"):
            payload["last_seen_ok_at"] = payload["last_seen_ok_at"].timestamp()
        payload["last_error"] = str(payload.get("last_error") or "").strip()
        payload["enabled"] = _coerce_bool(payload.get("enabled"), True)
        payload["connection_config"] = _as_dict(payload.get("connection_config"))
        payload["secret_refs"] = _normalize_secret_refs_payload(payload.get("secret_refs"))
        payload["connection_routes"] = [
            dict(item)
            for item in (payload.get("connection_routes") or [])
            if isinstance(item, dict)
        ]
        payload["ingress_config"] = _as_dict(payload.get("ingress_config"))
        payload["workflow_mappings"] = _as_dict(payload.get("workflow_mappings"))
        payload["tags"] = _normalize_tags(payload.get("tags"))
        return payload

    def list_project_connections(
        self,
        project_id: Any,
        *,
        enabled_only: bool = True,
        extension_id: str = "",
        extension_install_id: str = "",
    ) -> list[dict]:
        if not self._table_exists("project_connections"):
            return []

        conditions = [SQL_PROJECT_ID_EQ]
        params: list[Any] = [project_id]
        normalized_extension_id = str(extension_id or "").strip()
        if normalized_extension_id:
            conditions.append(_MESSAGE_EXTENSION_ID_S)
            params.append(normalized_extension_id)
        normalized_extension_install_id = str(extension_install_id or "").strip()
        if normalized_extension_install_id:
            conditions.append(_MESSAGE_EXTENSION_INSTALL_ID_S)
            params.append(normalized_extension_install_id)
        if enabled_only:
            conditions.append(SQL_ENABLED_TRUE)

        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                sql.SQL("""
                SELECT connection_id, project_id, extension_id, account_id,
                       display_name, connection_kind, connection_type, environment,
                       credential_revision, extension_install_id,
                       oauth_configuration_id, oauth_provider_id,
                       oauth_extension_release_id, oauth_authorization_revision,
                       oauth_requested_scopes, oauth_granted_scopes,
                       oauth_provider_security_fingerprint,
                       oauth_provider_revocation_fingerprint,
                       runtime_mode, remote_base_url,
                       health_status, runtime_status, last_seen_ok_at, last_error,
                       enabled,
                       connection_config, secret_refs, connection_routes,
                       ingress_config, workflow_mappings,
                       tags, created_at, updated_at
                FROM project_connections
                WHERE {conditions}
                ORDER BY display_name, extension_id, created_at
                """).format(
                    conditions=sql.SQL(SQL_AND).join(sql.SQL(condition) for condition in conditions)
                ),
                params,
            )
            rows = cur.fetchall()
        return [self._normalize_project_connection_row(dict(row)) for row in rows]

    def list_enabled_extension_connections(self) -> list[dict]:
        """Every enabled extension connection, in every project, oldest first."""
        if not self._table_exists("project_connections"):
            return []
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT connection_id, project_id, extension_id, account_id,
                       display_name, extension_install_id, created_at
                FROM project_connections
                WHERE enabled = true
                  AND extension_install_id IS NOT NULL
                  AND extension_install_id <> ''
                ORDER BY created_at, connection_id
                """
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    def get_project_connection(
        self,
        *,
        project_id: Any | None = None,
        connection_id: str = "",
        extension_id: str = "",
        extension_install_id: str = "",
        enabled_only: bool = False,
    ) -> dict | None:
        if not self._table_exists("project_connections"):
            return None
        conditions: list[str] = []
        params: list[Any] = []
        normalized_connection_id = str(connection_id or "").strip()
        normalized_extension_id = str(extension_id or "").strip()
        if normalized_connection_id:
            conditions.append("connection_id = %s")
            params.append(normalized_connection_id)
        if project_id is not None:
            conditions.append(SQL_PROJECT_ID_EQ)
            params.append(project_id)
        if normalized_extension_id:
            conditions.append(_MESSAGE_EXTENSION_ID_S)
            params.append(normalized_extension_id)
        normalized_extension_install_id = str(extension_install_id or "").strip()
        if normalized_extension_install_id:
            conditions.append(_MESSAGE_EXTENSION_INSTALL_ID_S)
            params.append(normalized_extension_install_id)
        if enabled_only:
            conditions.append(SQL_ENABLED_TRUE)
        if not conditions:
            return None
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                sql.SQL("""
                SELECT connection_id, project_id, extension_id, account_id,
                       display_name, connection_kind, connection_type, environment,
                       credential_revision, extension_install_id,
                       oauth_configuration_id, oauth_provider_id,
                       oauth_extension_release_id, oauth_authorization_revision,
                       oauth_requested_scopes, oauth_granted_scopes,
                       oauth_provider_security_fingerprint,
                       oauth_provider_revocation_fingerprint,
                       runtime_mode, remote_base_url,
                       health_status, runtime_status, last_seen_ok_at, last_error,
                       enabled,
                       connection_config, secret_refs, connection_routes,
                       ingress_config, workflow_mappings,
                       tags, created_at, updated_at
                FROM project_connections
                WHERE {conditions}
                ORDER BY created_at DESC
                LIMIT 1
                """).format(
                    conditions=sql.SQL(SQL_AND).join(sql.SQL(condition) for condition in conditions)
                ),
                params,
            )
            row = cur.fetchone()
        if not row:
            return None
        return self._normalize_project_connection_row(dict(row))

    def get_project_connection_runtime_metadata(self, connection_id: str) -> dict | None:
        """Load a connection's runtime eligibility fields without secret references."""
        if not self._table_exists("project_connections"):
            return None
        from core.infrastructure.db import project_connection_store

        return project_connection_store.get_project_connection_runtime_metadata(
            self.conn,
            str(connection_id or "").strip(),
        )

    def upsert_project_connection(
        self,
        *,
        project_id: Any,
        extension_id: str,
        connection_id: str = "",
        account_id: str | None = None,
        connection_kind: str = "extension",
        display_name: str = "",
        connection_type: str = "",
        environment: str = "",
        credential_revision: int = 1,
        extension_install_id: str = "",
        runtime_mode: str = "",
        remote_base_url: str = "",
        health_status: str = "",
        runtime_status: str = "",
        last_seen_ok_at: float | None = None,
        last_error: str = "",
        enabled: bool = True,
        connection_config: dict | None = None,
        secret_refs: dict | list[str] | None = None,
        connection_routes: list[dict] | None = None,
        ingress_config: dict | None = None,
        workflow_mappings: dict | None = None,
        tags: list[str] | None = None,
    ) -> dict:
        if not self._table_exists("project_connections"):
            raise RuntimeError("Project connections are not available")

        normalized_kind = str(connection_kind or "extension").strip() or "extension"
        normalized_extension_id = str(extension_id or "").strip()
        normalized_extension_install_id = str(extension_install_id or "").strip()
        if normalized_kind in ("extension", "mcp_connector") and not normalized_extension_id:
            raise ValueError("extension_id is required")
        if normalized_kind == "extension_runtime" and not normalized_extension_install_id:
            raise ValueError("extension_install_id is required")
        if normalized_kind == "extension_runtime":
            normalized_extension_id = ""

        normalized_connection_id = str(connection_id or "").strip()
        existing = None
        if normalized_connection_id:
            existing = self.get_project_connection(
                project_id=project_id,
                connection_id=normalized_connection_id,
                enabled_only=False,
            )
        if existing is None:
            existing = self.get_project_connection(
                project_id=project_id,
                extension_id=normalized_extension_id,
                extension_install_id=normalized_extension_install_id,
                enabled_only=False,
            )
        resolved_connection_id = str(
            normalized_connection_id
            or (existing or {}).get("connection_id")
            or f"conn_{uuid.uuid4().hex[:16]}"
        ).strip()
        resolved_account_id = str(account_id or "").strip()
        if not resolved_account_id and self._column_exists("projects", "account_id"):
            with self.conn.cursor() as cur:
                cur.execute("SELECT account_id FROM projects WHERE id = %s", (project_id,))
                row = cur.fetchone()
            if row:
                if isinstance(row, dict):
                    resolved_account_id = str(row.get("account_id") or "").strip()
                else:
                    resolved_account_id = str(row[0] or "").strip()
        resolved_account_id = resolved_account_id or _default_account_id()
        normalized_display_name = (
            str(display_name or "").strip()
            or str((existing or {}).get("display_name") or "").strip()
            or normalized_connection_id
            or normalized_extension_id
            or normalized_extension_install_id
            or resolved_connection_id
        )
        normalized_connection_type = (
            str(connection_type or "").strip()
            or str((existing or {}).get("connection_type") or "").strip()
            or normalized_extension_id
            or normalized_extension_install_id
            or normalized_kind
        )
        normalized_environment = (
            str(environment or "").strip() or str((existing or {}).get("environment") or "").strip()
        )
        normalized_revision = max(
            1,
            _coerce_int(
                credential_revision
                if credential_revision is not None
                else (existing or {}).get("credential_revision"),
                1,
            ),
        )

        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO project_connections (
                    connection_id,
                    project_id,
                    extension_id,
                    account_id,
                    display_name,
                    connection_kind,
                    connection_type,
                    environment,
                    credential_revision,
                    extension_install_id,
                    runtime_mode,
                    remote_base_url,
                    health_status,
                    runtime_status,
                    last_seen_ok_at,
                    last_error,
                    enabled,
                    connection_config,
                    secret_refs,
                    connection_routes,
                    ingress_config,
                    workflow_mappings,
                    tags
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,to_timestamp(%s),%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb)
                ON CONFLICT (connection_id) DO UPDATE SET
                    project_id = EXCLUDED.project_id,
                    extension_id = EXCLUDED.extension_id,
                    account_id = EXCLUDED.account_id,
                    display_name = EXCLUDED.display_name,
                    connection_kind = EXCLUDED.connection_kind,
                    connection_type = EXCLUDED.connection_type,
                    environment = EXCLUDED.environment,
                    credential_revision = EXCLUDED.credential_revision,
                    extension_install_id = EXCLUDED.extension_install_id,
                    runtime_mode = EXCLUDED.runtime_mode,
                    remote_base_url = EXCLUDED.remote_base_url,
                    health_status = EXCLUDED.health_status,
                    runtime_status = EXCLUDED.runtime_status,
                    last_seen_ok_at = EXCLUDED.last_seen_ok_at,
                    last_error = EXCLUDED.last_error,
                    enabled = EXCLUDED.enabled,
                    connection_config = EXCLUDED.connection_config,
                    secret_refs = EXCLUDED.secret_refs,
                    connection_routes = EXCLUDED.connection_routes,
                    ingress_config = EXCLUDED.ingress_config,
                    workflow_mappings = EXCLUDED.workflow_mappings,
                    tags = EXCLUDED.tags,
                    updated_at = now()
                """,
                (
                    resolved_connection_id,
                    project_id,
                    normalized_extension_id or None,
                    resolved_account_id,
                    normalized_display_name,
                    normalized_kind,
                    normalized_connection_type,
                    normalized_environment,
                    normalized_revision,
                    normalized_extension_install_id or None,
                    str(runtime_mode or "").strip() or None,
                    str(remote_base_url or "").strip() or None,
                    str(health_status or "").strip() or None,
                    str(runtime_status or "").strip() or None,
                    float(last_seen_ok_at) if last_seen_ok_at is not None else None,
                    str(last_error or "").strip(),
                    bool(enabled),
                    json.dumps(_as_dict(connection_config)),
                    json.dumps(_normalize_secret_refs_payload(secret_refs)),
                    json.dumps(
                        [dict(item) for item in (connection_routes or []) if isinstance(item, dict)]
                    ),
                    json.dumps(_as_dict(ingress_config)),
                    json.dumps(_as_dict(workflow_mappings)),
                    json.dumps(_normalize_tags(tags)),
                ),
            )
        return self.get_project_connection(
            project_id=project_id,
            connection_id=resolved_connection_id,
            enabled_only=False,
        ) or {
            "connection_id": resolved_connection_id,
            "project_id": project_id,
            "extension_id": normalized_extension_id or None,
            "account_id": resolved_account_id,
            "display_name": normalized_display_name,
            "connection_kind": normalized_kind,
            "connection_type": normalized_connection_type,
            "environment": normalized_environment,
            "credential_revision": normalized_revision,
            "extension_install_id": normalized_extension_install_id,
            "runtime_mode": str(runtime_mode or "").strip(),
            "remote_base_url": str(remote_base_url or "").strip(),
            "health_status": str(health_status or "").strip(),
            "runtime_status": str(runtime_status or "").strip(),
            "last_seen_ok_at": last_seen_ok_at,
            "last_error": str(last_error or "").strip(),
            "enabled": bool(enabled),
            "connection_config": _as_dict(connection_config),
            "secret_refs": _normalize_secret_refs_payload(secret_refs),
            "connection_routes": [
                dict(item) for item in (connection_routes or []) if isinstance(item, dict)
            ],
            "ingress_config": _as_dict(ingress_config),
            "workflow_mappings": _as_dict(workflow_mappings),
            "tags": _normalize_tags(tags),
        }

    def get_project_provider_bindings(
        self,
        project_id: Any,
        enabled_only: bool = True,
        project_row: dict | None = None,
    ) -> list[dict]:
        """Return no rows; provider bindings now live in project connections."""
        _ = (project_id, enabled_only, project_row)
        return []

    def save_project_provider_binding(
        self,
        project_id: Any,
        provider_key: str,
        *,
        config: dict | None = None,
        enabled: bool = True,
    ) -> None:
        """Persist provider binding intent through project connections."""
