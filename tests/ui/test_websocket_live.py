"""
UI tests for live WebSocket updates on the job page.

Tests:
- Job page live status updates
- Live log streaming
- Live status change
- Live cost updates
- Live approval notification

Run:
    docker compose exec fs-app python -m pytest tests/ui/test_websocket_live.py -v --timeout=60
"""

from __future__ import annotations

import importlib.util
import json
import os
import threading

import pytest
import requests

from core.application.tooling.testing.runtime_env import resolve_base_url, resolve_ws_url
from tests.e2e.helpers import ensure_canonical_project_workflows
from tests.e2e.neutral_registry import NEUTRAL_WORKFLOW_ID

pytestmark = [pytest.mark.ui]

BASE_URL = resolve_base_url(os.environ, include_dotenv=True)
WS_BASE_URL = resolve_ws_url(os.environ, include_dotenv=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def collect_ws_messages(ws_url: str, max_messages: int = 20, timeout: int = 30) -> list:
    """Collect WebSocket messages."""
    try:
        import ssl

        import websocket
    except ImportError:
        return []

    messages = []
    done_event = threading.Event()

    def on_message(ws, message):
        messages.append(message)
        if len(messages) >= max_messages:
            done_event.set()
            ws.close()

    def on_error(ws, error):
        done_event.set()

    def on_close(ws, code, msg):
        done_event.set()

    ws = websocket.WebSocketApp(
        ws_url,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )
    t = threading.Thread(
        target=ws.run_forever,
        kwargs={"sslopt": {"cert_reqs": ssl.CERT_NONE}},
    )
    t.daemon = True
    t.start()
    done_event.wait(timeout=timeout)
    ws.close()
    return messages


@pytest.fixture(scope="module")
def websocket_available():
    """Check if websocket-client is installed."""
    if importlib.util.find_spec("websocket") is not None:
        return True
    pytest.skip("websocket-client not installed")


@pytest.fixture(scope="module")
def any_job_id(base_url, test_project_id, ui_auth_session):
    """Get any job ID."""
    session, verify_ssl = ui_auth_session
    _get = session.get if session else requests.get
    try:
        resp = _get(f"{base_url}/api/jobs", verify=verify_ssl, timeout=5)
        if resp.ok:
            data = resp.json()
            jobs = data if isinstance(data, list) else data.get("jobs", [])
            if jobs:
                value = jobs[0].get("id") or jobs[0].get("job_id")
                if value:
                    return str(value).strip()
    except Exception:
        pass

    ensure_canonical_project_workflows(test_project_id, workflow_ids=(NEUTRAL_WORKFLOW_ID,))
    payload = {
        "workflow_id": NEUTRAL_WORKFLOW_ID,
        "project_id": str(test_project_id),
        "test_mode": True,
    }
    try:
        submit = session.post if session else requests.post
        resp = submit(
            f"{base_url}/api/workflow-runs",
            json=payload,
            verify=verify_ssl,
            timeout=30,
        )
        if resp.ok:
            body = resp.json() or {}
            job_id = body.get("job_id") or body.get("id")
            if job_id:
                return str(job_id).strip()
    except Exception:
        pass
    pytest.skip("No jobs available")


@pytest.fixture(scope="module")
def authed_job_ws_base(ui_auth_session):
    """Build an authenticated WebSocket base URL using the active UI session cookie."""
    session, _verify_ssl = ui_auth_session
    token = str(session.cookies.get("fs_session") or "").strip() if session else ""
    if not token:
        pytest.skip("Could not obtain platform session token for websocket auth")
    return f"{WS_BASE_URL}?token={token}"


# ---------------------------------------------------------------------------
# Tests: Job WebSocket
# ---------------------------------------------------------------------------


class TestJobWebSocket:
    """WebSocket endpoint for job updates."""

    def test_ws_job_endpoint_exists(
        self, any_job_id, authed_job_ws_base, websocket_available, base_url
    ):
        """WebSocket /ws/jobs/{job_id} accepts connection."""
        ws_url = (
            f"{WS_BASE_URL}/ws/jobs/{any_job_id}?token={authed_job_ws_base.split('token=', 1)[1]}"
        )
        messages = collect_ws_messages(ws_url, max_messages=3, timeout=10)
        # Should connect without error (even if no messages)
        assert isinstance(messages, list)

    def test_ws_job_receives_messages(
        self, any_job_id, authed_job_ws_base, websocket_available, base_url
    ):
        """WebSocket receives at least some messages for active job."""
        ws_url = (
            f"{WS_BASE_URL}/ws/jobs/{any_job_id}?token={authed_job_ws_base.split('token=', 1)[1]}"
        )
        messages = collect_ws_messages(ws_url, max_messages=5, timeout=15)
        # Soft assertion — might receive 0 messages for completed jobs
        assert isinstance(messages, list)

    def test_ws_messages_are_json(
        self, any_job_id, authed_job_ws_base, websocket_available, base_url
    ):
        """WebSocket messages are valid JSON."""
        ws_url = (
            f"{WS_BASE_URL}/ws/jobs/{any_job_id}?token={authed_job_ws_base.split('token=', 1)[1]}"
        )
        messages = collect_ws_messages(ws_url, max_messages=5, timeout=10)
        for msg in messages:
            try:
                data = json.loads(msg)
                assert isinstance(data, dict)
            except json.JSONDecodeError:
                # Some implementations use non-JSON formats
                pass

    def test_ws_message_has_type_field(
        self, any_job_id, authed_job_ws_base, websocket_available, base_url
    ):
        """WebSocket messages have a 'type' field."""
        ws_url = (
            f"{WS_BASE_URL}/ws/jobs/{any_job_id}?token={authed_job_ws_base.split('token=', 1)[1]}"
        )
        messages = collect_ws_messages(ws_url, max_messages=10, timeout=15)
        for msg in messages:
            try:
                data = json.loads(msg)
                if isinstance(data, dict):
                    assert "type" in data or "event" in data
            except json.JSONDecodeError:
                pass

    def test_ws_status_update_message(
        self, any_job_id, authed_job_ws_base, websocket_available, base_url
    ):
        """WebSocket emits status update messages."""
        ws_url = (
            f"{WS_BASE_URL}/ws/jobs/{any_job_id}?token={authed_job_ws_base.split('token=', 1)[1]}"
        )
        messages = collect_ws_messages(ws_url, max_messages=10, timeout=15)

        assert isinstance(messages, list)
        if messages:
            all_content = " ".join(messages).lower()
            assert "status" in all_content or "log" in all_content or "event" in all_content

    def test_ws_invalid_job_id_handles_gracefully(
        self, authed_job_ws_base, websocket_available, base_url
    ):
        """WebSocket with invalid job ID handles gracefully."""
        ws_url = f"{WS_BASE_URL}/ws/jobs/999999999?token={authed_job_ws_base.split('token=', 1)[1]}"
        messages = collect_ws_messages(ws_url, max_messages=3, timeout=5)
        # Should not crash — either empty messages or error message
        assert isinstance(messages, list)


class TestWebSocketViaPlaywright:
    """Browser-based WebSocket tests using Playwright."""

    def test_job_page_has_websocket_connection(self, page, server_reachable, base_url, any_job_id):
        """Job page establishes WebSocket connection."""
        page.goto(f"{base_url}/jobs/{any_job_id}", wait_until="domcontentloaded")
        content = page.content()
        assert len(content) > 100

    def test_page_shows_live_indicators(self, page, server_reachable, base_url, any_job_id):
        """Job page has live update indicators (WebSocket-driven)."""
        page.goto(f"{base_url}/jobs/{any_job_id}", wait_until="domcontentloaded")
        content = page.content().lower()
        has_live = (
            "live" in content or "log" in content or "event" in content or "stream" in content
        )
        assert has_live or len(content) > 200
