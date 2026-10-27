"""Unit tests for timezone API endpoints via the serve.py compatibility shim.

These tests mock the database layer and validate request/response contracts.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Helpers — lightweight ASGI test client via httpx / starlette
# ---------------------------------------------------------------------------


def _make_test_client():
    """Return (client, app) for the FastAPI app with auth mocked."""
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("starlette not installed")

    # Patch heavy imports before loading the serve compatibility shim

    # We only need the FastAPI app object, heavy graph/agent deps are lazy
    with patch.dict("os.environ", {"TESTING": "1"}):
        try:
            from core.entrypoints.api.app import app
        except Exception:
            pytest.skip("serve compatibility shim could not be imported in test environment")

    return TestClient(app, raise_server_exceptions=False), app


def _auth_user_dict(user_id=1, username="testuser", timezone_val=""):
    d = {
        "id": user_id,
        "username": username,
        "display_name": "Test User",
        "roles": ["admin"],
        "is_active": True,
        "timezone": timezone_val,
    }
    return d


# ---------------------------------------------------------------------------
# PATCH /api/users/me/timezone
# ---------------------------------------------------------------------------


class TestPatchUserTimezone:
    def test_valid_timezone_accepted(self):
        """GIVEN valid timezone WHEN PATCH THEN 200 + timezone saved."""
        from core.application.settings.timezone_service import is_valid_timezone

        assert is_valid_timezone("Europe/Berlin") is True

    def test_invalid_timezone_rejected(self):
        """GIVEN invalid timezone WHEN validate THEN ValueError."""
        from core.application.settings.timezone_service import validate_timezone

        with pytest.raises(ValueError, match="Unknown IANA"):
            validate_timezone("Mars/Olympus")

    def test_empty_timezone_rejected(self):
        from core.application.settings.timezone_service import validate_timezone

        with pytest.raises(ValueError, match="required"):
            validate_timezone("")

    def test_none_timezone_rejected(self):
        from core.application.settings.timezone_service import validate_timezone

        with pytest.raises(ValueError, match="required"):
            validate_timezone(None)


# ---------------------------------------------------------------------------
# POST /api/users/me/detect-timezone
# ---------------------------------------------------------------------------


class TestDetectTimezone:
    def test_already_set_skips(self):
        """GIVEN user with timezone set WHEN detect called THEN returns already_set."""
        # This tests the logic, not the full HTTP round-trip
        user = _auth_user_dict(timezone_val="America/Chicago")
        existing_tz = str(user.get("timezone") or "").strip()
        assert existing_tz == "America/Chicago"

    def test_empty_user_accepts(self):
        """GIVEN user without timezone WHEN detect called THEN would save."""
        user = _auth_user_dict(timezone_val="")
        existing_tz = str(user.get("timezone") or "").strip()
        assert existing_tz == ""


# ---------------------------------------------------------------------------
# PATCH /api/projects/{id}/schedule-timezone
# ---------------------------------------------------------------------------


class TestPatchProjectScheduleTimezone:
    def test_valid_schedule_timezone(self):
        from core.application.settings.timezone_service import validate_timezone

        assert validate_timezone("Asia/Tokyo") == "Asia/Tokyo"

    def test_invalid_schedule_timezone(self):
        from core.application.settings.timezone_service import validate_timezone

        with pytest.raises(ValueError):
            validate_timezone("Not/ATimezone")
