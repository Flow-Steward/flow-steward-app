# mypy: ignore-errors
from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

from core.infrastructure.db import psycopg_runtime as psycopg_runtime
from core.infrastructure.db.logger_extensions_accounts_common import (
    SQL_ACCOUNT_ID_EQ,
    SQL_AND,
    _default_account_id,
    _normalize_int_list,
    _normalize_string_list,
    _scopes_within,
)

_MESSAGE_AND_ACCOUNT_ID_S = "AND account_id = %s"
_MESSAGE_SERVICE_ACCOUNT_KEY_IS_REQUIRED = (  # pragma: allowlist secret - validation text
    "service_account_key is required"
)


class PlatformLoggerServiceTokensMixin:
    @staticmethod
    def _effective_machine_token_scopes(
        token_scopes: list[str], service_scopes: list[str]
    ) -> list[str]:
        normalized_token_scopes = _normalize_string_list(token_scopes)
        normalized_service_scopes = _normalize_string_list(service_scopes)
        if not normalized_token_scopes:
            return []
        if not normalized_service_scopes:
            return normalized_token_scopes
        return [
            scope
            for scope in normalized_token_scopes
            if _scopes_within(normalized_service_scopes, [scope])
        ]

    @staticmethod
    def _effective_machine_token_ids(token_ids: list[str], service_ids: list[str]) -> list[str]:
        normalized_token_ids = _normalize_int_list(token_ids)
        normalized_service_ids = _normalize_int_list(service_ids)
        if not normalized_token_ids:
            return normalized_service_ids
        if not normalized_service_ids:
            return normalized_token_ids
        service_set = set(normalized_service_ids)
        return [item for item in normalized_token_ids if item in service_set]

    @staticmethod
    def _effective_machine_token_workflow_ids(
        token_ids: list[str], service_ids: list[str]
    ) -> list[str]:
        normalized_token_ids = _normalize_string_list(token_ids)
        normalized_service_ids = _normalize_string_list(service_ids)
        if not normalized_token_ids:
            return normalized_service_ids
        if not normalized_service_ids:
            return normalized_token_ids
        service_set = set(normalized_service_ids)
        return [item for item in normalized_token_ids if item in service_set]

    def list_service_accounts(
        self, account_id: str | None = None, include_inactive: bool = False
    ) -> list[dict]:
        if not self._table_exists("service_accounts"):
            return []
        where_parts = []
        params: list[Any] = []
        if account_id:
            where_parts.append(SQL_ACCOUNT_ID_EQ)
            params.append(str(account_id).strip())
        if not include_inactive:
            where_parts.append("is_active = true")
        where_sql = f"WHERE {SQL_AND.join(where_parts)}" if where_parts else ""
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT
                    id,
                    account_id,
                    service_account_key,
                    display_name,
                    description,
                    scopes_jsonb,
                    allowed_project_ids_jsonb,
                    allowed_workflow_ids_jsonb,
                    is_active,
                    created_by,
                    created_at,
                    updated_at
                FROM service_accounts
                {where_sql}
                ORDER BY account_id, service_account_key
                """,
                tuple(params),
            )
            rows = cur.fetchall()
        return [self._normalize_service_account_row(dict(row)) for row in rows]

    def create_service_account(
        self,
        *,
        service_account_key: str,
        display_name: str = "",
        description: str = "",
        scopes: list[str] | None = None,
        allowed_project_ids: list[str] | None = None,
        allowed_workflow_ids: list[str] | None = None,
        account_id: str | None = None,
        created_by: str = "system",
        is_active: bool = True,
    ) -> dict | None:
        if not self._table_exists("service_accounts"):
            return None
        normalized_key = str(service_account_key or "").strip().lower()
        if not normalized_key:
            raise ValueError(_MESSAGE_SERVICE_ACCOUNT_KEY_IS_REQUIRED)
        normalized_account = (
            str(account_id or _default_account_id()).strip() or _default_account_id()
        )
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO service_accounts (
                    account_id,
                    service_account_key,
                    display_name,
                    description,
                    scopes_jsonb,
                    allowed_project_ids_jsonb,
                    allowed_workflow_ids_jsonb,
                    is_active,
                    created_by
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (service_account_key) DO UPDATE SET
                    display_name = EXCLUDED.display_name,
                    description = EXCLUDED.description,
                    scopes_jsonb = EXCLUDED.scopes_jsonb,
                    allowed_project_ids_jsonb = EXCLUDED.allowed_project_ids_jsonb,
                    allowed_workflow_ids_jsonb = EXCLUDED.allowed_workflow_ids_jsonb,
                    is_active = EXCLUDED.is_active,
                    updated_at = now()
                WHERE service_accounts.account_id = EXCLUDED.account_id
                RETURNING
                    id,
                    account_id,
                    service_account_key,
                    display_name,
                    description,
                    scopes_jsonb,
                    allowed_project_ids_jsonb,
                    allowed_workflow_ids_jsonb,
                    is_active,
                    created_by,
                    created_at,
                    updated_at
                """,
                (
                    normalized_account,
                    normalized_key,
                    str(display_name or normalized_key).strip() or normalized_key,
                    str(description or "").strip(),
                    json.dumps(_normalize_string_list(scopes)),
                    json.dumps(_normalize_int_list(allowed_project_ids)),
                    json.dumps(_normalize_string_list(allowed_workflow_ids)),
                    bool(is_active),
                    str(created_by or "system").strip() or "system",
                ),
            )
            row = cur.fetchone()
        if not row:
            raise ValueError("Service account key already exists for another account")
        return self._normalize_service_account_row(dict(row)) if row else None

    def update_service_account(
        self,
        service_account_key: str,
        *,
        display_name: str = "",
        description: str = "",
        scopes: list[str] | None = None,
        allowed_project_ids: list[str] | None = None,
        allowed_workflow_ids: list[str] | None = None,
        account_id: str | None = None,
        is_active: bool = True,
    ) -> dict | None:
        if not self._table_exists("service_accounts"):
            return None
        normalized_key = str(service_account_key or "").strip().lower()
        if not normalized_key:
            raise ValueError(_MESSAGE_SERVICE_ACCOUNT_KEY_IS_REQUIRED)
        normalized_account = str(account_id or "").strip()
        account_clause = _MESSAGE_AND_ACCOUNT_ID_S if normalized_account else ""
        params: list[Any] = [
            str(display_name or normalized_key).strip() or normalized_key,
            str(description or "").strip(),
            json.dumps(_normalize_string_list(scopes)),
            json.dumps(_normalize_int_list(allowed_project_ids)),
            json.dumps(_normalize_string_list(allowed_workflow_ids)),
            bool(is_active),
            normalized_key,
        ]
        if normalized_account:
            params.append(normalized_account)
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                UPDATE service_accounts
                SET
                    display_name = %s,
                    description = %s,
                    scopes_jsonb = %s,
                    allowed_project_ids_jsonb = %s,
                    allowed_workflow_ids_jsonb = %s,
                    is_active = %s,
                    updated_at = now()
                WHERE service_account_key = %s
                {account_clause}
                RETURNING
                    id,
                    account_id,
                    service_account_key,
                    display_name,
                    description,
                    scopes_jsonb,
                    allowed_project_ids_jsonb,
                    allowed_workflow_ids_jsonb,
                    is_active,
                    created_by,
                    created_at,
                    updated_at
                """,
                tuple(params),
            )
            row = cur.fetchone()
        return self._normalize_service_account_row(dict(row)) if row else None

    def delete_service_account(
        self, service_account_key: str, account_id: str | None = None
    ) -> dict | None:
        if not self._table_exists("service_accounts"):
            return None
        normalized_key = str(service_account_key or "").strip().lower()
        if not normalized_key:
            raise ValueError(_MESSAGE_SERVICE_ACCOUNT_KEY_IS_REQUIRED)
        normalized_account = str(account_id or "").strip()
        account_clause = _MESSAGE_AND_ACCOUNT_ID_S if normalized_account else ""
        params: list[Any] = [normalized_key]
        if normalized_account:
            params.append(normalized_account)
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                DELETE FROM service_accounts
                WHERE service_account_key = %s
                {account_clause}
                RETURNING
                    id,
                    account_id,
                    service_account_key,
                    display_name,
                    description,
                    scopes_jsonb,
                    allowed_project_ids_jsonb,
                    allowed_workflow_ids_jsonb,
                    is_active,
                    created_by,
                    created_at,
                    updated_at
                """,
                tuple(params),
            )
            row = cur.fetchone()
        return self._normalize_service_account_row(dict(row)) if row else None

    def list_service_account_tokens(
        self,
        service_account_key: str,
        account_id: str | None = None,
        include_revoked: bool = False,
    ) -> list[dict]:
        if not self._table_exists("machine_tokens"):
            return []
        key = str(service_account_key or "").strip().lower()
        if not key:
            return []
        normalized_account = str(account_id or "").strip()
        account_clause = "AND sa.account_id = %s" if normalized_account else ""
        params: list[Any] = [key, bool(include_revoked)]
        if normalized_account:
            params.append(normalized_account)
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT
                    mt.id,
                    mt.service_account_id,
                    mt.account_id,
                    mt.label,
                    mt.scopes_jsonb,
                    mt.allowed_project_ids_jsonb,
                    mt.allowed_workflow_ids_jsonb,
                    mt.expires_at,
                    mt.last_used_at,
                    mt.revoked,
                    mt.created_by,
                    mt.created_at,
                    mt.updated_at
                FROM machine_tokens mt
                JOIN service_accounts sa ON sa.id = mt.service_account_id
                WHERE sa.service_account_key = %s
                  AND (%s OR mt.revoked = false)
                  {account_clause}
                ORDER BY mt.created_at DESC, mt.id DESC
                """,
                tuple(params),
            )
            rows = cur.fetchall()
        return [self._normalize_machine_token_row(dict(row)) for row in rows]

    def create_machine_token(
        self,
        *,
        service_account_key: str,
        label: str = "",
        scopes: list[str] | None = None,
        allowed_project_ids: list[str] | None = None,
        allowed_workflow_ids: list[str] | None = None,
        expires_in_days: int | None = None,
        account_id: str | None = None,
        created_by: str = "system",
    ) -> dict:
        if not self._table_exists("machine_tokens"):
            raise RuntimeError("Machine token storage is not available")
        normalized_key = str(service_account_key or "").strip().lower()
        if not normalized_key:
            raise ValueError(_MESSAGE_SERVICE_ACCOUNT_KEY_IS_REQUIRED)
        normalized_account = str(account_id or "").strip()
        account_clause = _MESSAGE_AND_ACCOUNT_ID_S if normalized_account else ""
        params: list[Any] = [normalized_key]
        if normalized_account:
            params.append(normalized_account)
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT
                    id,
                    account_id,
                    scopes_jsonb,
                    allowed_project_ids_jsonb,
                    allowed_workflow_ids_jsonb,
                    is_active
                FROM service_accounts
                WHERE service_account_key = %s
                {account_clause}
                LIMIT 1
                """,
                tuple(params),
            )
            sa_row = cur.fetchone()
            if not sa_row:
                raise ValueError(f"Unknown service account '{normalized_key}'")
            if not bool(sa_row.get("is_active", True)):
                raise ValueError(f"Service account '{normalized_key}' is disabled")

            plain_token = f"fs_svc_{uuid.uuid4().hex}.{secrets.token_urlsafe(24)}"
            token_hash = hashlib.sha256(plain_token.encode("utf-8")).hexdigest()
            ttl_days = int(expires_in_days) if expires_in_days is not None else 0
            expires_at_sql = "now() + make_interval(days => %s)" if ttl_days > 0 else "NULL"
            service_scopes = _normalize_string_list(sa_row.get("scopes_jsonb"))
            service_project_ids = _normalize_int_list(sa_row.get("allowed_project_ids_jsonb"))
            service_workflow_ids = _normalize_string_list(sa_row.get("allowed_workflow_ids_jsonb"))

            requested_scopes = _normalize_string_list(scopes)
            requested_project_ids = _normalize_int_list(allowed_project_ids)
            requested_workflow_ids = _normalize_string_list(allowed_workflow_ids)

            if requested_scopes and not _scopes_within(service_scopes, requested_scopes):
                raise ValueError("Token scopes must be a subset of service account scopes")
            if (
                requested_project_ids
                and service_project_ids
                and not set(requested_project_ids).issubset(set(service_project_ids))
            ):
                raise ValueError(
                    "Token allowed_project_ids must be a subset of service account allowed_project_ids"
                )
            if (
                requested_workflow_ids
                and service_workflow_ids
                and not set(requested_workflow_ids).issubset(set(service_workflow_ids))
            ):
                raise ValueError(
                    "Token allowed_workflow_ids must be a subset of service account allowed_workflow_ids"
                )

            # Machine-token scope selection is explicit. An empty selection is a
            # valid least-privilege token and must not inherit the service account.
            effective_scopes = requested_scopes
            effective_project_ids = requested_project_ids or service_project_ids
            effective_workflow_ids = requested_workflow_ids or service_workflow_ids
            account_id = (
                str(sa_row.get("account_id") or _default_account_id()).strip()
                or _default_account_id()
            )

            params: list[Any] = [
                str(sa_row["id"]),
                account_id,
                str(label or f"{normalized_key}-token").strip() or f"{normalized_key}-token",
                token_hash,
                json.dumps(effective_scopes),
                json.dumps(effective_project_ids),
                json.dumps(effective_workflow_ids),
                False,
                str(created_by or "system").strip() or "system",
            ]
            if ttl_days > 0:
                params.insert(7, ttl_days)
            cur.execute(
                f"""
                INSERT INTO machine_tokens (
                    service_account_id,
                    account_id,
                    label,
                    token_hash,
                    scopes_jsonb,
                    allowed_project_ids_jsonb,
                    allowed_workflow_ids_jsonb,
                    expires_at,
                    revoked,
                    created_by
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,{expires_at_sql},%s,%s
                )
                RETURNING
                    id,
                    service_account_id,
                    account_id,
                    label,
                    scopes_jsonb,
                    allowed_project_ids_jsonb,
                    allowed_workflow_ids_jsonb,
                    expires_at,
                    last_used_at,
                    revoked,
                    created_by,
                    created_at,
                    updated_at
                """,
                tuple(params),
            )
            saved = cur.fetchone()
        token_row = self._normalize_machine_token_row(dict(saved or {}))
        token_row["token"] = plain_token
        return token_row

    def update_machine_token(
        self,
        token_id: str,
        *,
        label: str = "",
        scopes: list[str] | None = None,
        allowed_project_ids: list[str] | None = None,
        allowed_workflow_ids: list[str] | None = None,
        account_id: str | None = None,
    ) -> dict | None:
        if not self._table_exists("machine_tokens"):
            return None
        normalized_account = str(account_id or "").strip()
        account_clause = "AND mt.account_id = %s" if normalized_account else ""
        params: list[Any] = [str(token_id)]
        if normalized_account:
            params.append(normalized_account)
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT
                    mt.id,
                    mt.revoked,
                    mt.scopes_jsonb AS token_scopes_jsonb,
                    mt.allowed_project_ids_jsonb AS token_allowed_project_ids_jsonb,
                    mt.allowed_workflow_ids_jsonb AS token_allowed_workflow_ids_jsonb,
                    sa.service_account_key,
                    sa.scopes_jsonb AS service_scopes_jsonb,
                    sa.allowed_project_ids_jsonb AS service_allowed_project_ids_jsonb,
                    sa.allowed_workflow_ids_jsonb AS service_allowed_workflow_ids_jsonb,
                    sa.is_active
                FROM machine_tokens mt
                JOIN service_accounts sa ON sa.id = mt.service_account_id
                WHERE mt.id = %s
                {account_clause}
                LIMIT 1
                """,
                tuple(params),
            )
            token_row = cur.fetchone()
            if not token_row:
                return None
            service_key = str(token_row.get("service_account_key") or "").strip().lower()
            if bool(token_row.get("revoked", False)):
                raise ValueError("Cannot update a revoked machine token")
            if not bool(token_row.get("is_active", True)):
                raise ValueError(f"Service account '{service_key}' is disabled")

            current_scopes = _normalize_string_list(token_row.get("token_scopes_jsonb"))
            current_project_ids = _normalize_int_list(
                token_row.get("token_allowed_project_ids_jsonb")
            )
            current_workflow_ids = _normalize_string_list(
                token_row.get("token_allowed_workflow_ids_jsonb")
            )
            service_scopes = _normalize_string_list(token_row.get("service_scopes_jsonb"))
            service_project_ids = _normalize_int_list(
                token_row.get("service_allowed_project_ids_jsonb")
            )
            service_workflow_ids = _normalize_string_list(
                token_row.get("service_allowed_workflow_ids_jsonb")
            )

            requested_scopes = current_scopes if scopes is None else _normalize_string_list(scopes)
            requested_project_ids = (
                current_project_ids
                if allowed_project_ids is None
                else _normalize_int_list(allowed_project_ids)
            )
            requested_workflow_ids = (
                current_workflow_ids
                if allowed_workflow_ids is None
                else _normalize_string_list(allowed_workflow_ids)
            )

            if requested_scopes and not _scopes_within(service_scopes, requested_scopes):
                raise ValueError("Token scopes must be a subset of service account scopes")
            if (
                requested_project_ids
                and service_project_ids
                and not set(requested_project_ids).issubset(set(service_project_ids))
            ):
                raise ValueError(
                    "Token allowed_project_ids must be a subset of service account allowed_project_ids"
                )
            if (
                requested_workflow_ids
                and service_workflow_ids
                and not set(requested_workflow_ids).issubset(set(service_workflow_ids))
            ):
                raise ValueError(
                    "Token allowed_workflow_ids must be a subset of service account allowed_workflow_ids"
                )

            update_params: list[Any] = [
                str(label or f"{service_key}-token").strip() or f"{service_key}-token",
                json.dumps(requested_scopes) if scopes is not None else None,
                json.dumps(requested_project_ids) if allowed_project_ids is not None else None,
                (json.dumps(requested_workflow_ids) if allowed_workflow_ids is not None else None),
                str(token_id),
            ]
            if normalized_account:
                update_params.append(normalized_account)
            cur.execute(
                f"""
                UPDATE machine_tokens mt
                SET
                    label = %s,
                    scopes_jsonb = COALESCE(%s::jsonb, mt.scopes_jsonb),
                    allowed_project_ids_jsonb = COALESCE(
                        %s::jsonb, mt.allowed_project_ids_jsonb
                    ),
                    allowed_workflow_ids_jsonb = COALESCE(
                        %s::jsonb, mt.allowed_workflow_ids_jsonb
                    ),
                    updated_at = now()
                WHERE mt.id = %s
                {account_clause}
                RETURNING
                    id,
                    service_account_id,
                    account_id,
                    label,
                    scopes_jsonb,
                    allowed_project_ids_jsonb,
                    allowed_workflow_ids_jsonb,
                    expires_at,
                    last_used_at,
                    revoked,
                    created_by,
                    created_at,
                    updated_at
                """,
                tuple(update_params),
            )
            updated = cur.fetchone()
        return self._normalize_machine_token_row(dict(updated)) if updated else None

    def revoke_machine_token(self, token_id: str, account_id: str | None = None) -> dict | None:
        if not self._table_exists("machine_tokens"):
            return None
        normalized_account = str(account_id or "").strip()
        account_clause = _MESSAGE_AND_ACCOUNT_ID_S if normalized_account else ""
        params: list[Any] = [str(token_id)]
        if normalized_account:
            params.append(normalized_account)
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                UPDATE machine_tokens
                SET revoked = true, updated_at = now()
                WHERE id = %s
                {account_clause}
                RETURNING
                    id,
                    service_account_id,
                    account_id,
                    label,
                    scopes_jsonb,
                    allowed_project_ids_jsonb,
                    allowed_workflow_ids_jsonb,
                    expires_at,
                    last_used_at,
                    revoked,
                    created_by,
                    created_at,
                    updated_at
                """,
                tuple(params),
            )
            row = cur.fetchone()
        return self._normalize_machine_token_row(dict(row)) if row else None

    def delete_machine_token(self, token_id: str, account_id: str | None = None) -> dict | None:
        if not self._table_exists("machine_tokens"):
            return None
        normalized_account = str(account_id or "").strip()
        account_clause = _MESSAGE_AND_ACCOUNT_ID_S if normalized_account else ""
        params: list[Any] = [str(token_id)]
        if normalized_account:
            params.append(normalized_account)
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                DELETE FROM machine_tokens
                WHERE id = %s
                {account_clause}
                RETURNING
                    id,
                    service_account_id,
                    account_id,
                    label,
                    scopes_jsonb,
                    allowed_project_ids_jsonb,
                    allowed_workflow_ids_jsonb,
                    expires_at,
                    last_used_at,
                    revoked,
                    created_by,
                    created_at,
                    updated_at
                """,
                tuple(params),
            )
            row = cur.fetchone()
        return self._normalize_machine_token_row(dict(row)) if row else None

    def resolve_machine_token(self, token: str) -> dict | None:
        if not self._table_exists("machine_tokens"):
            return None
        normalized_token = str(token or "").strip()
        if not normalized_token:
            return None
        token_hash = hashlib.sha256(normalized_token.encode("utf-8")).hexdigest()
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                    mt.id AS token_id,
                    mt.account_id AS token_account_id,
                    mt.label AS token_label,
                    mt.scopes_jsonb AS token_scopes_jsonb,
                    mt.allowed_project_ids_jsonb AS token_allowed_project_ids_jsonb,
                    mt.allowed_workflow_ids_jsonb AS token_allowed_workflow_ids_jsonb,
                    mt.expires_at,
                    mt.revoked,
                    sa.id AS service_account_id,
                    sa.account_id AS service_account_account_id,
                    sa.service_account_key,
                    sa.display_name,
                    sa.description,
                    sa.scopes_jsonb AS service_scopes_jsonb,
                    sa.allowed_project_ids_jsonb AS service_allowed_project_ids_jsonb,
                    sa.allowed_workflow_ids_jsonb AS service_allowed_workflow_ids_jsonb,
                    sa.is_active
                FROM machine_tokens mt
                JOIN service_accounts sa ON sa.id = mt.service_account_id
                WHERE mt.token_hash = %s
                LIMIT 1
                """,
                (token_hash,),
            )
            row = cur.fetchone()
            if not row:
                return None
            if bool(row.get("revoked", False)):
                return None
            expires_at = row.get("expires_at")
            if expires_at is not None and expires_at <= datetime.now(UTC):
                return None
            if not bool(row.get("is_active", True)):
                return None

            cur.execute(
                "UPDATE machine_tokens SET last_used_at = now(), updated_at = now() WHERE id = %s",
                (str(row.get("token_id") or ""),),
            )
        account_id = (
            str(
                row.get("token_account_id")
                or row.get("service_account_account_id")
                or _default_account_id()
            ).strip()
            or _default_account_id()
        )
        account_is_active = getattr(self, "_account_is_active", None)
        if callable(account_is_active) and not account_is_active(account_id):
            return None
        scopes = self._effective_machine_token_scopes(
            _normalize_string_list(row.get("token_scopes_jsonb")),
            _normalize_string_list(row.get("service_scopes_jsonb")),
        )
        allowed_project_ids = self._effective_machine_token_ids(
            _normalize_int_list(row.get("token_allowed_project_ids_jsonb")),
            _normalize_int_list(row.get("service_allowed_project_ids_jsonb")),
        )
        allowed_workflow_ids = self._effective_machine_token_workflow_ids(
            _normalize_string_list(row.get("token_allowed_workflow_ids_jsonb")),
            _normalize_string_list(row.get("service_allowed_workflow_ids_jsonb")),
        )
        return {
            "principal_type": "service_account",
            "token_id": str(row.get("token_id") or ""),
            "service_account_id": str(row.get("service_account_id") or ""),
            "service_account_key": str(row.get("service_account_key") or "").strip(),
            "display_name": str(row.get("display_name") or "").strip(),
            "account_id": account_id,
            "scopes": scopes,
            "allowed_project_ids": allowed_project_ids,
            "allowed_workflow_ids": allowed_workflow_ids,
            "roles": ["operator"],
            "is_active": True,
            "auth_source": "machine",
            "token_label": str(row.get("token_label") or "").strip(),
        }
