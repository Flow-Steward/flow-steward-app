#!/usr/bin/env python3
"""Print Postgres query-diagnostics settings, top statements, and seq-scan hotspots."""

from __future__ import annotations

import argparse
from collections.abc import Iterable
from typing import Any

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.db import psycopg_runtime as psycopg_runtime

SETTINGS = (
    "compute_query_id",
    "shared_preload_libraries",
    "session_preload_libraries",
    "log_min_duration_statement",
    "log_line_prefix",
    "log_lock_waits",
    "deadlock_timeout",
    "track_io_timing",
    "pg_stat_statements.track",
    "pg_stat_statements.max",
    "auto_explain.log_min_duration",
    "auto_explain.log_analyze",
    "auto_explain.log_buffers",
    "auto_explain.log_timing",
    "auto_explain.log_nested_statements",
    "auto_explain.sample_rate",
)

SORT_COLUMNS = {
    "mean": "mean_exec_time",
    "total": "total_exec_time",
    "max": "max_exec_time",
    "calls": "calls",
}


def _resolve_dsn(raw: str) -> str:
    explicit = str(raw or "").strip()
    if explicit:
        return explicit
    for key in ("FS_OBSERVABILITY_DSN", "FS_DB_DSN", "DATABASE_URL", "POSTGRES_DSN"):
        value = str(_runtime_env.get(key, "")).strip()
        if value:
            return value
    return ""


def _normalize_query(query: str, max_len: int) -> str:
    compact = " ".join(str(query or "").split())
    if len(compact) <= max_len:
        return compact
    return compact[: max(0, max_len - 3)] + "..."


def _print_section(title: str) -> None:
    print()
    print(title)
    print("=" * len(title))


def _print_table(headers: list[str], rows: Iterable[Iterable[object]]) -> None:
    rendered_rows = [[str(cell) for cell in row] for row in rows]
    widths = [len(header) for header in headers]
    for row in rendered_rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(cell))

    def _fmt(row: list[str]) -> str:
        return "  ".join(cell.ljust(widths[idx]) for idx, cell in enumerate(row))

    print(_fmt(headers))
    print(_fmt(["-" * width for width in widths]))
    for row in rendered_rows:
        print(_fmt(row))


def _fetch_settings(cur) -> list[list[str]]:
    cur.execute(
        """
        SELECT name, setting, COALESCE(unit, ''), source
        FROM pg_settings
        WHERE name = ANY(%s)
        ORDER BY name
        """,
        (list(SETTINGS),),
    )
    rows = []
    for name, setting, unit, source in cur.fetchall() or []:
        rendered_value = (
            f"{setting}{unit}" if unit and not str(setting).endswith(unit) else str(setting)
        )
        rows.append([str(name), rendered_value, str(source)])
    return rows


def _extension_installed(cur, extension_name: str) -> bool:
    cur.execute("SELECT 1 FROM pg_extension WHERE extname = %s", (extension_name,))
    return bool(cur.fetchone())


def _fetch_top_statements(cur, *, limit: int, sort_by: str, max_query_len: int) -> list[list[str]]:
    sort_column = SORT_COLUMNS[sort_by]
    query_sql = "\n".join(
        [
            "SELECT",
            "    calls,",
            "    ROUND(total_exec_time::numeric, 1) AS total_ms,",
            "    ROUND(mean_exec_time::numeric, 1) AS mean_ms,",
            "    ROUND(max_exec_time::numeric, 1) AS max_ms,",
            "    rows,",
            "    shared_blks_hit + shared_blks_read AS shared_blks,",
            "    temp_blks_written,",
            "    query",
            "FROM pg_stat_statements",
            "WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())",
            "  AND query NOT ILIKE '%%pg_stat_statements%%'",
            "ORDER BY ",
            sort_column,
            " DESC, calls DESC",
            "LIMIT %s",
        ]
    )
    cur.execute(
        query_sql,
        (max(1, int(limit)),),
    )
    rows = []
    for calls, total_ms, mean_ms, max_ms, result_rows, shared_blks, temp_blks_written, query in (
        cur.fetchall() or []
    ):
        rows.append(
            [
                str(calls),
                str(total_ms),
                str(mean_ms),
                str(max_ms),
                str(result_rows),
                str(shared_blks),
                str(temp_blks_written),
                _normalize_query(query, max_query_len),
            ]
        )
    return rows


def _fetch_seq_scan_hotspots(cur, *, limit: int, min_rows: int) -> list[list[str]]:
    cur.execute(
        """
        SELECT
            relname,
            COALESCE(n_live_tup::bigint, 0),
            pg_size_pretty(pg_total_relation_size(relid)),
            seq_scan,
            idx_scan,
            COALESCE(ROUND((100.0 * seq_scan) / NULLIF(seq_scan + idx_scan, 0), 1), 0.0),
            seq_tup_read
        FROM pg_stat_user_tables
        WHERE COALESCE(n_live_tup, 0) >= %s OR seq_scan > 0
        ORDER BY seq_tup_read DESC, seq_scan DESC, pg_total_relation_size(relid) DESC
        LIMIT %s
        """,
        (max(0, int(min_rows)), max(1, int(limit))),
    )
    rows = []
    for relname, n_live_tup, total_size, seq_scan, idx_scan, seq_pct, seq_tup_read in (
        cur.fetchall() or []
    ):
        rows.append(
            [
                str(relname),
                str(n_live_tup),
                str(total_size),
                str(seq_scan),
                str(idx_scan),
                f"{seq_pct}%",
                str(seq_tup_read),
            ]
        )
    return rows


def collect_query_diagnostics(
    *,
    dsn: str = "",
    limit: int = 15,
    sort_by: str = "mean",
    min_rows: int = 1000,
    max_query_len: int = 140,
    connect=psycopg_runtime.connect,
) -> dict[str, Any]:
    """Collect query-diagnostics rows without printing or parsing console output."""
    resolved_dsn = _resolve_dsn(dsn)
    if not resolved_dsn:
        raise RuntimeError("No Postgres DSN found for query diagnostics")
    if sort_by not in SORT_COLUMNS:
        raise ValueError(f"Unsupported sort key: {sort_by}")

    with connect(resolved_dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT current_database(), current_user")
        db_name, db_user = cur.fetchone()
        settings = _fetch_settings(cur)
        if not _extension_installed(cur, "pg_stat_statements"):
            return {
                "status": "missing_extension",
                "database": str(db_name),
                "user": str(db_user),
                "settings": settings,
                "top_statements": [],
                "seq_scan_hotspots": [],
            }
        return {
            "status": "completed",
            "database": str(db_name),
            "user": str(db_user),
            "settings": settings,
            "top_statements": _fetch_top_statements(
                cur,
                limit=limit,
                sort_by=sort_by,
                max_query_len=max(40, int(max_query_len)),
            ),
            "seq_scan_hotspots": _fetch_seq_scan_hotspots(
                cur,
                limit=limit,
                min_rows=max(0, int(min_rows)),
            ),
        }


def _print_diagnostics(result: dict[str, Any], *, sort_by: str) -> int:
    _print_section("Postgres Query Diagnostics")
    print(f"database={result['database']} user={result['user']}")

    _print_section("Effective Settings")
    _print_table(["name", "value", "source"], result.get("settings") or [])

    if result.get("status") == "missing_extension":
        print()
        print("pg_stat_statements is not installed in this database yet.")
        print(
            "Run: docker compose exec fs-app "
            "flow-steward database upgrade --maintenance-confirmed --confirm"
        )
        return 1

    _print_section(f"Top Statements ({sort_by})")
    top_rows = list(result.get("top_statements") or [])
    if top_rows:
        _print_table(
            [
                "calls",
                "total_ms",
                "mean_ms",
                "max_ms",
                "rows",
                "shared_blks",
                "temp_blks",
                "query",
            ],
            top_rows,
        )
    else:
        print(
            "No pg_stat_statements rows yet. Reproduce a slow page first, then rerun this script."
        )

    _print_section("Seq Scan Hotspots")
    _print_table(
        ["table", "est_rows", "size", "seq_scan", "idx_scan", "seq_pct", "seq_tup_read"],
        result.get("seq_scan_hotspots") or [],
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Print effective Postgres slow-query settings and top normalized statements."
    )
    parser.add_argument("--dsn", default=_runtime_env.get("FS_OBSERVABILITY_DSN", ""))
    parser.add_argument("--limit", type=int, default=15, help="Rows to show in each section.")
    parser.add_argument(
        "--sort-by",
        choices=tuple(SORT_COLUMNS.keys()),
        default="mean",
        help="Sort pg_stat_statements by mean/total/max/calls.",
    )
    parser.add_argument(
        "--min-rows",
        type=int,
        default=1000,
        help="Highlight seq-scan tables at or above this estimated row count.",
    )
    parser.add_argument(
        "--max-query-len",
        type=int,
        default=140,
        help="Truncate normalized SQL text to this many characters.",
    )
    parser.add_argument(
        "--reset-stats",
        action="store_true",
        help="Reset pg_stat_statements and exit. Use before reproducing a slow page.",
    )
    args = parser.parse_args()

    dsn = _resolve_dsn(args.dsn)
    if not dsn:
        print("No Postgres DSN found. Set FS_DB_DSN, FS_OBSERVABILITY_DSN, or pass --dsn.")
        return 2

    with psycopg_runtime.connect(dsn) as conn, conn.cursor() as cur:
        if args.reset_stats:
            if not _extension_installed(cur, "pg_stat_statements"):
                print("pg_stat_statements is not installed in this database yet.")
                print(
                    "Run: docker compose exec fs-app "
                    "flow-steward database upgrade --maintenance-confirmed --confirm"
                )
                return 1
            cur.execute("SELECT pg_stat_statements_reset()")
            conn.commit()
            print("pg_stat_statements has been reset.")
            print("Reproduce the slow page, then rerun this script without --reset-stats.")
            return 0

    result = collect_query_diagnostics(
        dsn=dsn,
        limit=args.limit,
        sort_by=args.sort_by,
        min_rows=args.min_rows,
        max_query_len=args.max_query_len,
    )
    exit_code = _print_diagnostics(result, sort_by=args.sort_by)

    print()
    print(
        "Tip: live-tail slow queries with: docker compose logs -f pg-fs | rg 'duration:|Query Text:|plan:'"
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
