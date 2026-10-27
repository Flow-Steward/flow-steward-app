"""Integration coverage for ``step_kind=http_request``."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from tests._step_run_state import step_run_state

pytestmark = [pytest.mark.integration]


def test_execute_step_http_request_writes_response_target_and_message():
    from core.application.orchestration.graph.nodes import execute_step_node

    state = step_run_state(
        {
            "step_kind": "http_request",
            "override": {
                "http_request": {
                    "method": "GET",
                    "url": "https://example.com/health",
                    "query_params": {},
                    "headers": {},
                    "auth_mode": "none",
                    "auth_ref": "",
                    "timeout_seconds": 10,
                    "body_template": {},
                    "expected_statuses": [200],
                    "response_target": "state.outputs.http_result",
                }
            },
        },
        agent_messages=[],
    )

    fake_result = {
        "ok": True,
        "status_code": 200,
        "retryable": False,
        "failure_reason": "",
        "response_excerpt": '{"ok":true}',
        "response_body": {"ok": True},
        "response_headers": {"content_type": "application/json", "content_length": "12"},
        "request": {"method": "GET", "url": "https://example.com/health"},
    }
    with patch(
        "core.application.orchestration.http_request_step_runtime.execute_http_request_step",
        return_value=fake_result,
    ):
        update = execute_step_node(state)

    assert update["outputs"]["http_result"] == {"ok": True}
    msg = update["agent_messages"][-1]
    assert msg["from_agent"] == "http_request_step"
    assert msg["status"] == "pass"
    assert msg["artifacts"]["http_request"]["status_code"] == 200


def test_execute_step_http_request_failure_emits_retryable_context():
    from core.application.orchestration.graph.nodes import execute_step_node

    state = step_run_state(
        {
            "step_kind": "http_request",
            "override": {
                "http_request": {
                    "method": "GET",
                    "url": "https://example.com/health",
                    "query_params": {},
                    "headers": {},
                    "auth_mode": "none",
                    "auth_ref": "",
                    "timeout_seconds": 10,
                    "body_template": {},
                    "expected_statuses": [200],
                    "response_target": "state.outputs.http_result",
                }
            },
        },
        agent_messages=[],
    )

    fake_result = {
        "ok": False,
        "status_code": 503,
        "retryable": True,
        "failure_reason": "unexpected_status",
        "response_excerpt": "service unavailable",
        "response_body": "service unavailable",
        "response_headers": {"content_type": "text/plain", "content_length": "18"},
        "request": {"method": "GET", "url": "https://example.com/health"},
    }
    with patch(
        "core.application.orchestration.http_request_step_runtime.execute_http_request_step",
        return_value=fake_result,
    ):
        update = execute_step_node(state)

    msg = update["agent_messages"][-1]
    assert msg["status"] == "fail"
    assert msg["artifacts"]["http_request"]["retryable"] is True


def test_execute_step_http_request_artifact_mode_writes_handle_to_response_target():
    from core.application.orchestration.graph.nodes import execute_step_node

    state = step_run_state(
        {
            "step_kind": "http_request",
            "override": {
                "http_request": {
                    "method": "GET",
                    "url": "https://example.com/file.pdf",
                    "expected_statuses": [200],
                    "response_mode": "artifact",
                    "response_target": "state.outputs.download",
                }
            },
        },
        agent_messages=[],
    )
    fake_result = {
        "ok": True,
        "status_code": 200,
        "retryable": False,
        "failure_reason": "",
        "response_excerpt": "",
        "response_mode": "artifact",
        "artifact_handle": "artifact:art_download",
        "artifact_id": "art_download",
        "filename": "file.pdf",
        "content_type": "application/pdf",
        "size_bytes": 4,
        "sha256": "hash",
        "request": {"method": "GET", "url": "https://example.com/file.pdf"},
    }
    with patch(
        "core.application.orchestration.http_request_step_runtime.execute_http_request_step",
        return_value=fake_result,
    ):
        update = execute_step_node(state)

    assert update["outputs"]["download"] == "artifact:art_download"
    assert update["agent_messages"][-1]["artifacts"]["http_request"]["filename"] == "file.pdf"
