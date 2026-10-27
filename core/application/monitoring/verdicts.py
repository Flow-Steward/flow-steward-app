"""One verdict per role and dependency, from current observations only.

The same reading the Services tab gives an operator, for the diagnostic report:
is each role running and healthy now, and what does that rest on. Past process
starts never decide a verdict.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from core.application.monitoring.queries import ComponentRow, ComponentsProjection, ExpectedGap

_TONE_ORDER = {"problem": 0, "warning": 1, "unknown": 2, "ok": 3}


def _fresh(row: ComponentRow) -> bool:
    return row.freshness == "fresh" and row.source_freshness == "fresh"


def _verdict(
    name: str, kind: str, rows: list[ComponentRow], gap: ExpectedGap | None
) -> dict[str, Any]:
    processes = [row for row in rows if row.observation_kind == "process"]
    instances = {row.instance_id for row in processes}
    running = {row.instance_id for row in processes if _fresh(row)}
    silent = len(instances - running)
    checked = [
        row
        for row in rows
        if _fresh(row) and row.health.freshness == "fresh" and row.health.state != "not_observed"
    ]
    failing = next((row for row in checked if row.health.state == "unhealthy"), None)
    healthy = next((row for row in checked if row.health.state == "healthy"), None)
    not_ready = any(
        _fresh(row) and row.readiness.freshness == "fresh" and row.readiness.state == "not_ready"
        for row in rows
    )
    any_fresh = any(_fresh(row) for row in rows)
    verdict, tone = "running_health_not_measured", "unknown"
    if failing is not None or not_ready:
        verdict, tone = ("unreachable" if kind == "dependency" else "unhealthy"), "problem"
    elif gap is not None and gap.reason == "replica_target_unobserved":
        verdict, tone = f"{len(running)}_of_{gap.desired_count}_running", "warning"
    elif (gap is not None and gap.reason == "missing") or (not rows and gap is not None):
        verdict, tone = "not_reporting", "warning"
    elif not any_fresh or (gap is not None and gap.reason == "stale") or silent:
        verdict, tone = (
            ("instances_not_reporting" if silent and any_fresh else "not_reporting"),
            "warning",
        )
    elif healthy is not None:
        verdict, tone = "healthy", "ok"
    evidence_row = failing or healthy
    latest = max(rows, key=lambda row: row.observed_at, default=None)
    return {
        "name": name,
        "kind": kind,
        "verdict": verdict,
        "severity": tone,
        "running_instances": len(running) if kind == "role" else None,
        "not_reporting_instances": silent if kind == "role" else None,
        "desired_instances": gap.desired_count if gap is not None else None,
        "health_evidence": evidence_row.health.evidence if evidence_row is not None else None,
        "health_checked_seconds_ago": round(evidence_row.health.age_seconds or 0)
        if evidence_row is not None
        else None,
        "last_report_seconds_ago": round(latest.effective_age_seconds) if latest else None,
    }


def service_verdicts(components: ComponentsProjection | None) -> list[dict[str, Any]]:
    if components is None or not components.available:
        return []
    current = [
        row for row in components.rows if not row.superseded and row.observation_kind != "channel"
    ]
    grouped: dict[tuple[str, str], list[ComponentRow]] = defaultdict(list)
    for row in current:
        kind = "dependency" if row.observation_kind == "dependency" else "role"
        grouped[(kind, row.service_type)].append(row)
    gaps = {
        ("dependency" if gap.observation_kind == "dependency" else "role", gap.service_type): gap
        for gap in components.expected_gaps
    }
    for key in gaps:
        grouped.setdefault(key, [])
    verdicts = [
        _verdict(name, kind, rows, gaps.get((kind, name))) for (kind, name), rows in grouped.items()
    ]
    return sorted(
        verdicts, key=lambda item: (_TONE_ORDER[item["severity"]], item["kind"], item["name"])
    )


__all__ = ["service_verdicts"]
