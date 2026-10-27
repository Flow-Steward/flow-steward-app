# mypy: ignore-errors
from __future__ import annotations

import json
from typing import Any

from core.infrastructure.db import psycopg_runtime as psycopg_runtime
from core.infrastructure.db.logger_extensions_accounts_common import (
    _as_dict,
)


class PlatformLoggerExtensionsAccountsCallbacksMixin:
    def lock_extension_callback_registration_scope(
        self,
        *,
        extension_id: str,
        callback_id: str,
        project_id: str,
        connection_id: str | None,
    ) -> None:
        """Serialize idempotent registration creation within the caller transaction."""
        identity = "\x1f".join(
            (
                str(project_id or "").strip(),
                str(extension_id or "").strip(),
                str(callback_id or "").strip(),
                str(connection_id or "").strip(),
            )
        )
        if not all(identity.split("\x1f")[:3]):
            raise ValueError("extension_id, callback_id, and project_id are required")
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"extension_callback_registration:{identity}",),
            )

    def claim_extension_callback_delivery(
        self,
        *,
        registration_id: str,
        delivery_id: str,
        delivered_at: int,
    ) -> bool:
        if not self._table_exists("extension_callback_deliveries"):
            raise RuntimeError("Callback replay storage is unavailable")
        normalized_registration_id = str(registration_id or "").strip()
        normalized_delivery_id = str(delivery_id or "").strip()
        if not normalized_registration_id or not normalized_delivery_id:
            raise ValueError("registration_id and delivery_id are required")
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO extension_callback_deliveries (
                    registration_id,
                    delivery_id,
                    delivered_at
                ) VALUES (%s, %s, to_timestamp(%s))
                ON CONFLICT (registration_id, delivery_id) DO NOTHING
                RETURNING delivery_id
                """,
                (normalized_registration_id, normalized_delivery_id, int(delivered_at)),
            )
            return cur.fetchone() is not None

    def create_extension_callback_registration(
        self,
        *,
        registration_id: str,
        extension_id: str,
        callback_id: str,
        callback_token_hash: str,
        project_id: str,
        connection_id: str | None = None,
        enabled: bool = True,
        auth_type: str = "none",
        auth_config: dict | None = None,
    ) -> dict | None:
        if not self._table_exists("extension_callback_registrations"):
            return None
        normalized_registration_id = str(registration_id or "").strip()
        normalized_extension_id = str(extension_id or "").strip()
        normalized_callback_id = str(callback_id or "").strip()
        normalized_hash = str(callback_token_hash or "").strip()
        normalized_project_id = str(project_id or "").strip()
        if not all(
            (
                normalized_registration_id,
                normalized_extension_id,
                normalized_callback_id,
                normalized_hash,
                normalized_project_id,
            )
        ):
            raise ValueError(
                "registration_id, extension_id, callback_id, token hash, and project_id are required"
            )
        normalized_connection_id = (
            str(connection_id or "").strip() if connection_id is not None else None
        )
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            if normalized_connection_id is None:
                cur.execute(
                    """
                    SELECT id
                    FROM extension_callback_registrations
                    WHERE extension_id = %s
                      AND callback_id = %s
                      AND project_id = %s
                      AND connection_id IS NULL
                    LIMIT 1
                    """,
                    (normalized_extension_id, normalized_callback_id, normalized_project_id),
                )
            else:
                cur.execute(
                    """
                    SELECT id
                    FROM extension_callback_registrations
                    WHERE extension_id = %s
                      AND callback_id = %s
                      AND project_id = %s
                      AND connection_id = %s
                    LIMIT 1
                    """,
                    (
                        normalized_extension_id,
                        normalized_callback_id,
                        normalized_project_id,
                        normalized_connection_id,
                    ),
                )
            existing = cur.fetchone()
            if existing:
                cur.execute(
                    """
                    UPDATE extension_callback_registrations
                    SET callback_token_hash = %s,
                        enabled = %s,
                        auth_type = %s,
                        auth_config_jsonb = %s,
                        updated_at = now()
                    WHERE id = %s
                    RETURNING *
                    """,
                    (
                        normalized_hash,
                        bool(enabled),
                        str(auth_type or "none").strip().lower() or "none",
                        json.dumps(_as_dict(auth_config)),
                        str(existing["id"]),
                    ),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO extension_callback_registrations (
                        id,
                        extension_id,
                        callback_id,
                        callback_token_hash,
                        project_id,
                        connection_id,
                        enabled,
                        auth_type,
                        auth_config_jsonb
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    RETURNING *
                    """,
                    (
                        normalized_registration_id,
                        normalized_extension_id,
                        normalized_callback_id,
                        normalized_hash,
                        normalized_project_id,
                        normalized_connection_id,
                        bool(enabled),
                        str(auth_type or "none").strip().lower() or "none",
                        json.dumps(_as_dict(auth_config)),
                    ),
                )
            row = cur.fetchone()
        return self._normalize_extension_callback_registration(dict(row or {}))

    def list_extension_callback_registrations(
        self,
        *,
        extension_id: str,
        callback_id: str | None = None,
        project_id: str | None = None,
    ) -> list[dict]:
        if not self._table_exists("extension_callback_registrations"):
            return []
        clauses = ["extension_id = %s"]
        params: list[Any] = [str(extension_id or "").strip()]
        if callback_id:
            clauses.append("callback_id = %s")
            params.append(str(callback_id or "").strip())
        if project_id:
            clauses.append("project_id = %s")
            params.append(str(project_id or "").strip())
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT *
                FROM extension_callback_registrations
                WHERE {" AND ".join(clauses)}
                ORDER BY updated_at DESC, created_at DESC
                """,
                tuple(params),
            )
            rows = cur.fetchall() or []
        return [self._normalize_extension_callback_registration(dict(row)) for row in rows]

    def get_extension_callback_registration_by_id(self, registration_id: str) -> dict | None:
        if not self._table_exists("extension_callback_registrations"):
            return None
        normalized_id = str(registration_id or "").strip()
        if not normalized_id:
            return None
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT *
                FROM extension_callback_registrations
                WHERE id = %s
                LIMIT 1
                """,
                (normalized_id,),
            )
            row = cur.fetchone()
        return self._normalize_extension_callback_registration(dict(row or {})) if row else None

    def get_extension_callback_registration_by_token_hash(
        self,
        *,
        callback_token_hash: str,
    ) -> dict | None:
        if not self._table_exists("extension_callback_registrations"):
            return None
        normalized_hash = str(callback_token_hash or "").strip()
        if not normalized_hash:
            return None
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT *
                FROM extension_callback_registrations
                WHERE callback_token_hash = %s
                LIMIT 1
                """,
                (normalized_hash,),
            )
            row = cur.fetchone()
        return self._normalize_extension_callback_registration(dict(row or {})) if row else None

    def update_extension_callback_registration_token(
        self,
        *,
        registration_id: str,
        callback_token_hash: str,
    ) -> dict | None:
        if not self._table_exists("extension_callback_registrations"):
            return None
        normalized_id = str(registration_id or "").strip()
        normalized_hash = str(callback_token_hash or "").strip()
        if not normalized_id or not normalized_hash:
            raise ValueError("registration_id and callback_token_hash are required")
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                """
                UPDATE extension_callback_registrations
                SET callback_token_hash = %s,
                    updated_at = now()
                WHERE id = %s
                RETURNING *
                """,
                (normalized_hash, normalized_id),
            )
            row = cur.fetchone()
        return self._normalize_extension_callback_registration(dict(row or {})) if row else None

    def update_extension_callback_registration_state(
        self,
        *,
        registration_id: str,
        enabled: bool | None = None,
        error: str | None = None,
    ) -> dict | None:
        if not self._table_exists("extension_callback_registrations"):
            return None
        normalized_id = str(registration_id or "").strip()
        if not normalized_id:
            raise ValueError("registration_id is required")
        assignments: list[str] = ["updated_at = now()"]
        params: list[Any] = []
        if enabled is not None:
            assignments.append("enabled = %s")
            params.append(bool(enabled))
        if error is not None:
            assignments.append("last_error = %s")
            params.append(str(error or "").strip() or None)
        if len(assignments) == 1:
            return self.get_extension_callback_registration_by_id(normalized_id)
        params.append(normalized_id)
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                UPDATE extension_callback_registrations
                SET {", ".join(assignments)}
                WHERE id = %s
                RETURNING *
                """,
                tuple(params),
            )
            row = cur.fetchone()
        return self._normalize_extension_callback_registration(dict(row or {})) if row else None

    def mark_extension_callback_registration_seen(
        self,
        *,
        registration_id: str,
        error: str = "",
    ) -> None:
        if not self._table_exists("extension_callback_registrations"):
            return
        normalized_id = str(registration_id or "").strip()
        if not normalized_id:
            return
        normalized_error = str(error or "").strip() or None
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE extension_callback_registrations
                SET last_seen_at = now(),
                    last_error = %s,
                    updated_at = now()
                WHERE id = %s
                """,
                (normalized_error, normalized_id),
            )
