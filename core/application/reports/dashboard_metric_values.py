"""Pure date, statistic and display values shared by dashboard and report summaries."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except Exception:
        return float(default)
    if parsed < 0:
        return float(default)
    return parsed


def _dashboard_parse_dt(raw_value: Any) -> datetime | None:
    if isinstance(raw_value, datetime):
        parsed = raw_value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    text = str(raw_value or "").strip()
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


def _dashboard_percentile(values: list[float], quantile: float) -> float | None:
    series = sorted(float(v) for v in values if v is not None)
    if not series:
        return None
    if len(series) == 1:
        return float(series[0])
    q = max(0.0, min(float(quantile), 1.0))
    pos = (len(series) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(series) - 1)
    if lo == hi:
        return float(series[lo])
    weight = pos - lo
    return float(series[lo] + (series[hi] - series[lo]) * weight)


def _dashboard_pct_delta(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None:
        return None
    if abs(previous) < 1e-9:
        return 0.0 if abs(current) < 1e-9 else 100.0
    return ((current - previous) / abs(previous)) * 100.0


def _dashboard_format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    sec = max(0, round(seconds))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        mins = sec // 60
        rem = sec % 60
        return f"{mins}m {rem}s"
    hours = sec // 3600
    mins = (sec % 3600) // 60
    return f"{hours}h {mins}m"


def _dashboard_format_percent(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.1f}%"


def _dashboard_outcome_counts(records: list[dict[str, Any]]) -> tuple[int, int]:
    completed = sum(1 for record in records if record["result"] == "completed")
    failed = sum(1 for record in records if record["result"] == "failed")
    return completed, failed


def _dashboard_success_rate(completed: int, failed: int) -> float | None:
    total = completed + failed
    return ((completed / total) * 100.0) if total else None


def _dashboard_duration_values(records: list[dict[str, Any]]) -> list[float]:
    return [
        float(record["duration_sec"])
        for record in records
        if record.get("duration_sec") is not None
    ]


def _dashboard_spend_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    known_values = [
        float(record["cost_usd"]) for record in records if record.get("cost_usd") is not None
    ]
    unknown_jobs = sum(
        1
        for record in records
        if record.get("cost_usd") is None
        or str(record.get("cost_knownness") or "known") in {"partial", "unknown"}
    )
    known_total = float(sum(known_values))
    if unknown_jobs and not known_values:
        return {
            "value_usd": None,
            "display": "Unavailable",
            "availability": "unavailable",
            "unknown_jobs": unknown_jobs,
        }
    availability = "partial" if unknown_jobs else "measured"
    display = f"${known_total:,.2f}"
    if availability == "partial":
        display = f"{display} (partial)"
    return {
        "value_usd": known_total,
        "display": display,
        "availability": availability,
        "unknown_jobs": unknown_jobs,
    }


def _dashboard_running_counts(
    running: list[dict[str, Any]], blocked: list[dict[str, Any]]
) -> tuple[int, int]:
    pending_now = sum(
        1 for row in running if str(row.get("status") or "").strip().lower() == "pending"
    )
    in_progress_now = sum(
        1 for row in running if str(row.get("status") or "").strip().lower() == "in_progress"
    )
    return pending_now, in_progress_now + len(blocked)
