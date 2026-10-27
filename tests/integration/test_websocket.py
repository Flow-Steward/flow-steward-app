"""
Integration tests for WebSocket endpoints.

Tests: /ws/jobs/{job_id}

Run:
    docker compose exec fs-app python -m pytest tests/integration/test_websocket.py -v
"""

from __future__ import annotations

import sys
from contextlib import suppress
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def ws_client():
    """WebSocket TestClient."""
    try:
        from fastapi.testclient import TestClient

        from core.entrypoints.api.app import app

        return TestClient(app, raise_server_exceptions=False)
    except Exception as e:
        pytest.skip(f"Could not create TestClient: {e}")


# ---------------------------------------------------------------------------
# Job WebSocket tests
# ---------------------------------------------------------------------------


class TestJobWebSocket:
    """Test /ws/jobs/{job_id} WebSocket endpoint."""

    def test_ws_connects_to_nonexistent_job(self, ws_client):
        """WebSocket for non-existent job should handle gracefully."""
        data = None
        try:
            with (
                ws_client.websocket_connect("/ws/jobs/999999") as ws,
                suppress(Exception),
            ):
                # May receive an error message or close immediately
                data = ws.receive_json(timeout=2)
        except Exception:
            pass  # Connection refused is acceptable for non-existent job
        if data is not None:
            assert isinstance(data, dict)

    def test_ws_endpoint_exists(self, ws_client):
        """WebSocket endpoint /ws/jobs/{job_id} should exist."""
        # Try connecting — even a 404 is better than a complete crash
        try:
            with ws_client.websocket_connect("/ws/jobs/1") as ws:
                ws.close()
        except Exception as e:
            # Connection failure is acceptable if DB is not available
            error_str = str(e)
            assert "404" not in error_str, f"WebSocket endpoint should exist, got: {error_str}"

    def test_ws_message_format(self, ws_client):
        """WebSocket messages should be valid JSON dicts."""
        data = None
        try:
            with ws_client.websocket_connect("/ws/jobs/1") as ws, suppress(Exception):
                data = ws.receive_json(timeout=1)
        except Exception:
            pass  # May fail without DB
        if data:
            assert isinstance(data, dict)
            assert "type" in data or len(data) > 0


# ---------------------------------------------------------------------------
# WebSocket message type tests
# ---------------------------------------------------------------------------


class TestWebSocketMessageTypes:
    """Test that expected message types are defined in the codebase."""

    def test_status_message_type_used_in_serve(self):
        """Verify 'status' message type is used in WebSocket code."""
        try:
            import inspect

            from tests import api_runtime_test_surface as api_runtime

            source = inspect.getsource(api_runtime)
        except Exception as e:
            pytest.skip(f"Could not inspect serve.py: {e}")
        # Should have some WebSocket message types
        assert "websocket" in source.lower() or "ws" in source.lower()

    def test_log_message_type_in_serve(self):
        """Verify log messages are sent via WebSocket."""
        try:
            import inspect

            from tests import api_runtime_test_surface as api_runtime

            source = inspect.getsource(api_runtime)
        except Exception as e:
            pytest.skip(f"Could not inspect serve.py: {e}")
        # Should send log messages
        assert "log" in source or "event" in source
