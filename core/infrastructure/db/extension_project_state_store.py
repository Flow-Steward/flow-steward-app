"""Persistence helpers for non-secret extension project state."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any


class ExtensionProjectStateOwnershipError(ValueError):
    """Raised when project state scope does not match the project owner."""


def _row_to_dict(row: Any, description: Any = None) -> dict[str, Any]:
    if isinstance(row, dict):
        payload = dict(row)
    else:
        columns = [
            col[0] if hasattr(col, "__getitem__") else getattr(col, "name", "")
            for col in (description or [])
        ]
        if not isinstance(row, (tuple, list)) or len(columns) != len(row):
            return {}
        payload = {str(columns[idx]): row[idx] for idx in range(len(columns))}
    for key, value in list(payload.items()):
        if isinstance(value, datetime):
            payload[key] = value.isoformat()
    if isinstance(payload.get("value_jsonb"), str):
        try:
            parsed = json.loads(payload["value_jsonb"])
        except Exception:
            parsed = {}
        payload["value_jsonb"] = parsed
    return payload


def _jsonb(value: Any) -> str:
    payload = value if isinstance(value, (dict, list)) else {}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _connection_predicate(connection_id: str | None, params: list[Any]) -> str:
    normalized = str(connection_id or "").strip()
    if not normalized:
        return "connection_id IS NULL"
    params.append(normalized)
    return "connection_id = %s"


def _install_predicate(extension_install_id: str | None, params: list[Any]) -> str:
    normalized = str(extension_install_id or "").strip()
    if not normalized:
        return "extension_install_id IS NULL"
    params.append(normalized)
    return "extension_install_id = %s"


def _project_account_id(db: Any, project_id: str) -> tuple[bool, str]:
    normalized_project_id = str(project_id or "").strip()
    if not normalized_project_id:
        return False, ""
    get_project = getattr(db, "get_project", None)
    if callable(get_project):
        project = get_project(normalized_project_id) or {}
        return True, str((project or {}).get("account_id") or "").strip()
    if not callable(getattr(db, "cursor", None)):
        return False, ""
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT account_id::text AS account_id
              FROM projects
             WHERE id::text = %s
             LIMIT 1
            """,
            (normalized_project_id,),
        )
        row = cur.fetchone()
        if not row:
            return True, ""
        if isinstance(row, dict):
            return True, str(row.get("account_id") or "").strip()
        return True, str(row[0] if row else "").strip()


def _ensure_project_account_matches(db: Any, *, account_id: str, project_id: str) -> None:
    expected_account_id = str(account_id or "").strip()
    resolved, project_account_id = _project_account_id(db, str(project_id or "").strip())
    if not resolved:
        return
    if not project_account_id or project_account_id != expected_account_id:
        raise ExtensionProjectStateOwnershipError(
            "extension project state project_id does not belong to account_id"
        )


def get_extension_project_state(
    db: Any,
    *,
    account_id: str,
    project_id: str,
    extension_id: str,
    extension_install_id: str | None = None,
    namespace: str,
    state_key: str,
    connection_id: str | None = None,
    include_expired: bool = False,
) -> dict[str, Any] | None:
    _ensure_project_account_matches(db, account_id=account_id, project_id=project_id)
    if not str(extension_install_id or "").strip():
        raise ExtensionProjectStateOwnershipError("extension_install_id is required")
    params: list[Any] = [str(account_id), str(project_id), str(extension_id)]
    install_sql = _install_predicate(extension_install_id, params)
    params.extend((str(namespace), str(state_key)))
    connection_sql = _connection_predicate(connection_id, params)
    expiry_sql = "" if include_expired else "AND (expires_at IS NULL OR expires_at > now())"
    with db.cursor() as cur:
        sql = f"""
            SELECT *
              FROM extension_project_state
             WHERE account_id::text = %s
               AND project_id::text = %s
               AND extension_id = %s
               AND {install_sql}
               AND namespace = %s
               AND state_key = %s
               AND {connection_sql}
               {expiry_sql}
             LIMIT 1
            """  # nosec B608 - interpolated clauses are fixed internal predicates.
        cur.execute(sql, tuple(params))
        row = cur.fetchone()
        return _row_to_dict(row, cur.description) if row else None


def _update_extension_project_state(
    db: Any,
    *,
    account_id: str,
    project_id: str,
    extension_id: str,
    extension_install_id: str | None,
    namespace: str,
    state_key: str,
    value_jsonb: dict[str, Any],
    connection_id: str | None,
    expected_revision: int | None,
    expires_at: str | None,
    updated_by: str | None,
) -> dict[str, Any] | None:
    params: list[Any] = [
        _jsonb(value_jsonb),
        str(expires_at).strip() if expires_at else None,
        str(updated_by).strip() if updated_by else None,
        str(account_id),
        str(project_id),
        str(extension_id),
        str(extension_install_id or "").strip(),
        str(namespace),
        str(state_key),
    ]
    connection_sql = _connection_predicate(connection_id, params)
    revision_sql = ""
    if expected_revision is not None:
        revision_sql = "AND revision = %s"
        params.append(max(1, int(expected_revision)))
    with db.cursor() as cur:
        sql = f"""
            UPDATE extension_project_state
               SET value_jsonb = %s::jsonb,
                   revision = revision + 1,
                   expires_at = %s,
                   updated_by = %s,
                   updated_at = now()
             WHERE account_id::text = %s
               AND project_id::text = %s
               AND extension_id = %s
               AND COALESCE(extension_install_id, '') = %s
               AND namespace = %s
               AND state_key = %s
               AND {connection_sql}
               {revision_sql}
             RETURNING *
            """  # nosec B608 - interpolated clauses are fixed internal predicates.
        cur.execute(sql, tuple(params))
        row = cur.fetchone()
        return _row_to_dict(row, cur.description) if row else None


def insert_extension_project_state(
    db: Any,
    *,
    account_id: str,
    project_id: str,
    extension_id: str,
    extension_install_id: str | None = None,
    namespace: str,
    state_key: str,
    value_jsonb: dict[str, Any],
    connection_id: str | None = None,
    expires_at: str | None = None,
    created_by: str | None = None,
    updated_by: str | None = None,
) -> dict[str, Any]:
    _ensure_project_account_matches(db, account_id=account_id, project_id=project_id)
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO extension_project_state (
                account_id,
                project_id,
                extension_id,
                extension_install_id,
                connection_id,
                namespace,
                state_key,
                value_jsonb,
                revision,
                expires_at,
                created_by,
                updated_by
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,1,%s,%s,%s)
            RETURNING *
            """,
            (
                str(account_id),
                str(project_id),
                str(extension_id),
                str(extension_install_id or "").strip() or None,
                str(connection_id).strip() if connection_id else None,
                str(namespace),
                str(state_key),
                _jsonb(value_jsonb),
                str(expires_at).strip() if expires_at else None,
                str(created_by).strip() if created_by else None,
                str(updated_by or created_by or "").strip() or None,
            ),
        )
        row = cur.fetchone()
        return _row_to_dict(row, cur.description)


def put_extension_project_state(
    db: Any,
    *,
    account_id: str,
    project_id: str,
    extension_id: str,
    extension_install_id: str | None = None,
    namespace: str,
    state_key: str,
    value_jsonb: dict[str, Any],
    connection_id: str | None = None,
    expected_revision: int | None = None,
    expires_at: str | None = None,
    actor_id: str | None = None,
) -> dict[str, Any] | None:
    _ensure_project_account_matches(db, account_id=account_id, project_id=project_id)
    if not str(extension_install_id or "").strip():
        raise ExtensionProjectStateOwnershipError("extension_install_id is required")
    if expected_revision is not None:
        return _update_extension_project_state(
            db,
            account_id=account_id,
            project_id=project_id,
            extension_id=extension_id,
            extension_install_id=extension_install_id,
            namespace=namespace,
            state_key=state_key,
            value_jsonb=value_jsonb,
            connection_id=connection_id,
            expected_revision=expected_revision,
            expires_at=expires_at,
            updated_by=actor_id,
        )

    params: list[Any] = [
        str(account_id),
        str(project_id),
        str(extension_id),
        str(extension_install_id or "").strip() or None,
        str(connection_id).strip() if connection_id else None,
        str(namespace),
        str(state_key),
        _jsonb(value_jsonb),
        str(expires_at).strip() if expires_at else None,
        str(actor_id).strip() if actor_id else None,
        str(actor_id or "").strip() or None,
    ]
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO extension_project_state (
                account_id,
                project_id,
                extension_id,
                extension_install_id,
                connection_id,
                namespace,
                state_key,
                value_jsonb,
                revision,
                expires_at,
                created_by,
                updated_by
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,1,%s,%s,%s)
            ON CONFLICT (
                account_id,
                project_id,
                extension_id,
                COALESCE(extension_install_id, ''::text),
                namespace,
                state_key,
                COALESCE(connection_id, ''::text)
            ) DO UPDATE SET
                value_jsonb = EXCLUDED.value_jsonb,
                revision = extension_project_state.revision + 1,
                expires_at = EXCLUDED.expires_at,
                updated_by = EXCLUDED.updated_by,
                updated_at = now()
            RETURNING *
            """,
            tuple(params),
        )
        row = cur.fetchone()
        return _row_to_dict(row, cur.description) if row else None


def list_extension_project_state(
    db: Any,
    *,
    account_id: str,
    project_id: str,
    extension_id: str,
    extension_install_id: str | None = None,
    namespace: str | None = None,
    connection_id: str | None = None,
    include_all_connections: bool = False,
    include_expired: bool = False,
    limit: int = 200,
) -> list[dict[str, Any]]:
    _ensure_project_account_matches(db, account_id=account_id, project_id=project_id)
    params: list[Any] = [str(account_id), str(project_id), str(extension_id)]
    filters = [
        "account_id::text = %s",
        "project_id::text = %s",
        "extension_id = %s",
    ]
    filters.append(_install_predicate(extension_install_id, params))
    normalized_namespace = str(namespace or "").strip()
    if normalized_namespace:
        filters.append("namespace = %s")
        params.append(normalized_namespace)
    normalized_connection_id = str(connection_id or "").strip()
    if normalized_connection_id:
        filters.append("connection_id = %s")
        params.append(normalized_connection_id)
    elif not include_all_connections:
        filters.append("connection_id IS NULL")
    if not include_expired:
        filters.append("(expires_at IS NULL OR expires_at > now())")
    params.append(max(1, min(int(limit or 200), 1000)))
    with db.cursor() as cur:
        sql = f"""
            SELECT *
              FROM extension_project_state
             WHERE {" AND ".join(filters)}
             ORDER BY namespace, state_key
             LIMIT %s
            """  # nosec B608 - filters are selected from fixed internal predicates.
        cur.execute(sql, tuple(params))
        rows = cur.fetchall() or []
        return [_row_to_dict(row, cur.description) for row in rows]


def delete_extension_project_state(
    db: Any,
    *,
    account_id: str,
    project_id: str,
    extension_id: str,
    extension_install_id: str | None = None,
    namespace: str,
    state_key: str,
    connection_id: str | None = None,
    expected_revision: int | None = None,
) -> int:
    _ensure_project_account_matches(db, account_id=account_id, project_id=project_id)
    params: list[Any] = [str(account_id), str(project_id), str(extension_id)]
    install_sql = _install_predicate(extension_install_id, params)
    params.extend((str(namespace), str(state_key)))
    connection_sql = _connection_predicate(connection_id, params)
    revision_sql = ""
    if expected_revision is not None:
        revision_sql = "AND revision = %s"
        params.append(max(1, int(expected_revision)))
    with db.cursor() as cur:
        sql = f"""
            DELETE FROM extension_project_state
             WHERE account_id::text = %s
               AND project_id::text = %s
               AND extension_id = %s
               AND {install_sql}
               AND namespace = %s
               AND state_key = %s
               AND {connection_sql}
               {revision_sql}
            """  # nosec B608 - interpolated clauses are fixed internal predicates.
        cur.execute(sql, tuple(params))
        return int(getattr(cur, "rowcount", 0) or 0)
