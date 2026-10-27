"""Persistence helpers for named model-provider configurations."""

from __future__ import annotations

import json
from typing import Any

_MESSAGE_PROJECT_ID_AND_TENANT_ID_MUST_BE_PROVIDED_TOGETHER = (
    "project_id and tenant_id must be provided together"
)

_CLAIM_VERIFICATION_SQL = """
    UPDATE model_providers
       SET verification_status = 'pending',
           verification_generation = %s,
           verification_started_at = now(),
           verification_error_safe = NULL,
           updated_at = now()
     WHERE id::text = %s
       AND account_id = %s
       AND (
           verification_status <> 'pending'
           OR verification_started_at IS NULL
           OR verification_started_at < now() - interval '10 minutes'
       )
       AND provider_type IS NOT DISTINCT FROM %s
       AND base_url IS NOT DISTINCT FROM %s
       AND api_key_ref IS NOT DISTINCT FROM %s
       AND api_version IS NOT DISTINCT FROM %s
       AND deployment_name IS NOT DISTINCT FROM %s
       AND default_model IS NOT DISTINCT FROM %s
       AND config_jsonb IS NOT DISTINCT FROM %s::jsonb
       AND upstream_revision IS NOT DISTINCT FROM %s
     RETURNING verification_generation
"""
_CLAIM_VERIFICATION_WITH_SECRET_SQL = """
    UPDATE model_providers
       SET verification_status = 'pending',
           verification_generation = %s,
           verification_started_at = now(),
           verification_error_safe = NULL,
           updated_at = now()
     WHERE id::text = %s
       AND account_id = %s
       AND (
           verification_status <> 'pending'
           OR verification_started_at IS NULL
           OR verification_started_at < now() - interval '10 minutes'
       )
       AND provider_type IS NOT DISTINCT FROM %s
       AND base_url IS NOT DISTINCT FROM %s
       AND api_key_ref IS NOT DISTINCT FROM %s
       AND api_version IS NOT DISTINCT FROM %s
       AND deployment_name IS NOT DISTINCT FROM %s
       AND default_model IS NOT DISTINCT FROM %s
       AND config_jsonb IS NOT DISTINCT FROM %s::jsonb
       AND upstream_revision IS NOT DISTINCT FROM %s
       AND (SELECT encrypted_value FROM secret_refs
             WHERE account_id = %s AND ref_key = %s) IS NOT DISTINCT FROM %s
     RETURNING verification_generation
"""


def _cursor_kwargs() -> dict[str, Any]:
    try:
        _pge2 = __import__("psycopg_runtime.extras", fromlist=["RealDictCursor"])
        return {"cursor_factory": _pge2.RealDictCursor}
    except ModuleNotFoundError:
        try:
            from core.infrastructure.db.psycopg_runtime import extras as _pge2

            return {"cursor_factory": _pge2.RealDictCursor}
        except Exception:
            return {}
    except ImportError:
        return {}
    except Exception:
        return {}


def _row_to_dict(row: Any, description: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)

    columns = [
        col[0] if hasattr(col, "__getitem__") else getattr(col, "name", "")
        for col in (description or [])
    ]
    if isinstance(row, (tuple, list)) and columns and len(columns) == len(row):
        return {str(columns[idx]): row[idx] for idx in range(len(columns))}

    try:
        return dict(row)
    except Exception:
        return {}


def upsert_secret_ref(
    conn,
    *,
    account_id: str,
    ref_key: str,
    encrypted_value: str,
    project_id: str | None = None,
    tenant_id: str | None = None,
) -> None:
    if project_id is not None or tenant_id is not None:
        if not str(project_id or "").strip() or not str(tenant_id or "").strip():
            raise ValueError(_MESSAGE_PROJECT_ID_AND_TENANT_ID_MUST_BE_PROVIDED_TOGETHER)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO secret_refs
                       (account_id, project_id, tenant_id, ref_key, encrypted_value)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (account_id, ref_key)
                DO UPDATE SET encrypted_value = EXCLUDED.encrypted_value
                 WHERE secret_refs.project_id = EXCLUDED.project_id
                   AND secret_refs.tenant_id = EXCLUDED.tenant_id
                """,
                (account_id, project_id, tenant_id, ref_key, encrypted_value),
            )
            if not cur.rowcount:
                raise ValueError("secret ref belongs to a different project scope")
        return
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO secret_refs (account_id, ref_key, encrypted_value)
            VALUES (%s, %s, %s)
            ON CONFLICT (account_id, ref_key)
            DO UPDATE SET encrypted_value = EXCLUDED.encrypted_value
            """,
            (account_id, ref_key, encrypted_value),
        )


def get_secret_ref_ciphertext(
    conn,
    *,
    account_id: str,
    ref_key: str,
    project_id: str | None = None,
    tenant_id: str | None = None,
) -> str | None:
    with conn.cursor() as cur:
        query = "SELECT encrypted_value FROM secret_refs WHERE account_id = %s AND ref_key = %s"
        params: list[str] = [str(account_id), str(ref_key)]
        if project_id is not None or tenant_id is not None:
            if not str(project_id or "").strip() or not str(tenant_id or "").strip():
                raise ValueError(_MESSAGE_PROJECT_ID_AND_TENANT_ID_MUST_BE_PROVIDED_TOGETHER)
            query += " AND project_id = %s AND tenant_id = %s"
            params.extend((str(project_id), str(tenant_id)))
        cur.execute(query, tuple(params))
        row = cur.fetchone()
    if isinstance(row, dict):
        value = row.get("encrypted_value")
    elif isinstance(row, (list, tuple)) and row:
        value = row[0]
    else:
        value = None
    return str(value) if value is not None else None


def insert_model_provider(
    conn,
    *,
    account_id: str,
    name: str,
    provider_type: str,
    base_url: str | None,
    api_key_ref: str | None,
    api_version: str | None,
    deployment_name: str | None,
    default_model: str | None,
    upstream_revision: str | None,
    config: dict[str, Any] | None,
) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO model_providers
                   (account_id, name, provider_type, base_url, api_key_ref,
                    api_version, deployment_name, default_model, upstream_revision,
                    config_jsonb, is_self_hosted)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true)
               RETURNING id
            """,
            (
                account_id,
                name,
                provider_type,
                base_url,
                api_key_ref,
                api_version,
                deployment_name,
                default_model,
                upstream_revision,
                json.dumps(config or {}),
            ),
        )
        row = cur.fetchone()
    return str(row[0]) if row else None


def list_enabled_model_providers(
    conn, *, account_id: str, routable_only: bool = False
) -> list[dict[str, Any]]:
    kwargs = _cursor_kwargs()
    with conn.cursor(**kwargs) as cur:
        if routable_only:
            cur.execute(
                """
                SELECT *
                  FROM model_providers
                 WHERE account_id = %s
                   AND enabled = true
                   AND verification_status = 'passed'
                   AND lifecycle = 'active'
                   AND verified_at >= now() - interval '30 days'
                   AND verified_at <= now()
                 ORDER BY name
                """,
                (account_id,),
            )
        else:
            cur.execute(
                """
                SELECT *
                  FROM model_providers
                 WHERE account_id = %s
                   AND enabled = true
                 ORDER BY name
                """,
                (account_id,),
            )
        rows = cur.fetchall()
        description = list(cur.description or [])

    normalized_rows: list[dict[str, Any]] = []
    for row in rows:
        payload = _row_to_dict(row, description)
        if not payload:
            continue
        payload["id"] = str(payload.get("id") or "").strip()
        if "account_id" in payload:
            payload["account_id"] = str(payload.get("account_id") or "").strip()
        payload.pop("api_key_ref", None)
        payload.pop("config_jsonb", None)
        normalized_rows.append(payload)
    return normalized_rows


def get_model_provider(
    conn, *, provider_id: Any, account_id: str | None = None
) -> dict[str, Any] | None:
    kwargs = _cursor_kwargs()
    normalized_account_id = str(account_id or "").strip()
    with conn.cursor(**kwargs) as cur:
        if normalized_account_id:
            cur.execute(
                """
                SELECT *
                  FROM model_providers
                 WHERE id::text = %s
                   AND account_id = %s
                """,
                (str(provider_id), normalized_account_id),
            )
        else:
            cur.execute(
                """
                SELECT *
                  FROM model_providers
                 WHERE id::text = %s
                """,
                (str(provider_id),),
            )
        row = cur.fetchone()
        description = list(cur.description or [])
    payload = _row_to_dict(row, description) if row is not None else {}
    if payload:
        payload["id"] = str(payload.get("id") or "").strip()
        payload["account_id"] = str(payload.get("account_id") or "").strip()
    return payload or None


def lock_model_provider_for_update(
    conn, *, provider_id: Any, account_id: str
) -> dict[str, Any] | None:
    """Lock and return one account-scoped provider routing snapshot."""
    kwargs = _cursor_kwargs()
    with conn.cursor(**kwargs) as cur:
        cur.execute(
            """
            SELECT *
              FROM model_providers
             WHERE id::text = %s
               AND account_id = %s
             FOR UPDATE
            """,
            (str(provider_id), str(account_id)),
        )
        row = cur.fetchone()
        description = list(cur.description or [])
    payload = _row_to_dict(row, description) if row is not None else {}
    if payload:
        payload["id"] = str(payload.get("id") or "").strip()
        payload["account_id"] = str(payload.get("account_id") or "").strip()
    return payload or None


def update_model_provider(
    conn,
    *,
    provider_id: Any,
    name: str,
    provider_type: str,
    base_url: str | None,
    api_key_ref: str | None,
    api_version: str | None,
    deployment_name: str | None,
    default_model: str | None,
    upstream_revision: str | None = None,
    config: dict[str, Any] | None = None,
    account_id: str | None = None,
    invalidate_verification: bool = True,
) -> dict[str, Any] | None:
    kwargs = _cursor_kwargs()
    normalized_account_id = str(account_id or "").strip()
    params: tuple[Any, ...] = (
        name,
        provider_type,
        base_url,
        api_key_ref,
        api_version,
        deployment_name,
        default_model,
        upstream_revision,
        json.dumps(config or {}),
        bool(invalidate_verification),
        bool(invalidate_verification),
        bool(invalidate_verification),
        bool(invalidate_verification),
        bool(invalidate_verification),
        bool(invalidate_verification),
        str(provider_id),
    )
    with conn.cursor(**kwargs) as cur:
        if normalized_account_id:
            cur.execute(
                """
                UPDATE model_providers
                   SET name = %s,
                       provider_type = %s,
                       base_url = %s,
                       api_key_ref = COALESCE(%s, api_key_ref),
                       api_version = %s,
                       deployment_name = %s,
                       default_model = %s,
                       upstream_revision = %s,
                       config_jsonb = %s::jsonb,
                       verification_status = CASE WHEN %s THEN 'stale' ELSE verification_status END,
                       verified_capabilities_jsonb = CASE WHEN %s THEN '[]'::jsonb ELSE verified_capabilities_jsonb END,
                       verification_error_safe = CASE WHEN %s THEN NULL ELSE verification_error_safe END,
                       verified_at = CASE WHEN %s THEN NULL ELSE verified_at END,
                       verification_started_at = CASE WHEN %s THEN NULL ELSE verification_started_at END,
                       verification_generation = CASE WHEN %s THEN NULL ELSE verification_generation END,
                       updated_at = now()
                 WHERE id::text = %s
                   AND account_id = %s
                 RETURNING *
                """,
                (*params, normalized_account_id),
            )
        else:
            cur.execute(
                """
                UPDATE model_providers
                   SET name = %s,
                       provider_type = %s,
                       base_url = %s,
                       api_key_ref = COALESCE(%s, api_key_ref),
                       api_version = %s,
                       deployment_name = %s,
                       default_model = %s,
                       upstream_revision = %s,
                       config_jsonb = %s::jsonb,
                       verification_status = CASE WHEN %s THEN 'stale' ELSE verification_status END,
                       verified_capabilities_jsonb = CASE WHEN %s THEN '[]'::jsonb ELSE verified_capabilities_jsonb END,
                       verification_error_safe = CASE WHEN %s THEN NULL ELSE verification_error_safe END,
                       verified_at = CASE WHEN %s THEN NULL ELSE verified_at END,
                       verification_started_at = CASE WHEN %s THEN NULL ELSE verification_started_at END,
                       verification_generation = CASE WHEN %s THEN NULL ELSE verification_generation END,
                       updated_at = now()
                 WHERE id::text = %s
                 RETURNING *
                """,
                params,
            )
        row = cur.fetchone()
        description = list(cur.description or [])
    payload = _row_to_dict(row, description) if row is not None else {}
    if payload:
        payload["id"] = str(payload.get("id") or "").strip()
        payload["account_id"] = str(payload.get("account_id") or "").strip()
        payload.pop("api_key_ref", None)
        payload.pop("config_jsonb", None)
    return payload or None


def _provider_snapshot_params(expected: dict[str, Any]) -> tuple[Any, ...]:
    return (
        expected.get("provider_type"),
        expected.get("base_url"),
        expected.get("api_key_ref"),
        expected.get("api_version"),
        expected.get("deployment_name"),
        expected.get("default_model"),
        json.dumps(expected.get("config_jsonb") or {}),
        expected.get("upstream_revision"),
    )


def claim_model_provider_verification(
    conn,
    *,
    account_id: str,
    provider_id: str,
    generation: str,
    expected_provider: dict[str, Any],
) -> dict[str, Any] | None:
    """Atomically claim one paid probe for an exact provider snapshot."""
    expected = expected_provider
    sql = _CLAIM_VERIFICATION_SQL
    params: tuple[Any, ...] = (
        str(generation),
        str(provider_id),
        str(account_id),
        *_provider_snapshot_params(expected),
    )
    if expected.get("api_key_ref"):
        sql = _CLAIM_VERIFICATION_WITH_SECRET_SQL
        params += (
            str(account_id),
            str(expected.get("api_key_ref")),
            expected.get("secret_ciphertext"),
        )
    kwargs = _cursor_kwargs()
    with conn.cursor(**kwargs) as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        description = list(cur.description or [])
    payload = _row_to_dict(row, description) if row is not None else {}
    return payload or None


def set_model_provider_verification(
    conn,
    *,
    account_id: str,
    provider_id: str,
    generation: str,
    status: str,
    verified_capabilities: list[str] | None = None,
    safe_error: str | None = None,
) -> bool:
    params: tuple[Any, ...] = (
        str(status),
        json.dumps(sorted({str(value) for value in (verified_capabilities or [])})),
        str(safe_error or "") or None,
        str(status),
        str(provider_id),
        str(account_id),
        str(generation),
    )
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE model_providers
               SET verification_status = %s,
                   verified_capabilities_jsonb = %s::jsonb,
                   verification_error_safe = %s,
                   verified_at = CASE WHEN %s = 'passed' THEN now() ELSE NULL END,
                   verification_started_at = NULL,
                   verification_generation = NULL,
                   updated_at = now()
             WHERE id::text = %s
               AND account_id = %s
               AND verification_status = 'pending'
               AND verification_generation = %s
            """,
            params,
        )
        return cur.rowcount > 0


def cancel_model_provider_verification(
    conn, *, account_id: str, provider_id: str, generation: str
) -> bool:
    """Release only the pending lease owned by ``generation``."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE model_providers
               SET verification_status = 'stale',
                   verification_started_at = NULL,
                   verification_generation = NULL,
                   updated_at = now()
             WHERE id::text = %s
               AND account_id = %s
               AND verification_status = 'pending'
               AND verification_generation = %s
            """,
            (str(provider_id), str(account_id), str(generation)),
        )
        return cur.rowcount > 0


def delete_secret_ref(
    conn,
    *,
    ref_key: str,
    account_id: str | None = None,
    project_id: str | None = None,
    tenant_id: str | None = None,
) -> None:
    normalized_account_id = str(account_id or "").strip()
    with conn.cursor() as cur:
        if project_id is not None or tenant_id is not None:
            if not normalized_account_id:
                raise ValueError("account_id is required for project-scoped secret refs")
            if not str(project_id or "").strip() or not str(tenant_id or "").strip():
                raise ValueError(_MESSAGE_PROJECT_ID_AND_TENANT_ID_MUST_BE_PROVIDED_TOGETHER)
            cur.execute(
                """DELETE FROM secret_refs
                    WHERE ref_key = %s AND account_id = %s
                      AND project_id = %s AND tenant_id = %s""",
                (ref_key, normalized_account_id, str(project_id), str(tenant_id)),
            )
        elif normalized_account_id:
            cur.execute(
                """
                DELETE FROM secret_refs
                 WHERE ref_key = %s
                   AND account_id = %s
                """,
                (ref_key, normalized_account_id),
            )
        else:
            cur.execute(
                "DELETE FROM secret_refs WHERE ref_key = %s",
                (ref_key,),
            )


def delete_model_provider(conn, *, provider_id: Any, account_id: str | None = None) -> None:
    normalized_account_id = str(account_id or "").strip()
    with conn.cursor() as cur:
        if normalized_account_id:
            cur.execute(
                """
                DELETE FROM model_providers
                 WHERE id::text = %s
                   AND account_id = %s
                """,
                (str(provider_id), normalized_account_id),
            )
        else:
            cur.execute(
                "DELETE FROM model_providers WHERE id::text = %s",
                (str(provider_id),),
            )
