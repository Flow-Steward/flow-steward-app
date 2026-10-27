from __future__ import annotations

import http.client
import threading
import time
from unittest.mock import MagicMock

from core.infrastructure.observability.process_metrics_runtime import (
    build_control_process_metrics_lines,
    build_mcp_process_metrics_lines,
    build_process_metrics_payload,
    build_worker_process_metrics_lines,
    start_process_metrics_server,
)


def test_build_process_metrics_payload_includes_base_and_worker_metrics() -> None:
    payload = build_process_metrics_payload(
        component="worker",
        service_name="platform",
        instance_id="worker-1",
        extra_metrics_fn=lambda: build_worker_process_metrics_lines(project_id=7),
    )

    assert 'platform_process_up{component="worker"} 1' in payload
    assert 'platform_process_info{component="worker",instance="worker-1"' in payload
    assert 'platform_worker_mode{mode="dedicated"} 1' in payload
    assert "platform_worker_project_scoped 1" in payload


def test_build_control_process_metrics_lines_reports_enabled_loops_and_running_threads() -> None:
    blocker = threading.Event()
    worker = threading.Thread(target=blocker.wait, name="control-watchdog", daemon=True)
    worker.start()
    try:
        lines = build_control_process_metrics_lines(
            loop_flags={"watchdog": True, "scheduled_jobs": False},
            threads_resolver=lambda: {"watchdog": (worker, threading.Event())},
            security_posture_status_resolver=lambda: {
                "last_attempt_at": "2026-08-10T12:00:00+00:00",
                "last_success_at": "2026-08-10T11:59:00+00:00",
                "consecutive_failures": 2,
            },
        )
    finally:
        blocker.set()
        worker.join(timeout=1.0)

    payload = "\n".join(lines)
    assert 'platform_control_loop_enabled{loop="watchdog"} 1' in payload
    assert 'platform_control_loop_enabled{loop="scheduled_jobs"} 0' in payload
    assert "platform_control_threads_running 1" in payload
    assert "platform_security_posture_consecutive_failures 2" in payload
    assert "platform_security_posture_last_attempt_timestamp_seconds" in payload
    assert "platform_security_posture_last_success_timestamp_seconds" in payload


def test_build_mcp_process_metrics_lines_reports_auth_and_transport() -> None:
    payload = "\n".join(
        build_mcp_process_metrics_lines(auth_enabled=True, transport="streamable-http")
    )

    assert "platform_mcp_auth_enabled 1" in payload
    assert 'platform_mcp_transport_info{transport="streamable-http"} 1' in payload


def test_start_process_metrics_server_serves_metrics_and_enforces_token() -> None:
    logger = MagicMock()
    handle = start_process_metrics_server(
        component="worker",
        logger=logger,
        port_key="worker_metrics_port",
        port_env_name="FS_WORKER_METRICS_PORT",
        host="127.0.0.1",
        port=0,
        metrics_token="metrics-secret",
        allow_ephemeral_port=True,
        extra_metrics_fn=lambda: build_worker_process_metrics_lines(project_id=None),
    )

    assert handle.configured is True
    conn = http.client.HTTPConnection("127.0.0.1", handle.port, timeout=5)
    try:
        conn.request("GET", "/metrics")
        response = conn.getresponse()
        assert response.status == 401
        response.read()

        conn.request("GET", "/metrics", headers={"X-Metrics-Token": "metrics-secret"})
        response = conn.getresponse()
        body = response.read().decode("utf-8")
        assert response.status == 200
        assert 'platform_process_up{component="worker"} 1' in body
        assert 'platform_worker_mode{mode="generic"} 1' in body
    finally:
        conn.close()
        handle.close()
        time.sleep(0.05)


def test_start_process_metrics_server_requires_token_even_when_unset() -> None:
    logger = MagicMock()
    handle = start_process_metrics_server(
        component="worker",
        logger=logger,
        port_key="worker_metrics_port",
        port_env_name="FS_WORKER_METRICS_PORT",
        host="127.0.0.1",
        port=0,
        metrics_token="",
        allow_ephemeral_port=True,
    )

    assert handle.configured is True
    conn = http.client.HTTPConnection("127.0.0.1", handle.port, timeout=5)
    try:
        conn.request("GET", "/metrics")
        response = conn.getresponse()
        assert response.status == 401
        response.read()
    finally:
        conn.close()
        handle.close()
        time.sleep(0.05)
