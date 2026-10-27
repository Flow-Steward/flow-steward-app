# mypy: ignore-errors
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime
from typing import Any

from core.infrastructure.db import psycopg_runtime as psycopg_runtime
from core.infrastructure.db.logger_extensions_accounts_common import (
    _default_account_id,
    _normalize_string_list,
)


class PlatformLoggerPersonalAccessTokensMixin:
    def resolve_pat(self, token: str) -> dict | None:
        """Resolve a bearer token as a personal access token.

        Returns a principal dict with principal_type='user' and the user's
        actual roles, plus any PAT-level scope/boundary restrictions.
        """
        if not self._table_exists("personal_access_tokens"):
            return None
        normalized_token = str(token or "").strip()
        if not normalized_token:
            return None
        token_hash = hashlib.sha256(normalized_token.encode("utf-8")).hexdigest()
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                    pat.id AS pat_id,
                    pat.user_id,
                    pat.account_id AS pat_account_id,
                    pat.label AS pat_label,
                    pat.scopes_jsonb AS pat_scopes_jsonb,
                    pat.allowed_project_ids_jsonb AS pat_allowed_project_ids_jsonb,
                    pat.allowed_workflow_ids_jsonb AS pat_allowed_workflow_ids_jsonb,
                    pat.expires_at,
                    pat.revoked,
                    u.id AS user_id_check,
                    u.username,
                    u.display_name,
                    u.is_active
                FROM personal_access_tokens pat
                JOIN users u ON u.id = pat.user_id
                WHERE pat.token_hash = %s
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
                "UPDATE personal_access_tokens SET last_used_at = now(), updated_at = now() WHERE id = %s",
                (str(row.get("pat_id") or ""),),
            )

            # Fetch user's actual roles
            cur.execute(
                """
                SELECT r.key FROM user_roles ur
                JOIN roles r ON r.id = ur.role_id
                WHERE ur.user_id = %s ORDER BY r.key
                """,
                (str(row.get("user_id") or ""),),
            )
            roles = [str(r["key"]) for r in cur.fetchall() if r and r.get("key")]

        account_id = (
            str(row.get("pat_account_id") or _default_account_id()).strip() or _default_account_id()
        )
        membership_allowed = getattr(self, "_user_has_active_account_membership", None)
        if callable(membership_allowed) and not membership_allowed(
            str(row.get("user_id") or ""), account_id
        ):
            return None
        scopes = _normalize_string_list(row.get("pat_scopes_jsonb"))
        allowed_project_ids = _normalize_string_list(row.get("pat_allowed_project_ids_jsonb"))
        allowed_workflow_ids = _normalize_string_list(row.get("pat_allowed_workflow_ids_jsonb"))
        return {
            "principal_type": "user",
            "id": str(row.get("user_id") or ""),
            "username": str(row.get("username") or "").strip(),
            "display_name": str(row.get("display_name") or "").strip(),
            "account_id": account_id,
            "scopes": scopes,
            "allowed_project_ids": allowed_project_ids,
            "allowed_workflow_ids": allowed_workflow_ids,
            "roles": roles,
            "is_active": True,
            "auth_source": "pat",
            "pat_id": str(row.get("pat_id") or ""),
            "pat_label": str(row.get("pat_label") or "").strip(),
        }

    def create_pat(
        self,
        *,
        user_id: str,
        label: str = "",
        scopes: list[str] | None = None,
        allowed_project_ids: list[str] | None = None,
        allowed_workflow_ids: list[str] | None = None,
        expires_in_days: int | None = None,
        account_id: str | None = None,
    ) -> dict:
        """Create a personal access token. Returns dict with id and raw_token (shown once)."""
        resolved_account = str(account_id or "").strip()
        if not resolved_account:
            raise ValueError("account_id is required to create a personal access token")
        raw_token = f"fs_pat_{secrets.token_urlsafe(32)}"
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        scopes_json = json.dumps(scopes or [])
        project_ids_json = json.dumps(allowed_project_ids or [])
        workflow_ids_json = json.dumps(allowed_workflow_ids or [])

        expires_at = None
        if expires_in_days and int(expires_in_days) > 0:
            from datetime import timedelta

            expires_at = datetime.now(UTC) + timedelta(days=int(expires_in_days))

        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO personal_access_tokens
                    (user_id, account_id, label, token_hash, scopes_jsonb,
                     allowed_project_ids_jsonb, allowed_workflow_ids_jsonb, expires_at)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s)
                RETURNING id, label, scopes_jsonb, allowed_project_ids_jsonb,
                          allowed_workflow_ids_jsonb, expires_at, created_at, account_id
                """,
                (
                    str(user_id),
                    resolved_account,
                    str(label or "").strip(),
                    token_hash,
                    scopes_json,
                    project_ids_json,
                    workflow_ids_json,
                    expires_at,
                ),
            )
            row = cur.fetchone()
        return {
            "id": str(row["id"]),
            "account_id": str(row.get("account_id") or resolved_account).strip(),
            "label": str(row.get("label") or "").strip(),
            "raw_token": raw_token,
            "scopes": _normalize_string_list(row.get("scopes_jsonb")),
            "allowed_project_ids": _normalize_string_list(row.get("allowed_project_ids_jsonb")),
            "allowed_workflow_ids": _normalize_string_list(row.get("allowed_workflow_ids_jsonb")),
            "expires_at": str(row["expires_at"]) if row.get("expires_at") else None,
            "created_at": str(row["created_at"]),
        }

    def list_user_pats(self, user_id: str, *, include_revoked: bool = False) -> list[dict]:
        """List personal access tokens for a user."""
        return self.list_user_pats_paginated(
            user_id,
            include_revoked=include_revoked,
            limit=None,
            offset=0,
        )

    def list_user_pats_paginated(
        self,
        user_id: str,
        *,
        include_revoked: bool = False,
        limit: int | None = 25,
        offset: int = 0,
    ) -> list[dict]:
        """List personal access tokens for a user with optional pagination."""
        limit_sql = ""
        params: list[Any] = [str(user_id)]
        if limit is not None:
            limit_sql = " LIMIT %s OFFSET %s"
            params.extend([max(1, min(int(limit or 25), 500)), max(0, int(offset or 0))])
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            if include_revoked:
                cur.execute(
                    f"""
                    SELECT id, label, scopes_jsonb, allowed_project_ids_jsonb,
                           allowed_workflow_ids_jsonb, expires_at, last_used_at,
                           revoked, created_at, updated_at
                    FROM personal_access_tokens
                    WHERE user_id = %s
                    ORDER BY created_at DESC
                    {limit_sql}
                    """,
                    tuple(params),
                )
            else:
                cur.execute(
                    f"""
                    SELECT id, label, scopes_jsonb, allowed_project_ids_jsonb,
                           allowed_workflow_ids_jsonb, expires_at, last_used_at,
                           revoked, created_at, updated_at
                    FROM personal_access_tokens
                    WHERE user_id = %s AND revoked = false
                    ORDER BY created_at DESC
                    {limit_sql}
                    """,
                    tuple(params),
                )
            rows = cur.fetchall()
        result = []
        for row in rows:
            expired = False
            if row.get("expires_at") is not None:
                expired = row["expires_at"] <= datetime.now(UTC)
            result.append(
                {
                    "id": str(row["id"]),
                    "label": str(row.get("label") or "").strip(),
                    "scopes": _normalize_string_list(row.get("scopes_jsonb")),
                    "allowed_project_ids": _normalize_string_list(
                        row.get("allowed_project_ids_jsonb")
                    ),
                    "allowed_workflow_ids": _normalize_string_list(
                        row.get("allowed_workflow_ids_jsonb")
                    ),
                    "expires_at": str(row["expires_at"]) if row.get("expires_at") else None,
                    "last_used_at": str(row["last_used_at"]) if row.get("last_used_at") else None,
                    "revoked": bool(row.get("revoked", False)),
                    "expired": expired,
                    "created_at": str(row["created_at"]),
                }
            )
        return result

    def count_user_pats(self, user_id: str, *, include_revoked: bool = False) -> int:
        """Return total personal access token count for a user."""
        with self.conn.cursor() as cur:
            if include_revoked:
                cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM personal_access_tokens
                    WHERE user_id = %s
                    """,
                    (str(user_id),),
                )
            else:
                cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM personal_access_tokens
                    WHERE user_id = %s AND revoked = false
                    """,
                    (str(user_id),),
                )
            row = cur.fetchone()
            return int(row[0] if row else 0)

    def revoke_pat(self, pat_id: str, user_id: str) -> dict | None:
        """Revoke a personal access token. Returns the revoked token info or None."""
        with self.conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                """
                UPDATE personal_access_tokens
                SET revoked = true, updated_at = now()
                WHERE id = %s AND user_id = %s
                RETURNING id, label, revoked
                """,
                (str(pat_id), str(user_id)),
            )
            row = cur.fetchone()
        if not row:
            return None
        return {
            "id": str(row["id"]),
            "label": str(row.get("label") or "").strip(),
            "revoked": True,
        }
