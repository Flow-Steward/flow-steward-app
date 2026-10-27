"""Unit tests for the workload signal service and endpoint."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.application.jobs.workload_service import _classify, get_workload_signal

# ---------------------------------------------------------------------------
# Classification logic
# ---------------------------------------------------------------------------


class TestClassify:
    def test_normal_when_below_all_thresholds(self):
        assert _classify(2, 5) == "normal"

    def test_normal_when_metrics_unavailable(self):
        assert _classify(-1, -1) == "normal"

    def test_throttle_on_active_jobs(self):
        assert _classify(5, 0) == "throttle"

    def test_throttle_on_queue_depth(self):
        assert _classify(0, 10) == "throttle"

    def test_shed_on_active_jobs(self):
        assert _classify(10, 0) == "shed"

    def test_shed_on_queue_depth(self):
        assert _classify(0, 50) == "shed"

    def test_pause_on_active_jobs(self):
        assert _classify(20, 0) == "pause"

    def test_pause_on_queue_depth(self):
        assert _classify(0, 100) == "pause"

    def test_highest_severity_wins(self):
        # active_jobs at pause level, queue at normal
        assert _classify(25, 3) == "pause"

    def test_zero_zero_is_normal(self):
        assert _classify(0, 0) == "normal"


# ---------------------------------------------------------------------------
# Service function
# ---------------------------------------------------------------------------


class TestGetWorkloadSignal:
    def _mock_bl(self, active_count: int = 2, db_conns: int = 5):
        bl = MagicMock()
        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        bl.conn.cursor.return_value = cursor

        call_count = 0

        def fake_fetchone():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return (active_count,)
            return (db_conns,)

        cursor.fetchone.side_effect = fake_fetchone
        return bl

    def test_normal_signal(self):
        bl = self._mock_bl(active_count=1)
        result = get_workload_signal(bl, get_ready_queue_depth_fn=lambda: 2)
        assert result["recommendation"] == "normal"
        assert result["metrics"]["active_jobs"] == 1
        assert result["metrics"]["queue_depth"] == 2
        assert result["metrics"]["db_connections"] == 5
        assert "timestamp" in result

    def test_throttle_signal(self):
        bl = self._mock_bl(active_count=7)
        result = get_workload_signal(bl, get_ready_queue_depth_fn=lambda: 3)
        assert result["recommendation"] == "throttle"

    def test_shed_signal(self):
        bl = self._mock_bl(active_count=15)
        result = get_workload_signal(bl, get_ready_queue_depth_fn=lambda: 0)
        assert result["recommendation"] == "shed"

    def test_pause_signal(self):
        bl = self._mock_bl(active_count=25)
        result = get_workload_signal(bl, get_ready_queue_depth_fn=lambda: 0)
        assert result["recommendation"] == "pause"

    def test_graceful_when_queue_unavailable(self):
        bl = self._mock_bl(active_count=1)
        result = get_workload_signal(bl, get_ready_queue_depth_fn=None)
        assert result["recommendation"] == "normal"
        assert result["metrics"]["queue_depth"] == -1

    def test_graceful_when_queue_raises(self):
        bl = self._mock_bl(active_count=1)

        def broken():
            raise ConnectionError("RabbitMQ down")

        result = get_workload_signal(bl, get_ready_queue_depth_fn=broken)
        assert result["recommendation"] == "normal"
        assert result["metrics"]["queue_depth"] == -1

    def test_includes_thresholds(self):
        bl = self._mock_bl()
        result = get_workload_signal(bl)
        assert "thresholds" in result
        assert "throttle" in result["thresholds"]
        assert "shed" in result["thresholds"]
        assert "pause" in result["thresholds"]


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


class TestWorkloadEndpoint:
    def test_route_registered(self):
        try:
            from tests import api_runtime_test_surface as api_runtime
        except Exception:
            pytest.skip("api_runtime not importable")

        routes = [r.path for r in api_runtime.app.routes if hasattr(r, "path")]
        assert "/api/workload" in routes

    def test_returns_json_with_recommendation(self):
        try:
            from fastapi.testclient import TestClient

            import core.entrypoints.admin.deps as route_deps
            from core.entrypoints.api.app import create_app
        except Exception as e:
            pytest.skip(f"not available: {e}")

        mock_bl = MagicMock()
        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        cursor.fetchone.return_value = (3,)
        mock_bl.conn.cursor.return_value = cursor

        current_app = create_app()
        current_app.state.local_auth_enabled_override = lambda: False
        current_app.state.external_auth_enabled_override = lambda: False
        with (
            patch.object(route_deps, "_get_logger_only_fn", return_value=mock_bl),
            patch.object(route_deps, "_get_ready_queue_depth_fn", return_value=2),
        ):
            client = TestClient(current_app, raise_server_exceptions=False)
            resp = client.get("/api/workload")

        assert resp.status_code == 200
        body = resp.json()
        assert body["recommendation"] in ("normal", "throttle", "shed", "pause")
        assert "metrics" in body
        assert "timestamp" in body

    def test_viewer_cannot_read_global_capacity_metrics(self):
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient

            from core.entrypoints.api.workload import router
        except Exception as e:
            pytest.skip(f"not available: {e}")

        app = FastAPI()

        @app.middleware("http")
        async def _viewer_user(request, call_next):
            request.state.auth_user = {
                "id": 31,
                "username": "viewer",
                "display_name": "Viewer",
                "is_active": True,
                "roles": ["viewer"],
            }
            return await call_next(request)

        app.include_router(router)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/workload")

        assert resp.status_code == 403
        assert "operator role required" in resp.text.lower()

    def test_returns_503_when_no_db(self):
        try:
            from fastapi.testclient import TestClient

            import core.entrypoints.admin.deps as route_deps
            from core.entrypoints.api.app import create_app
        except Exception as e:
            pytest.skip(f"not available: {e}")

        current_app = create_app()
        current_app.state.local_auth_enabled_override = lambda: False
        current_app.state.external_auth_enabled_override = lambda: False
        with patch.object(route_deps, "_get_logger_only_fn", return_value=None):
            client = TestClient(current_app, raise_server_exceptions=False)
            resp = client.get("/api/workload")

        assert resp.status_code == 503
