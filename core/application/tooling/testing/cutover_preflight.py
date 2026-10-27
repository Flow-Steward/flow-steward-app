#!/usr/bin/env python3
"""Phase 5 hard-cutover preflight checks.

Usage:
  docker compose exec -T fs-cli flow-steward testing cutover-preflight --project-id <project-uuid>
"""

from __future__ import annotations

import argparse
import sys

import requests
import urllib3

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.db import psycopg_runtime as psycopg_runtime

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _print_check(ok: bool, title: str, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    suffix = f" :: {detail}" if detail else ""
    print(f"[{status}] {title}{suffix}", flush=True)


def _resolve_dsn() -> str:
    candidates = (
        _runtime_env.get("FS_OBSERVABILITY_DSN", ""),
        _runtime_env.get("FS_DB_DSN", ""),
        _runtime_env.get("DATABASE_URL", ""),
    )
    for value in candidates:
        value = str(value or "").strip()
        if value:
            return value
    return ""


def check_health(base_url: str, verify_ssl: bool) -> tuple[bool, str]:
    try:
        response = requests.get(f"{base_url.rstrip('/')}/health", verify=verify_ssl, timeout=10)
        if response.status_code != 200:
            return False, f"HTTP {response.status_code}"
        return True, response.text.strip()[:200]
    except Exception as exc:
        return False, str(exc)


def check_message_table(database_dsn: str = "") -> tuple[bool, str]:
    dsn = str(database_dsn or "").strip() or _resolve_dsn()
    if not dsn:
        return False, "No observability DSN configured"
    try:
        with psycopg_runtime.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute("select to_regclass('public.platform_agent_messages')")
            row = cur.fetchone()
            table_name = row[0] if row else None
            if not table_name:
                return False, "platform_agent_messages table is missing (run migration 017)"
            return True, str(table_name)
    except Exception as exc:
        return False, str(exc)


def check_graph_compiles(database_dsn: str = "") -> tuple[bool, str]:
    dsn = str(database_dsn or "").strip() or _resolve_dsn()
    if not dsn:
        return False, "No database DSN configured. Set FS_DB_DSN or DATABASE_URL."
    try:
        from core.application.orchestration.graph_runtime import build_registry_graph
        from core.application.registry.registry_manager import RegistryManager

        registry = RegistryManager(dsn, sync_builtins=False)
        # Compile-only: this check never runs the graph.
        _ = build_registry_graph(checkpointer=False, registry=registry, refresh_registry=False)
        return True, "Registry graph compiles"
    except Exception as exc:
        return False, str(exc)


def check_queue_depth(project_id: str, rabbitmq_url: str = "") -> tuple[bool, str]:
    try:
        from core.infrastructure.runtime.worker.queue_broker import get_queue_depth

        depth = get_queue_depth(project_id, amqp_url=str(rabbitmq_url or "").strip() or None)
        if depth < 0:
            return False, "RabbitMQ queue depth unavailable"
        return True, f"queue depth={depth}"
    except Exception as exc:
        return False, str(exc)


def run_preflight(
    *,
    project_id: str,
    base_url: str,
    verify_ssl: bool,
    database_dsn: str = "",
    rabbitmq_url: str = "",
) -> int:
    """Run cutover preflight checks from typed operation inputs."""
    resolved_database_dsn = str(database_dsn or "").strip() or _resolve_dsn()
    resolved_rabbitmq_url = (
        str(rabbitmq_url or "").strip() or str(_runtime_env.get("RABBITMQ_URL") or "").strip()
    )
    missing_configuration: list[str] = []
    if not resolved_database_dsn:
        missing_configuration.append("FS_OBSERVABILITY_DSN/FS_DB_DSN")
    if not resolved_rabbitmq_url:
        missing_configuration.append("RABBITMQ_URL")
    if missing_configuration:
        for name in missing_configuration:
            _print_check(False, "Required configuration", f"{name} is not configured")
        print("\nPreflight BLOCKED (required configuration is missing).", flush=True)
        return 2

    checks: list[tuple[str, tuple[bool, str]]] = [
        ("API health", check_health(base_url, verify_ssl)),
        ("Agent message table", check_message_table(resolved_database_dsn)),
        ("Graph compile", check_graph_compiles(resolved_database_dsn)),
        ("Queue depth", check_queue_depth(project_id, resolved_rabbitmq_url)),
    ]

    failed = 0
    for title, (ok, detail) in checks:
        _print_check(ok, title, detail)
        if not ok:
            failed += 1

    if failed:
        print(f"\nPreflight FAILED ({failed} check(s) failed).", flush=True)
        return 1

    print("\nPreflight PASSED.", flush=True)
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Phase 5 hard-cutover preflight checks")
    parser.add_argument(
        "--project-id", required=True, help="Project UUID to verify queue depth for"
    )
    parser.add_argument(
        "--base-url", default=_runtime_env.get("APP_BASE_URL", "https://localhost:8099")
    )
    parser.add_argument(
        "--verify-ssl",
        action="store_true",
        help="Verify TLS certificate for health checks (default: disabled for self-signed local certs)",
    )
    args = parser.parse_args(argv)
    return run_preflight(
        project_id=str(args.project_id),
        base_url=str(args.base_url),
        verify_ssl=bool(args.verify_ssl),
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
