#!/usr/bin/env python3
"""Runtime deployment probes for deployment doctor."""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from core.application.tooling.ops.deployment_doctor_common import (
    API_READINESS_ENDPOINT_IS_REACHABLE_VALUE,
    DEFAULT_COMPOSE_PROBE_SERVICE,
    LIVE_PROBES_RUN_FROM_EXPECTED_BOUNDARY_VALUE,
    MCP_ENDPOINT_IS_REACHABLE_WITH_EXPECTED_AUTH_VALUE,
    POSTGRES_IS_REACHABLE_AND_SCHEMA_IS_INSTALLED_VALUE,
    QUEUE_AND_WORKER_HEALTH_ARE_VISIBLE_VALUE,
    RABBITMQ_SOCKET_IS_REACHABLE_VALUE,
    REQUIRED_COMPOSE_SERVICES_ARE_RUNNING_MESSAGE,
    REQUIRED_DB_TABLES,
    RUNTIME_COMPOSE_SERVICES,
    CheckResult,
    DoctorOptions,
    _compose_service_name,
    _docker_compose_command,
    _http_json,
    _join_url,
    _parse_compose_ps_json,
    _parse_json,
    _resolve_bearer_token,
    _resolve_container_base_url,
    _runtime_check_detail,
    _truncate_detail,
)
from core.infrastructure.config.rabbitmq_transport import build_rabbitmq_parameters
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.runtime_containers import operator_container


def _operator_container(service: str, deployment_profile: str) -> str:
    """AC17-R106: doctor advice is read when an install is already broken."""
    return operator_container(service, deployment_profile=deployment_profile)


def _profile_of(env: Mapping[str, str]) -> str:
    return str(env.get("FS_DEPLOYMENT_PROFILE") or "scalable").strip().lower()


def _run_compose_live_probe(
    *,
    options: DoctorOptions,
    repo_root: Path,
    env_path: Path,
    env: dict[str, str],
) -> list[CheckResult]:
    if shutil.which("docker") is None:
        return [
            CheckResult(
                id="live_probe_boundary",
                category="runtime",
                title=LIVE_PROBES_RUN_FROM_EXPECTED_BOUNDARY_VALUE,
                status="fail",
                detail="docker command not found; cannot execute probes inside Compose.",
                remediation=(
                    "Run deployment doctor on the Docker host or execute --live-only "
                    f"inside {_operator_container('fs-app', _profile_of(env))}."
                ),
            )
        ]
    container_base_url = _resolve_container_base_url(env, options.container_base_url)
    bearer_token = _resolve_bearer_token(env, options.bearer_token)
    command = _docker_compose_command(
        options,
        repo_root,
        env_path,
        ["exec", "-T", "-e", "FS_DEPLOYMENT_DOCTOR_IN_CONTAINER=1"],
    )
    if bearer_token:
        command.extend(["-e", "MCP_BEARER_TOKEN"])
    command.extend(
        [
            str(options.compose_service or DEFAULT_COMPOSE_PROBE_SERVICE),
            "python",
            "-m",
            # Invoke the doctor module directly. The packaged CLI exposes no
            # "tooling" command, so shelling in through it made every compose
            # probe die on a Click usage error and report the boundary check as
            # failed — the mode could never actually probe anything.
            "core.entrypoints.cli.ops.deployment_doctor",
            "--live-only",
            "--probe-mode",
            "host",
            "--stack-environment",
            str(options.stack_environment or "production"),
            "--base-url",
            container_base_url,
            "--json",
            "--timeout-seconds",
            str(max(1.0, float(options.timeout_seconds or 0))),
        ]
    )
    if options.mcp_url:
        command.extend(["--mcp-url", str(options.mcp_url)])
    if options.skip_provider_cli:
        command.append("--skip-provider-cli")
    if options.allow_lifecycle_mutation:
        command.append("--allow-lifecycle-mutation")
    if options.allow_release_transition:
        command.append("--allow-release-transition")
    process_env = dict(_runtime_env)
    if bearer_token:
        process_env["MCP_BEARER_TOKEN"] = bearer_token
    completed = subprocess.run(
        command,
        cwd=str(repo_root),
        check=False,
        capture_output=True,
        env=process_env,
        text=True,
        timeout=max(15, int(float(options.timeout_seconds or 5.0) * 8)),
    )
    if completed.returncode not in {0, 1}:
        return [
            CheckResult(
                id="live_probe_boundary",
                category="runtime",
                title=LIVE_PROBES_RUN_FROM_EXPECTED_BOUNDARY_VALUE,
                status="fail",
                detail=_truncate_detail((completed.stderr or completed.stdout).strip()),
                remediation=(
                    f"Start {_operator_container('fs-app', _profile_of(env))} and ensure "
                    "docker compose exec can run the doctor inside the service."
                ),
            )
        ]
    payload = _parse_json(completed.stdout)
    if not isinstance(payload, dict):
        return [
            CheckResult(
                id="live_probe_boundary",
                category="runtime",
                title=LIVE_PROBES_RUN_FROM_EXPECTED_BOUNDARY_VALUE,
                status="fail",
                detail=_truncate_detail((completed.stderr or completed.stdout).strip()),
                remediation="Ensure container-side deployment doctor emits JSON.",
            )
        ]
    checks = [
        CheckResult(
            id="live_probe_boundary",
            category="runtime",
            title=LIVE_PROBES_RUN_FROM_EXPECTED_BOUNDARY_VALUE,
            status="pass",
            detail=f"docker compose exec {options.compose_service or DEFAULT_COMPOSE_PROBE_SERVICE}",
        )
    ]
    for row in payload.get("checks") or []:
        if isinstance(row, dict):
            if str(row.get("id") or "") == "live_probe_boundary":
                continue
            checks.append(
                CheckResult(
                    id=str(row.get("id") or ""),
                    category=str(row.get("category") or "runtime"),
                    title=str(row.get("title") or row.get("id") or "Container live probe"),
                    status=str(row.get("status") or "fail"),
                    detail=str(row.get("detail") or ""),
                    remediation=str(row.get("remediation") or ""),
                )
            )
    return checks


def _expected_lifecycle_mutation_only(payload: Any) -> bool:
    checks_raw = payload.get("checks") if isinstance(payload, dict) else None
    if not isinstance(checks_raw, dict) or not checks_raw:
        return False
    lifecycle = checks_raw.get("lifecycle_recovery")
    if (
        not isinstance(lifecycle, dict)
        or lifecycle.get("ok") is not False
        or lifecycle.get("state") != "mutation_in_progress"
    ):
        return False
    return all(
        isinstance(check, dict) and (check.get("ok") is True or check_id == "lifecycle_recovery")
        for check_id, check in checks_raw.items()
    )


_STALE_WORKFLOW_KNOWLEDGE_ERROR = (
    "Workflow knowledge index is stale for the requested account/project scope."
)


def _expected_release_transition_only(payload: Any) -> bool:
    checks_raw = payload.get("checks") if isinstance(payload, dict) else None
    if not isinstance(checks_raw, dict) or not checks_raw:
        return False
    lifecycle = checks_raw.get("lifecycle_recovery")
    if (
        not isinstance(lifecycle, dict)
        or lifecycle.get("ok") is not False
        or lifecycle.get("state") != "mutation_in_progress"
    ):
        return False
    memory = checks_raw.get("memory_backends")
    if not isinstance(memory, dict) or bool(memory.get("ok")):
        return False
    workflow = memory.get("workflow_knowledge")
    scope = workflow.get("scope") if isinstance(workflow, dict) else None
    graph = workflow.get("graph") if isinstance(workflow, dict) else None
    vector = workflow.get("vector") if isinstance(workflow, dict) else None
    exact_memory_transition = bool(
        memory.get("error") == _STALE_WORKFLOW_KNOWLEDGE_ERROR
        and isinstance(workflow, dict)
        and workflow.get("error") == _STALE_WORKFLOW_KNOWLEDGE_ERROR
        and isinstance(scope, dict)
        and isinstance(graph, dict)
        and graph.get("available") is True
        and isinstance(vector, dict)
        and vector.get("available") is True
        and scope.get("freshness") == "stale"
        and scope.get("empty") is False
        and scope.get("digest_fresh") is False
        and isinstance(scope.get("catalog_digest"), str)
        and bool(scope.get("catalog_digest"))
        and isinstance(scope.get("stored_digest"), str)
        and bool(scope.get("stored_digest"))
        and scope.get("catalog_digest") != scope.get("stored_digest")
    )
    if not exact_memory_transition:
        return False
    sync = checks_raw.get("workflow_knowledge_sync")
    if (
        not isinstance(sync, dict)
        or sync.get("ok") is not False
        or sync.get("last_failure_reason") != ""
        or sync.get("consecutive_failures") != 0
        or not isinstance(sync.get("detail"), str)
        or not sync["detail"].startswith("workflow knowledge sync is stale by ")
    ):
        return False
    for check_id, check in checks_raw.items():
        if not isinstance(check, dict):
            return False
        if check.get("ok") is True or check_id == "memory_backends":
            continue
        if check_id == "lifecycle_recovery" and check.get("state") == "mutation_in_progress":
            continue
        if check_id == "workflow_knowledge_sync":
            continue
        return False
    return True


def _check_api_readiness(
    base_url: str,
    *,
    deployment_profile: str,
    timeout: float,
    allow_lifecycle_mutation: bool = False,
    allow_release_transition: bool = False,
) -> CheckResult:
    if not base_url:
        return CheckResult(
            id="api_ready",
            category="runtime",
            title=API_READINESS_ENDPOINT_IS_REACHABLE_VALUE,
            status="fail",
            detail="No app base URL resolved.",
            remediation="Pass --base-url or set FS_PUBLIC_BASE_URL/APP_PORT.",
        )
    status, payload, detail = _http_json(_join_url(base_url, "/health/ready"), timeout=timeout)
    ready = status == 200 and isinstance(payload, dict) and bool(payload.get("ready"))
    if allow_lifecycle_mutation and _expected_lifecycle_mutation_only(payload):
        ready = True
    if allow_release_transition and _expected_release_transition_only(payload):
        ready = True
    if ready:
        return CheckResult(
            id="api_ready",
            category="runtime",
            title=API_READINESS_ENDPOINT_IS_REACHABLE_VALUE,
            status="pass",
            detail=_runtime_check_detail(payload),
        )
    checks_raw = payload.get("checks") if isinstance(payload, dict) else None
    checks = checks_raw if isinstance(checks_raw, dict) else {}
    for check_id, raw_check in checks.items():
        check = raw_check if isinstance(raw_check, dict) else {}
        if bool(check.get("ok")):
            continue
        return CheckResult(
            id=str(check.get("id") or check_id),
            category=str(check.get("category") or "runtime"),
            title=str(check.get("title") or check_id),
            status="fail",
            detail=_truncate_detail(
                str(
                    check.get("detail")
                    or check.get("error")
                    or check.get("state_message")
                    or detail
                    or f"HTTP {status}"
                )
            ),
            remediation=str(check.get("remediation") or "").strip()
            or "Resolve this readiness check, then rerun flow-steward doctor.",
        )
    return CheckResult(
        id="api_ready",
        category="runtime",
        title=API_READINESS_ENDPOINT_IS_REACHABLE_VALUE,
        status="fail",
        detail=detail or _runtime_check_detail(payload) or f"HTTP {status}",
        remediation=(
            f"Start {_operator_container('fs-app', deployment_profile)} and fix failing "
            "/health/ready checks."
        ),
    )


def _check_queue_worker_health(
    base_url: str,
    *,
    timeout: float,
    allow_lifecycle_mutation: bool = False,
    allow_release_transition: bool = False,
) -> CheckResult:
    if not base_url:
        return CheckResult(
            id="queue_worker_health",
            category="runtime",
            title=QUEUE_AND_WORKER_HEALTH_ARE_VISIBLE_VALUE,
            status="fail",
            detail="No app base URL resolved.",
            remediation="Pass --base-url or set FS_PUBLIC_BASE_URL/APP_PORT.",
        )
    status, payload, detail = _http_json(_join_url(base_url, "/health/ready"), timeout=timeout)
    ready = status == 200 and isinstance(payload, dict) and bool(payload.get("ready"))
    if allow_lifecycle_mutation and _expected_lifecycle_mutation_only(payload):
        ready = True
    if allow_release_transition and _expected_release_transition_only(payload):
        ready = True
    if ready:
        checks_raw = payload.get("checks")
        checks: dict[Any, Any] = checks_raw if isinstance(checks_raw, dict) else {}
        queue_checks = (
            "worker_manager",
            "queue_lag",
            "queue_delivery",
            "lease_health",
            "stuck_jobs",
        )
        failed = [
            name
            for name in queue_checks
            if name not in checks or not bool((checks.get(name) or {}).get("ok"))
        ]
        if not failed:
            return CheckResult(
                id="queue_worker_health",
                category="runtime",
                title=QUEUE_AND_WORKER_HEALTH_ARE_VISIBLE_VALUE,
                status="pass",
                detail=f"{len(queue_checks)} queue and worker readiness checks passed",
            )
        detail = "failed or missing checks: " + ", ".join(failed)
    return CheckResult(
        id="queue_worker_health",
        category="runtime",
        title=QUEUE_AND_WORKER_HEALTH_ARE_VISIBLE_VALUE,
        status="fail",
        detail=detail or _runtime_check_detail(payload) or f"HTTP {status}",
        remediation="Fix RabbitMQ, worker manager, queue lag, lease, or stuck-job readiness checks.",
    )


def _check_db_connectivity(env: dict[str, str], *, timeout: float) -> CheckResult:
    dsn = str(env.get("FS_DB_DSN") or "").strip()
    if not dsn:
        return CheckResult(
            id="db_connectivity",
            category="runtime",
            title=POSTGRES_IS_REACHABLE_AND_SCHEMA_IS_INSTALLED_VALUE,
            status="fail",
            detail="FS_DB_DSN is empty.",
            remediation="Set FS_DB_DSN to the running production database.",
        )
    try:
        from core.infrastructure.db import psycopg_runtime as psycopg_runtime
    except Exception as exc:
        return CheckResult(
            id="db_connectivity",
            category="runtime",
            title=POSTGRES_IS_REACHABLE_AND_SCHEMA_IS_INSTALLED_VALUE,
            status="fail",
            detail=f"psycopg runtime unavailable: {type(exc).__name__}",
            remediation="Install runtime database dependencies before running deployment doctor.",
        )
    try:
        with (
            psycopg_runtime.connect(dsn, connect_timeout=max(1, int(timeout))) as conn,
            conn.cursor() as cur,
        ):
            cur.execute("SELECT 1")
            cur.fetchone()
            cur.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name IN ('projects', 'jobs', 'platform_agent_messages', 'project_execution_slots')
                """
            )
            found = {str(row[0]) for row in cur.fetchall()}
    except Exception as exc:
        return CheckResult(
            id="db_connectivity",
            category="runtime",
            title=POSTGRES_IS_REACHABLE_AND_SCHEMA_IS_INSTALLED_VALUE,
            status="fail",
            detail=f"{type(exc).__name__}: {_truncate_detail(str(exc))}",
            remediation="Verify database host, credentials, network, and schema bootstrap.",
        )
    missing = [table for table in REQUIRED_DB_TABLES if table not in found]
    if missing:
        return CheckResult(
            id="db_connectivity",
            category="runtime",
            title=POSTGRES_IS_REACHABLE_AND_SCHEMA_IS_INSTALLED_VALUE,
            status="fail",
            detail="missing tables: " + ", ".join(missing),
            remediation="Run canonical schema initialization before release.",
        )
    return CheckResult(
        id="db_connectivity",
        category="runtime",
        title=POSTGRES_IS_REACHABLE_AND_SCHEMA_IS_INSTALLED_VALUE,
        status="pass",
        detail=f"{len(REQUIRED_DB_TABLES)} required tables found.",
    )


def _check_queue_connectivity(env: dict[str, str], *, timeout: float) -> CheckResult:
    raw = str(env.get("RABBITMQ_URL") or "").strip()
    try:
        import pika

        parameters = build_rabbitmq_parameters(raw, parameter_factory=pika.URLParameters, env=env)
    except (ValueError, ImportError):
        return CheckResult(
            id="rabbitmq_socket",
            category="runtime",
            title=RABBITMQ_SOCKET_IS_REACHABLE_VALUE,
            status="fail",
            detail="RabbitMQ transport configuration is invalid or unavailable.",
            remediation="Use verified amqps or the matching deployment-owned local transport mode.",
        )
    host, port = parameters.host, parameters.port
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.getpeername()
    except Exception as exc:
        return CheckResult(
            id="rabbitmq_socket",
            category="runtime",
            title=RABBITMQ_SOCKET_IS_REACHABLE_VALUE,
            status="fail",
            detail=f"{host}:{port} {type(exc).__name__}: {_truncate_detail(str(exc))}",
            remediation="Start rabbitmq-fs and verify AMQP networking from this host/container.",
        )
    return CheckResult(
        id="rabbitmq_socket",
        category="runtime",
        title=RABBITMQ_SOCKET_IS_REACHABLE_VALUE,
        status="pass",
        detail=f"{host}:{port}",
    )


def _check_mcp_auth_readiness(
    mcp_url: str,
    *,
    deployment_profile: str,
    bearer_token: str,
    auth_disabled: bool,
    stack_environment: str,
    timeout: float,
) -> CheckResult:
    if not mcp_url:
        return CheckResult(
            id="mcp_auth",
            category="runtime",
            title=MCP_ENDPOINT_IS_REACHABLE_WITH_EXPECTED_AUTH_VALUE,
            status="fail",
            detail="MCP_URL is empty.",
            remediation="Set MCP_URL or pass --mcp-url.",
        )
    if auth_disabled and stack_environment != "local_development":
        return CheckResult(
            id="mcp_auth",
            category="runtime",
            title=MCP_ENDPOINT_IS_REACHABLE_WITH_EXPECTED_AUTH_VALUE,
            status="fail",
            detail="MCP authentication is disabled in production.",
            remediation="Set FS_MCP_AUTH_DISABLED=0 and provide a PAT or machine token.",
        )
    if not auth_disabled and not bearer_token:
        return CheckResult(
            id="mcp_auth",
            category="runtime",
            title=MCP_ENDPOINT_IS_REACHABLE_WITH_EXPECTED_AUTH_VALUE,
            status="fail",
            detail="MCP auth is enabled but no bearer token was provided.",
            remediation=(
                "Set FS_WORKER_MCP_BEARER_TOKEN in the deployment env file "
                "(or MCP_BEARER_TOKEN/FS_SESSION_TOKEN), or pass --bearer-token."
            ),
        )
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "deployment-doctor", "version": "0.1"},
        },
    }
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    status, body, detail = _http_json(
        mcp_url,
        timeout=timeout,
        headers=headers,
        body=json.dumps(payload).encode("utf-8"),
    )
    if status in {200, 202} and isinstance(body, dict):
        return CheckResult(
            id="mcp_auth",
            category="runtime",
            title=MCP_ENDPOINT_IS_REACHABLE_WITH_EXPECTED_AUTH_VALUE,
            status="pass",
            detail=f"HTTP {status}",
        )
    if status in {401, 403}:
        remediation = "Refresh the PAT/machine token and pass it as MCP_BEARER_TOKEN."
    else:
        remediation = (
            f"Start {_operator_container('fs-mcp', deployment_profile)} and verify "
            "MCP_URL, auth settings, and network reachability."
        )
    return CheckResult(
        id="mcp_auth",
        category="runtime",
        title=MCP_ENDPOINT_IS_REACHABLE_WITH_EXPECTED_AUTH_VALUE,
        status="fail",
        detail=detail or f"HTTP {status}",
        remediation=remediation,
    )


def _check_compose_runtime_services(
    options: DoctorOptions, repo_root: Path, env_path: Path
) -> CheckResult:
    if shutil.which("docker") is None:
        return CheckResult(
            id="compose_runtime",
            category="runtime",
            title=REQUIRED_COMPOSE_SERVICES_ARE_RUNNING_MESSAGE,
            status="warn",
            detail="docker command not found; skipped runtime service-state check.",
            remediation="Run on the deployment host or verify `docker compose ps` manually.",
        )
    completed = subprocess.run(
        _docker_compose_command(options, repo_root, env_path, ["ps", "--format", "json"]),
        cwd=str(repo_root),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        return CheckResult(
            id="compose_runtime",
            category="runtime",
            title=REQUIRED_COMPOSE_SERVICES_ARE_RUNNING_MESSAGE,
            status="fail",
            detail=_truncate_detail((completed.stderr or completed.stdout).strip()),
            remediation="Start the production Compose stack and rerun deployment doctor.",
        )
    rows = _parse_compose_ps_json(completed.stdout)
    if not rows:
        return CheckResult(
            id="compose_runtime",
            category="runtime",
            title=REQUIRED_COMPOSE_SERVICES_ARE_RUNNING_MESSAGE,
            status="fail",
            detail="No services returned by docker compose ps.",
            remediation="Start the production Compose stack and rerun deployment doctor.",
        )
    states = {
        _compose_service_name(row): str(row.get("State") or row.get("state") or "").lower()
        for row in rows
    }
    runtime_services = options.runtime_compose_services or RUNTIME_COMPOSE_SERVICES
    missing = [service for service in runtime_services if service not in states]
    not_running = [
        f"{service}={states.get(service) or 'missing'}"
        for service in runtime_services
        if service in states and states[service] not in {"running", "healthy"}
    ]
    if missing or not_running:
        detail_parts = []
        if missing:
            detail_parts.append("missing: " + ", ".join(missing))
        if not_running:
            detail_parts.append("not running: " + ", ".join(not_running))
        return CheckResult(
            id="compose_runtime",
            category="runtime",
            title=REQUIRED_COMPOSE_SERVICES_ARE_RUNNING_MESSAGE,
            status="fail",
            detail="; ".join(detail_parts),
            remediation="Start or repair the missing/stopped selected services.",
        )
    return CheckResult(
        id="compose_runtime",
        category="runtime",
        title=REQUIRED_COMPOSE_SERVICES_ARE_RUNNING_MESSAGE,
        status="pass",
        detail=", ".join(runtime_services),
    )
