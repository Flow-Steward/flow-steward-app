"""Helpers for user account membership and active-account resolution."""

from __future__ import annotations

from typing import Any

from core.application.auth.config import DEFAULT_ACCOUNT_ID


def _dict_cursor(bl: Any):
    try:
        from core.infrastructure.db.psycopg_runtime import extras as _pge2

        return bl.conn.cursor(cursor_factory=_pge2.RealDictCursor)
    except Exception:
        return bl.conn.cursor()


def user_account_membership_tables_available(bl: Any) -> bool:
    if not bl:
        return False
    try:
        return bool(bl._table_exists("user_account_memberships")) and bool(
            bl._table_exists("accounts")
        )
    except Exception:
        return False


def _accounts_table_available(bl: Any) -> bool:
    if not bl:
        return False
    try:
        return bool(bl._table_exists("accounts"))
    except Exception:
        return False


def _accounts_identity_column(bl: Any) -> str:
    column_exists = getattr(bl, "_column_exists", None)
    if not callable(column_exists):
        return "account_id"
    try:
        if bool(column_exists("accounts", "account_id")):
            return "account_id"
        if bool(column_exists("accounts", "id")):
            return "id"
    except Exception:
        return "account_id"
    return "account_id"


def _accounts_identity_expr(bl: Any, *, alias: str = "") -> str:
    column = _accounts_identity_column(bl)
    prefix = f"{alias}." if alias else ""
    return f"{prefix}{column}"


def _fallback_account_membership(bl: Any, account_id: str) -> dict[str, Any]:
    normalized_account_id = str(account_id or "").strip() or DEFAULT_ACCOUNT_ID
    membership = {
        "account_id": normalized_account_id,
        "display_name": normalized_account_id,
        "is_active": True,
        "is_default": True,
    }
    if not _accounts_table_available(bl):
        return membership
    try:
        account_expr = _accounts_identity_expr(bl)
        with _dict_cursor(bl) as cur:
            cur.execute(
                f"""
                SELECT
                    {account_expr}::text AS account_id,
                    COALESCE(display_name, '') AS display_name,
                    COALESCE(is_active, true) AS is_active
                FROM accounts
                WHERE {account_expr} = %s
                LIMIT 1
                """,
                (normalized_account_id,),
            )
            row = cur.fetchone()
    except Exception:
        row = None
    if not row:
        return membership
    payload = dict(row or {})
    membership["display_name"] = (
        str(payload.get("display_name") or normalized_account_id).strip() or normalized_account_id
    )
    membership["is_active"] = bool(payload.get("is_active", True))
    return membership


def normalize_membership_account_ids(
    account_ids: list[str] | tuple[str, ...] | set[str] | None,
    *,
    fallback_account_id: str | None = None,
) -> list[str]:
    seen: set[str] = set()
    normalized: list[str] = []
    for raw in account_ids or []:
        account_id = str(raw or "").strip()
        if not account_id or account_id in seen:
            continue
        seen.add(account_id)
        normalized.append(account_id)
    fallback = str(fallback_account_id or "").strip()
    if fallback and fallback not in seen:
        normalized.insert(0, fallback)
    return normalized


def list_user_account_memberships(bl: Any, user_id: Any) -> list[dict[str, Any]]:
    normalized_user_id = str(user_id or "").strip()
    if not normalized_user_id:
        return []
    if not user_account_membership_tables_available(bl):
        return []
    account_expr = _accounts_identity_expr(bl, alias="a")
    with _dict_cursor(bl) as cur:
        cur.execute(
            f"""
            SELECT
                uam.account_id,
                COALESCE(a.display_name, '') AS display_name,
                COALESCE(a.is_active, true) AS is_active,
                COALESCE(uam.is_default, false) AS is_default
            FROM user_account_memberships uam
            JOIN accounts a ON {account_expr} = uam.account_id
            WHERE uam.user_id = %s
            ORDER BY uam.is_default DESC, uam.account_id
            """,
            (normalized_user_id,),
        )
        rows = cur.fetchall() or []
    normalized_rows: list[dict[str, Any]] = []
    for row in rows:
        payload = dict(row or {})
        normalized_rows.append(
            {
                "account_id": str(payload.get("account_id") or "").strip(),
                "display_name": str(payload.get("display_name") or "").strip(),
                "is_active": bool(payload.get("is_active", True)),
                "is_default": bool(payload.get("is_default", False)),
            }
        )
    return normalized_rows


def get_user_membership_map(
    bl: Any, user_ids: list[Any] | tuple[Any, ...] | set[Any]
) -> dict[str, list[dict[str, Any]]]:
    normalized_user_ids = sorted({str(user_id or "").strip() for user_id in (user_ids or [])})
    normalized_user_ids = [user_id for user_id in normalized_user_ids if user_id]
    if not normalized_user_ids:
        return {}
    if not user_account_membership_tables_available(bl):
        return {}
    account_expr = _accounts_identity_expr(bl, alias="a")
    with _dict_cursor(bl) as cur:
        cur.execute(
            f"""
            SELECT
                uam.user_id,
                uam.account_id,
                COALESCE(a.display_name, '') AS display_name,
                COALESCE(a.is_active, true) AS is_active,
                COALESCE(uam.is_default, false) AS is_default
            FROM user_account_memberships uam
            JOIN accounts a ON {account_expr} = uam.account_id
            WHERE uam.user_id = ANY(%s)
            ORDER BY uam.user_id, uam.is_default DESC, uam.account_id
            """,
            (normalized_user_ids,),
        )
        rows = cur.fetchall() or []
    mapping: dict[str, list[dict[str, Any]]] = {user_id: [] for user_id in normalized_user_ids}
    for row in rows:
        payload = dict(row or {})
        mapping.setdefault(str(payload.get("user_id") or "").strip(), []).append(
            {
                "account_id": str(payload.get("account_id") or "").strip(),
                "display_name": str(payload.get("display_name") or "").strip(),
                "is_active": bool(payload.get("is_active", True)),
                "is_default": bool(payload.get("is_default", False)),
            }
        )
    return mapping


def enrich_user_payload_with_account_memberships(
    bl: Any, payload: dict[str, Any] | None
) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return payload
    account_id = str(payload.get("account_id") or "").strip()
    memberships = list_user_account_memberships(bl, str(payload.get("id") or "").strip())
    if not memberships and account_id:
        memberships = [_fallback_account_membership(bl, account_id)]
    payload["account_memberships"] = memberships
    payload["account_ids"] = [
        str(item.get("account_id") or "").strip()
        for item in memberships
        if str(item.get("account_id") or "").strip()
    ]
    payload["email"] = str(payload.get("email") or payload.get("username") or "").strip()
    active_membership = next(
        (item for item in memberships if str(item.get("account_id") or "").strip() == account_id),
        memberships[0] if memberships else None,
    )
    payload["account_display_name"] = (
        str(
            (active_membership or {}).get("display_name") or account_id or DEFAULT_ACCOUNT_ID
        ).strip()
        or DEFAULT_ACCOUNT_ID
    )
    if not account_id:
        active_account_id = next(
            (
                str(item.get("account_id") or "").strip()
                for item in memberships
                if bool(item.get("is_default"))
            ),
            "",
        )
        payload["account_id"] = active_account_id or DEFAULT_ACCOUNT_ID
    return payload


def resolve_login_account_memberships(
    bl: Any,
    user: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if not isinstance(user, dict):
        return []
    enriched = enrich_user_payload_with_account_memberships(bl, dict(user)) or {}
    memberships = enriched.get("account_memberships") or []
    return [dict(item or {}) for item in memberships if isinstance(item, dict)]


def sync_user_account_memberships(
    bl: Any,
    *,
    user_id: Any,
    account_ids: list[str] | tuple[str, ...] | set[str] | None,
    primary_account_id: str | None = None,
) -> list[str]:
    normalized_primary = str(primary_account_id or "").strip()
    normalized_accounts = normalize_membership_account_ids(
        account_ids,
        fallback_account_id=normalized_primary or None,
    )
    if not normalized_accounts:
        fallback = normalized_primary or DEFAULT_ACCOUNT_ID
        normalized_accounts = [fallback]
    active_primary = normalized_primary or normalized_accounts[0]

    if not user_account_membership_tables_available(bl):
        return normalized_accounts

    with bl.conn.cursor() as cur:
        cur.execute(
            "DELETE FROM user_account_memberships WHERE user_id = %s AND NOT (account_id = ANY(%s))",
            (str(user_id or "").strip(), normalized_accounts),
        )
        cur.execute(
            "UPDATE user_account_memberships SET is_default = false WHERE user_id = %s",
            (str(user_id or "").strip(),),
        )
        for account_id in normalized_accounts:
            cur.execute(
                """
                INSERT INTO user_account_memberships (user_id, account_id, is_default)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id, account_id) DO UPDATE
                SET is_default = EXCLUDED.is_default,
                    updated_at = now()
                """,
                (str(user_id or "").strip(), account_id, account_id == active_primary),
            )
    return normalized_accounts
