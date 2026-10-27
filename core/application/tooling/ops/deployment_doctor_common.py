#!/usr/bin/env python3
"""Shared deployment doctor contracts and low-level utilities."""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

API_READINESS_ENDPOINT_IS_REACHABLE_VALUE = "API readiness endpoint is reachable"
DOCKER_COMPOSE_CONFIG_RENDERS_VALUE = "Docker Compose config renders"
LIVE_PROBES_RUN_FROM_EXPECTED_BOUNDARY_VALUE = "Live probes run from expected boundary"
MCP_ENDPOINT_IS_REACHABLE_WITH_EXPECTED_AUTH_VALUE = "MCP endpoint is reachable with expected auth"
NO_TRACKED_ENTERPRISE_PACKAGE_PATHS_VALUE = "No tracked enterprise package paths"
POSTGRES_IS_REACHABLE_AND_SCHEMA_IS_INSTALLED_VALUE = (
    "Postgres is reachable and schema is installed"
)
QUEUE_AND_WORKER_HEALTH_ARE_VISIBLE_VALUE = "Queue and worker health are visible"
RABBITMQ_SOCKET_IS_REACHABLE_VALUE = "RabbitMQ socket is reachable"
REQUIRED_COMPOSE_SERVICES_ARE_RUNNING_MESSAGE = "Required Compose services are running"
RUNTIME_IMAGES_ARE_EXPLICITLY_PINNED_VALUE = "Runtime images are explicitly pinned"


REQUIRED_REPO_FILES = (
    "infra/docker/docker-compose.prod.yaml",
    "infra/docker/Dockerfile",
    "infra/mcp/Dockerfile",
    ".env.example.prod",
    "db/seeds/registry_seed.json",
    "core/entrypoints/cli/scripts/lint.sh",
    "core/entrypoints/cli/scripts/check_no_enterprise_leak.sh",
)
REQUIRED_REPO_GLOBS = ("db/schema/*.sql",)

REQUIRED_COMPOSE_SERVICES = (
    "traefik",
    "rabbitmq-fs",
    "pg-fs",
    "fs-mcp",
    "fs-app",
    "fs-worker",
    "control-plane",
)

INTERNAL_ONLY_SERVICES = ("pg-fs", "rabbitmq-fs", "fs-mcp")

PLACEHOLDER_MARKERS = ("CHANGE_ME", "local-dev-secret", "flow-steward.example.com")
REQUIRED_DB_TABLES = ("projects", "jobs", "platform_agent_messages", "project_execution_slots")
RUNTIME_COMPOSE_SERVICES = ("fs-app", "fs-worker", "fs-mcp", "pg-fs", "rabbitmq-fs")
DEFAULT_COMPOSE_PROBE_SERVICE = "fs-app"
_MAX_HTTP_JSON_BYTES = 1024 * 1024


@dataclass(frozen=True)
class CheckResult:
    id: str
    category: str
    title: str
    status: str
    detail: str = ""
    remediation: str = ""

    def dump(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "remediation": self.remediation,
        }


@dataclass(frozen=True)
class DoctorOptions:
    repo_root: Path
    env_file: Path
    compose_project: str = ""
    strict: bool = False
    run_compose_config: bool = False
    skip_git: bool = False
    static_only: bool = False
    base_url: str = ""
    mcp_url: str = ""
    bearer_token: str = ""
    timeout_seconds: float = 5.0
    skip_provider_cli: bool = False
    live_only: bool = False
    probe_mode: str = "compose"
    compose_service: str = DEFAULT_COMPOSE_PROBE_SERVICE
    container_base_url: str = ""
    compose_files: tuple[Path, ...] = ()
    compose_profiles: tuple[str, ...] = ()
    required_compose_services: tuple[str, ...] = ()
    runtime_compose_services: tuple[str, ...] = ()
    stack_environment: str = "production"
    allow_initial_auth_bootstrap: bool = False
    allow_lifecycle_mutation: bool = False
    allow_release_transition: bool = False


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if not key:
            continue
        values[key] = _strip_quotes(value.strip())
    return values


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _normalize_stack_environment(value: str) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    if normalized == "local_development":
        return "local_development"
    return "production"


def _resolve_path(path: Path, repo_root: Path) -> Path:
    return path if path.is_absolute() else repo_root / path


def _resolve_compose_files(options: DoctorOptions, repo_root: Path) -> tuple[Path, ...]:
    files = options.compose_files or (Path("infra/docker/docker-compose.prod.yaml"),)
    return tuple(_resolve_path(path, repo_root) for path in files)


def _docker_compose_command(
    options: DoctorOptions,
    repo_root: Path,
    env_path: Path,
    args: Sequence[str],
) -> list[str]:
    command = [
        "docker",
        "compose",
        "-p",
        str(options.compose_project or "").strip()
        or _runtime_env.get("FS_COMPOSE_PROJECT_NAME", "flow-steward"),
    ]
    for compose_file in _resolve_compose_files(options, repo_root):
        command.extend(["-f", str(compose_file)])
    command.extend(["--env-file", str(env_path)])
    for profile in options.compose_profiles:
        command.extend(["--profile", profile])
    command.extend(args)
    return command


def _is_real_secret_value(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    return not any(marker in text for marker in PLACEHOLDER_MARKERS)


def _compose_service_section(compose_text: str, service: str) -> str:
    lines = compose_text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line == f"  {service}:":
            start = index
            break
    if start is None:
        return ""
    collected = []
    for line in lines[start + 1 :]:
        if line.startswith("  ") and not line.startswith("    ") and line.rstrip().endswith(":"):
            break
        collected.append(line)
    return "\n".join(collected)


def _ports_are_localhost_only(section: str) -> bool:
    in_ports = False
    port_lines: list[str] = []
    for raw_line in section.splitlines():
        stripped = raw_line.strip()
        if stripped == "ports:":
            in_ports = True
            continue
        if in_ports and stripped and not raw_line.startswith("      "):
            break
        if in_ports and stripped.startswith("-"):
            port_lines.append(stripped)
    return bool(port_lines) and all(
        "127.0.0.1:" in line or "localhost:" in line for line in port_lines
    )


def _section_declares_env_value(section: str, key: str, expected: str) -> bool:
    for line in section.splitlines():
        stripped = line.strip().strip("'\"")
        if stripped.startswith("- "):
            stripped = stripped[2:].strip().strip("'\"")
        if stripped.startswith(f"{key}:"):
            return stripped.split(":", 1)[1].strip().strip("'\"") == expected
        if stripped.startswith(f"{key}="):
            return stripped.split("=", 1)[1].strip().strip("'\"") == expected
    return False


def _summary(checks: Iterable[CheckResult]) -> dict[str, int]:
    summary = {"pass": 0, "warn": 0, "fail": 0}
    for check in checks:
        if check.status in summary:
            summary[check.status] += 1
    return summary


def _resolve_base_url(env: dict[str, str], explicit: str) -> str:
    value = (
        str(explicit or "").strip() or str(env.get("FS_DEPLOYMENT_DOCTOR_BASE_URL") or "").strip()
    )
    if not value and _running_in_container():
        value = str(env.get("TEST_RUNNER_BASE_URL") or "").strip()
    if not value:
        value = str(env.get("FS_PUBLIC_BASE_URL") or "").strip()
    if not value:
        port = str(env.get("APP_PORT") or "8099").strip() or "8099"
        value = f"http://localhost:{port}"
    return value.rstrip("/")


def _resolve_container_base_url(env: dict[str, str], explicit: str) -> str:
    value = (
        str(explicit or "").strip()
        or str(env.get("FS_DEPLOYMENT_DOCTOR_CONTAINER_BASE_URL") or "").strip()
    )
    if value:
        return value.rstrip("/")
    port = str(env.get("PORT") or "8099").strip() or "8099"
    return f"http://localhost:{port}"


def _resolve_mcp_url(env: dict[str, str], explicit: str) -> str:
    return (str(explicit or "").strip() or str(env.get("MCP_URL") or "").strip()).rstrip("/")


def _resolve_bearer_token(env: dict[str, str], explicit: str) -> str:
    """Resolve a machine token the MCP probe can authenticate with.

    FS_WORKER_MCP_BEARER_TOKEN is included because it is the only one of these
    names the shipped production template actually declares. Compose maps it to
    MCP_BEARER_TOKEN for the worker service alone, so the app container - where
    compose-mode probing runs - has neither MCP_BEARER_TOKEN nor
    FS_SESSION_TOKEN. Without this the mcp_auth check reported
    "no bearer token was provided" on every correctly configured production
    deployment, and passed only when the operator happened to have the value
    exported in their shell.
    """
    return (
        str(explicit or "").strip()
        or str(_runtime_env.get("MCP_BEARER_TOKEN") or "").strip()
        or str(_runtime_env.get("FS_SESSION_TOKEN") or "").strip()
        or str(_runtime_env.get("FS_WORKER_MCP_BEARER_TOKEN") or "").strip()
        or str(env.get("MCP_BEARER_TOKEN") or "").strip()
        or str(env.get("FS_SESSION_TOKEN") or "").strip()
        or str(env.get("FS_WORKER_MCP_BEARER_TOKEN") or "").strip()
    )


def _env_flag(env: dict[str, str], key: str) -> bool:
    return str(env.get(key) or _runtime_env.get(key) or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _join_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _http_json(
    url: str,
    *,
    timeout: float,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, Any, str]:
    request = urlrequest.Request(
        url, data=body, headers=headers or {}, method="POST" if body else "GET"
    )
    try:
        if any(key.lower() == "authorization" for key in (headers or {})):
            response_context = urlrequest.build_opener(_NoAuthRedirectHandler()).open(
                request, timeout=timeout
            )
        else:
            response_context = urlrequest.urlopen(request, timeout=timeout)  # nosec B310
        with response_context as response:
            status = int(response.status)
            raw_bytes = response.read(_MAX_HTTP_JSON_BYTES + 1)
            if len(raw_bytes) > _MAX_HTTP_JSON_BYTES:
                return status, {}, "JSON response exceeds the 1 MiB safety limit"
            raw = raw_bytes.decode("utf-8", errors="replace")
            return status, _parse_json(raw), ""
    except urlerror.HTTPError as exc:
        raw_bytes = exc.read(_MAX_HTTP_JSON_BYTES + 1)
        if len(raw_bytes) > _MAX_HTTP_JSON_BYTES:
            return int(exc.code), {}, "JSON response exceeds the 1 MiB safety limit"
        raw = raw_bytes.decode("utf-8", errors="replace")
        return int(exc.code), _parse_json(raw), _truncate_detail(raw or str(exc))
    except Exception as exc:
        return 0, {}, f"{type(exc).__name__}: {_truncate_detail(str(exc))}"


class _NoAuthRedirectHandler(urlrequest.HTTPRedirectHandler):
    """Fail closed so authenticated readiness credentials never cross origins."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _parse_json(raw: str) -> Any:
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}


def _load_env(env_path: Path, *, from_process: bool) -> dict[str, str]:
    file_env = _parse_env_file(env_path) if env_path.exists() else {}
    process_env = {str(key): str(value) for key, value in _runtime_env.items()}
    return {**file_env, **process_env} if from_process else file_env


def _running_in_container() -> bool:
    if _runtime_env.get("FS_DEPLOYMENT_DOCTOR_IN_CONTAINER"):
        return True
    try:
        if Path("/.dockerenv").exists():
            return True
        cgroup = Path("/proc/1/cgroup")
        return cgroup.exists() and any(
            token in cgroup.read_text(encoding="utf-8", errors="ignore")
            for token in ("docker", "kubepods", "containerd")
        )
    except OSError:
        return False


def _runtime_check_detail(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    checks_raw = payload.get("checks")
    checks: dict[Any, Any] = checks_raw if isinstance(checks_raw, dict) else {}
    if not checks:
        return str(payload.get("status") or "")
    failed = [name for name, row in checks.items() if not bool((row or {}).get("ok"))]
    if failed:
        return "failed checks: " + ", ".join(failed)
    return f"{len(checks)} readiness checks passed"


def _runtime_meta_detail(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    meta_raw = payload.get("meta")
    meta: dict[Any, Any] = meta_raw if isinstance(meta_raw, dict) else {}
    parts = []
    for key in ("pending_jobs", "in_progress_jobs", "awaiting_approval_jobs", "active_leases"):
        if key in meta:
            parts.append(f"{key}={meta.get(key)}")
    return ", ".join(parts) or _runtime_check_detail(payload)


def _parse_compose_ps_json(raw: str) -> list[dict[str, Any]]:
    text = str(raw or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed_rows = []
        for line in text.splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                parsed_rows.append(item)
        return parsed_rows
    if isinstance(parsed, list):
        return [dict(row) for row in parsed if isinstance(row, dict)]
    if isinstance(parsed, dict):
        return [parsed]
    return []


def _compose_service_name(row: dict[str, Any]) -> str:
    return str(
        row.get("Service") or row.get("service") or row.get("Name") or row.get("name") or ""
    ).strip()


def _first_env(env: dict[str, str], *keys: str) -> str:
    for key in keys:
        value = str(env.get(key) or _runtime_env.get(key) or "").strip()
        if _is_real_secret_value(value):
            return value
    return ""


def _truncate_detail(value: str, *, limit: int = 500) -> str:
    text = str(value or "").strip().replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."
