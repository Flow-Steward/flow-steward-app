"""Persist provider webhook delivery reservations and outcomes."""

from __future__ import annotations

from typing import Any

_PROVIDER_WEBHOOK_REPLAY_STALE_SECONDS = 120


def _provider_delivery_row(cur: Any, row: Any) -> dict[str, Any]:
    if not row:
        return {}
    columns = [str(col[0]) for col in (getattr(cur, "description", None) or [])]
    if columns:
        return dict(zip(columns, row, strict=False))
    if isinstance(row, dict):
        return dict(row)
    return {}


def _provider_delivery_table_missing(exc: Exception) -> bool:
    text = str(exc or "").strip().lower()
    return "provider_webhook_deliveries" in text and "does not exist" in text


def _claim_provider_webhook_delivery(
    bl: Any,
    *,
    provider_key: str,
    delivery_key: str,
    request_id: str,
    stale_after_seconds: int = _PROVIDER_WEBHOOK_REPLAY_STALE_SECONDS,
) -> dict[str, Any]:
    normalized_provider = str(provider_key or "").strip().lower()
    normalized_delivery = str(delivery_key or "").strip()
    normalized_request = str(request_id or "").strip()
    if not normalized_provider or not normalized_delivery or not normalized_request:
        return {"claimed": False, "status": "invalid"}
    conn = getattr(bl, "conn", None)
    if conn is None:
        raise RuntimeError("provider_webhook_deliveries storage is unavailable")

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO provider_webhook_deliveries (
                provider_key,
                delivery_key,
                request_id,
                status
            )
            VALUES (%s, %s, %s, 'pending')
            ON CONFLICT (provider_key, delivery_key) DO NOTHING
            RETURNING provider_key, delivery_key, request_id, status, job_id, updated_at
            """,
            (normalized_provider, normalized_delivery, normalized_request),
        )
        inserted = _provider_delivery_row(cur, cur.fetchone())
    if inserted:
        inserted["claimed"] = True
        return inserted

    ttl_spec = f"{max(1, int(stale_after_seconds))} seconds"
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE provider_webhook_deliveries
               SET request_id = %s,
                   status = 'pending',
                   job_id = NULL,
                   last_error = '',
                   updated_at = now()
             WHERE provider_key = %s
               AND delivery_key = %s
               AND (
                   status = 'failed'
                   OR (status = 'pending' AND updated_at < now() - %s::interval)
               )
            RETURNING provider_key, delivery_key, request_id, status, job_id, updated_at
            """,
            (normalized_request, normalized_provider, normalized_delivery, ttl_spec),
        )
        reclaimed = _provider_delivery_row(cur, cur.fetchone())
    if reclaimed:
        reclaimed["claimed"] = True
        return reclaimed

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT provider_key, delivery_key, request_id, status, job_id, updated_at
            FROM provider_webhook_deliveries
            WHERE provider_key = %s
              AND delivery_key = %s
            LIMIT 1
            """,
            (normalized_provider, normalized_delivery),
        )
        current = _provider_delivery_row(cur, cur.fetchone())
    if current:
        current["claimed"] = False
        return current
    return {"claimed": False, "status": "missing"}


def _mark_provider_webhook_delivery_accepted(
    bl: Any,
    *,
    provider_key: str,
    delivery_key: str,
    request_id: str,
    job_id: str,
) -> None:
    conn = getattr(bl, "conn", None)
    if conn is None:
        return
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE provider_webhook_deliveries
               SET status = 'accepted',
                   request_id = %s,
                   job_id = %s,
                   last_error = '',
                   updated_at = now()
             WHERE provider_key = %s
               AND delivery_key = %s
            """,
            (
                str(request_id or "").strip(),
                str(job_id),
                str(provider_key or "").strip().lower(),
                str(delivery_key or "").strip(),
            ),
        )


def _mark_provider_webhook_delivery_failed(
    bl: Any,
    *,
    provider_key: str,
    delivery_key: str,
    request_id: str,
    error_text: str,
) -> None:
    conn = getattr(bl, "conn", None)
    if conn is None:
        return
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE provider_webhook_deliveries
               SET status = 'failed',
                   request_id = %s,
                   job_id = NULL,
                   last_error = %s,
                   updated_at = now()
             WHERE provider_key = %s
               AND delivery_key = %s
            """,
            (
                str(request_id or "").strip(),
                str(error_text or "").strip()[:2000],
                str(provider_key or "").strip().lower(),
                str(delivery_key or "").strip(),
            ),
        )
