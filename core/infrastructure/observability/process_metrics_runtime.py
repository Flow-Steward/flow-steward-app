"""Prometheus exporters for non-HTTP long-running platform processes."""

from __future__ import annotations

import hmac
import os
import platform
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.observability.observability_metrics import (
    metric_header_lines,
    metrics_line,
)

APPLICATION_JSON_VALUE = "application/json"


_METRIC_NAMESPACE = "platform"
_PROCESS_START_TIME_SECONDS = time.time()


def _metric_name(suffix: str) -> str:
    normalized = str(suffix or "").strip().replace("-", "_")
    return f"{_METRIC_NAMESPACE}_{normalized}"


def _config_value(
    config: dict[str, Any] | None,
    key: str,
    env_name: str,
    default: Any,
) -> Any:
    if config is not None and key in config:
        value = config[key]
        if value not in (None, ""):
            return value
    return _runtime_env.get(env_name, default)


def _parse_int(value: Any, default: int, *, minimum: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    return max(int(minimum), parsed)


def _metrics_request_authorized(headers, token: str) -> bool:
    if not token:
        return False
    supplied = str(headers.get("x-metrics-token") or "").strip()
    if supplied and hmac.compare_digest(supplied, token):
        return True
    auth_header = str(headers.get("authorization") or "").strip()
    if auth_header.lower().startswith("bearer "):
        bearer = auth_header[7:].strip()
        if bearer and hmac.compare_digest(bearer, token):
            return True
    return False


def build_process_metrics_payload(
    *,
    component: str,
    service_name: str = "platform",
    instance_id: str = "",
    extra_metrics_fn: Callable[[], list[str]] | None = None,
) -> str:
    normalized_component = str(component or "runtime").strip() or "runtime"
    normalized_service_name = str(service_name or "platform").strip() or "platform"
    normalized_instance_id = str(instance_id or "").strip() or (
        f"{socket.gethostname()}-{os.getpid()}"
    )
    lines: list[str] = []
    lines.extend(
        metric_header_lines(
            suffix="process_up",
            help_text="Process liveness state (1 when exporter responds).",
            metric_type="gauge",
        )
    )
    lines.append(
        metrics_line(
            _metric_name("process_up"),
            1,
            labels={"component": normalized_component},
        )
    )
    lines.extend(
        metric_header_lines(
            suffix="process_info",
            help_text="Static process information.",
            metric_type="gauge",
        )
    )
    lines.append(
        metrics_line(
            _metric_name("process_info"),
            1,
            labels={
                "component": normalized_component,
                "instance": normalized_instance_id,
                "python_version": platform.python_version(),
                "service": normalized_service_name,
            },
        )
    )
    lines.extend(
        metric_header_lines(
            suffix="process_start_time_seconds",
            help_text="Process start time in Unix seconds.",
            metric_type="gauge",
        )
    )
    lines.append(
        metrics_line(
            _metric_name("process_start_time_seconds"),
            f"{_PROCESS_START_TIME_SECONDS:.6f}",
            labels={"component": normalized_component},
        )
    )
    lines.extend(
        metric_header_lines(
            suffix="process_threads",
            help_text="Current process thread count.",
            metric_type="gauge",
        )
    )
    lines.append(
        metrics_line(
            _metric_name("process_threads"),
            threading.active_count(),
            labels={"component": normalized_component},
        )
    )
    lines.extend(
        metric_header_lines(
            suffix="process_cpu_seconds_total",
            help_text="Process CPU time in seconds.",
            metric_type="counter",
        )
    )
    lines.append(
        metrics_line(
            _metric_name("process_cpu_seconds_total"),
            f"{time.process_time():.6f}",
            labels={"component": normalized_component},
        )
    )
    if callable(extra_metrics_fn):
        extra_lines = extra_metrics_fn() or []
        lines.extend([str(line) for line in extra_lines if str(line or "").strip()])
    return "\n".join(lines) + "\n"


def build_worker_process_metrics_lines(*, project_id: str | None) -> list[str]:
    mode = "dedicated" if project_id is not None else "generic"
    lines: list[str] = []
    lines.extend(
        metric_header_lines(
            suffix="worker_mode",
            help_text="Worker execution mode (1 for the active mode label).",
            metric_type="gauge",
        )
    )
    lines.append(metrics_line(_metric_name("worker_mode"), 1, labels={"mode": mode}))
    lines.extend(
        metric_header_lines(
            suffix="worker_project_scoped",
            help_text="Whether this worker is dedicated to a project.",
            metric_type="gauge",
        )
    )
    lines.append(metrics_line(_metric_name("worker_project_scoped"), 1 if project_id else 0))
    return lines


def build_control_process_metrics_lines(
    *,
    loop_flags: dict[str, bool],
    threads_resolver: (
        Callable[[], dict[str, tuple[threading.Thread, threading.Event]]] | None
    ) = None,
    security_posture_status_resolver: Callable[[], dict[str, Any]] | None = None,
) -> list[str]:
    lines: list[str] = []
    lines.extend(
        metric_header_lines(
            suffix="control_loop_enabled",
            help_text="Configured control loop enablement by loop name (1 enabled, 0 disabled).",
            metric_type="gauge",
        )
    )
    for loop_name, enabled in sorted((loop_flags or {}).items()):
        lines.append(
            metrics_line(
                _metric_name("control_loop_enabled"),
                1 if bool(enabled) else 0,
                labels={"loop": str(loop_name or "unknown")},
            )
        )
    running_threads = 0
    if callable(threads_resolver):
        threads = threads_resolver() or {}
        running_threads = sum(
            1
            for value in threads.values()
            if isinstance(value, tuple)
            and value
            and hasattr(value[0], "is_alive")
            and value[0].is_alive()
        )
    lines.extend(
        metric_header_lines(
            suffix="control_threads_running",
            help_text="Currently running control-loop threads.",
            metric_type="gauge",
        )
    )
    lines.append(metrics_line(_metric_name("control_threads_running"), running_threads))

    posture_status: dict[str, Any] = {}
    posture_status_readable = True
    if callable(security_posture_status_resolver):
        try:
            resolved = security_posture_status_resolver() or {}
            posture_status = dict(resolved) if isinstance(resolved, dict) else {}
        except Exception:
            posture_status_readable = False
    lines.extend(
        metric_header_lines(
            suffix="security_posture_status_readable",
            help_text="Whether durable security-posture loop status can be read.",
            metric_type="gauge",
        )
    )
    lines.append(
        metrics_line(
            _metric_name("security_posture_status_readable"),
            1 if posture_status_readable else 0,
        )
    )
    lines.extend(
        metric_header_lines(
            suffix="security_posture_consecutive_failures",
            help_text="Consecutive failed security-posture iterations.",
            metric_type="gauge",
        )
    )
    lines.append(
        metrics_line(
            _metric_name("security_posture_consecutive_failures"),
            max(0, int(posture_status.get("consecutive_failures") or 0)),
        )
    )
    for field, suffix in (
        ("last_attempt_at", "security_posture_last_attempt_timestamp_seconds"),
        ("last_success_at", "security_posture_last_success_timestamp_seconds"),
    ):
        value = str(posture_status.get(field) or "").strip()
        if not value:
            continue
        try:
            timestamp = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            continue
        lines.extend(
            metric_header_lines(
                suffix=suffix,
                help_text=f"Durable security-posture {field.replace('_', ' ')}.",
                metric_type="gauge",
            )
        )
        lines.append(metrics_line(_metric_name(suffix), f"{timestamp:.6f}"))
    return lines


def build_mcp_process_metrics_lines(*, auth_enabled: bool, transport: str) -> list[str]:
    normalized_transport = str(transport or "unknown").strip() or "unknown"
    lines: list[str] = []
    lines.extend(
        metric_header_lines(
            suffix="mcp_auth_enabled",
            help_text="MCP auth enablement state (1 enabled, 0 disabled).",
            metric_type="gauge",
        )
    )
    lines.append(metrics_line(_metric_name("mcp_auth_enabled"), 1 if auth_enabled else 0))
    lines.extend(
        metric_header_lines(
            suffix="mcp_transport_info",
            help_text="Active MCP transport mode (1 for the active transport label).",
            metric_type="gauge",
        )
    )
    lines.append(
        metrics_line(
            _metric_name("mcp_transport_info"),
            1,
            labels={"transport": normalized_transport},
        )
    )
    return lines


@dataclass
class ProcessMetricsServerHandle:
    host: str
    port: int
    thread: threading.Thread | None
    server: ThreadingHTTPServer | None
    configured: bool
    reason: str = ""

    def close(self) -> None:
        if self.server is None:
            return
        self.server.shutdown()
        self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=2.0)


def start_process_metrics_server(
    *,
    component: str,
    config: dict[str, Any] | None = None,
    logger,
    port_key: str,
    port_env_name: str,
    host_key: str = "process_metrics_host",
    host_env_name: str = "FS_PROCESS_METRICS_HOST",
    token_key: str = "metrics_token",
    token_env_name: str = "FS_METRICS_TOKEN",
    allow_ephemeral_port: bool = False,
    port: int | None = None,
    host: str | None = None,
    metrics_token: str | None = None,
    extra_metrics_fn: Callable[[], list[str]] | None = None,
) -> ProcessMetricsServerHandle:
    resolved_port = (
        int(port)
        if port is not None
        else _parse_int(_config_value(config, port_key, port_env_name, 0), 0, minimum=0)
    )
    if resolved_port <= 0 and not allow_ephemeral_port:
        return ProcessMetricsServerHandle(
            host=str(host or _config_value(config, host_key, host_env_name, "0.0.0.0")),
            port=resolved_port,
            thread=None,
            server=None,
            configured=False,
            reason="disabled",
        )

    resolved_host = str(host or _config_value(config, host_key, host_env_name, "0.0.0.0")).strip()
    resolved_host = resolved_host or "0.0.0.0"
    resolved_token = str(
        metrics_token
        if metrics_token is not None
        else _config_value(config, token_key, token_env_name, "")
    ).strip()
    service_name = (
        str(_config_value(config, "runtime_service_name", "FS_SERVICE_NAME", "platform")).strip()
        or "platform"
    )
    instance_id = f"{socket.gethostname()}-{os.getpid()}"

    class _MetricsHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = (self.path or "").split("?", 1)[0]
            if path == "/health/live":
                body = b'{"status":"ok"}'
                self.send_response(200)
                self.send_header("Content-Type", APPLICATION_JSON_VALUE)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path != "/metrics":
                self.send_error(404)
                return
            if not _metrics_request_authorized(self.headers, resolved_token):
                body = b'{"error":"Metrics token required"}'
                self.send_response(401)
                self.send_header("Content-Type", APPLICATION_JSON_VALUE)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            try:
                payload = build_process_metrics_payload(
                    component=component,
                    service_name=service_name,
                    instance_id=instance_id,
                    extra_metrics_fn=extra_metrics_fn,
                ).encode("utf-8")
            except Exception as exc:
                logger.exception("%s metrics exporter payload failed: %s", component, exc)
                body = b'{"error":"Metrics payload failed"}'
                self.send_response(500)
                self.send_header("Content-Type", APPLICATION_JSON_VALUE)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:
            logger.debug("%s metrics exporter: " + format, component, *args)

    server = ThreadingHTTPServer((resolved_host, resolved_port), _MetricsHandler)
    server.daemon_threads = True
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.5},
        name=f"{component}-metrics-exporter",
        daemon=True,
    )
    thread.start()
    actual_port = int(server.server_address[1])
    logger.info(
        "%s metrics exporter listening on %s:%d",
        component,
        resolved_host,
        actual_port,
    )
    return ProcessMetricsServerHandle(
        host=resolved_host,
        port=actual_port,
        thread=thread,
        server=server,
        configured=True,
    )


__all__ = [
    "ProcessMetricsServerHandle",
    "build_control_process_metrics_lines",
    "build_mcp_process_metrics_lines",
    "build_process_metrics_payload",
    "build_worker_process_metrics_lines",
    "start_process_metrics_server",
]
