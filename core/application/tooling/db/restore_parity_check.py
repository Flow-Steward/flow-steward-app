#!/usr/bin/env python3
"""Backup/restore parity verification for platform observability DB.

Typical flow:
  1) Capture baseline before restore:
       flow-steward database restore-parity-capture --output backups/restore/baseline.json
  2) Restore backup into disposable DB.
  3) Verify restored DB against baseline:
       flow-steward database restore-parity-verify --baseline backups/restore/baseline.json
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.application.jobs.schema_metadata import JOB_ACTIVITY_TIMESTAMP_COLUMN
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

CRITICAL_TABLES = (
    "projects",
    "jobs",
    "platform_action_log",
    "platform_agent_messages",
    "platform_events",
    "platform_event_retry_queue",
)


def _query_scalar(cur, sql: str, params: tuple = ()) -> Any:
    cur.execute(sql, params)
    row = cur.fetchone()
    return row[0] if row else None


def _resolve_dsn(raw: str) -> str:
    explicit = str(raw or "").strip()
    if explicit:
        return explicit
    for key in (
        "FS_RESTORE_PARITY_DSN",
        "FS_OBSERVABILITY_DSN",
        "FS_DB_DSN",
        "DATABASE_URL",
    ):
        value = str(_runtime_env.get(key, "")).strip()
        if value:
            return value
    return ""


def _table_exists(cur, table_name: str) -> bool:
    value = _query_scalar(cur, "SELECT to_regclass(%s)", (f"public.{table_name}",))
    return bool(value)


def _table_has_id_column(cur, table_name: str) -> bool:
    value = _query_scalar(
        cur,
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = %s
              AND column_name = 'id'
        )
        """,
        (table_name,),
    )
    return bool(value)


def _latest_ids(cur, table_name: str, limit: int = 5) -> list[str]:
    latest_ids_query = "\n".join(
        [
            "SELECT id",
            "FROM ",
            table_name,
            "ORDER BY id::text DESC",
            "LIMIT %s",
        ]
    )
    cur.execute(
        latest_ids_query,
        (limit,),
    )
    rows = cur.fetchall() or []
    return [str(r[0]) for r in rows if r and r[0] is not None]


def _collect_snapshot(dsn: str, label: str) -> dict[str, Any]:
    if not dsn:
        raise RuntimeError("Observability DSN is not configured.")
    try:
        from core.infrastructure.db import psycopg_runtime as psycopg_runtime
    except Exception as exc:
        raise RuntimeError("psycopg_runtime is required for restore parity checks") from exc

    snapshot: dict[str, Any] = {
        "captured_at": datetime.now(UTC).isoformat(),
        "label": label or "snapshot",
        "tables": {},
        "latest_counts": {},
        "latest_ids": {},
    }

    with psycopg_runtime.connect(dsn) as conn, conn.cursor() as cur:
        for table_name in CRITICAL_TABLES:
            exists = _table_exists(cur, table_name)
            table_entry: dict[str, Any] = {"exists": exists}
            if exists:
                row_count_query = "".join(["SELECT COUNT(*) FROM ", table_name])
                table_entry["row_count"] = int(_query_scalar(cur, row_count_query) or 0)
                if _table_has_id_column(cur, table_name):
                    max_id_query = "".join(
                        ["SELECT id FROM ", table_name, " ORDER BY id::text DESC LIMIT 1"]
                    )
                    table_entry["max_id"] = str(_query_scalar(cur, max_id_query) or "")
                else:
                    table_entry["max_id"] = None
            snapshot["tables"][table_name] = table_entry

        snapshot["latest_counts"] = {
            "jobs_total": int(_query_scalar(cur, "SELECT COUNT(*) FROM jobs") or 0),
            "jobs_last_24h": int(
                _query_scalar(
                    cur,
                    f"SELECT COUNT(*) FROM jobs WHERE {JOB_ACTIVITY_TIMESTAMP_COLUMN} >= NOW() - INTERVAL '24 hours'",  # nosec B608 -- column name is a canonical source constant.
                )
                or 0
            ),
            "messages_total": int(
                _query_scalar(cur, "SELECT COUNT(*) FROM platform_agent_messages") or 0
            ),
            "messages_last_24h": int(
                _query_scalar(
                    cur,
                    "SELECT COUNT(*) FROM platform_agent_messages WHERE timestamp >= NOW() - INTERVAL '24 hours'",
                )
                or 0
            ),
            "events_total": int(_query_scalar(cur, "SELECT COUNT(*) FROM platform_events") or 0),
            "events_last_24h": int(
                _query_scalar(
                    cur,
                    "SELECT COUNT(*) FROM platform_events WHERE timestamp >= NOW() - INTERVAL '24 hours'",
                )
                or 0
            ),
        }

        snapshot["latest_ids"] = {
            "jobs": _latest_ids(cur, "jobs"),
            "messages": _latest_ids(cur, "platform_agent_messages"),
            "events": _latest_ids(cur, "platform_events"),
        }

    return snapshot


def _compare_snapshots(baseline: dict[str, Any], current: dict[str, Any]) -> list[str]:
    mismatches: list[str] = []

    for table_name in CRITICAL_TABLES:
        base_table = (baseline.get("tables") or {}).get(table_name) or {}
        cur_table = (current.get("tables") or {}).get(table_name) or {}
        if bool(base_table.get("exists")) != bool(cur_table.get("exists")):
            mismatches.append(
                f"table '{table_name}': exists baseline={base_table.get('exists')} current={cur_table.get('exists')}"
            )
            continue
        if not base_table.get("exists"):
            continue
        for field in ("row_count", "max_id"):
            if base_table.get(field) != cur_table.get(field):
                mismatches.append(
                    f"table '{table_name}': {field} baseline={base_table.get(field)} current={cur_table.get(field)}"
                )

    metric_keys = (
        "jobs_total",
        "jobs_last_24h",
        "messages_total",
        "messages_last_24h",
        "events_total",
        "events_last_24h",
    )
    base_counts = baseline.get("latest_counts") or {}
    cur_counts = current.get("latest_counts") or {}
    for key in metric_keys:
        if base_counts.get(key) != cur_counts.get(key):
            mismatches.append(
                f"metric '{key}': baseline={base_counts.get(key)} current={cur_counts.get(key)}"
            )

    id_keys = ("jobs", "messages", "events")
    base_ids = baseline.get("latest_ids") or {}
    cur_ids = current.get("latest_ids") or {}
    for key in id_keys:
        if list(base_ids.get(key) or []) != list(cur_ids.get(key) or []):
            mismatches.append(
                f"latest_ids '{key}': baseline={base_ids.get(key)} current={cur_ids.get(key)}"
            )

    return mismatches


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _cmd_capture(args: argparse.Namespace) -> int:
    result = capture_restore_parity_metadata(
        dsn=args.dsn,
        label=args.label,
        output=Path(args.output),
    )
    counts = result["snapshot"]["latest_counts"]
    print(f"[restore-parity] baseline written: {result['output']}")
    print(f"[restore-parity] jobs_total={counts['jobs_total']}")
    print(f"[restore-parity] messages_total={counts['messages_total']}")
    print(f"[restore-parity] events_total={counts['events_total']}")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    result = verify_restore_parity_metadata(
        dsn=args.dsn,
        label=args.label,
        baseline=Path(args.baseline),
        output=Path(args.output) if args.output else None,
    )
    if result.get("output"):
        print(f"[restore-parity] current snapshot written: {result['output']}")

    if result["mismatches"]:
        print("[restore-parity] FAIL: snapshot mismatch detected")
        for msg in result["mismatches"]:
            print(f"  - {msg}")
        return 1

    current = result["current"]
    print("[restore-parity] PASS: baseline and restored DB match")
    print(f"[restore-parity] jobs_total={current['latest_counts']['jobs_total']}")
    print(f"[restore-parity] messages_total={current['latest_counts']['messages_total']}")
    print(f"[restore-parity] events_total={current['latest_counts']['events_total']}")
    return 0


def capture_restore_parity_metadata(
    *,
    dsn: str = "",
    label: str = "baseline",
    output: Path,
) -> dict[str, Any]:
    """Capture restore-parity metadata and write it to ``output``."""
    resolved_dsn = _resolve_dsn(dsn)
    snapshot = _collect_snapshot(dsn=resolved_dsn, label=label)
    _write_json(output, snapshot)
    return {"output": str(output), "snapshot": snapshot}


def verify_restore_parity_metadata(
    *,
    dsn: str = "",
    label: str = "restored",
    baseline: Path,
    output: Path | None = None,
) -> dict[str, Any]:
    """Verify current restore-parity metadata against a captured baseline."""
    if not baseline.exists():
        raise FileNotFoundError(f"Baseline snapshot not found: {baseline}")

    baseline_payload = _load_json(baseline)
    resolved_dsn = _resolve_dsn(dsn)
    current = _collect_snapshot(dsn=resolved_dsn, label=label)
    mismatches = _compare_snapshots(baseline_payload, current)

    if output is not None:
        _write_json(output, current)

    return {
        "passed": not mismatches,
        "baseline": str(baseline),
        "output": str(output) if output is not None else "",
        "mismatches": mismatches,
        "current": current,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture/verify DB parity for backup restore drills."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    capture = sub.add_parser("capture", help="Capture a baseline snapshot")
    capture.add_argument(
        "--dsn",
        default=_runtime_env.get("FS_RESTORE_PARITY_DSN", ""),
        help="Postgres DSN override",
    )
    capture.add_argument("--label", default="baseline", help="Snapshot label")
    capture.add_argument("--output", required=True, help="Output JSON file")
    capture.set_defaults(func=_cmd_capture)

    verify = sub.add_parser("verify", help="Verify restored DB matches a baseline snapshot")
    verify.add_argument("--baseline", required=True, help="Baseline JSON file from capture step")
    verify.add_argument(
        "--dsn",
        default=_runtime_env.get("FS_RESTORE_PARITY_DSN", ""),
        help="Postgres DSN override",
    )
    verify.add_argument("--label", default="restored", help="Snapshot label")
    verify.add_argument(
        "--output", default="", help="Optional output JSON file for current snapshot"
    )
    verify.set_defaults(func=_cmd_verify)

    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
