"""Reusable dashboard rollup queries and refresh helpers."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from core.infrastructure.db import psycopg_runtime as psycopg_runtime

SQL_DELETED_AT_IS_NULL = "deleted_at IS NULL"
SQL_ACCOUNT_ID_EQ = "account_id = %s"
SQL_FINISHED_AT_GTE = "j.finished_at >= %s"
SQL_PROJECT_DELETED_AT_IS_NULL = "p.deleted_at IS NULL"
SQL_JOB_DELETED_AT_IS_NULL = "j.deleted_at IS NULL"
SQL_PROJECT_ACCOUNT_ID_EQ = "p.account_id = %s"
SQL_AND = " AND "


def parse_dt(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_date(value: date | datetime | str | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _normalize_account_id(account_id: str | None, default_account_id: str) -> str:
    return str(account_id or default_account_id).strip() or default_account_id


def list_dashboard_rollup_accounts(
    conn,
    *,
    table_exists: Callable[[str], bool],
    column_exists: Callable[[str, str], bool],
    default_account_id: str,
) -> list[str]:
    if not table_exists("projects"):
        return [default_account_id]
    if not column_exists("projects", "account_id"):
        return [default_account_id]

    where = ["account_id IS NOT NULL"]
    if column_exists("projects", "deleted_at"):
        where.append(SQL_DELETED_AT_IS_NULL)
    where_sql = f"WHERE {SQL_AND.join(where)}" if where else ""
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT DISTINCT account_id
                FROM projects
                {where_sql}
                ORDER BY account_id
                """,
            )
            accounts = [
                str((row[0] if isinstance(row, (list, tuple)) else row) or "").strip()
                for row in cur.fetchall()
            ]
    except Exception:
        accounts = []
    normalized = [account for account in accounts if account]
    return normalized or [default_account_id]


def get_dashboard_rollup_state(
    conn,
    *,
    table_exists: Callable[[str], bool],
    account_id: str | None,
    default_account_id: str,
) -> dict[str, Any]:
    if not table_exists("dashboard_rollup_state"):
        return {}
    normalized_account = _normalize_account_id(account_id, default_account_id)
    with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                account_id,
                facts_covered_from,
                facts_covered_until,
                facts_refreshed_at,
                daily_refreshed_at,
                updated_at
            FROM dashboard_rollup_state
            WHERE account_id = %s
            """,
            (normalized_account,),
        )
        return dict(cur.fetchone() or {})


def list_dashboard_job_facts(
    conn,
    *,
    table_exists: Callable[[str], bool],
    account_id: str | None,
    default_account_id: str,
    window_start: datetime | str | None = None,
    window_end: datetime | str | None = None,
    limit: int = 0,
    offset: int = 0,
) -> list[dict[str, Any]]:
    if not table_exists("dashboard_job_facts"):
        return []

    normalized_account = _normalize_account_id(account_id, default_account_id)
    start_dt = parse_dt(window_start)
    end_dt = parse_dt(window_end)
    where = [SQL_ACCOUNT_ID_EQ]
    params: list[Any] = [normalized_account]
    if start_dt is not None:
        where.append("finished_at >= %s")
        params.append(start_dt)
    if end_dt is not None:
        where.append("finished_at < %s")
        params.append(end_dt)
    where_sql = f"WHERE {SQL_AND.join(where)}"

    paging_sql = ""
    normalized_limit = max(0, int(limit or 0))
    if normalized_limit > 0:
        paging_sql = "LIMIT %s OFFSET %s"
        params.extend([min(normalized_limit, 200_000), max(0, int(offset or 0))])

    with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT
                job_id,
                job_id AS id,
                project_id,
                project_name,
                workflow AS selected_workflow,
                agent AS current_agent,
                status,
                result,
                started_at,
                finished_at,
                duration_seconds,
                cost_usd
            FROM dashboard_job_facts
            {where_sql}
            ORDER BY finished_at DESC, job_id DESC
            {paging_sql}
            """,
            tuple(params),
        )
        return [dict(row) for row in cur.fetchall()]


def list_dashboard_daily_rollups(
    conn,
    *,
    table_exists: Callable[[str], bool],
    account_id: str | None,
    default_account_id: str,
    start_date: date | datetime | str | None = None,
    end_date: date | datetime | str | None = None,
) -> list[dict[str, Any]]:
    if not table_exists("dashboard_daily_rollups"):
        return []

    normalized_account = _normalize_account_id(account_id, default_account_id)
    start_dt = parse_date(start_date)
    end_dt = parse_date(end_date)
    where = [SQL_ACCOUNT_ID_EQ]
    params: list[Any] = [normalized_account]
    if start_dt is not None:
        where.append("rollup_date >= %s")
        params.append(start_dt)
    if end_dt is not None:
        where.append("rollup_date <= %s")
        params.append(end_dt)
    where_sql = f"WHERE {SQL_AND.join(where)}"

    with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT
                account_id,
                rollup_date,
                completed_count,
                failed_count,
                spend_usd,
                p50_seconds,
                p95_seconds,
                refreshed_at
            FROM dashboard_daily_rollups
            {where_sql}
            ORDER BY rollup_date ASC
            """,
            tuple(params),
        )
        return [dict(row) for row in cur.fetchall()]


def _empty_slo_payload(
    *,
    now_utc: datetime,
    window_hours: int,
    normalized_account: str,
) -> dict[str, Any]:
    return {
        "generated_at": now_utc.isoformat(),
        "window_hours": window_hours,
        "account_id": normalized_account or None,
        "p95_latency_seconds": None,
        "retry_rate": 0.0,
        "dead_letter_rate": 0.0,
        "stuck_jobs": 0,
        "per_agent_failure_rate": [],
        "samples": {
            "latency_jobs": 0,
            "dispatches": 0,
        },
    }


def _dashboard_table_flags(
    *,
    table_exists: Callable[[str], bool],
    column_exists: Callable[[str, str], bool],
) -> dict[str, bool]:
    has_jobs = table_exists("jobs")
    has_projects = table_exists("projects")
    return {
        "has_jobs": has_jobs,
        "has_projects": has_projects,
        "has_dispatches": table_exists("event_dispatches"),
        "has_projects_account": has_projects and column_exists("projects", "account_id"),
        "has_projects_deleted": has_projects and column_exists("projects", "deleted_at"),
        "has_jobs_deleted": has_jobs and column_exists("jobs", "deleted_at"),
    }


def _jobs_window_where(
    *,
    window_start: datetime,
    normalized_account: str,
    flags: dict[str, bool],
) -> tuple[str, list[Any]]:
    where_parts = [
        "j.finished_at IS NOT NULL",
        "j.started_at IS NOT NULL",
        SQL_FINISHED_AT_GTE,
    ]
    params: list[Any] = [window_start]
    if flags["has_projects_deleted"]:
        where_parts.append(SQL_PROJECT_DELETED_AT_IS_NULL)
    if flags["has_jobs_deleted"]:
        where_parts.append(SQL_JOB_DELETED_AT_IS_NULL)
    if normalized_account and flags["has_projects_account"]:
        where_parts.append(SQL_PROJECT_ACCOUNT_ID_EQ)
        params.append(normalized_account)
    return SQL_AND.join(where_parts), params


def _load_latency_metrics(conn, *, where_sql: str, params: list[Any]) -> tuple[float | None, int]:
    terminal_statuses = [
        "completed",
        "failed",
        "max_iterations",
        "retryable_interrupted_step",
        "cancelled",
        "stuck",
    ]
    with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT
                percentile_cont(0.95) WITHIN GROUP (
                    ORDER BY EXTRACT(EPOCH FROM (j.finished_at - j.started_at))
                ) AS p95_latency_seconds,
                COUNT(*)::int AS sample_count
            FROM jobs j
            JOIN projects p ON p.id = j.project_id
            WHERE {where_sql}
              AND j.status = ANY(%s)
            """,
            (*params, terminal_statuses),
        )
        row = dict(cur.fetchone() or {})
    try:
        raw_latency = row.get("p95_latency_seconds")
        latency = float(str(raw_latency).strip()) if raw_latency is not None else None
    except Exception:
        latency = None
    return latency, int(row.get("sample_count") or 0)


def _load_stuck_jobs(
    conn,
    *,
    normalized_account: str,
    flags: dict[str, bool],
) -> int:
    stuck_where_parts = ["j.status = 'stuck'"]
    stuck_params: list[Any] = []
    if flags["has_projects_deleted"]:
        stuck_where_parts.append(SQL_PROJECT_DELETED_AT_IS_NULL)
    if flags["has_jobs_deleted"]:
        stuck_where_parts.append(SQL_JOB_DELETED_AT_IS_NULL)
    if normalized_account and flags["has_projects_account"]:
        stuck_where_parts.append(SQL_PROJECT_ACCOUNT_ID_EQ)
        stuck_params.append(normalized_account)
    with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT COUNT(*)::int AS stuck_jobs
            FROM jobs j
            JOIN projects p ON p.id = j.project_id
            WHERE {SQL_AND.join(stuck_where_parts)}
            """,
            tuple(stuck_params),
        )
        row = dict(cur.fetchone() or {})
    return int(row.get("stuck_jobs") or 0)


def _load_per_agent_failure_rates(
    conn,
    *,
    where_sql: str,
    params: list[Any],
) -> list[dict[str, Any]]:
    failure_statuses = [
        "failed",
        "max_iterations",
        "retryable_interrupted_step",
    ]
    considered_statuses = ["completed", *failure_statuses]
    with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT
                COALESCE(NULLIF(BTRIM(j.current_agent), ''), 'unknown') AS agent,
                COUNT(*) FILTER (WHERE j.status = ANY(%s))::int AS failed_jobs,
                COUNT(*) FILTER (WHERE j.status = ANY(%s))::int AS considered_jobs
            FROM jobs j
            JOIN projects p ON p.id = j.project_id
            WHERE {where_sql}
            GROUP BY agent
            HAVING COUNT(*) FILTER (WHERE j.status = ANY(%s)) > 0
            ORDER BY failed_jobs DESC, considered_jobs DESC, agent ASC
            LIMIT 100
            """,
            (failure_statuses, considered_statuses, *params, considered_statuses),
        )
        agent_rows = [dict(row) for row in cur.fetchall()]
    result: list[dict[str, Any]] = []
    for row in agent_rows:
        considered = int(row.get("considered_jobs") or 0)
        failed = int(row.get("failed_jobs") or 0)
        result.append(
            {
                "agent": str(row.get("agent") or "unknown"),
                "failure_rate": float(failed / considered) if considered > 0 else 0.0,
                "failed_jobs": failed,
                "considered_jobs": considered,
            }
        )
    return result


def _load_dispatch_metrics(
    conn,
    *,
    window_start: datetime,
    normalized_account: str,
) -> dict[str, float | int]:
    dispatch_where_parts = ["created_at >= %s"]
    dispatch_params: list[Any] = [window_start]
    if normalized_account:
        dispatch_where_parts.append(SQL_ACCOUNT_ID_EQ)
        dispatch_params.append(normalized_account)
    with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT
                COUNT(*)::int AS total_dispatches,
                COUNT(*) FILTER (WHERE status = 'dead_lettered')::int AS dead_lettered_dispatches,
                COUNT(*) FILTER (WHERE attempt_count > 1)::int AS retried_dispatches
            FROM event_dispatches
            WHERE {SQL_AND.join(dispatch_where_parts)}
            """,
            tuple(dispatch_params),
        )
        row = dict(cur.fetchone() or {})
    total_dispatches = int(row.get("total_dispatches") or 0)
    dead_lettered_dispatches = int(row.get("dead_lettered_dispatches") or 0)
    retried_dispatches = int(row.get("retried_dispatches") or 0)
    return {
        "dispatches": total_dispatches,
        "dead_letter_rate": (
            float(dead_lettered_dispatches / total_dispatches) if total_dispatches > 0 else 0.0
        ),
        "retry_rate": float(retried_dispatches / total_dispatches) if total_dispatches > 0 else 0.0,
    }


def get_core_slo_metrics(
    conn,
    *,
    table_exists: Callable[[str], bool],
    column_exists: Callable[[str, str], bool],
    account_id: str | None,
    default_account_id: str,
    window_hours: int = 24,
) -> dict[str, Any]:
    normalized_window_hours = max(1, min(int(window_hours or 24), 720))
    now_utc = datetime.now(UTC)
    window_start = now_utc - timedelta(hours=normalized_window_hours)
    normalized_account = str(account_id or "").strip()
    payload = _empty_slo_payload(
        now_utc=now_utc,
        window_hours=normalized_window_hours,
        normalized_account=normalized_account,
    )
    flags = _dashboard_table_flags(table_exists=table_exists, column_exists=column_exists)

    if flags["has_jobs"] and flags["has_projects"]:
        if (
            normalized_account
            and (not flags["has_projects_account"])
            and normalized_account != default_account_id
        ):
            return payload
        where_sql, params = _jobs_window_where(
            window_start=window_start,
            normalized_account=normalized_account,
            flags=flags,
        )
        latency, latency_jobs = _load_latency_metrics(conn, where_sql=where_sql, params=params)
        payload["p95_latency_seconds"] = latency
        payload["samples"]["latency_jobs"] = latency_jobs
        payload["stuck_jobs"] = _load_stuck_jobs(
            conn,
            normalized_account=normalized_account,
            flags=flags,
        )
        payload["per_agent_failure_rate"] = _load_per_agent_failure_rates(
            conn,
            where_sql=where_sql,
            params=params,
        )

    if flags["has_dispatches"]:
        dispatch_metrics = _load_dispatch_metrics(
            conn,
            window_start=window_start,
            normalized_account=normalized_account,
        )
        payload["samples"]["dispatches"] = int(dispatch_metrics["dispatches"])
        payload["dead_letter_rate"] = float(dispatch_metrics["dead_letter_rate"])
        payload["retry_rate"] = float(dispatch_metrics["retry_rate"])

    return payload


def refresh_dashboard_job_facts_window(
    conn,
    *,
    table_exists: Callable[[str], bool],
    column_exists: Callable[[str, str], bool],
    account_id: str,
    default_account_id: str,
    window_start: datetime,
    window_end: datetime,
) -> dict[str, int]:
    if not table_exists("dashboard_job_facts"):
        return {"deleted": 0, "inserted": 0}
    if window_end <= window_start:
        return {"deleted": 0, "inserted": 0}

    normalized_account = _normalize_account_id(account_id, default_account_id)
    has_projects_account = column_exists("projects", "account_id")
    has_jobs_deleted = column_exists("jobs", "deleted_at")
    has_projects_deleted = column_exists("projects", "deleted_at")

    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM dashboard_job_facts
            WHERE account_id = %s
              AND finished_at >= %s
              AND finished_at < %s
            """,
            (normalized_account, window_start, window_end),
        )
        deleted = int(cur.rowcount or 0)

    if (not has_projects_account) and normalized_account != default_account_id:
        return {"deleted": deleted, "inserted": 0}

    account_expr = "COALESCE(p.account_id, %s::uuid)" if has_projects_account else "%s::uuid"
    where = [
        "j.finished_at IS NOT NULL",
        SQL_FINISHED_AT_GTE,
        "j.finished_at < %s",
    ]
    params: list[Any] = [default_account_id, window_start, window_end]
    if has_jobs_deleted:
        where.append(SQL_JOB_DELETED_AT_IS_NULL)
    if has_projects_deleted:
        where.append(SQL_PROJECT_DELETED_AT_IS_NULL)
    if has_projects_account:
        where.append("COALESCE(p.account_id, %s::uuid) = %s::uuid")
        params.extend([default_account_id, normalized_account])
    where_sql = SQL_AND.join(where)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH in_scope_jobs AS (
                SELECT
                    j.id,
                    j.project_id,
                    COALESCE(NULLIF(BTRIM(p.name), ''), 'Unknown') AS project_name,
                    COALESCE(NULLIF(BTRIM(j.selected_workflow), ''), 'auto') AS workflow,
                    NULLIF(BTRIM(j.current_agent), '') AS current_agent,
                    LOWER(COALESCE(j.status, '')) AS status_lower,
                    CASE
                        WHEN LOWER(COALESCE(j.status, '')) = 'completed' THEN 'completed'
                        WHEN LOWER(COALESCE(j.status, '')) IN ('failed', 'max_iterations', 'triage_needed') THEN 'failed'
                        ELSE NULL
                    END AS result,
                    j.started_at,
                    j.finished_at,
                    {account_expr} AS account_id
                FROM jobs j
                JOIN projects p ON p.id = j.project_id
                WHERE {where_sql}
            ),
            cost_totals AS (
                SELECT
                    bal.job_id,
                    COALESCE(SUM(COALESCE(bal.cost_usd, 0)), 0)::double precision AS cost_usd
                FROM platform_action_log bal
                JOIN in_scope_jobs sj ON sj.id = bal.job_id
                GROUP BY bal.job_id
            ),
            ranked_agents AS (
                SELECT
                    ranked.job_id,
                    ranked.agent_key,
                    ROW_NUMBER() OVER (
                        PARTITION BY ranked.job_id
                        ORDER BY ranked.agent_cost DESC, ranked.last_seen DESC
                    ) AS rn
                FROM (
                    SELECT
                        bal.job_id,
                        NULLIF(BTRIM(bal.agent), '') AS agent_key,
                        COALESCE(SUM(COALESCE(bal.cost_usd, 0)), 0)::double precision AS agent_cost,
                        MAX(bal.timestamp) AS last_seen
                    FROM platform_action_log bal
                    JOIN in_scope_jobs sj ON sj.id = bal.job_id
                    GROUP BY bal.job_id, NULLIF(BTRIM(bal.agent), '')
                ) AS ranked
                WHERE ranked.agent_key IS NOT NULL
            ),
            dominant_agents AS (
                SELECT job_id, agent_key
                FROM ranked_agents
                WHERE rn = 1
            )
            INSERT INTO dashboard_job_facts (
                job_id,
                account_id,
                project_id,
                project_name,
                workflow,
                agent,
                status,
                result,
                started_at,
                finished_at,
                finished_date,
                duration_seconds,
                cost_usd,
                refreshed_at
            )
            SELECT
                sj.id AS job_id,
                sj.account_id,
                sj.project_id,
                sj.project_name,
                sj.workflow,
                COALESCE(sj.current_agent, da.agent_key, 'unknown') AS agent,
                sj.status_lower AS status,
                sj.result,
                sj.started_at,
                sj.finished_at,
                sj.finished_at::date AS finished_date,
                CASE
                    WHEN sj.started_at IS NOT NULL AND sj.finished_at >= sj.started_at
                        THEN EXTRACT(EPOCH FROM (sj.finished_at - sj.started_at))::double precision
                    ELSE NULL
                END AS duration_seconds,
                COALESCE(ct.cost_usd, 0)::double precision AS cost_usd,
                NOW()
            FROM in_scope_jobs sj
            LEFT JOIN cost_totals ct
                ON ct.job_id = sj.id
            LEFT JOIN dominant_agents da
                ON da.job_id = sj.id
            WHERE sj.result IS NOT NULL
            """,
            tuple(params),
        )
        inserted = int(cur.rowcount or 0)
    return {"deleted": deleted, "inserted": inserted}


def refresh_dashboard_daily_rollups_window(
    conn,
    *,
    table_exists: Callable[[str], bool],
    account_id: str,
    default_account_id: str,
    start_date: date,
    end_date: date,
) -> dict[str, int]:
    if not table_exists("dashboard_daily_rollups"):
        return {"deleted": 0, "inserted": 0}
    if end_date < start_date:
        return {"deleted": 0, "inserted": 0}

    normalized_account = _normalize_account_id(account_id, default_account_id)
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM dashboard_daily_rollups
            WHERE account_id = %s
              AND rollup_date >= %s
              AND rollup_date <= %s
            """,
            (normalized_account, start_date, end_date),
        )
        deleted = int(cur.rowcount or 0)
        cur.execute(
            """
            INSERT INTO dashboard_daily_rollups (
                account_id,
                rollup_date,
                completed_count,
                failed_count,
                spend_usd,
                p50_seconds,
                p95_seconds,
                refreshed_at
            )
            SELECT
                account_id,
                finished_date AS rollup_date,
                SUM(CASE WHEN result = 'completed' THEN 1 ELSE 0 END)::integer AS completed_count,
                SUM(CASE WHEN result = 'failed' THEN 1 ELSE 0 END)::integer AS failed_count,
                COALESCE(SUM(cost_usd), 0)::double precision AS spend_usd,
                PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY duration_seconds)
                    FILTER (WHERE duration_seconds IS NOT NULL) AS p50_seconds,
                PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY duration_seconds)
                    FILTER (WHERE duration_seconds IS NOT NULL) AS p95_seconds,
                NOW()
            FROM dashboard_job_facts
            WHERE account_id = %s
              AND finished_date >= %s
              AND finished_date <= %s
            GROUP BY account_id, finished_date
            """,
            (normalized_account, start_date, end_date),
        )
        inserted = int(cur.rowcount or 0)
    return {"deleted": deleted, "inserted": inserted}


def refresh_dashboard_rollups(
    conn,
    *,
    table_exists: Callable[[str], bool],
    column_exists: Callable[[str, str], bool],
    default_account_id: str,
    account_id: str | None = None,
    now: datetime | str | None = None,
    backfill_days: int = 180,
    lookback_days: int = 3,
) -> dict[str, Any]:
    required = ("dashboard_job_facts", "dashboard_daily_rollups", "dashboard_rollup_state")
    if not all(table_exists(name) for name in required):
        return {"unavailable": True, "refreshed_accounts": 0, "details": []}

    now_utc = parse_dt(now) or datetime.now(UTC)
    lookback = max(1, int(lookback_days or 1))
    backfill = max(1, int(backfill_days or 1))
    normalized_account = str(account_id or "").strip()
    accounts = (
        [normalized_account]
        if normalized_account
        else list_dashboard_rollup_accounts(
            conn,
            table_exists=table_exists,
            column_exists=column_exists,
            default_account_id=default_account_id,
        )
    )

    refreshed_accounts = 0
    facts_rows_deleted = 0
    facts_rows_inserted = 0
    daily_rows_deleted = 0
    daily_rows_inserted = 0
    details: list[dict[str, Any]] = []

    for account in accounts:
        state = get_dashboard_rollup_state(
            conn,
            table_exists=table_exists,
            account_id=account,
            default_account_id=default_account_id,
        )
        covered_from = parse_dt(state.get("facts_covered_from"))
        if covered_from is None:
            window_start = now_utc - timedelta(days=backfill)
            bootstrap = True
        else:
            window_start = now_utc - timedelta(days=lookback)
            bootstrap = False
        if window_start > now_utc:
            window_start = now_utc
        window_end = now_utc

        facts_stats = refresh_dashboard_job_facts_window(
            conn,
            table_exists=table_exists,
            column_exists=column_exists,
            account_id=account,
            default_account_id=default_account_id,
            window_start=window_start,
            window_end=window_end,
        )
        daily_stats = refresh_dashboard_daily_rollups_window(
            conn,
            table_exists=table_exists,
            account_id=account,
            default_account_id=default_account_id,
            start_date=window_start.date(),
            end_date=window_end.date(),
        )

        merged_covered_from = covered_from
        if merged_covered_from is None or window_start < merged_covered_from:
            merged_covered_from = window_start

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO dashboard_rollup_state (
                    account_id,
                    facts_covered_from,
                    facts_covered_until,
                    facts_refreshed_at,
                    daily_refreshed_at,
                    updated_at
                ) VALUES (%s, %s, %s, %s, %s, NOW())
                ON CONFLICT (account_id) DO UPDATE SET
                    facts_covered_from = EXCLUDED.facts_covered_from,
                    facts_covered_until = EXCLUDED.facts_covered_until,
                    facts_refreshed_at = EXCLUDED.facts_refreshed_at,
                    daily_refreshed_at = EXCLUDED.daily_refreshed_at,
                    updated_at = NOW()
                """,
                (
                    account,
                    merged_covered_from,
                    window_end,
                    now_utc,
                    now_utc,
                ),
            )

        refreshed_accounts += 1
        facts_rows_deleted += int(facts_stats.get("deleted") or 0)
        facts_rows_inserted += int(facts_stats.get("inserted") or 0)
        daily_rows_deleted += int(daily_stats.get("deleted") or 0)
        daily_rows_inserted += int(daily_stats.get("inserted") or 0)
        details.append(
            {
                "account_id": account,
                "bootstrap": bool(bootstrap),
                "window_start": window_start.isoformat(),
                "window_end": window_end.isoformat(),
                "facts_deleted": int(facts_stats.get("deleted") or 0),
                "facts_inserted": int(facts_stats.get("inserted") or 0),
                "daily_deleted": int(daily_stats.get("deleted") or 0),
                "daily_inserted": int(daily_stats.get("inserted") or 0),
            }
        )

    return {
        "refreshed_at": now_utc.isoformat(),
        "refreshed_accounts": refreshed_accounts,
        "facts_rows_deleted": facts_rows_deleted,
        "facts_rows_inserted": facts_rows_inserted,
        "daily_rows_deleted": daily_rows_deleted,
        "daily_rows_inserted": daily_rows_inserted,
        "details": details,
    }
