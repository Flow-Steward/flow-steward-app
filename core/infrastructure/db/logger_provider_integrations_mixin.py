from __future__ import annotations

from typing import Any

from core.infrastructure.db import psycopg_runtime as psycopg_runtime
from core.infrastructure.db.logger_scheduled_providers_common import (
    DEFAULT_PROVIDER_TYPES,
    ERR_PROVIDER_KEY_REQUIRED,
    SQL_AND,
    _as_dict,
    _coerce_bool,
    _decrypt_secret_value,
    _encrypt_secret_value,
    _json_dumps,
    _normalize_string_list,
)


class PlatformLoggerProviderIntegrationsMixin:
    conn: Any
    _table_exists: Any

    def provider_configuration_transaction(self):
        """Return the shared technical transaction boundary for provider updates."""
        from core.infrastructure.db.db_pool import transaction_connection

        return transaction_connection(self.conn)

    @staticmethod
    def _provider_type_defaults(enabled_only: bool = True) -> list[dict]:
        rows = [dict(item) for item in DEFAULT_PROVIDER_TYPES]
        if enabled_only:
            rows = [item for item in rows if bool(item.get("enabled", True))]
        return rows

    @staticmethod
    def _normalize_provider_row(row: dict | None) -> dict:
        payload = dict(row or {})
        payload["config"] = _as_dict(payload.get("config"))
        payload["features"] = _normalize_string_list(payload.get("features"))
        payload["enabled"] = _coerce_bool(payload.get("enabled"), True)
        return payload

    def list_provider_types(self, enabled_only: bool = True) -> list[dict]:
        if not self._table_exists("provider_types"):
            return self._provider_type_defaults(enabled_only=enabled_only)

        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            if enabled_only:
                cur.execute("SELECT * FROM provider_types WHERE enabled = true ORDER BY key")
            else:
                cur.execute("SELECT * FROM provider_types ORDER BY key")
            return [dict(row) for row in cur.fetchall()]

    def _resolve_provider_type_id_cur(self, cur, provider_type: str | int) -> str:
        provider_type_key = str(provider_type or "").strip().lower()
        if not provider_type_key:
            raise ValueError("provider_type_key is required")

        cur.execute(
            "SELECT id FROM provider_types WHERE id::text = %s OR key = %s",
            (provider_type_key, provider_type_key),
        )
        row = cur.fetchone()
        if not row:
            raise ValueError(
                f"Unknown provider_type_key '{provider_type_key}'. "
                "Seed provider_types first or pass a valid provider_type_id."
            )
        return str(row[0] if not isinstance(row, dict) else row.get("id") or "").strip()

    def get_provider(self, provider_key: str) -> dict | None:
        normalized_key = str(provider_key or "").strip()
        if not normalized_key:
            return None
        if not (self._table_exists("providers") and self._table_exists("provider_types")):
            return None

        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                    p.id AS provider_id,
                    p.provider_key,
                    pt.id AS provider_type_id,
                    pt.key AS provider_type_key,
                    pt.label AS provider_type_label,
                    p.display_name,
                    p.description,
                    p.implementation_kind,
                    p.integration_key,
                    p.config_jsonb AS config,
                    p.features_jsonb AS features,
                    p.enabled,
                    p.created_at,
                    p.updated_at
                FROM providers p
                JOIN provider_types pt ON pt.id = p.provider_type_id
                WHERE p.provider_key = %s
                """,
                (normalized_key,),
            )
            row = cur.fetchone()
        return self._normalize_provider_row(dict(row)) if row else None

    def lock_provider_for_update(self, provider_key: str) -> dict | None:
        normalized_key = str(provider_key or "").strip()
        if not normalized_key:
            return None
        if not (self._table_exists("providers") and self._table_exists("provider_types")):
            return None

        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                    p.id AS provider_id,
                    p.provider_key,
                    pt.id AS provider_type_id,
                    pt.key AS provider_type_key,
                    pt.label AS provider_type_label,
                    p.display_name,
                    p.description,
                    p.implementation_kind,
                    p.integration_key,
                    p.config_jsonb AS config,
                    p.features_jsonb AS features,
                    p.enabled,
                    p.created_at,
                    p.updated_at
                FROM providers p
                JOIN provider_types pt ON pt.id = p.provider_type_id
                WHERE p.provider_key = %s
                FOR UPDATE OF p
                """,
                (normalized_key,),
            )
            row = cur.fetchone()
        return self._normalize_provider_row(dict(row)) if row else None

    def list_providers(
        self,
        enabled_only: bool = True,
        provider_type_key: str | None = None,
    ) -> list[dict]:
        if not (self._table_exists("providers") and self._table_exists("provider_types")):
            return []

        conditions: list[str] = []
        params: list = []
        normalized_type = str(provider_type_key or "").strip().lower()
        if enabled_only:
            conditions.append("p.enabled = true")
            conditions.append("pt.enabled = true")
        if normalized_type:
            conditions.append("pt.key = %s")
            params.append(normalized_type)
        where_sql = f"WHERE {SQL_AND.join(conditions)}" if conditions else ""

        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT
                    p.id AS provider_id,
                    p.provider_key,
                    pt.id AS provider_type_id,
                    pt.key AS provider_type_key,
                    pt.label AS provider_type_label,
                    p.display_name,
                    p.description,
                    p.implementation_kind,
                    p.integration_key,
                    p.config_jsonb AS config,
                    p.features_jsonb AS features,
                    p.enabled,
                    p.created_at,
                    p.updated_at
                FROM providers p
                JOIN provider_types pt ON pt.id = p.provider_type_id
                {where_sql}
                ORDER BY pt.key, p.provider_key
                """,
                params,
            )
            rows = cur.fetchall()
        return [self._normalize_provider_row(dict(row)) for row in rows]

    def list_latest_provider_health(
        self,
        *,
        project_id: str | None = None,
    ) -> dict[str, dict]:
        if not self._table_exists("provider_health_events"):
            return {}

        where = []
        params: list = []
        if project_id is not None:
            where.append("(phe.project_id = %s OR phe.project_id IS NULL)")
            params.append(str(project_id))
        where_sql = f"WHERE {SQL_AND.join(where)}" if where else ""
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                WITH ranked AS (
                    SELECT
                        p.provider_key,
                        phe.project_id,
                        phe.status,
                        phe.summary,
                        phe.payload_jsonb,
                        phe.created_at,
                        ROW_NUMBER() OVER (
                            PARTITION BY p.provider_key
                            ORDER BY
                                CASE WHEN phe.project_id IS NULL THEN 1 ELSE 0 END,
                                phe.created_at DESC
                        ) AS rn
                    FROM provider_health_events phe
                    JOIN providers p ON p.id = phe.provider_id
                    {where_sql}
                )
                SELECT
                    provider_key,
                    project_id,
                    status,
                    summary,
                    payload_jsonb,
                    created_at
                FROM ranked
                WHERE rn = 1
                """,
                tuple(params),
            )
            rows = cur.fetchall()
        output: dict[str, dict] = {}
        for row in rows:
            provider_key = str(row.get("provider_key") or "").strip()
            if not provider_key:
                continue
            output[provider_key] = {
                "status": str(row.get("status") or "unknown").strip() or "unknown",
                "summary": str(row.get("summary") or "").strip(),
                "project_id": str(row.get("project_id") or "").strip() or None,
                "payload": _as_dict(row.get("payload_jsonb")),
                "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
            }
        return output

    def _resolve_provider_id_cur(self, cur, provider_key: str) -> str:
        normalized_key = str(provider_key or "").strip()
        if not normalized_key:
            raise ValueError(ERR_PROVIDER_KEY_REQUIRED)
        cur.execute("SELECT id FROM providers WHERE provider_key = %s", (normalized_key,))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"Unknown provider_key '{normalized_key}'")
        if isinstance(row, dict):
            return str(row.get("id") or "").strip()
        return str(row[0] if row else "").strip()

    def save_provider_secret(
        self,
        *,
        provider_key: str,
        secret_key: str,
        secret_value: str,
        project_id: str | None = None,
        active: bool = True,
        metadata: dict | None = None,
    ) -> bool:
        if not self._table_exists("provider_secrets"):
            return False
        normalized_secret_key = str(secret_key or "").strip()
        if not normalized_secret_key:
            raise ValueError("secret_key is required")
        cipher_text, nonce_text, key_version = _encrypt_secret_value(str(secret_value or ""))
        with self.conn.cursor() as cur:
            provider_id = self._resolve_provider_id_cur(cur, provider_key)
            normalized_project_id = str(project_id).strip() if project_id is not None else None
            if normalized_project_id is None:
                cur.execute(
                    """
                    UPDATE provider_secrets
                       SET active = false, updated_at = now()
                     WHERE provider_id = %s
                       AND secret_key = %s
                       AND active = true
                       AND project_id IS NULL
                    """,
                    (provider_id, normalized_secret_key),
                )
            else:
                cur.execute(
                    """
                    UPDATE provider_secrets
                       SET active = false, updated_at = now()
                     WHERE provider_id = %s
                       AND secret_key = %s
                       AND active = true
                       AND project_id = %s
                    """,
                    (provider_id, normalized_secret_key, normalized_project_id),
                )
            cur.execute(
                """
                INSERT INTO provider_secrets (
                    provider_id,
                    project_id,
                    secret_key,
                    secret_value_ciphertext,
                    secret_value_nonce,
                    key_version,
                    metadata_jsonb,
                    active
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    provider_id,
                    normalized_project_id,
                    normalized_secret_key,
                    cipher_text,
                    nonce_text,
                    key_version,
                    _json_dumps(_as_dict(metadata)),
                    bool(active),
                ),
            )
        return True

    def get_provider_secret(
        self,
        *,
        provider_key: str,
        secret_key: str,
        project_id: str | None = None,
        include_global: bool = True,
    ) -> str:
        if not self._table_exists("provider_secrets"):
            return ""
        normalized_secret_key = str(secret_key or "").strip()
        if not normalized_secret_key:
            return ""
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            provider_id = self._resolve_provider_id_cur(cur, provider_key)
            normalized_project_id = str(project_id).strip() if project_id is not None else None
            if normalized_project_id is not None:
                cur.execute(
                    """
                    SELECT secret_value_ciphertext, secret_value_nonce, key_version
                    FROM provider_secrets
                    WHERE provider_id = %s
                      AND project_id = %s
                      AND secret_key = %s
                      AND active = true
                    ORDER BY updated_at DESC, id DESC
                    LIMIT 1
                    """,
                    (provider_id, normalized_project_id, normalized_secret_key),
                )
                row = cur.fetchone()
                if row:
                    return _decrypt_secret_value(
                        str(row.get("secret_value_ciphertext") or ""),
                        str(row.get("secret_value_nonce") or "") or None,
                        str(row.get("key_version") or "") or None,
                    )
            if include_global:
                cur.execute(
                    """
                    SELECT secret_value_ciphertext, secret_value_nonce, key_version
                    FROM provider_secrets
                    WHERE provider_id = %s
                      AND project_id IS NULL
                      AND secret_key = %s
                      AND active = true
                    ORDER BY updated_at DESC, id DESC
                    LIMIT 1
                    """,
                    (provider_id, normalized_secret_key),
                )
                row = cur.fetchone()
                if row:
                    return _decrypt_secret_value(
                        str(row.get("secret_value_ciphertext") or ""),
                        str(row.get("secret_value_nonce") or "") or None,
                        str(row.get("key_version") or "") or None,
                    )
        return ""

    def save_project_secret(
        self,
        *,
        project_id: str,
        secret_key: str,
        secret_value: str,
        active: bool = True,
        metadata: dict | None = None,
    ) -> bool:
        if not self._table_exists("project_secrets"):
            return False
        normalized_secret_key = str(secret_key or "").strip()
        if not normalized_secret_key:
            raise ValueError("secret_key is required")
        normalized_project_id = str(project_id or "").strip()
        if not normalized_project_id:
            raise ValueError("project_id is required")
        cipher_text, nonce_text, key_version = _encrypt_secret_value(str(secret_value or ""))
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE project_secrets
                   SET active = false, updated_at = now()
                 WHERE project_id = %s
                   AND secret_key = %s
                   AND active = true
                """,
                (normalized_project_id, normalized_secret_key),
            )
            cur.execute(
                """
                INSERT INTO project_secrets (
                    project_id,
                    secret_key,
                    secret_value_ciphertext,
                    secret_value_nonce,
                    key_version,
                    metadata_jsonb,
                    active
                ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    normalized_project_id,
                    normalized_secret_key,
                    cipher_text,
                    nonce_text,
                    key_version,
                    _json_dumps(_as_dict(metadata)),
                    bool(active),
                ),
            )
        return True

    def get_project_secret(
        self,
        *,
        project_id: str,
        secret_key: str,
    ) -> str:
        if not self._table_exists("project_secrets"):
            return ""
        normalized_secret_key = str(secret_key or "").strip()
        if not normalized_secret_key:
            return ""
        normalized_project_id = str(project_id or "").strip()
        if not normalized_project_id:
            return ""
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT secret_value_ciphertext, secret_value_nonce, key_version
                FROM project_secrets
                WHERE project_id = %s
                  AND secret_key = %s
                  AND active = true
                ORDER BY updated_at DESC, id DESC
                LIMIT 1
                """,
                (normalized_project_id, normalized_secret_key),
            )
            row = cur.fetchone()
            if row:
                return _decrypt_secret_value(
                    str(row.get("secret_value_ciphertext") or ""),
                    str(row.get("secret_value_nonce") or "") or None,
                    str(row.get("key_version") or "") or None,
                )
        return ""

    def save_provider(self, data: dict) -> None:
        if not (self._table_exists("providers") and self._table_exists("provider_types")):
            raise RuntimeError("Provider tables are not available")

        provider_key = str(data.get("provider_key") or "").strip()
        if not provider_key:
            raise ValueError(ERR_PROVIDER_KEY_REQUIRED)

        with self.conn.cursor() as cur:
            provider_type_ref: str | int | None = data.get("provider_type_id")
            if provider_type_ref is None:
                raw_key = str(data.get("provider_type_key") or "").strip()
                provider_type_ref = raw_key or None
            if provider_type_ref is None:
                raise ValueError("provider_type_id or provider_type_key is required")
            provider_type_id = self._resolve_provider_type_id_cur(
                cur,
                provider_type_ref,
            )
            cur.execute(
                """
                INSERT INTO providers (
                    provider_key,
                    provider_type_id,
                    display_name,
                    description,
                    implementation_kind,
                    integration_key,
                    config_jsonb,
                    features_jsonb,
                    enabled
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (provider_key) DO UPDATE SET
                    provider_type_id = EXCLUDED.provider_type_id,
                    display_name = EXCLUDED.display_name,
                    description = EXCLUDED.description,
                    implementation_kind = EXCLUDED.implementation_kind,
                    integration_key = EXCLUDED.integration_key,
                    config_jsonb = EXCLUDED.config_jsonb,
                    features_jsonb = EXCLUDED.features_jsonb,
                    enabled = EXCLUDED.enabled
                """,
                (
                    provider_key,
                    provider_type_id,
                    str(data.get("display_name") or provider_key).strip(),
                    str(data.get("description") or "").strip(),
                    str(data.get("implementation_kind") or "builtin").strip() or "builtin",
                    str(data.get("integration_key") or "").strip() or None,
                    _json_dumps(_as_dict(data.get("config"))),
                    _json_dumps(_normalize_string_list(data.get("features"))),
                    _coerce_bool(data.get("enabled"), True),
                ),
            )

    @staticmethod
    def _normalize_project_provider_binding_row(row: dict | None) -> dict:
        payload = dict(row or {})
        payload["provider_config"] = _as_dict(payload.get("provider_config"))
        payload["binding_config"] = _as_dict(payload.get("binding_config"))
        payload["features"] = _normalize_string_list(payload.get("features"))
        payload["enabled"] = _coerce_bool(payload.get("enabled"), True)
        payload["provider_enabled"] = _coerce_bool(payload.get("provider_enabled"), True)
        payload["binding_source"] = str(payload.get("binding_source") or "db").strip() or "db"
        return payload


__all__ = ["PlatformLoggerProviderIntegrationsMixin"]
