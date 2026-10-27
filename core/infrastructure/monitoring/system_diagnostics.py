"""Read-only probes behind the system diagnostic report.

Every probe answers what this process can see from inside its own container:
the interpreter, the kernel, the CPU and memory the container is allowed, the
PostgreSQL server and the message broker. None of them writes anything, and
none returns a configuration value: the report assembles and redacts them.
"""

from __future__ import annotations

import os
import platform
import shutil
import ssl
import time
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any

# The libraries a bug report most often turns on; a version mismatch in one of
# them is cheaper to spot here than to reproduce.
_REPORTED_PACKAGES = (
    "fastapi",
    "uvicorn",
    "pydantic",
    "langgraph",
    "langchain-core",
    "psycopg",
    "psycopg2-binary",
    "pika",
    "httpx",
)
_DISK_PATHS = ("/", "/app", "/app/data", "/app/logs")


def _read_text(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _meminfo() -> dict[str, int]:
    values: dict[str, int] = {}
    for line in _read_text("/proc/meminfo").splitlines():
        name, _, rest = line.partition(":")
        parts = rest.split()
        if name in {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"} and parts:
            try:
                values[name] = int(parts[0]) * 1024
            except ValueError:
                continue
    return values


def _cgroup_limit(raw: str) -> int | None:
    if not raw or raw == "max":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _cgroup_cpu_limit() -> float | None:
    quota, _, period = _read_text("/sys/fs/cgroup/cpu.max").partition(" ")
    if not quota or quota == "max":
        return None
    try:
        return round(int(quota) / int(period or "100000"), 2)
    except (ValueError, ZeroDivisionError):
        return None


def _disk_usage() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for path in _DISK_PATHS:
        try:
            device = os.stat(path).st_dev
            usage = shutil.disk_usage(path)
        except OSError:
            continue
        if device in seen:
            continue
        seen.add(device)
        rows.append(
            {
                "path": path,
                "total_bytes": usage.total,
                "free_bytes": usage.free,
                "used_percent": round(usage.used * 100 / usage.total, 1) if usage.total else None,
            }
        )
    return rows


def _package_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for name in _REPORTED_PACKAGES:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            continue
    return versions


# Each platform marks its containers with variables of its own. Only the
# platform name and region are reported, never the project or service ids.
_HOSTING_MARKERS = (
    ("railway", ("RAILWAY_ENVIRONMENT_NAME", "RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID")),
    ("kubernetes", ("KUBERNETES_SERVICE_HOST",)),
    ("fly", ("FLY_APP_NAME",)),
    ("render", ("RENDER_SERVICE_ID",)),
    ("heroku", ("DYNO",)),
)
_HOSTING_REGION_KEYS = ("RAILWAY_REPLICA_REGION", "FLY_REGION")


def hosting_platform_facts(environ: Any = None) -> dict[str, Any]:
    """Name the platform this container runs on, read from its own markers."""
    values = os.environ if environ is None else environ
    platform_name = next(
        (name for name, keys in _HOSTING_MARKERS if any(values.get(key) for key in keys)),
        "docker" if Path("/.dockerenv").exists() else "unknown",
    )
    region = next((str(values[key]) for key in _HOSTING_REGION_KEYS if values.get(key)), "")
    return {"platform": platform_name, "region": region}


def runtime_process_facts() -> dict[str, Any]:
    """Describe the interpreter, kernel and resources this container is given."""
    memory = _meminfo()
    try:
        load_average = [round(value, 2) for value in os.getloadavg()]
    except OSError:
        load_average = []
    uptime = _read_text("/proc/uptime").split(" ", 1)[0]
    return {
        "architecture": platform.machine(),
        "os": platform.system(),
        "kernel": platform.release(),
        "platform": platform.platform(),
        "libc": "-".join(part for part in platform.libc_ver() if part),
        "in_container": Path("/.dockerenv").exists(),
        "hosting": hosting_platform_facts(),
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
        },
        "openssl": ssl.OPENSSL_VERSION,
        "cpu": {
            "logical_count": os.cpu_count(),
            "available_count": len(os.sched_getaffinity(0))
            if hasattr(os, "sched_getaffinity")
            else None,
            "container_limit": _cgroup_cpu_limit(),
            "load_average": load_average,
        },
        "memory": {
            "host_total_bytes": memory.get("MemTotal"),
            "host_available_bytes": memory.get("MemAvailable"),
            "swap_total_bytes": memory.get("SwapTotal"),
            "container_limit_bytes": _cgroup_limit(_read_text("/sys/fs/cgroup/memory.max")),
            "container_used_bytes": _cgroup_limit(_read_text("/sys/fs/cgroup/memory.current")),
        },
        "disk": _disk_usage(),
        "clock": {
            "utc_now": datetime.now(UTC).isoformat(),
            "timezone": time.tzname[0] if time.tzname else "",
        },
        "host_uptime_seconds": float(uptime) if uptime.replace(".", "", 1).isdigit() else None,
        "packages": _package_versions(),
    }


def _scalar(cur: Any, sql: str, params: tuple[Any, ...] = ()) -> Any:
    cur.execute(sql, params)
    row = cur.fetchone()
    if not row:
        return None
    if isinstance(row, dict):
        return next(iter(row.values()), None)
    return row[0]


def _rows(cur: Any, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    cur.execute(sql, params)
    return [tuple(row.values()) if isinstance(row, dict) else tuple(row) for row in cur.fetchall()]


def load_database_facts(conn: Any) -> dict[str, Any]:
    """Describe the PostgreSQL server, its extensions and the schema it carries."""
    from core.infrastructure.db.schema_state import fetch_schema_state

    with conn.cursor() as cur:
        server_now = _scalar(cur, "SELECT now()")
        facts: dict[str, Any] = {
            "server_version": _scalar(cur, "SHOW server_version"),
            "version": _scalar(cur, "SELECT version()"),
            "timezone": _scalar(cur, "SHOW TimeZone"),
            "max_connections": _scalar(cur, "SHOW max_connections"),
            "connections_in_use": _scalar(cur, "SELECT count(*) FROM pg_stat_activity"),
            "database_size_bytes": _scalar(cur, "SELECT pg_database_size(current_database())"),
            "extensions": {
                str(name): str(version)
                for name, version in _rows(
                    cur, "SELECT extname, extversion FROM pg_extension ORDER BY extname"
                )
            },
        }
        if isinstance(server_now, datetime):
            facts["clock_skew_seconds"] = round(
                (datetime.now(UTC) - server_now.astimezone(UTC)).total_seconds(), 3
            )
        if _scalar(cur, "SELECT to_regclass('public.platform_schema_migrations')"):
            migrations = _rows(
                cur,
                "SELECT migration_id, applied_at FROM public.platform_schema_migrations "
                "ORDER BY applied_at DESC, migration_id DESC LIMIT 20",
            )
            facts["applied_migrations"] = [
                {"migration_id": str(migration_id), "applied_at": applied_at}
                for migration_id, applied_at in migrations
            ]
    state = fetch_schema_state(conn) or {}
    facts["schema_state"] = {
        "app_version": state.get("app_version"),
        "schema_fingerprint": state.get("schema_fingerprint"),
        "applied_by": state.get("applied_by"),
    }
    return facts


def load_activity_facts(conn: Any) -> dict[str, Any]:
    """Count what the installation holds and what ran in the last 24 hours."""
    with conn.cursor() as cur:
        jobs_by_status = {
            str(status): int(count)
            for status, count in _rows(
                cur,
                "SELECT status, count(*) FROM jobs "
                "WHERE started_at > now() - interval '24 hours' GROUP BY status ORDER BY status",
            )
        }
        recent_failures = [
            {"job_id": str(job_id), "status": str(status), "finished_at": finished_at}
            for job_id, status, finished_at in _rows(
                cur,
                "SELECT id, status, finished_at FROM jobs "
                "WHERE status IN ('failed', 'stuck', 'orphaned') AND deleted_at IS NULL "
                "ORDER BY coalesce(finished_at, started_at) DESC LIMIT 20",
            )
        ]
        totals = {
            "accounts": _scalar(cur, "SELECT count(*) FROM public.accounts"),
            "users": _scalar(cur, "SELECT count(*) FROM public.users"),
            "projects": _scalar(cur, "SELECT count(*) FROM public.projects"),
            "workflows": _scalar(cur, "SELECT count(*) FROM public.registry_workflows"),
        }
    return {
        "totals": totals,
        "jobs_last_24h_by_status": jobs_by_status,
        "recent_failed_jobs": recent_failures,
    }


__all__ = [
    "hosting_platform_facts",
    "load_activity_facts",
    "load_database_facts",
    "runtime_process_facts",
]
