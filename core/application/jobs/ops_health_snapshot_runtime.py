"""Shared runtime helpers for collecting ops health snapshots."""

from __future__ import annotations

import warnings
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.application.jobs.schema_metadata import JOB_ACTIVITY_TIMESTAMP_COLUMN
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env


def resolve_ops_health_dsn(raw: str) -> str:
    explicit = str(raw or "").strip()
    if explicit:
        return explicit
    for key in (
        "FS_OBSERVABILITY_DSN",
        "FS_DB_DSN",
        "DATABASE_URL",
    ):
        value = str(_runtime_env.get(key, "")).strip()
        if value:
            return value
    return ""


def collect_ops_health_snapshot(
    *,
    base_url: str,
    verify_ssl: bool,
    timeout_seconds: int,
    dsn: str,
    stale_heartbeat_seconds: int,
) -> dict[str, Any]:
    service = fetch_service_health(
        base_url=base_url,
        verify_ssl=verify_ssl,
        timeout_seconds=max(1, int(timeout_seconds)),
    )
    database = collect_db_health(
        dsn=resolve_ops_health_dsn(dsn),
        stale_seconds=max(1, int(stale_heartbeat_seconds)),
    )
    return {
        "captured_at": datetime.now(UTC).isoformat(),
        "service": service,
        "database": database,
        "ok": bool(service.get("ok")) and bool(database.get("ok")),
    }


def fetch_service_health(
    *,
    base_url: str,
    verify_ssl: bool,
    timeout_seconds: int,
) -> dict[str, Any]:
    import requests

    _suppress_tls_warnings()
    url = f"{str(base_url or '').rstrip('/')}/health"
    try:
        response = requests.get(url, verify=verify_ssl, timeout=max(1, int(timeout_seconds)))
        body: dict[str, Any]
        try:
            parsed = response.json()
            body = parsed if isinstance(parsed, dict) else {"payload": parsed}
        except Exception:
            body = {"raw": (response.text or "")[:400]}
        return {
            "ok": response.status_code == 200,
            "status_code": int(response.status_code),
            "url": url,
            "body": body,
        }
    except Exception as exc:
        return {"ok": False, "status_code": None, "url": url, "error": str(exc)}


def collect_db_health(*, dsn: str, stale_seconds: int) -> dict[str, Any]:
    if not dsn:
        return {"ok": False, "error": "Observability DSN is not configured"}

    try:
        from core.infrastructure.db import psycopg_runtime
        from core.infrastructure.runtime.worker.queue_broker import get_queue_depth
    except Exception as exc:
        return {"ok": False, "error": f"Required dependency missing: {exc}"}

    try:
        with psycopg_runtime.connect(dsn) as conn, conn.cursor() as cur:
            status_rows = _query_rows(
                cur,
                """
                SELECT status, COUNT(*)::int
                FROM jobs
                GROUP BY status
                ORDER BY status
                """,
            )
            jobs_by_status = {str(status): int(count) for status, count in status_rows}

            heartbeat_row = _query_rows(
                cur,
                """
                SELECT
                    COUNT(*)::int AS in_progress_jobs,
                    COUNT(*) FILTER (WHERE heartbeat_at IS NULL)::int AS missing_heartbeat,
                    COUNT(*) FILTER (
                        WHERE heartbeat_at IS NOT NULL
                          AND NOW() - heartbeat_at > (%s * INTERVAL '1 second')
                    )::int AS stale_heartbeat,
                    COALESCE(
                        MAX(EXTRACT(EPOCH FROM (NOW() - heartbeat_at))) FILTER (WHERE heartbeat_at IS NOT NULL),
                        0
                    )::int AS max_heartbeat_age_sec
                FROM jobs
                WHERE status = 'in_progress'
                """,
                (max(1, int(stale_seconds)),),
            )[0]

            retry_row = _query_rows(
                cur,
                """
                SELECT
                    COUNT(*) FILTER (WHERE sent_at IS NULL)::int AS pending,
                    COUNT(*) FILTER (WHERE sent_at IS NOT NULL)::int AS sent,
                    COALESCE(
                        MAX(EXTRACT(EPOCH FROM (NOW() - enqueued_at))) FILTER (WHERE sent_at IS NULL),
                        0
                    )::int AS oldest_pending_age_sec
                FROM platform_event_retry_queue
                """,
            )[0]

            latest_ids = {
                "jobs": str(
                    _query_scalar(
                        cur,
                        f"SELECT id FROM jobs ORDER BY {JOB_ACTIVITY_TIMESTAMP_COLUMN} DESC, id DESC LIMIT 1",
                    )
                    or ""
                ),
                "messages": str(
                    _query_scalar(
                        cur,
                        "SELECT id FROM platform_agent_messages ORDER BY timestamp DESC, id DESC LIMIT 1",
                    )
                    or ""
                ),
                "events": str(
                    _query_scalar(
                        cur,
                        "SELECT id FROM platform_events ORDER BY timestamp DESC, id DESC LIMIT 1",
                    )
                    or ""
                ),
            }
            latest_counts = {
                "jobs_last_24h": int(
                    _query_scalar(
                        cur,
                        f"SELECT COUNT(*) FROM jobs WHERE {JOB_ACTIVITY_TIMESTAMP_COLUMN} >= NOW() - INTERVAL '24 hours'",
                    )
                    or 0
                ),
                "messages_last_24h": int(
                    _query_scalar(
                        cur,
                        "SELECT COUNT(*) FROM platform_agent_messages WHERE timestamp >= NOW() - INTERVAL '24 hours'",
                    )
                    or 0
                ),
                "events_last_24h": int(
                    _query_scalar(
                        cur,
                        "SELECT COUNT(*) FROM platform_events WHERE timestamp >= NOW() - INTERVAL '24 hours'",
                    )
                    or 0
                ),
            }

            projects = _query_rows(cur, "SELECT id, name FROM projects ORDER BY id")
            queue_depth_by_project: list[dict[str, Any]] = []
            queue_error_count = 0
            for project_id, project_name in projects:
                project_id_text = str(project_id or "").strip()
                depth = int(get_queue_depth(project_id_text))
                if depth < 0:
                    queue_error_count += 1
                queue_depth_by_project.append(
                    {
                        "project_id": project_id_text,
                        "project_name": str(project_name),
                        "queue_depth": depth,
                    }
                )

            total_queue_depth = sum(
                max(0, int(row["queue_depth"])) for row in queue_depth_by_project
            )

            return {
                "ok": queue_error_count == 0,
                "jobs_by_status": jobs_by_status,
                "heartbeat_summary": {
                    "stale_threshold_sec": max(1, int(stale_seconds)),
                    "in_progress_jobs": int(heartbeat_row[0]),
                    "missing_heartbeat": int(heartbeat_row[1]),
                    "stale_heartbeat": int(heartbeat_row[2]),
                    "max_heartbeat_age_sec": int(heartbeat_row[3]),
                },
                "retry_queue": {
                    "pending": int(retry_row[0]),
                    "sent": int(retry_row[1]),
                    "oldest_pending_age_sec": int(retry_row[2]),
                },
                "latest_ids": latest_ids,
                "latest_counts": latest_counts,
                "queues": {
                    "total_depth": int(total_queue_depth),
                    "error_count": int(queue_error_count),
                    "projects": queue_depth_by_project,
                },
            }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def write_ops_health_snapshot(path: str, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_render_snapshot(payload), encoding="utf-8")


def _render_snapshot(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, indent=2, sort_keys=True)


def _query_rows(cur, sql: str, params: tuple = ()) -> list[tuple]:
    cur.execute(sql, params)
    return cur.fetchall() or []


def _query_scalar(cur, sql: str, params: tuple = ()) -> Any:
    cur.execute(sql, params)
    row = cur.fetchone()
    return row[0] if row else None


def _suppress_tls_warnings() -> None:
    import urllib3

    warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
