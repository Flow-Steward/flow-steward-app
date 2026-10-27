"""Dashboard metrics calculation helpers extracted from operator dashboard routes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from core.application.observability_vocabulary import (
    JOB_FAILURE_STATUSES,
    JOB_SUCCESS_STATUSES,
    dashboard_result_from_status,
    job_attention_group,
    job_status_meta,
    normalize_job_status,
)
from core.application.reports.dashboard_metric_values import (
    _dashboard_duration_values,
    _dashboard_format_duration,
    _dashboard_format_percent,
    _dashboard_outcome_counts,
    _dashboard_parse_dt,
    _dashboard_pct_delta,
    _dashboard_percentile,
    _dashboard_running_counts,
    _dashboard_spend_summary,
    _dashboard_success_rate,
    _safe_float,
)

_DASHBOARD_RANGE_HOURS: dict[str, int] = {
    "24h": 24,
    "7d": 24 * 7,
    "30d": 24 * 30,
}
_DASHBOARD_DEFAULT_RANGE = "7d"
_DASHBOARD_METRICS_PAGE_SIZE = 1000
_DASHBOARD_METRICS_MAX_ROWS = 0
_DASHBOARD_TRIAGE_LIMIT = 25
_DASHBOARD_ROLLUP_READ_ENABLED = True


def configure_dashboard_metrics(
    *,
    dashboard_default_range: str,
    dashboard_range_hours: dict[str, int],
    dashboard_metrics_page_size: int,
    dashboard_metrics_max_rows: int,
    dashboard_triage_limit: int,
    dashboard_rollup_read_enabled: bool,
) -> None:
    """Configure module-level dashboard runtime settings."""
    global _DASHBOARD_DEFAULT_RANGE
    global _DASHBOARD_RANGE_HOURS
    global _DASHBOARD_METRICS_PAGE_SIZE
    global _DASHBOARD_METRICS_MAX_ROWS
    global _DASHBOARD_TRIAGE_LIMIT
    global _DASHBOARD_ROLLUP_READ_ENABLED

    _DASHBOARD_DEFAULT_RANGE = str(dashboard_default_range or "7d").strip().lower() or "7d"
    _DASHBOARD_RANGE_HOURS = {
        str(key).strip().lower(): int(value)
        for key, value in (dashboard_range_hours or {"7d": 24 * 7}).items()
    }
    if _DASHBOARD_DEFAULT_RANGE not in _DASHBOARD_RANGE_HOURS:
        _DASHBOARD_RANGE_HOURS[_DASHBOARD_DEFAULT_RANGE] = 24 * 7

    _DASHBOARD_METRICS_PAGE_SIZE = max(1, int(dashboard_metrics_page_size or 1000))
    _DASHBOARD_METRICS_MAX_ROWS = max(0, int(dashboard_metrics_max_rows or 0))
    _DASHBOARD_TRIAGE_LIMIT = max(1, int(dashboard_triage_limit or 25))
    _DASHBOARD_ROLLUP_READ_ENABLED = bool(dashboard_rollup_read_enabled)


def _dashboard_normalize_range(raw_value: str) -> str:
    range_key = str(raw_value or _DASHBOARD_DEFAULT_RANGE).strip().lower()
    if range_key not in _DASHBOARD_RANGE_HOURS:
        return _DASHBOARD_DEFAULT_RANGE
    return range_key


def _dashboard_rows_payload(payload: Any) -> list[dict[str, Any]]:
    raw = payload.dump() if hasattr(payload, "dump") else payload
    if not isinstance(raw, dict):
        return []
    rows = raw.get("jobs")
    if not isinstance(rows, list):
        return []
    return [dict(row) for row in rows if isinstance(row, dict)]


def _dashboard_groups_payload(payload: Any) -> dict[str, list[dict[str, Any]]]:
    group_order = ("needs_attention", "acknowledged", "running", "blocked", "done")
    raw = payload.dump() if hasattr(payload, "dump") else payload
    if not isinstance(raw, dict):
        return {group_key: [] for group_key in group_order}
    groups = raw.get("groups") or {}
    if not isinstance(groups, dict):
        groups = {}

    def _rows(group_key: str) -> list[dict[str, Any]]:
        enriched: list[dict[str, Any]] = []
        for row in groups.get(group_key) or []:
            if not isinstance(row, dict):
                continue
            payload_row = dict(row)
            status_meta = job_status_meta(payload_row.get("status"))
            payload_row.setdefault("status", status_meta["code"])
            payload_row.setdefault("status_label", str(status_meta.get("label") or ""))
            payload_row.setdefault("status_tone", str(status_meta.get("tone") or "gray"))
            payload_row.setdefault("status_terminal", bool(status_meta.get("terminal")))
            payload_row.setdefault(
                "attention_group",
                job_attention_group(
                    payload_row.get("status"),
                    payload_row.get("acknowledged_at"),
                    retryable_interrupted_step_status="retryable_interrupted_step",
                ),
            )
            enriched.append(payload_row)
        return enriched

    return {group_key: _rows(group_key) for group_key in group_order}


def _dashboard_collect_metric_rows(
    *,
    bl: Any,
    account_id: str,
    window_start: datetime,
    window_end: datetime,
) -> tuple[list[dict[str, Any]], bool]:
    if not bl or not hasattr(bl, "list_jobs_for_attention"):
        return [], False

    rows: list[dict[str, Any]] = []
    offset = 0
    page_size = max(1, int(_DASHBOARD_METRICS_PAGE_SIZE or 1000))
    max_rows = max(0, int(_DASHBOARD_METRICS_MAX_ROWS or 0))
    truncated = False

    while True:
        raw_batch = (
            bl.list_jobs_for_attention(
                limit=page_size,
                offset=offset,
                account_id=account_id,
                finished_after=window_start,
                finished_before=window_end,
            )
            or []
        )

        if not raw_batch:
            break

        batch = [dict(raw) for raw in raw_batch if isinstance(raw, dict)]

        if batch:
            rows.extend(batch)
            if max_rows > 0 and len(rows) >= max_rows:
                rows = rows[:max_rows]
                truncated = True
                break

        if len(raw_batch) < page_size:
            break
        offset += page_size

    return rows, truncated


def _dashboard_row_job_id(row: dict[str, Any]) -> str:
    return str(row.get("job_id") or row.get("id") or "").strip()


def _dashboard_dedup_job_ids(rows: list[dict[str, Any]]) -> list[str]:
    job_ids = [_dashboard_row_job_id(row) for row in rows]
    return [job_id for job_id in dict.fromkeys(job_ids) if job_id]


def _dashboard_cost_entry(raw: dict[str, Any]) -> dict[str, Any]:
    dominant_agent = str(raw.get("dominant_agent") or "").strip() or None
    raw_cost = raw.get("cost_usd")
    unknown_tokens = max(0, int(raw.get("unknown_cli_tokens") or 0))
    cost_usd = None if raw_cost is None else _safe_float(raw_cost)
    if unknown_tokens > 0 and not cost_usd:
        cost_usd = None
    cost_knownness = "known"
    if unknown_tokens > 0:
        cost_knownness = "partial" if cost_usd is not None else "unknown"
    return {
        "cost_usd": cost_usd,
        "cost_knownness": cost_knownness,
        "unknown_cli_tokens": unknown_tokens,
        "dominant_agent": dominant_agent,
    }


def _dashboard_fetch_cost_by_job_bulk(
    *, bl: Any, job_ids: list[str]
) -> dict[str, dict[str, Any]] | None:
    if not hasattr(bl, "get_jobs_cost_summary"):
        return None
    try:
        payload = bl.get_jobs_cost_summary(job_ids) or {}
    except Exception:
        return None

    payload_by_job_id = {str(key): value for key, value in payload.items()}
    costs: dict[str, dict[str, Any]] = {}
    for job_id in job_ids:
        row = payload_by_job_id.get(str(job_id))
        row = row or {}
        costs[job_id] = _dashboard_cost_entry(row if isinstance(row, dict) else {})
    return costs


def _dashboard_fetch_cost_by_job_single(
    *, bl: Any, job_ids: list[str]
) -> dict[str, dict[str, Any]]:
    if not hasattr(bl, "get_job_cost_summary"):
        return {}

    costs: dict[str, dict[str, Any]] = {}
    for job_id in job_ids:
        try:
            payload = bl.get_job_cost_summary(job_id) or {}
        except Exception:
            costs[job_id] = {"cost_usd": None, "dominant_agent": None}
            continue
        costs[job_id] = _dashboard_cost_entry(payload if isinstance(payload, dict) else {})
    return costs


def _dashboard_fetch_cost_by_job(
    *,
    bl: Any,
    rows: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    dedup_job_ids = _dashboard_dedup_job_ids(rows)
    if not dedup_job_ids or not bl:
        return {}

    bulk_result = _dashboard_fetch_cost_by_job_bulk(bl=bl, job_ids=dedup_job_ids)
    if bulk_result is not None:
        return bulk_result
    return _dashboard_fetch_cost_by_job_single(bl=bl, job_ids=dedup_job_ids)


def _dashboard_enrich_rows_with_cost(*, bl: Any, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    enrichment_by_job = _dashboard_fetch_cost_by_job(bl=bl, rows=rows)
    if not enrichment_by_job:
        return
    for row in rows:
        job_id = _dashboard_row_job_id(row)
        if not job_id:
            continue
        enrichment = enrichment_by_job.get(job_id) or {}
        raw_cost = enrichment.get("cost_usd")
        row["cost_usd"] = None if raw_cost is None else _safe_float(raw_cost)
        row["cost_knownness"] = str(enrichment.get("cost_knownness") or "known")
        row["unknown_cli_tokens"] = int(enrichment.get("unknown_cli_tokens") or 0)
        current_agent = str(row.get("current_agent") or "").strip()
        if current_agent:
            continue
        dominant_agent = str(enrichment.get("dominant_agent") or "").strip()
        if dominant_agent:
            row["current_agent"] = dominant_agent


def _dashboard_full_day_range(
    window_start: datetime, window_end: datetime
) -> tuple[Any | None, Any | None]:
    if window_end <= window_start:
        return None, None
    start_floor = window_start.replace(hour=0, minute=0, second=0, microsecond=0)
    first_full = (
        start_floor.date()
        if window_start <= start_floor
        else (start_floor + timedelta(days=1)).date()
    )
    end_floor = window_end.replace(hour=0, minute=0, second=0, microsecond=0)
    last_full = (end_floor - timedelta(days=1)).date()
    if first_full > last_full:
        return None, None
    return first_full, last_full


def _dashboard_result_from_status(
    status: str, *, success_statuses: set[str], failed_statuses: set[str]
) -> str:
    normalized = normalize_job_status(status)
    if normalized in success_statuses or normalized in failed_statuses:
        return dashboard_result_from_status(normalized)
    return dashboard_result_from_status(normalized)


def _dashboard_build_record(
    row: dict[str, Any], *, success_statuses: set[str], failed_statuses: set[str]
) -> dict[str, Any] | None:
    status = str(row.get("status") or "").strip().lower()
    result = _dashboard_result_from_status(
        status,
        success_statuses=success_statuses,
        failed_statuses=failed_statuses,
    )
    if not result:
        return None

    finished_at = _dashboard_parse_dt(row.get("finished_at"))
    if not finished_at:
        return None

    started_at = _dashboard_parse_dt(row.get("started_at"))
    duration_sec: float | None = None
    if started_at and finished_at >= started_at:
        duration_sec = float((finished_at - started_at).total_seconds())

    return {
        "job_id": _dashboard_row_job_id(row) or None,
        "project_name": str(row.get("project_name") or "Unknown").strip() or "Unknown",
        "workflow": str(row.get("selected_workflow") or "auto").strip() or "auto",
        "agent": str(row.get("current_agent") or "unknown").strip() or "unknown",
        "finished_at": finished_at,
        "duration_sec": duration_sec,
        "result": result,
        "cost_usd": (None if row.get("cost_usd") is None else _safe_float(row.get("cost_usd"))),
        "cost_knownness": str(row.get("cost_knownness") or "known"),
        "unknown_cli_tokens": int(row.get("unknown_cli_tokens") or 0),
    }


def _dashboard_partition_records(
    *,
    rows: list[dict[str, Any]],
    success_statuses: set[str],
    failed_statuses: set[str],
    current_start: datetime,
    previous_start: datetime,
    now_utc: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    current_records: list[dict[str, Any]] = []
    previous_records: list[dict[str, Any]] = []
    for row in rows:
        record = _dashboard_build_record(
            row,
            success_statuses=success_statuses,
            failed_statuses=failed_statuses,
        )
        if not record:
            continue
        finished_at = record["finished_at"]
        if current_start <= finished_at <= now_utc:
            current_records.append(record)
        elif previous_start <= finished_at < current_start:
            previous_records.append(record)
    return current_records, previous_records


def _dashboard_init_daily_maps(
    *, start_day: Any, end_day: Any
) -> tuple[dict[str, dict[str, int]], dict[str, list[float]]]:
    throughput_map: dict[str, dict[str, int]] = {}
    duration_map: dict[str, list[float]] = {}
    day_cursor = start_day
    while day_cursor <= end_day:
        key = day_cursor.isoformat()
        throughput_map[key] = {"completed": 0, "failed": 0}
        duration_map[key] = []
        day_cursor += timedelta(days=1)
    return throughput_map, duration_map


def _dashboard_rollup_float(raw: Any) -> float | None:
    try:
        return float(raw)
    except Exception:
        return None


def _dashboard_apply_daily_rollups(
    *,
    daily_rollups: list[dict[str, Any]] | None,
    throughput_map: dict[str, dict[str, int]],
) -> tuple[dict[str, dict[str, float | None]], set[str]]:
    duration_overrides: dict[str, dict[str, float | None]] = {}
    if not isinstance(daily_rollups, list):
        return duration_overrides, set()

    for rollup in daily_rollups:
        if not isinstance(rollup, dict):
            continue
        day_key = str(rollup.get("rollup_date") or "").strip()[:10]
        if not day_key or day_key not in throughput_map:
            continue
        throughput_map[day_key] = {
            "completed": int(rollup.get("completed_count") or 0),
            "failed": int(rollup.get("failed_count") or 0),
        }
        duration_overrides[day_key] = {
            "p50_seconds": _dashboard_rollup_float(rollup.get("p50_seconds")),
            "p95_seconds": _dashboard_rollup_float(rollup.get("p95_seconds")),
        }
    return duration_overrides, set(duration_overrides.keys())


def _dashboard_attention_by_project(needs_attention: list[dict[str, Any]]) -> dict[str, int]:
    attention_by_project: dict[str, int] = {}
    for row in needs_attention:
        project_name = str(row.get("project_name") or "Unknown").strip() or "Unknown"
        attention_by_project[project_name] = int(attention_by_project.get(project_name, 0)) + 1
    return attention_by_project


def _dashboard_init_project_row(project_name: str, attention_count: int) -> dict[str, Any]:
    return {
        "project_name": project_name,
        "attention_count": int(attention_count),
        "completed": 0,
        "failed": 0,
        "durations": [],
        "spend_usd": 0.0,
        "spend_known_count": 0,
        "spend_unknown_count": 0,
    }


def _dashboard_accumulate_current_records(
    *,
    current_records: list[dict[str, Any]],
    throughput_map: dict[str, dict[str, int]],
    duration_map: dict[str, list[float]],
    rollup_covered_days: set[str],
    attention_by_project: dict[str, int],
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
]:
    spend_by_project: dict[str, dict[str, Any]] = {}
    spend_by_workflow: dict[str, dict[str, Any]] = {}
    spend_by_agent: dict[str, dict[str, Any]] = {}
    project_rows: dict[str, dict[str, Any]] = {}

    for record in current_records:
        day_key = record["finished_at"].date().isoformat()
        if day_key in throughput_map and day_key not in rollup_covered_days:
            throughput_map[day_key][record["result"]] = (
                int(throughput_map[day_key][record["result"]]) + 1
            )
            if record.get("duration_sec") is not None:
                duration_map[day_key].append(float(record["duration_sec"]))

        project_name = record["project_name"]
        workflow_key = record["workflow"]
        agent_key = record["agent"]
        raw_spend = record.get("cost_usd")
        spend_value = None if raw_spend is None else float(raw_spend)
        spend_partial = str(record.get("cost_knownness") or "known") in {
            "partial",
            "unknown",
        }
        for payload, key in (
            (spend_by_project, project_name),
            (spend_by_workflow, workflow_key),
            (spend_by_agent, agent_key),
        ):
            bucket = payload.setdefault(key, {"known_total": 0.0, "known": 0, "unknown": 0})
            if spend_value is None:
                bucket["unknown"] = int(bucket["unknown"]) + 1
            else:
                bucket["known_total"] = float(bucket["known_total"]) + spend_value
                bucket["known"] = int(bucket["known"]) + 1
                if spend_partial:
                    bucket["unknown"] = int(bucket["unknown"]) + 1

        project_row = project_rows.setdefault(
            project_name,
            _dashboard_init_project_row(
                project_name=project_name,
                attention_count=int(attention_by_project.get(project_name, 0)),
            ),
        )
        if record["result"] == "completed":
            project_row["completed"] = int(project_row["completed"]) + 1
        else:
            project_row["failed"] = int(project_row["failed"]) + 1
        if record.get("duration_sec") is not None:
            project_row["durations"].append(float(record["duration_sec"]))
        if spend_value is None:
            project_row["spend_unknown_count"] = int(project_row["spend_unknown_count"]) + 1
        else:
            project_row["spend_usd"] = float(project_row["spend_usd"] + spend_value)
            project_row["spend_known_count"] = int(project_row["spend_known_count"]) + 1
            if spend_partial:
                project_row["spend_unknown_count"] = int(project_row["spend_unknown_count"]) + 1

    return spend_by_project, spend_by_workflow, spend_by_agent, project_rows


def _dashboard_add_attention_only_projects(
    *, project_rows: dict[str, dict[str, Any]], attention_by_project: dict[str, int]
) -> None:
    for project_name, attention_count in attention_by_project.items():
        project_rows.setdefault(
            project_name,
            _dashboard_init_project_row(project_name=project_name, attention_count=attention_count),
        )


def _dashboard_top_breakdown(
    payload: dict[str, dict[str, Any]], *, limit: int = 8
) -> list[dict[str, Any]]:
    ranked = sorted(
        payload.items(),
        key=lambda item: float(item[1].get("known_total") or 0.0),
        reverse=True,
    )[: max(1, int(limit or 8))]
    rows: list[dict[str, Any]] = []
    for name, bucket in ranked:
        known = int(bucket.get("known") or 0)
        unknown = int(bucket.get("unknown") or 0)
        availability = "measured"
        if unknown:
            availability = "partial" if known else "unavailable"
        rows.append(
            {
                "name": str(name),
                "spend_usd": float(bucket.get("known_total") or 0.0) if known else None,
                "availability": availability,
            }
        )
    return rows


def _dashboard_build_series(
    *,
    throughput_map: dict[str, dict[str, int]],
    duration_map: dict[str, list[float]],
    duration_overrides: dict[str, dict[str, float | None]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    throughput_series: list[dict[str, Any]] = []
    duration_series: list[dict[str, Any]] = []
    for key in sorted(throughput_map.keys()):
        completed_count = int(throughput_map[key]["completed"])
        failed_count = int(throughput_map[key]["failed"])

        duration_override = duration_overrides.get(key) or {}
        p50_seconds = duration_override.get("p50_seconds")
        p95_seconds = duration_override.get("p95_seconds")
        if p50_seconds is None and p95_seconds is None:
            durations = duration_map[key]
            p50_seconds = _dashboard_percentile(durations, 0.50)
            p95_seconds = _dashboard_percentile(durations, 0.95)

        throughput_series.append(
            {
                "date": key,
                "completed": completed_count,
                "failed": failed_count,
                "total": completed_count + failed_count,
            }
        )
        duration_series.append(
            {
                "date": key,
                "p50_seconds": p50_seconds,
                "p95_seconds": p95_seconds,
            }
        )
    return throughput_series, duration_series


def _dashboard_build_breakdown_rows(
    project_rows: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    breakdown_rows: list[dict[str, Any]] = []
    for row in project_rows.values():
        done_count = int(row["completed"])
        fail_count = int(row["failed"])
        finished_count = done_count + fail_count
        success_rate = ((done_count / finished_count) * 100.0) if finished_count else None
        p50_project = _dashboard_percentile([float(value) for value in row["durations"]], 0.50)
        spend_unknown_count = int(row["spend_unknown_count"])
        spend_known_count = int(row["spend_known_count"])
        spend_availability = "measured"
        if spend_unknown_count:
            spend_availability = "partial" if spend_known_count else "unavailable"
        breakdown_rows.append(
            {
                "project_name": row["project_name"],
                "attention_count": int(row["attention_count"]),
                "completed": done_count,
                "failed": fail_count,
                "finished": finished_count,
                "success_rate_pct": success_rate,
                "p50_duration_seconds": p50_project,
                "spend_usd": float(row["spend_usd"]) if spend_known_count else None,
                "spend_availability": spend_availability,
            }
        )
    breakdown_rows.sort(
        key=lambda row: (
            int(row.get("attention_count") or 0),
            int(row.get("failed") or 0),
            float(row.get("spend_usd") or 0.0),
        ),
        reverse=True,
    )
    return breakdown_rows


def _dashboard_compute_summary(
    *,
    rows: list[dict[str, Any]],
    groups: dict[str, list[dict[str, Any]]],
    range_hours: int,
    daily_rollups: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    success_statuses = set(JOB_SUCCESS_STATUSES)
    failed_statuses = set(JOB_FAILURE_STATUSES)
    now_utc = datetime.now(UTC)
    current_start = now_utc - timedelta(hours=max(1, int(range_hours or 24)))
    previous_start = current_start - timedelta(hours=max(1, int(range_hours or 24)))

    current_records, previous_records = _dashboard_partition_records(
        rows=rows,
        success_statuses=success_statuses,
        failed_statuses=failed_statuses,
        current_start=current_start,
        previous_start=previous_start,
        now_utc=now_utc,
    )

    current_success, current_failed = _dashboard_outcome_counts(current_records)
    previous_success, previous_failed = _dashboard_outcome_counts(previous_records)

    current_success_rate = _dashboard_success_rate(current_success, current_failed)
    previous_success_rate = _dashboard_success_rate(previous_success, previous_failed)

    current_durations = _dashboard_duration_values(current_records)
    previous_durations = _dashboard_duration_values(previous_records)
    current_p50 = _dashboard_percentile(current_durations, 0.50)
    previous_p50 = _dashboard_percentile(previous_durations, 0.50)
    current_p95 = _dashboard_percentile(current_durations, 0.95)

    current_spend = _dashboard_spend_summary(current_records)
    previous_spend = _dashboard_spend_summary(previous_records)

    running = groups.get("running") or []
    blocked = groups.get("blocked") or []
    needs_attention = groups.get("needs_attention") or []
    acknowledged = groups.get("acknowledged") or []
    triage_groups = {
        "needs_attention": list(needs_attention),
        "acknowledged": list(acknowledged),
        "running": list(running),
        "blocked": list(blocked),
        "done": list(groups.get("done") or []),
    }
    pending_now, working_now = _dashboard_running_counts(running, blocked)
    in_progress_now = sum(
        1 for row in running if normalize_job_status(row.get("status")) == "in_progress"
    )
    awaiting_approval_now = sum(
        1
        for row in (*needs_attention, *acknowledged, *running, *blocked)
        if normalize_job_status(row.get("status")) == "awaiting_approval"
    )

    throughput_map, duration_map = _dashboard_init_daily_maps(
        start_day=current_start.date(),
        end_day=now_utc.date(),
    )
    duration_overrides, rollup_covered_days = _dashboard_apply_daily_rollups(
        daily_rollups=daily_rollups,
        throughput_map=throughput_map,
    )
    attention_by_project = _dashboard_attention_by_project(needs_attention)
    spend_by_project, spend_by_workflow, spend_by_agent, project_rows = (
        _dashboard_accumulate_current_records(
            current_records=current_records,
            throughput_map=throughput_map,
            duration_map=duration_map,
            rollup_covered_days=rollup_covered_days,
            attention_by_project=attention_by_project,
        )
    )
    _dashboard_add_attention_only_projects(
        project_rows=project_rows,
        attention_by_project=attention_by_project,
    )

    throughput_series, duration_series = _dashboard_build_series(
        throughput_map=throughput_map,
        duration_map=duration_map,
        duration_overrides=duration_overrides,
    )
    breakdown_rows = _dashboard_build_breakdown_rows(project_rows)

    return {
        "generated_at": now_utc.isoformat(),
        "hero_kpis": {
            "needs_attention": {
                "label": "Needs Attention",
                "value": len(needs_attention),
            },
            "in_progress": {
                "label": "In Progress",
                "value": int(in_progress_now),
            },
            "awaiting_approval": {
                "label": "Waiting for Approval",
                "value": int(awaiting_approval_now),
            },
            "completed": {
                "label": "Completed",
                "value": int(current_success),
            },
            "done": {
                "label": "Completed",
                "value": int(current_success),
            },
            "queue_working": {
                "label": "Queue / Working",
                "queue": int(pending_now),
                "working": int(working_now),
                "value": f"{int(pending_now)} / {int(working_now)}",
            },
            "success_rate": {
                "label": "Success Rate",
                "value_pct": current_success_rate,
                "display": _dashboard_format_percent(current_success_rate),
                "completed": int(current_success),
                "failed": int(current_failed),
                "delta_pp": (current_success_rate - previous_success_rate)
                if (current_success_rate is not None and previous_success_rate is not None)
                else None,
            },
            "p50_duration": {
                "label": "P50 Duration",
                "value_seconds": current_p50,
                "p95_seconds": current_p95,
                "display": _dashboard_format_duration(current_p50),
                "delta_pct": _dashboard_pct_delta(current_p50, previous_p50),
            },
            "spend": {
                "label": "Spend",
                **current_spend,
                "delta_pct": _dashboard_pct_delta(
                    current_spend["value_usd"], previous_spend["value_usd"]
                ),
            },
        },
        "trends": {
            "daily_throughput": throughput_series,
            "duration": duration_series,
            "spend_breakdown": {
                "project": _dashboard_top_breakdown(spend_by_project),
                "workflow": _dashboard_top_breakdown(spend_by_workflow),
                "agent": _dashboard_top_breakdown(spend_by_agent),
            },
        },
        "breakdown_rows": breakdown_rows[:25],
        "groups": triage_groups,
        "triage_groups": triage_groups,
        "sampled_jobs": len(rows),
    }


def _dashboard_collect_rows_with_enrichment(
    *,
    bl: Any,
    account_id: str,
    window_start: datetime,
    window_end: datetime,
) -> tuple[list[dict[str, Any]], bool]:
    rows, truncated = _dashboard_collect_metric_rows(
        bl=bl,
        account_id=account_id,
        window_start=window_start,
        window_end=window_end,
    )
    _dashboard_enrich_rows_with_cost(bl=bl, rows=rows)
    return rows, truncated


def _dashboard_collect_rollup_fact_rows(
    *,
    bl: Any,
    account_id: str,
    historical_window_start: datetime,
    historical_window_end: datetime,
) -> tuple[list[dict[str, Any]], str]:
    if not _DASHBOARD_ROLLUP_READ_ENABLED or not bl or not hasattr(bl, "list_dashboard_job_facts"):
        return [], "live"
    try:
        historical_rows = (
            bl.list_dashboard_job_facts(
                account_id=account_id,
                window_start=historical_window_start,
                window_end=historical_window_end,
            )
            or []
        )
        historical_rows = [dict(row) for row in historical_rows if isinstance(row, dict)]
    except Exception:
        return [], "live"

    if historical_rows:
        return historical_rows, "rollup_facts"

    if hasattr(bl, "get_dashboard_rollup_state"):
        state = bl.get_dashboard_rollup_state(account_id=account_id) or {}
        covered_from = _dashboard_parse_dt(state.get("facts_covered_from"))
        covered_until = _dashboard_parse_dt(state.get("facts_covered_until"))
        if (
            covered_from
            and covered_until
            and covered_from <= historical_window_start
            and covered_until >= historical_window_end
        ):
            return [], "rollup_facts"

    return [], "live"


def _dashboard_collect_historical_rows(
    *,
    bl: Any,
    metrics_bl: Any | None = None,
    account_id: str,
    historical_window_start: datetime,
    historical_window_end: datetime,
) -> tuple[list[dict[str, Any]], bool, str]:
    if historical_window_start >= historical_window_end:
        return [], False, "live"

    historical_rows, historical_source = _dashboard_collect_rollup_fact_rows(
        bl=bl,
        account_id=account_id,
        historical_window_start=historical_window_start,
        historical_window_end=historical_window_end,
    )
    if historical_rows or historical_source == "rollup_facts":
        return historical_rows, False, historical_source

    historical_rows, historical_truncated = _dashboard_collect_rows_with_enrichment(
        bl=metrics_bl if metrics_bl is not None else bl,
        account_id=account_id,
        window_start=historical_window_start,
        window_end=historical_window_end,
    )
    return historical_rows, historical_truncated, historical_source


def _dashboard_collect_daily_rollups(
    *,
    bl: Any,
    account_id: str,
    historical_source: str,
    historical_window_start: datetime,
    historical_window_end: datetime,
) -> list[dict[str, Any]]:
    if (
        historical_source != "rollup_facts"
        or not _DASHBOARD_ROLLUP_READ_ENABLED
        or not bl
        or not hasattr(bl, "list_dashboard_daily_rollups")
    ):
        return []

    full_day_start, full_day_end = _dashboard_full_day_range(
        historical_window_start,
        historical_window_end,
    )
    if not full_day_start or not full_day_end:
        return []

    try:
        daily_rollups = (
            bl.list_dashboard_daily_rollups(
                account_id=account_id,
                start_date=full_day_start,
                end_date=full_day_end,
            )
            or []
        )
    except Exception:
        return []
    return [dict(row) for row in daily_rollups if isinstance(row, dict)]


def _dashboard_collect_live_rows(
    *,
    bl: Any,
    account_id: str,
    live_window_start: datetime,
    now_utc: datetime,
) -> tuple[list[dict[str, Any]], bool]:
    if live_window_start >= now_utc:
        return [], False
    return _dashboard_collect_rows_with_enrichment(
        bl=bl,
        account_id=account_id,
        window_start=live_window_start,
        window_end=now_utc,
    )


def _dashboard_dedupe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped_rows: list[dict[str, Any]] = []
    seen_job_ids: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        job_id = _dashboard_row_job_id(row)
        if job_id:
            if job_id in seen_job_ids:
                continue
            seen_job_ids.add(job_id)
        deduped_rows.append(dict(row))
    return deduped_rows


__all__ = [
    "_DASHBOARD_DEFAULT_RANGE",
    "_DASHBOARD_METRICS_MAX_ROWS",
    "_DASHBOARD_RANGE_HOURS",
    "_DASHBOARD_TRIAGE_LIMIT",
    "_dashboard_collect_daily_rollups",
    "_dashboard_collect_historical_rows",
    "_dashboard_collect_live_rows",
    "_dashboard_compute_summary",
    "_dashboard_dedupe_rows",
    "_dashboard_fetch_cost_by_job",
    "_dashboard_groups_payload",
    "_dashboard_normalize_range",
    "configure_dashboard_metrics",
]
