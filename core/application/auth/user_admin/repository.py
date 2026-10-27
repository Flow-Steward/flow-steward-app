"""Persistence operations for local-auth user administration."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.application.auth.account_memberships import sync_user_account_memberships
from core.application.management.errors import ServiceConflictError, ServiceNotFoundError
from core.infrastructure.db.db_pool import transaction_connection


def _commit_if_possible(bl: Any) -> None:
    commit = getattr(getattr(bl, "conn", None), "commit", None)
    if callable(commit):
        commit()


def _lock_user_lifecycle(cur: Any) -> None:
    cur.execute(
        "SELECT pg_advisory_xact_lock(hashtext(%s))",
        ("flow-steward:user-lifecycle",),
    )


def _table_exists(bl: Any, table_name: str) -> bool:
    checker = getattr(bl, "_table_exists", None)
    if not callable(checker):
        return True
    try:
        return bool(checker(table_name))
    except Exception:
        return False


def _revoke_user_auth_material(cur: Any, bl: Any, user_id: str) -> int:
    cur.execute(
        """
        UPDATE auth_sessions
        SET revoked = true, updated_at = now()
        WHERE user_id = %s
          AND revoked = false
        """,
        (user_id,),
    )
    revoked_sessions = int(getattr(cur, "rowcount", 0) or 0)
    if _table_exists(bl, "personal_access_tokens"):
        cur.execute(
            """
            UPDATE personal_access_tokens
            SET revoked = true, updated_at = now()
            WHERE user_id = %s
              AND revoked = false
            """,
            (user_id,),
        )
    return revoked_sessions


def list_auth_users(
    bl: Any,
    *,
    search: str,
    include_inactive: bool,
    include_deleted: bool,
    limit: int,
    offset: int,
    role: str,
    active_filter: bool | None,
    managed_account_ids: list[str] | None,
) -> tuple[list[dict[str, Any]], int, int, int]:
    normalized_limit = max(1, min(int(limit or 50), 200))
    normalized_offset = max(0, int(offset or 0))
    normalized_search = str(search or "").strip()
    normalized_role = str(role or "").strip().lower()
    conditions: list[str] = []
    params: list[Any] = []

    if not include_deleted:
        conditions.append("AND u.deleted_at IS NULL")
    if active_filter is not None:
        conditions.append("AND u.is_active = %s")
        params.append(bool(active_filter))
    elif not include_inactive:
        conditions.append("AND u.is_active = true")
    if normalized_search:
        like = f"%{normalized_search}%"
        conditions.append("AND (u.username ILIKE %s OR COALESCE(u.display_name, '') ILIKE %s)")
        params.extend([like, like])
    if normalized_role:
        conditions.append(
            """
            AND EXISTS (
                SELECT 1 FROM user_roles ur_scope
                JOIN roles r_scope ON r_scope.id = ur_scope.role_id
                WHERE ur_scope.user_id = u.id AND r_scope.key = %s
            )
            """
        )
        params.append(normalized_role)
    if managed_account_ids is not None:
        normalized_account_ids = [
            str(account_id or "").strip()
            for account_id in managed_account_ids
            if str(account_id or "").strip()
        ]
        if not normalized_account_ids:
            conditions.append("AND FALSE")
        else:
            conditions.append(
                """
                AND (
                    u.account_id = ANY(%s)
                    OR EXISTS (
                        SELECT 1 FROM user_account_memberships uam_scope
                        WHERE uam_scope.user_id = u.id
                          AND uam_scope.account_id = ANY(%s)
                    )
                )
                """
            )
            params.extend([normalized_account_ids, normalized_account_ids])

    where_sql = " ".join(conditions)
    with _dict_cursor(bl) as cur:
        cur.execute(
            f"SELECT COUNT(*) AS total FROM users u WHERE 1 = 1 {where_sql}",  # nosec B608 - fixed predicates.
            tuple(params),
        )
        total_row = cur.fetchone() or {}
        if isinstance(total_row, dict):
            total = int(total_row.get("total") or 0)
        else:
            positional_total = total_row[0] if total_row else 0
            total = int(positional_total or 0)
        cur.execute(
            f"""
            SELECT u.id, u.username, u.display_name, u.is_active,
                   u.must_change_password AS must_change_password,
                   u.deleted_at AS deleted_at,
                   u.account_id AS account_id,
                   u.created_at, u.updated_at,
                   COALESCE(array_remove(array_agg(r.key ORDER BY r.key), NULL), ARRAY[]::text[]) AS roles
              FROM users u
              LEFT JOIN user_roles ur ON ur.user_id = u.id
              LEFT JOIN roles r ON r.id = ur.role_id
             WHERE 1 = 1 {where_sql}
             GROUP BY u.id
             ORDER BY u.id
             LIMIT %s OFFSET %s
            """,  # nosec B608 - fixed projections and predicates.
            (*params, normalized_limit, normalized_offset),
        )
        rows = [dict(row or {}) for row in (cur.fetchall() or [])]
    return rows, total, normalized_limit, normalized_offset


def _dict_cursor(bl: Any):
    return _dict_cursor_for_connection(bl.conn)


def _dict_cursor_for_connection(conn: Any):
    try:
        from core.infrastructure.db.psycopg_runtime import extras as _pge2

        return conn.cursor(cursor_factory=_pge2.RealDictCursor)
    except Exception:
        return conn.cursor()


def create_auth_user(
    bl: Any,
    *,
    email: str,
    password_hash_value: str,
    display_name: str,
    is_active: bool,
    must_change_password: bool,
    roles: list[str],
    primary_account_id: str,
    requested_account_ids: list[str],
    seed_default_roles: Callable[[Any], None],
) -> str:
    seed_default_roles(bl)
    with bl.conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO users (
                username,
                password_hash,
                display_name,
                is_active,
                must_change_password,
                account_id
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                str(email or "").strip(),
                str(password_hash_value or ""),
                str(display_name or "").strip(),
                bool(is_active),
                bool(must_change_password),
                str(primary_account_id or "").strip(),
            ),
        )
        row = cur.fetchone()
        user_id = str((row[0] if row else 0) or "").strip()
        if not user_id:
            raise RuntimeError("failed to create user")
        cur.execute("DELETE FROM user_roles WHERE user_id = %s", (user_id,))
        cur.execute(
            """
            INSERT INTO user_roles (user_id, role_id)
            SELECT %s, id
            FROM roles
            WHERE key = ANY(%s)
            ON CONFLICT (user_id, role_id) DO NOTHING
            """,
            (user_id, roles),
        )
        sync_user_account_memberships(
            bl,
            user_id=user_id,
            account_ids=requested_account_ids,
            primary_account_id=primary_account_id,
        )
    _commit_if_possible(bl)
    return user_id


def update_auth_user(
    bl: Any,
    *,
    user_id: Any,
    existing_user: dict[str, Any],
    display_name: str | None,
    is_active: bool | None,
    password_hash_value: str | None,
    must_change_password: bool | None,
    roles: list[str] | None,
    requested_account_ids: list[str] | None,
    primary_account_id: str | None,
    revoke_sessions: bool,
) -> int:
    normalized_user_id = str(user_id or "").strip()
    revoke_auth_material = (
        is_active is not None
        or password_hash_value is not None
        or must_change_password is not None
        or roles is not None
        or requested_account_ids is not None
    )
    with transaction_connection(bl.conn) as conn, conn.cursor() as cur:
        if is_active is not None or roles is not None:
            _lock_user_lifecycle(cur)
        if display_name is not None:
            cur.execute(
                "UPDATE users SET display_name = %s WHERE id = %s",
                (
                    str(display_name or "").strip() or existing_user.get("username"),
                    normalized_user_id,
                ),
            )
        if is_active is not None:
            cur.execute(
                "UPDATE users SET is_active = %s WHERE id = %s",
                (bool(is_active), normalized_user_id),
            )
        if password_hash_value is not None:
            cur.execute(
                "UPDATE users SET password_hash = %s WHERE id = %s",
                (str(password_hash_value or ""), normalized_user_id),
            )
        if must_change_password is not None:
            cur.execute(
                "UPDATE users SET must_change_password = %s WHERE id = %s",
                (bool(must_change_password), normalized_user_id),
            )
        if roles is not None:
            cur.execute("DELETE FROM user_roles WHERE user_id = %s", (normalized_user_id,))
            if roles:
                cur.execute(
                    """
                    INSERT INTO user_roles (user_id, role_id)
                    SELECT %s, id
                    FROM roles
                    WHERE key = ANY(%s)
                    ON CONFLICT (user_id, role_id) DO NOTHING
                    """,
                    (normalized_user_id, roles),
                )
        if requested_account_ids is not None and primary_account_id is not None:
            cur.execute(
                "UPDATE users SET account_id = %s WHERE id = %s",
                (str(primary_account_id or "").strip(), normalized_user_id),
            )
            sync_user_account_memberships(
                bl,
                user_id=normalized_user_id,
                account_ids=requested_account_ids,
                primary_account_id=primary_account_id,
            )
        revoked_sessions = 0
        if revoke_sessions and revoke_auth_material:
            revoked_sessions = _revoke_user_auth_material(cur, bl, normalized_user_id)
    return revoked_sessions


def force_password_reset(
    bl: Any,
    *,
    user_id: Any,
) -> int:
    with bl.conn.cursor() as cur:
        cur.execute(
            "UPDATE users SET must_change_password = true WHERE id = %s",
            (str(user_id or "").strip(),),
        )
        revoked_sessions = _revoke_user_auth_material(cur, bl, str(user_id or "").strip())
    _commit_if_possible(bl)
    return revoked_sessions


def delete_auth_user(
    bl: Any,
    *,
    user_id: Any,
    deleted_by: str,
) -> int:
    normalized_user_id = str(user_id or "").strip()
    with (
        transaction_connection(bl.conn) as conn,
        _dict_cursor_for_connection(conn) as cur,
    ):
        _lock_user_lifecycle(cur)
        cur.execute(
            """
            SELECT u.id::text AS id, u.is_active,
                   EXISTS (
                       SELECT 1
                       FROM user_roles ur
                       JOIN roles r ON r.id = ur.role_id
                       WHERE ur.user_id = u.id AND r.key = 'admin'
                   ) AS is_admin
            FROM users u
            WHERE u.id = %s AND u.deleted_at IS NULL
            FOR UPDATE OF u
            """,
            (normalized_user_id,),
        )
        target = cur.fetchone()
        if not target:
            raise ServiceNotFoundError(f"User id={normalized_user_id!s} not found")

        cur.execute("SELECT COUNT(*) AS total FROM users WHERE deleted_at IS NULL")
        total_row = cur.fetchone() or {}
        total = int((total_row.get("total") if isinstance(total_row, dict) else total_row[0]) or 0)
        if total <= 1:
            raise ServiceConflictError("The last user cannot be deleted")

        is_active = bool(target.get("is_active"))
        is_admin = bool(target.get("is_admin"))
        if is_active and is_admin:
            cur.execute(
                """
                SELECT COUNT(*) AS total
                FROM users u
                WHERE u.deleted_at IS NULL
                  AND u.is_active = true
                  AND EXISTS (
                           SELECT 1
                           FROM user_roles ur
                           JOIN roles r ON r.id = ur.role_id
                           WHERE ur.user_id = u.id AND r.key = 'admin'
                      )
                """
            )
            admin_row = cur.fetchone() or {}
            active_admins = int(
                (admin_row.get("total") if isinstance(admin_row, dict) else admin_row[0]) or 0
            )
            if active_admins <= 1:
                raise ServiceConflictError("The last active administrator cannot be deleted")

        revoked_sessions = _revoke_user_auth_material(cur, bl, normalized_user_id)
        cur.execute(
            """
            UPDATE users
            SET is_active = false,
                deleted_at = now(),
                deleted_by = %s,
                updated_at = now()
            WHERE id = %s AND deleted_at IS NULL
            """,
            (str(deleted_by or "").strip(), normalized_user_id),
        )
    return revoked_sessions
