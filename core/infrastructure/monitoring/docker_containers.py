"""Docker-backed container discovery for monitoring surfaces."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_COMPOSE_ROOT = Path(__file__).resolve().parents[3]
_DOCKER_PS_FORMAT = "{{json .}}"
_MONITORING_SERVICE_SPECS: tuple[dict[str, Any], ...] = (
    {"service": "fs-app", "title": "App", "category": "application", "required": True},
    {
        "service": "control-plane",
        "title": "Control Plane",
        "category": "application",
        "required": True,
    },
    {"service": "fs-worker", "title": "Workers", "category": "application", "required": True},
    {"service": "fs-mcp", "title": "MCP", "category": "application", "required": True},
    {"service": "pg-fs", "title": "Postgres", "category": "dependency", "required": True},
    {"service": "rabbitmq-fs", "title": "RabbitMQ", "category": "dependency", "required": True},
    {"service": "traefik", "title": "Traefik", "category": "edge", "required": True},
    {"service": "sonarqube", "title": "SonarQube", "category": "quality", "required": False},
    {
        "service": "sonar-scanner",
        "title": "Sonar Scanner",
        "category": "quality",
        "required": False,
    },
    {"service": "fs-audit", "title": "Audit Runner", "category": "quality", "required": False},
    {
        "service": "fs-test-runner",
        "title": "Test Runner",
        "category": "quality",
        "required": False,
    },
)


def docker_socket_available() -> bool:
    return os.path.exists("/var/run/docker.sock")


def _parse_label_string(raw_value: Any) -> dict[str, str]:
    labels: dict[str, str] = {}
    for chunk in str(raw_value or "").split(","):
        if "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key:
            labels[key] = value
    return labels


def infer_compose_service(row: dict[str, Any]) -> str:
    labels = _parse_label_string(row.get("Labels"))
    compose_service = str(labels.get("com.docker.compose.service") or "").strip()
    if compose_service:
        return compose_service

    names = [part.strip() for part in str(row.get("Names") or "").split(",") if part.strip()]
    for spec in _MONITORING_SERVICE_SPECS:
        service = str(spec["service"])
        for name in names:
            if (
                name == service
                or name.endswith((f"-{service}-1", f"_{service}_1"))
                or f"-{service}-" in name
                or f"_{service}_" in name
            ):
                return service
    return ""


def _container_tone(*, required: bool, rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "red" if required else "gray"

    statuses = [str(row.get("status") or "").lower() for row in rows]
    states = [str(row.get("state") or "").lower() for row in rows]

    if any("unhealthy" in status for status in statuses):
        return "red"
    if any(state in {"exited", "dead", "removing"} for state in states):
        return "red" if required else "amber"
    if all(state == "running" for state in states if state):
        return "green"
    return "amber"


def _build_container_row(spec: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    names = [
        str(row.get("name") or "").strip() for row in rows if str(row.get("name") or "").strip()
    ]
    running_instances = sum(
        1 for row in rows if str(row.get("state") or "").strip().lower() == "running"
    )
    return {
        "service": spec["service"],
        "title": spec["title"],
        "category": spec["category"],
        "required": bool(spec.get("required")),
        "present": bool(rows),
        "instance_count": len(rows),
        "running_instances": running_instances,
        "state_summary": ", ".join(
            sorted(
                {
                    str(row.get("state") or "").strip() or "unknown"
                    for row in rows
                    if str(row.get("state") or "").strip()
                }
            )
        )
        or "absent",
        "status_summary": " | ".join(
            [
                str(row.get("status") or "").strip()
                for row in rows
                if str(row.get("status") or "").strip()
            ]
        )
        or "No container detected",
        "container_names": names,
        "image": next(
            (
                str(row.get("image") or "").strip()
                for row in rows
                if str(row.get("image") or "").strip()
            ),
            "",
        ),
        "tone": _container_tone(required=bool(spec.get("required")), rows=rows),
    }


def _empty_services() -> list[dict[str, Any]]:
    return [_build_container_row(spec, []) for spec in _MONITORING_SERVICE_SPECS]


def _container_discovery_applies() -> bool:
    """False where the deployment has no separate containers to discover.

    AC17-R69: Compact runs every process as a sibling inside one container and mounts no
    Docker socket, deliberately -- mounting it would hand the runtime control of the host's
    Docker. The monitoring page reported that absence as "Docker socket UNAVAILABLE --
    Required for container-level service discovery", which reads as a misconfiguration the
    operator should go and fix. They cannot, and they should not.
    """
    from core.infrastructure.config.runtime_env import runtime_env

    profile = str(runtime_env.get("FS_DEPLOYMENT_PROFILE") or "scalable").strip().lower()
    return profile != "compact"


def list_monitoring_containers() -> dict[str, Any]:
    socket_available = docker_socket_available()
    docker_bin = shutil.which("docker")
    checked_at = datetime.now(UTC).isoformat()
    discovery_applies = _container_discovery_applies()

    if not discovery_applies:
        return {
            "available": False,
            "checked_at": checked_at,
            "docker_socket_available": socket_available,
            "docker_cli_available": bool(docker_bin),
            "container_discovery_applicable": False,
            "error": (
                "This deployment runs every service as a process inside one container, so "
                "there are no separate containers to discover. No Docker socket is mounted, "
                "by design."
            ),
            "services": _empty_services(),
        }

    if not socket_available:
        return {
            "available": False,
            "checked_at": checked_at,
            "docker_socket_available": False,
            "docker_cli_available": bool(docker_bin),
            "container_discovery_applicable": True,
            "error": "Docker socket is not mounted into the app container.",
            "services": _empty_services(),
        }

    if not docker_bin:
        return {
            "available": False,
            "checked_at": checked_at,
            "docker_socket_available": True,
            "docker_cli_available": False,
            "error": "Docker CLI is not available in the app container.",
            "services": _empty_services(),
        }

    try:
        proc = subprocess.run(
            [docker_bin, "ps", "-a", "--format", _DOCKER_PS_FORMAT],
            capture_output=True,
            check=False,
            text=True,
            cwd=str(_COMPOSE_ROOT),
        )
    except Exception as exc:
        return {
            "available": False,
            "checked_at": checked_at,
            "docker_socket_available": True,
            "docker_cli_available": True,
            "error": str(exc),
            "services": _empty_services(),
        }

    if proc.returncode != 0:
        return {
            "available": False,
            "checked_at": checked_at,
            "docker_socket_available": True,
            "docker_cli_available": True,
            "error": str(proc.stderr or proc.stdout or "docker ps failed").strip(),
            "services": _empty_services(),
        }

    grouped: dict[str, list[dict[str, Any]]] = {}
    for raw_line in str(proc.stdout or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        service = infer_compose_service(row)
        if not service:
            continue
        grouped.setdefault(service, []).append(
            {
                "name": str(row.get("Names") or "").strip(),
                "image": str(row.get("Image") or "").strip(),
                "status": str(row.get("Status") or "").strip(),
                "state": str(row.get("State") or "").strip(),
            }
        )

    services = [
        _build_container_row(spec, grouped.get(str(spec["service"]), []))
        for spec in _MONITORING_SERVICE_SPECS
    ]
    running_services = sum(1 for row in services if row["tone"] == "green")
    return {
        "available": True,
        "checked_at": checked_at,
        "docker_socket_available": True,
        "docker_cli_available": True,
        "running_services": running_services,
        "service_count": len(services),
        "services": services,
    }


__all__ = [
    "docker_socket_available",
    "infer_compose_service",
    "list_monitoring_containers",
]
