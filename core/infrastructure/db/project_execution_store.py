"""Reusable helpers for project execution leases and external agent job claims."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from core.infrastructure.db import psycopg_runtime as psycopg_runtime

_PROJECT_EXECUTION_SLOTS_TABLE = "project_execution_slots"
_PROJECT_POLICIES_TABLE = "project_policies"
_PROJECT_LEASE_LOCK_NAMESPACE = 43191


def normalize_project_execution_lease_row(
    row: dict | None,
    *,
    coerce_int,
) -> dict[str, Any]:
    _ = coerce_int
    payload = dict(row or {})
    payload["project_id"] = str(payload.get("project_id") or "").strip()
    payload["active_job_id"] = str(payload.get("active_job_id") or "").strip() or None
    payload["worker_instance_id"] = str(payload.get("worker_instance_id") or "").strip()
    payload["lease_token"] = str(payload.get("lease_token") or "").strip()
    payload["drain_status"] = str(payload.get("drain_status") or "").strip()
    return payload


def _slots_table_exists(table_exists) -> bool:
    return bool(table_exists(_PROJECT_EXECUTION_SLOTS_TABLE))


def _lease_filter_clauses(
    *,
    active_only: bool,
    project_id: str | None,
    worker_instance_id: str | None,
    lease_token: str | None,
    alias: str = "",
) -> tuple[list[str], list[Any]]:
    where: list[str] = []
    params: list[Any] = []
    prefix = f"{alias}." if alias else ""
    if active_only:
        where.append(f"{prefix}expires_at > now()")
    if project_id is not None:
        where.append(f"{prefix}project_id = %s")
        params.append(str(project_id).strip())
    normalized_worker_id = str(worker_instance_id or "").strip()
    if normalized_worker_id:
        where.append(f"{prefix}worker_instance_id = %s")
        params.append(normalized_worker_id)
    normalized_lease_token = str(lease_token or "").strip()
    if normalized_lease_token:
        where.append(f"{prefix}lease_token = %s")
        params.append(normalized_lease_token)
    return where, params


def _lease_select_sql(where_sql: str, *, table_name: str, alias: str = "") -> str:
    relation = f"{table_name} {alias}".strip()
    prefix = f"{alias}." if alias else ""
    return f"""
        SELECT
            {prefix}project_id AS project_id,
            {prefix}worker_instance_id AS worker_instance_id,
            {prefix}lease_token AS lease_token,
            {prefix}heartbeat_at AS heartbeat_at,
            {prefix}expires_at AS expires_at,
            {prefix}acquired_at AS acquired_at,
            {prefix}active_job_id AS active_job_id,
            {prefix}drain_status AS drain_status,
            {prefix}created_at AS created_at,
            {prefix}updated_at AS updated_at
        FROM {relation}
        {where_sql}
    """


def _project_concurrency_cap(
    *,
    cur,
    table_exists,
    coerce_int,
    project_id: str,
) -> int:
    if not table_exists(_PROJECT_POLICIES_TABLE):
        return 1
    cur.execute(
        """
        SELECT concurrency_cap
        FROM project_policies
        WHERE project_id = %s
        """,
        (str(project_id),),
    )
    row = cur.fetchone()
    if isinstance(row, dict):
        raw_value = row.get("concurrency_cap")
    elif row:
        raw_value = row[0]
    else:
        raw_value = None
    return max(1, coerce_int(raw_value, 1))


def _prune_project_execution_rows(
    *,
    cur,
    table_name: str,
    project_id: str,
) -> None:
    cur.execute(
        f"""
        DELETE FROM {table_name} AS lease_row
        WHERE
            lease_row.project_id = %s
            AND lease_row.expires_at <= now()
        """,
        (str(project_id),),
    )


def _count_active_project_execution_rows(
    *,
    cur,
    project_id: str,
) -> int:
    cur.execute(
        f"""
        SELECT COUNT(*)
        FROM {_PROJECT_EXECUTION_SLOTS_TABLE}
        WHERE
            project_id = %s
            AND expires_at > now()
        """,
        (str(project_id),),
    )
    row = cur.fetchone()
    if isinstance(row, dict):
        raw_value = row.get("count")
    elif row:
        raw_value = row[0]
    else:
        raw_value = 0
    return int(raw_value or 0)


def _project_execution_lock_key(project_id: str) -> int:
    digest = hashlib.sha256(str(project_id).encode("utf-8")).digest()
    return int.from_bytes(digest[:4], byteorder="big", signed=True)


def _with_project_execution_lock(cur, project_id: str) -> None:
    cur.execute(
        "SELECT pg_advisory_lock(%s, %s)",
        (_PROJECT_LEASE_LOCK_NAMESPACE, _project_execution_lock_key(project_id)),
    )


def _release_project_execution_lock(cur, project_id: str) -> None:
    cur.execute(
        "SELECT pg_advisory_unlock(%s, %s)",
        (_PROJECT_LEASE_LOCK_NAMESPACE, _project_execution_lock_key(project_id)),
    )


def _update_slot_lease_row(
    *,
    cur,
    coerce_int,
    project_id: str,
    worker_instance_id: str,
    lease_token: str,
    ttl_seconds: int,
    drain_status: str,
    active_job_id: str | None,
) -> dict[str, Any] | None:
    cur.execute(
        f"""
        UPDATE {_PROJECT_EXECUTION_SLOTS_TABLE}
        SET
            heartbeat_at = now(),
            expires_at = now() + make_interval(secs => %s),
            active_job_id = %s,
            drain_status = %s,
            updated_at = now()
        WHERE
            project_id = %s
            AND worker_instance_id = %s
            AND lease_token = %s
        RETURNING
            project_id,
            worker_instance_id,
            lease_token,
            heartbeat_at,
            expires_at,
            acquired_at,
            active_job_id,
            drain_status,
            created_at,
            updated_at
        """,
        (
            int(ttl_seconds),
            str(active_job_id).strip() if active_job_id is not None else None,
            str(drain_status or "").strip(),
            str(project_id),
            str(worker_instance_id or "").strip(),
            str(lease_token or "").strip(),
        ),
    )
    row = cur.fetchone()
    return normalize_project_execution_lease_row(dict(row), coerce_int=coerce_int) if row else None


def list_project_execution_leases(
    *,
    conn,
    table_exists,
    coerce_int,
    sql_and: str,
    _sql_project_id_eq: str,
    active_only: bool = True,
    project_id: str | None = None,
    worker_instance_id: str | None = None,
    lease_token: str | None = None,
) -> list[dict[str, Any]]:
    if not _slots_table_exists(table_exists):
        return []

    where, params = _lease_filter_clauses(
        active_only=active_only,
        project_id=project_id,
        worker_instance_id=worker_instance_id,
        lease_token=lease_token,
    )
    where_sql = f"WHERE {sql_and.join(where)}" if where else ""

    with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT
                project_id,
                worker_instance_id,
                lease_token,
                heartbeat_at,
                expires_at,
                acquired_at,
                active_job_id,
                drain_status,
                created_at,
                updated_at
            FROM {_PROJECT_EXECUTION_SLOTS_TABLE}
            {where_sql}
            ORDER BY expires_at DESC, acquired_at DESC
            """,
            tuple(params),
        )
        rows = cur.fetchall()
    return [normalize_project_execution_lease_row(dict(row), coerce_int=coerce_int) for row in rows]


def acquire_project_execution_lease(
    *,
    conn,
    table_exists,
    coerce_int,
    project_id: str,
    worker_instance_id: str,
    lease_token: str | None = None,
    ttl_seconds: int = 45,
    drain_status: str = "acquired",
    active_job_id: str | None = None,
) -> dict[str, Any] | None:
    if not _slots_table_exists(table_exists):
        return None
    normalized_project_id = str(project_id or "").strip()
    normalized_worker_id = str(worker_instance_id or "").strip()
    if not normalized_project_id:
        raise ValueError("project_id is required")
    if not normalized_worker_id:
        raise ValueError("worker_instance_id is required")
    normalized_token = str(lease_token or "").strip() or uuid.uuid4().hex
    normalized_ttl = max(15, coerce_int(ttl_seconds, 45))
    normalized_status = str(drain_status or "acquired").strip() or "acquired"
    with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
        _with_project_execution_lock(cur, normalized_project_id)
        try:
            existing = _update_slot_lease_row(
                cur=cur,
                coerce_int=coerce_int,
                project_id=normalized_project_id,
                worker_instance_id=normalized_worker_id,
                lease_token=normalized_token,
                ttl_seconds=normalized_ttl,
                drain_status=normalized_status,
                active_job_id=active_job_id,
            )
            if existing:
                return existing

            _prune_project_execution_rows(
                cur=cur,
                table_name=_PROJECT_EXECUTION_SLOTS_TABLE,
                project_id=normalized_project_id,
            )

            concurrency_cap = _project_concurrency_cap(
                cur=cur,
                table_exists=table_exists,
                coerce_int=coerce_int,
                project_id=normalized_project_id,
            )
            active_lease_count = _count_active_project_execution_rows(
                cur=cur,
                project_id=normalized_project_id,
            )
            if active_lease_count >= concurrency_cap:
                return None

            cur.execute(
                f"""
                INSERT INTO {_PROJECT_EXECUTION_SLOTS_TABLE} (
                    project_id,
                    worker_instance_id,
                    lease_token,
                    heartbeat_at,
                    expires_at,
                    acquired_at,
                    active_job_id,
                    drain_status
                ) VALUES (
                    %s,
                    %s,
                    %s,
                    now(),
                    now() + make_interval(secs => %s),
                    now(),
                    %s,
                    %s
                )
                RETURNING
                    project_id,
                    worker_instance_id,
                    lease_token,
                    heartbeat_at,
                    expires_at,
                    acquired_at,
                    active_job_id,
                    drain_status,
                    created_at,
                    updated_at
                """,
                (
                    normalized_project_id,
                    normalized_worker_id,
                    normalized_token,
                    normalized_ttl,
                    str(active_job_id).strip() if active_job_id is not None else None,
                    normalized_status,
                ),
            )
            row = cur.fetchone()
        finally:
            _release_project_execution_lock(cur, normalized_project_id)
    return normalize_project_execution_lease_row(dict(row), coerce_int=coerce_int) if row else None


def heartbeat_project_execution_lease(
    *,
    conn,
    table_exists,
    coerce_int,
    project_id: str,
    worker_instance_id: str,
    lease_token: str,
    ttl_seconds: int = 45,
    drain_status: str = "draining",
    active_job_id: str | None = None,
) -> dict[str, Any] | None:
    if not _slots_table_exists(table_exists):
        return None
    normalized_project_id = str(project_id or "").strip()
    normalized_worker_id = str(worker_instance_id or "").strip()
    normalized_token = str(lease_token or "").strip()
    if not normalized_project_id or not normalized_worker_id or not normalized_token:
        return None
    normalized_ttl = max(15, coerce_int(ttl_seconds, 45))
    normalized_status = str(drain_status or "draining").strip() or "draining"
    with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
        slot_row = _update_slot_lease_row(
            cur=cur,
            coerce_int=coerce_int,
            project_id=normalized_project_id,
            worker_instance_id=normalized_worker_id,
            lease_token=normalized_token,
            ttl_seconds=normalized_ttl,
            drain_status=normalized_status,
            active_job_id=active_job_id,
        )
        if slot_row:
            return slot_row
    return None


def release_project_execution_lease(
    *,
    conn,
    table_exists,
    project_id: str,
    worker_instance_id: str,
    lease_token: str,
) -> bool:
    if not _slots_table_exists(table_exists):
        return False
    normalized_project_id = str(project_id or "").strip()
    normalized_worker_id = str(worker_instance_id or "").strip()
    normalized_token = str(lease_token or "").strip()
    if not normalized_project_id or not normalized_worker_id or not normalized_token:
        return False
    with conn.cursor() as cur:
        cur.execute(
            f"""
            DELETE FROM {_PROJECT_EXECUTION_SLOTS_TABLE}
            WHERE
                project_id = %s
                AND worker_instance_id = %s
                AND lease_token = %s
            """,
            (
                normalized_project_id,
                normalized_worker_id,
                normalized_token,
            ),
        )
        return bool(cur.rowcount)


def claim_external_agent_job(
    *,
    conn,
    normalize_string_list,
    as_dict,
    default_account_id,
    sql_and: str,
    sql_job_project_id_eq: str,
    project_id: str,
    worker_instance_id: str,
    workflow_id: str | None = None,
    allowed_workflow_ids: list[str] | None = None,
    claim_token: str | None = None,
    agent_name: str | None = None,
) -> dict[str, Any] | None:
    """Atomically claim one external-agent-enabled pending job for a project."""
    normalized_project_id = str(project_id or "").strip()
    normalized_worker_id = str(worker_instance_id or "").strip()
    normalized_workflow_id = str(workflow_id or "").strip() or None
    normalized_claim_token = str(claim_token or "").strip() or uuid.uuid4().hex
    normalized_agent_name = str(agent_name or "").strip() or normalized_worker_id
    normalized_allowed_workflow_ids = [
        workflow_key for workflow_key in normalize_string_list(allowed_workflow_ids) if workflow_key
    ]
    if not normalized_project_id or not normalized_worker_id:
        return None

    where = [
        sql_job_project_id_eq,
        "j.status = 'pending'",
        "COALESCE(j.queue_name, '') = ''",
        "lower(COALESCE(j.meta->'external_agent'->>'enabled', 'false')) IN ('true', '1', 'yes', 'on')",
    ]
    params: list[Any] = [normalized_project_id]
    if normalized_workflow_id:
        where.append(
            "COALESCE(NULLIF(j.selected_workflow, ''), NULLIF(j.meta->'external_agent'->>'workflow_id', '')) = %s"
        )
        params.append(normalized_workflow_id)
    elif normalized_allowed_workflow_ids:
        where.append(
            "COALESCE(NULLIF(j.selected_workflow, ''), NULLIF(j.meta->'external_agent'->>'workflow_id', '')) = ANY(%s)"
        )
        params.append(normalized_allowed_workflow_ids)

    with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT
                j.*,
                p.name AS project_name,
                p.account_id
            FROM jobs j
            JOIN projects p ON p.id = j.project_id
            WHERE {sql_and.join(where)}
            ORDER BY j.queued_at ASC NULLS LAST, j.id ASC
            FOR UPDATE OF j SKIP LOCKED
            LIMIT 1
            """,
            tuple(params),
        )
        row = cur.fetchone()
        if not row:
            return None

        payload = dict(row)
        meta = as_dict(payload.get("meta"))
        external_meta = as_dict(meta.get("external_agent"))
        now_iso = datetime.now(UTC).isoformat()
        effective_workflow_id = (
            str(payload.get("selected_workflow") or "").strip()
            or str(external_meta.get("workflow_id") or "").strip()
            or normalized_workflow_id
            or None
        )

        external_meta.update(
            {
                "enabled": True,
                "claim_token": normalized_claim_token,
                "worker_instance_id": normalized_worker_id,
                "agent_name": normalized_agent_name,
                "claimed_at": now_iso,
            }
        )
        if effective_workflow_id:
            external_meta["workflow_id"] = effective_workflow_id
        meta["external_agent"] = external_meta

        cur.execute(
            """
            UPDATE jobs
               SET status = 'in_progress',
                   started_at = COALESCE(started_at, NOW()),
                   worker_id = %s,
                   heartbeat_at = NOW(),
                   current_agent = %s,
                   selected_workflow = COALESCE(NULLIF(selected_workflow, ''), %s),
                   meta = %s
             WHERE id = %s
            """,
            (
                normalized_worker_id,
                normalized_agent_name,
                effective_workflow_id,
                json.dumps(meta),
                str(payload.get("id") or "").strip(),
            ),
        )

    payload["status"] = "in_progress"
    payload["worker_id"] = normalized_worker_id
    payload["current_agent"] = normalized_agent_name
    payload["selected_workflow"] = effective_workflow_id
    payload["meta"] = meta
    payload["account_id"] = (
        str(payload.get("account_id") or default_account_id()).strip() or default_account_id()
    )
    payload["project_name"] = str(payload.get("project_name") or "").strip()
    return payload
