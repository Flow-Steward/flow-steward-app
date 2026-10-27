"""
Extended unit tests for auth endpoints and extension API in the shared HTTP runtime.

Covers: bootstrap, login, logout, /me, change-password, password-reset flow,
user management (list/create/update/unlock/force-password-reset),
and extension endpoints (list, scan, enable, health, compiled-manifest).

Run:
    docker compose exec fs-app python -m pytest tests/unit/test_api_auth_extended.py -v
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from tests.unit._sql_debug import composable_to_debug_sql

_USER_2 = "00000000-0000-0000-0000-000000000002"
_MISSING_USER = "00000000-0000-0000-0000-000000000999"

# Some endpoint code paths request RealDictCursor via psycopg_runtime.extras.
# Ensure psycopg_runtime is importable (even as a mock) for those paths.
try:
    from core.infrastructure.db import psycopg_runtime as psycopg_runtime
except ImportError:
    _mock_psycopg_runtime = MagicMock()
    _mock_psycopg_runtime.extras.RealDictCursor = "RealDictCursor"
    sys.modules.setdefault("psycopg_runtime", _mock_psycopg_runtime)
    sys.modules.setdefault("psycopg_runtime.extras", _mock_psycopg_runtime.extras)

_OPTIONAL_TEST_DEPS = {
    "fastapi",
    "starlette",
    "pydantic",
    "jinja2",
    "psycopg_runtime",
    "langgraph",
    "langchain_core",
}


def _skip_only_missing_optional_dep(exc: Exception, *, context: str) -> None:
    if isinstance(exc, ModuleNotFoundError) and getattr(exc, "name", "") in _OPTIONAL_TEST_DEPS:
        pytest.skip(f"{context}: missing optional dependency '{exc.name}'")
    raise exc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_password_hash(password: str) -> str:
    """Generate a PBKDF2-SHA256 password hash matching api_runtime._password_hash."""
    salt = secrets.token_hex(16)
    iterations = 600_000
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt),
        iterations,
    ).hex()
    return f"pbkdf2_sha256${iterations}${salt}${digest}"


def _make_mock_cursor(rows=None, fetchone_val=None, rowcount=0):
    """Create a mock cursor context manager."""
    cur = MagicMock()
    cur.fetchall.return_value = rows or []
    cur.fetchone.return_value = fetchone_val
    cur.rowcount = rowcount
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    return cur


def _make_mock_bl(*, cursor_factory_rows=None, cursor_fetchone=None, rowcount=0):
    """Create a mock Flow Steward logger with a working conn.cursor()."""
    bl = MagicMock()
    cur = _make_mock_cursor(
        rows=cursor_factory_rows,
        fetchone_val=cursor_fetchone,
        rowcount=rowcount,
    )
    bl.conn.cursor.return_value = cur
    bl._table_exists.return_value = True
    return bl


def _patch_app_db(test_client, bl):
    return patch.dict(
        test_client.app.state.runtime_container.service_factories,
        {"db": lambda: bl},
    )


def _make_admin_user():
    return {
        "id": 1,
        "username": "admin@example.com",
        "email": "admin@example.com",
        "display_name": "Admin User",
        "roles": ["admin"],
        "is_active": True,
        "password_hash": _make_password_hash("oldpassword1"),
        "must_change_password": False,
        "account_id": "acc_default",
        "account_ids": ["acc_default"],
        "account_memberships": [
            {
                "account_id": "acc_default",
                "display_name": "Default Account",
                "is_active": True,
                "is_default": True,
            }
        ],
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def serve_module():
    """Return the production route-binding module used by endpoint closures."""
    try:
        from core.entrypoints.api import route_registration as api_runtime

        return api_runtime
    except Exception as e:
        _skip_only_missing_optional_dep(e, context="Could not import api_runtime")


@pytest.fixture(scope="module")
def client(serve_module):
    """TestClient with auth disabled — for extension endpoints."""
    try:
        from fastapi.testclient import TestClient

        from core.entrypoints.api.app_factory import create_app

        with patch.dict(
            os.environ,
            {"FS_LOCAL_AUTH_ENABLED": "0", "FS_AUTH_ALLOW_DISABLED": "1"},
        ):
            yield TestClient(create_app(), raise_server_exceptions=False)
    except Exception as e:
        _skip_only_missing_optional_dep(e, context="Could not create TestClient")


@pytest.fixture(scope="module")
def auth_client(serve_module):
    """TestClient with local auth enabled — for auth endpoint tests."""
    try:
        from fastapi.testclient import TestClient

        from core.entrypoints.api.app_factory import create_app

        with patch.dict(os.environ, {"FS_LOCAL_AUTH_ENABLED": "1"}):
            yield TestClient(create_app(), raise_server_exceptions=False)
    except Exception as e:
        _skip_only_missing_optional_dep(e, context="Could not create auth TestClient")


# ═══════════════════════════════════════════════════════════════════════════
# TestAuthBootstrap
# ═══════════════════════════════════════════════════════════════════════════


class TestAuthBootstrap:
    def test_bootstrap_creates_first_user(self, auth_client, serve_module):
        bl = _make_mock_bl(cursor_fetchone=(1, "admin", "Admin"))
        # Fetch user count, inserted user, admin role, then the active default account.
        call_count = {"n": 0}

        def _fetchone_side_effect():
            call_count["n"] += 1
            if call_count["n"] == 1:
                return (0,)  # no existing users
            if call_count["n"] == 2:
                return (1, "admin", "Admin")  # inserted user
            if call_count["n"] == 3:
                return (1,)  # admin role id
            if call_count["n"] == 4:
                return ("00000000-0000-0000-0000-000000000002",)  # default account id
            return None

        cur = bl.conn.cursor.return_value.__enter__.return_value
        cur.fetchone.side_effect = _fetchone_side_effect

        with (
            _patch_app_db(auth_client, bl),
            patch(
                "core.entrypoints.api.route_registration._auth_tables_available", return_value=True
            ),
            patch(
                "core.entrypoints.api.route_registration._bootstrap_request_allowed",
                return_value=(True, ""),
            ),
            patch("core.entrypoints.api.route_registration._seed_default_roles"),
            patch("core.entrypoints.api.route_registration._log_auth_audit_event"),
        ):
            r = auth_client.post(
                "/api/auth/bootstrap",
                json={
                    "email": "admin@example.com",
                    "password": "Str0ngP@ssword!",
                    "display_name": "Admin",
                },
            )
        assert r.status_code == 200
        data = r.json()
        assert data.get("status") == "bootstrapped"
        assert data.get("user", {}).get("username") == "admin@example.com"

    def test_bootstrap_rejects_when_user_exists(self, auth_client, serve_module):
        with (
            _patch_app_db(auth_client, _make_mock_bl()),
            patch(
                "core.entrypoints.api.route_registration._auth_tables_available", return_value=True
            ),
            patch(
                "core.entrypoints.api.route_registration._bootstrap_request_allowed",
                return_value=(False, "Bootstrap already completed"),
            ),
            patch("core.entrypoints.api.route_registration._log_auth_audit_event"),
        ):
            r = auth_client.post(
                "/api/auth/bootstrap",
                json={
                    "email": "admin@example.com",
                    "password": "Str0ngP@ssword!",
                },
            )
        assert r.status_code == 403
        assert (
            "already" in r.json().get("error", "").lower()
            or "bootstrap" in r.json().get("error", "").lower()
        )

    def test_bootstrap_short_password_rejected(self, auth_client, serve_module):
        with (
            _patch_app_db(auth_client, _make_mock_bl()),
            patch(
                "core.entrypoints.api.route_registration._auth_tables_available", return_value=True
            ),
            patch(
                "core.entrypoints.api.route_registration._bootstrap_request_allowed",
                return_value=(True, ""),
            ),
        ):
            r = auth_client.post(
                "/api/auth/bootstrap",
                json={
                    "email": "admin@example.com",
                    "password": "short",  # < 10 chars
                },
            )
        assert r.status_code == 400
        assert "password" in r.json().get("error", "").lower()

    def test_bootstrap_missing_email_returns_error(self, auth_client, serve_module):
        with (
            _patch_app_db(auth_client, _make_mock_bl()),
            patch(
                "core.entrypoints.api.route_registration._auth_tables_available", return_value=True
            ),
            patch(
                "core.entrypoints.api.route_registration._bootstrap_request_allowed",
                return_value=(True, ""),
            ),
        ):
            r = auth_client.post(
                "/api/auth/bootstrap",
                json={
                    "email": "",
                    "password": "Str0ngP@ssword!",
                },
            )
        assert r.status_code == 400
        assert "email" in r.json().get("error", "").lower()

    def test_bootstrap_already_bootstrapped_returns_409(self, auth_client, serve_module):
        """If users already exist in DB, return 409."""
        bl = _make_mock_bl()
        cur = bl.conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (1,)  # 1 existing user

        with (
            _patch_app_db(auth_client, bl),
            patch(
                "core.entrypoints.api.route_registration._auth_tables_available", return_value=True
            ),
            patch(
                "core.entrypoints.api.route_registration._bootstrap_request_allowed",
                return_value=(True, ""),
            ),
            patch("core.entrypoints.api.route_registration._seed_default_roles"),
            patch("core.entrypoints.api.route_registration._log_auth_audit_event"),
        ):
            r = auth_client.post(
                "/api/auth/bootstrap",
                json={
                    "email": "admin@example.com",
                    "password": "Str0ngP@ssword!",
                },
            )
        assert r.status_code == 409


# ═══════════════════════════════════════════════════════════════════════════
# TestAuthLogin
# ═══════════════════════════════════════════════════════════════════════════


class TestAuthLogin:
    def test_ip_limiter_does_not_block_valid_unrelated_login(self, auth_client, serve_module):
        from core.application.auth import rate_limit
        from core.application.auth.config import AUTH_LOGIN_MAX_ATTEMPTS

        proxy_ip = "10.0.0.25"
        rate_limit._auth_login_attempts.clear()
        for _ in range(AUTH_LOGIN_MAX_ATTEMPTS):
            rate_limit.auth_login_record_failure("junk@example.invalid", proxy_ip)

        user = _make_admin_user()
        with (
            _patch_app_db(auth_client, _make_mock_bl()),
            patch(
                "core.entrypoints.api.route_registration._auth_tables_available",
                return_value=True,
            ),
            patch(
                "core.entrypoints.api.route_registration._request_client_ip",
                return_value=proxy_ip,
            ),
            patch(
                "core.entrypoints.api.route_registration._auth_login_lock_state",
                side_effect=rate_limit.auth_login_lock_state,
            ),
            patch(
                "core.entrypoints.api.route_registration._get_user_by_username", return_value=user
            ),
            patch("core.entrypoints.api.route_registration._verify_password", return_value=True),
            patch("core.entrypoints.api.route_registration._auth_login_clear_failures"),
            patch("core.entrypoints.api.route_registration._log_auth_audit_event"),
        ):
            response = auth_client.post(
                "/api/auth/login",
                json={"email": "admin@example.com", "password": "valid-password"},
            )

        assert response.status_code == 200

    def test_login_invalid_credentials(self, auth_client, serve_module):
        user = _make_admin_user()
        with (
            _patch_app_db(auth_client, _make_mock_bl()),
            patch(
                "core.entrypoints.api.route_registration._auth_tables_available", return_value=True
            ),
            patch(
                "core.entrypoints.api.route_registration._auth_login_lock_state",
                return_value=(False, 0),
            ),
            patch(
                "core.entrypoints.api.route_registration._get_user_by_username", return_value=user
            ),
            patch("core.entrypoints.api.route_registration._verify_password", return_value=False),
            patch(
                "core.entrypoints.api.route_registration._auth_login_record_failure",
                return_value=(False, 0),
            ),
            patch("core.entrypoints.api.route_registration._log_auth_audit_event"),
        ):
            r = auth_client.post(
                "/api/auth/login",
                json={
                    "email": "admin@example.com",
                    "password": "wrongpassword1",
                },
            )
        assert r.status_code == 401
        assert (
            "invalid" in r.json().get("error", "").lower()
            or "credentials" in r.json().get("error", "").lower()
        )

    def test_login_nonexistent_user(self, auth_client, serve_module):
        with (
            _patch_app_db(auth_client, _make_mock_bl()),
            patch(
                "core.entrypoints.api.route_registration._auth_tables_available", return_value=True
            ),
            patch(
                "core.entrypoints.api.route_registration._auth_login_lock_state",
                return_value=(False, 0),
            ),
            patch(
                "core.entrypoints.api.route_registration._get_user_by_username", return_value=None
            ),
            patch(
                "core.entrypoints.api.route_registration._auth_login_record_failure",
                return_value=(False, 0),
            ),
            patch("core.entrypoints.api.route_registration._log_auth_audit_event"),
        ):
            r = auth_client.post(
                "/api/auth/login",
                json={
                    "email": "nobody@example.com",
                    "password": "doesntmatter1",
                },
            )
        assert r.status_code == 401

    def test_login_temporary_password_requires_password_change(self, auth_client, serve_module):
        """User with must_change_password=True gets 403 until a new password is sent."""
        user = _make_admin_user()
        user["must_change_password"] = True
        with (
            _patch_app_db(auth_client, _make_mock_bl()),
            patch(
                "core.entrypoints.api.route_registration._auth_tables_available", return_value=True
            ),
            patch(
                "core.entrypoints.api.route_registration._auth_login_lock_state",
                return_value=(False, 0),
            ),
            patch(
                "core.entrypoints.api.route_registration._get_user_by_username", return_value=user
            ),
            patch("core.entrypoints.api.route_registration._verify_password", return_value=True),
            patch("core.entrypoints.api.route_registration._auth_login_clear_failures"),
            patch("core.entrypoints.api.route_registration._log_auth_audit_event"),
        ):
            r = auth_client.post(
                "/api/auth/login",
                json={
                    "email": "admin@example.com",
                    "password": "oldpassword1",
                },
            )
        assert r.status_code == 403
        data = r.json()
        assert data.get("password_change_required") is True
        assert "password_reset_required" not in data

    def test_login_rate_limited(self, auth_client, serve_module):
        with (
            _patch_app_db(auth_client, _make_mock_bl()),
            patch(
                "core.entrypoints.api.route_registration._auth_tables_available", return_value=True
            ),
            patch(
                "core.entrypoints.api.route_registration._auth_login_lock_state",
                return_value=(True, 300),
            ),
            patch("core.entrypoints.api.route_registration._log_auth_audit_event"),
        ):
            r = auth_client.post(
                "/api/auth/login",
                json={
                    "email": "admin@example.com",
                    "password": "doesntmatter1",
                },
            )
        assert r.status_code == 429
        assert "retry_after_seconds" in r.json()
        assert r.headers["Retry-After"] == "300"

    def test_login_sets_session_cookie(self, auth_client, serve_module):
        user = _make_admin_user()
        bl = _make_mock_bl()
        with (
            _patch_app_db(auth_client, bl),
            patch(
                "core.entrypoints.api.route_registration._auth_tables_available", return_value=True
            ),
            patch(
                "core.entrypoints.api.route_registration._auth_login_lock_state",
                return_value=(False, 0),
            ),
            patch(
                "core.entrypoints.api.route_registration._get_user_by_username", return_value=user
            ),
            patch("core.entrypoints.api.route_registration._verify_password", return_value=True),
            patch("core.entrypoints.api.route_registration._auth_login_clear_failures"),
            patch("core.entrypoints.api.route_registration._log_auth_audit_event"),
        ):
            r = auth_client.post(
                "/api/auth/login",
                json={
                    "email": "admin@example.com",
                    "password": "oldpassword1",
                },
            )
        assert r.status_code == 200
        data = r.json()
        assert data.get("user", {}).get("username") == "admin@example.com"
        # Session cookie should be set
        assert "fs_session" in r.cookies or data.get("csrf_token")

    def test_login_disabled_user_returns_401(self, auth_client, serve_module):
        user = _make_admin_user()
        user["is_active"] = False
        with (
            _patch_app_db(auth_client, _make_mock_bl()),
            patch(
                "core.entrypoints.api.route_registration._auth_tables_available", return_value=True
            ),
            patch(
                "core.entrypoints.api.route_registration._auth_login_lock_state",
                return_value=(False, 0),
            ),
            patch(
                "core.entrypoints.api.route_registration._get_user_by_username", return_value=user
            ),
            patch(
                "core.entrypoints.api.route_registration._auth_login_record_failure",
                return_value=(False, 0),
            ),
            patch("core.entrypoints.api.route_registration._log_auth_audit_event"),
        ):
            r = auth_client.post(
                "/api/auth/login",
                json={
                    "email": "admin@example.com",
                    "password": "oldpassword1",
                },
            )
        assert r.status_code == 401


# ═══════════════════════════════════════════════════════════════════════════
# TestAuthLogout
# ═══════════════════════════════════════════════════════════════════════════


class TestAuthLogout:
    def test_logout_returns_logged_out(self, auth_client, serve_module):
        with (
            _patch_app_db(auth_client, _make_mock_bl()),
            patch(
                "core.entrypoints.api.route_registration._auth_tables_available", return_value=True
            ),
            patch("core.entrypoints.api.route_registration._csrf_request_valid", return_value=True),
            patch("core.entrypoints.api.route_registration._log_auth_audit_event"),
        ):
            r = auth_client.post("/api/auth/logout")
        assert r.status_code == 200
        assert r.json().get("status") == "logged_out"

    def test_logout_without_auth_still_works(self, auth_client, serve_module):
        """Logout without a session cookie should still return 200."""
        with (
            _patch_app_db(auth_client, None),
            patch("core.entrypoints.api.route_registration._csrf_request_valid", return_value=True),
            patch("core.entrypoints.api.route_registration._log_auth_audit_event"),
        ):
            r = auth_client.post("/api/auth/logout")
        assert r.status_code == 200
        assert r.json().get("status") == "logged_out"


# ═══════════════════════════════════════════════════════════════════════════
# TestAuthMe
# ═══════════════════════════════════════════════════════════════════════════


class TestAuthMe:
    def test_me_without_auth_returns_401(self, auth_client, serve_module):
        """When auth is enabled but no session, middleware redirects or returns 401."""
        bl = _make_mock_bl()
        with (
            _patch_app_db(auth_client, bl),
            patch(
                "core.entrypoints.api.route_registration._auth_tables_available", return_value=True
            ),
            patch(
                "core.entrypoints.api.route_registration._get_user_by_session_token",
                return_value=None,
            ),
            patch(
                "core.entrypoints.api.route_registration._local_auth_bootstrap_required",
                return_value=False,
            ),
        ):
            r = auth_client.get("/api/auth/me")
        # Middleware returns 401 for unauthenticated API requests
        assert r.status_code == 401

    def test_me_with_auth_returns_user_info(self, auth_client, serve_module):
        """When middleware sets auth_user, /me returns user info."""
        user = _make_admin_user()
        bl = _make_mock_bl()
        with (
            _patch_app_db(auth_client, bl),
            patch(
                "core.entrypoints.api.route_registration._auth_tables_available", return_value=True
            ),
            patch(
                "core.entrypoints.api.route_registration._get_user_by_session_token",
                return_value=user,
            ),
            patch("core.entrypoints.api.route_registration._csrf_request_valid", return_value=True),
        ):
            auth_client.cookies.set("fs_session", "fake-session-token")
            r = auth_client.get("/api/auth/me")
        assert r.status_code == 200
        data = r.json()
        assert data.get("authenticated") is True
        assert data.get("user", {}).get("username") == "admin@example.com"


# ═══════════════════════════════════════════════════════════════════════════
# TestPasswordChange
# ═══════════════════════════════════════════════════════════════════════════


class TestPasswordChange:
    def test_change_password_success(self, auth_client, serve_module):
        user = _make_admin_user()
        bl = _make_mock_bl()
        with (
            _patch_app_db(auth_client, bl),
            patch(
                "core.entrypoints.api.route_registration._auth_tables_available", return_value=True
            ),
            patch(
                "core.entrypoints.api.route_registration._get_user_by_session_token",
                return_value=user,
            ),
            patch("core.entrypoints.api.route_registration._csrf_request_valid", return_value=True),
            patch(
                "core.entrypoints.api.route_registration._get_user_by_username", return_value=user
            ),
            patch("core.entrypoints.api.route_registration._verify_password", return_value=True),
            patch(
                "core.entrypoints.api.route_registration._auth_user_column_exists",
                return_value=True,
            ),
            patch("core.entrypoints.api.route_registration._log_auth_audit_event"),
        ):
            auth_client.cookies.set("fs_session", "fake-token")
            r = auth_client.post(
                "/api/auth/change-password",
                json={"current_password": "oldpassword1", "new_password": "NewStr0ngP@ss!"},
            )
        assert r.status_code == 200
        assert r.json().get("status") == "password_updated"

    def test_change_password_wrong_current(self, auth_client, serve_module):
        user = _make_admin_user()
        bl = _make_mock_bl()
        with (
            _patch_app_db(auth_client, bl),
            patch(
                "core.entrypoints.api.route_registration._auth_tables_available", return_value=True
            ),
            patch(
                "core.entrypoints.api.route_registration._get_user_by_session_token",
                return_value=user,
            ),
            patch("core.entrypoints.api.route_registration._csrf_request_valid", return_value=True),
            patch(
                "core.entrypoints.api.route_registration._get_user_by_username", return_value=user
            ),
            patch("core.entrypoints.api.route_registration._verify_password", return_value=False),
            patch("core.entrypoints.api.route_registration._log_auth_audit_event"),
        ):
            auth_client.cookies.set("fs_session", "fake-token")
            r = auth_client.post(
                "/api/auth/change-password",
                json={"current_password": "wrongpassword", "new_password": "NewStr0ngP@ss!"},
            )
        assert r.status_code == 401
        assert (
            "current password" in r.json().get("error", "").lower()
            or "invalid" in r.json().get("error", "").lower()
        )

    def test_change_password_short_new_password(self, auth_client, serve_module):
        user = _make_admin_user()
        bl = _make_mock_bl()
        with (
            _patch_app_db(auth_client, bl),
            patch(
                "core.entrypoints.api.route_registration._auth_tables_available", return_value=True
            ),
            patch(
                "core.entrypoints.api.route_registration._get_user_by_session_token",
                return_value=user,
            ),
            patch("core.entrypoints.api.route_registration._csrf_request_valid", return_value=True),
        ):
            auth_client.cookies.set("fs_session", "fake-token")
            r = auth_client.post(
                "/api/auth/change-password",
                json={"current_password": "oldpassword1", "new_password": "short"},
            )
        assert r.status_code == 400
        assert "password" in r.json().get("error", "").lower()

    def test_change_password_unauthenticated(self, auth_client, serve_module):
        bl = _make_mock_bl()
        with (
            _patch_app_db(auth_client, bl),
            patch(
                "core.entrypoints.api.route_registration._auth_tables_available", return_value=True
            ),
            patch(
                "core.entrypoints.api.route_registration._get_user_by_session_token",
                return_value=None,
            ),
            patch(
                "core.entrypoints.api.route_registration._local_auth_bootstrap_required",
                return_value=False,
            ),
        ):
            r = auth_client.post(
                "/api/auth/change-password",
                json={"current_password": "old", "new_password": "NewStr0ngP@ss!"},
            )
        assert r.status_code == 401


# ═══════════════════════════════════════════════════════════════════════════
# TestPasswordReset
# ═══════════════════════════════════════════════════════════════════════════


class TestServeAuthLookupQueries:
    def test_get_user_by_id_filters_deleted_users_when_supported(self, serve_module):
        bl = _make_mock_bl()
        cur = MagicMock()
        cur.fetchone.return_value = {
            "id": 1,
            "username": "admin",
            "display_name": "Admin",
            "password_hash": "hash",
            "is_active": True,
            "created_at": None,
            "updated_at": None,
            "must_change_password": False,
        }
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        bl.conn.cursor.return_value = cur

        with (
            patch(
                "core.entrypoints.api.route_registration._auth_user_column_exists",
                side_effect=lambda _bl, column: column in {"deleted_at", "must_change_password"},
            ),
            patch(
                "core.entrypoints.api.route_registration._get_user_roles", return_value=["admin"]
            ),
        ):
            user = serve_module._get_user_by_id(bl, 1)

        assert user is not None
        sql = composable_to_debug_sql(cur.execute.call_args_list[0].args[0])
        assert "u.deleted_at IS NULL" in sql

    def test_get_user_by_session_token_filters_deleted_users_when_supported(self, serve_module):
        bl = _make_mock_bl()
        cur = MagicMock()
        cur.fetchone.return_value = {
            "id": 1,
            "username": "admin",
            "display_name": "Admin",
            "is_active": True,
            "must_change_password": False,
            "account_id": "acc_default",
            "session_id": 17,
            "expires_at": datetime.now(UTC) + timedelta(hours=1),
        }
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        bl.conn.cursor.return_value = cur

        with (
            patch(
                "core.entrypoints.api.route_registration._auth_user_column_exists",
                side_effect=lambda _bl, column: (
                    column in {"deleted_at", "must_change_password", "account_id"}
                ),
            ),
            patch(
                "core.entrypoints.api.route_registration._get_user_roles", return_value=["admin"]
            ),
        ):
            user = serve_module._get_user_by_session_token(bl, "session-token")

        assert user is not None
        first_sql = composable_to_debug_sql(cur.execute.call_args_list[0].args[0])
        assert "u.deleted_at IS NULL" in first_sql

    def test_get_user_by_session_token_retries_without_prepare_on_duplicate_statement(
        self, serve_module
    ):
        bl = _make_mock_bl()
        cur = MagicMock()
        cur.fetchone.return_value = {
            "id": 1,
            "username": "admin",
            "display_name": "Admin",
            "is_active": True,
            "must_change_password": False,
            "account_id": "acc_default",
            "session_id": 17,
            "expires_at": datetime.now(UTC) + timedelta(hours=1),
        }
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        bl.conn.cursor.return_value = cur

        execute_attempt = {"count": 0}

        def _execute(*args, **kwargs):
            execute_attempt["count"] += 1
            if execute_attempt["count"] in {1, 3} and kwargs.get("prepare", True):
                raise psycopg.errors.DuplicatePreparedStatement("duplicate prepared statement")
            return

        cur.execute.side_effect = _execute

        with (
            patch(
                "core.entrypoints.api.route_registration._auth_user_column_exists",
                side_effect=lambda _bl, column: (
                    column in {"deleted_at", "must_change_password", "account_id"}
                ),
            ),
            patch(
                "core.entrypoints.api.route_registration._get_user_roles", return_value=["admin"]
            ),
        ):
            user = serve_module._get_user_by_session_token(bl, "session-token")

        assert user is not None
        assert cur.execute.call_args_list[1].kwargs.get("prepare") is False
        assert cur.execute.call_args_list[3].kwargs.get("prepare") is False


class TestPasswordReset:
    def test_debug_reveal_token_disabled_in_production(self, monkeypatch, serve_module):
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.setenv("FS_AUTH_PASSWORD_RESET_DEBUG_REVEAL_TOKEN", "1")
        monkeypatch.delenv("FS_STACK_ENVIRONMENT", raising=False)

        assert serve_module._password_reset_debug_reveal_token() is False

    def test_request_creates_token(self, auth_client, serve_module):
        user = _make_admin_user()
        bl = _make_mock_bl()
        with (
            _patch_app_db(auth_client, bl),
            patch(
                "core.entrypoints.api.route_registration._auth_tables_available", return_value=True
            ),
            patch(
                "core.entrypoints.api.route_registration._auth_password_reset_tables_available",
                return_value=True,
            ),
            patch(
                "core.entrypoints.api.route_registration._password_reset_enabled", return_value=True
            ),
            patch(
                "core.entrypoints.api.route_registration._password_reset_throttle_check",
                return_value=(False, 0),
            ),
            patch("core.entrypoints.api.route_registration._password_reset_throttle_record"),
            patch(
                "core.entrypoints.api.route_registration._get_user_by_username", return_value=user
            ),
            patch(
                "core.entrypoints.api.route_registration._password_reset_debug_reveal_token",
                return_value=True,
            ),
            patch("core.entrypoints.api.route_registration._auth_login_clear_user_failures"),
            patch("core.entrypoints.api.route_registration._log_auth_audit_event"),
        ):
            r = auth_client.post(
                "/api/auth/password-reset/request",
                json={
                    "email": "admin@example.com",
                },
            )
        assert r.status_code == 200
        data = r.json()
        assert data.get("status") == "accepted"
        # Debug mode reveals token
        assert "reset_token" in data

    def test_request_legacy_forgot_password_alias_removed(self, auth_client, serve_module):
        r = auth_client.post("/api/auth/forgot-password", json={"email": "admin@example.com"})

        assert r.status_code == 404

    def test_request_nonexistent_user_still_accepted(self, auth_client, serve_module):
        """To prevent user enumeration, reset request for unknown user still returns accepted."""
        bl = _make_mock_bl()
        with (
            _patch_app_db(auth_client, bl),
            patch(
                "core.entrypoints.api.route_registration._auth_tables_available", return_value=True
            ),
            patch(
                "core.entrypoints.api.route_registration._auth_password_reset_tables_available",
                return_value=True,
            ),
            patch(
                "core.entrypoints.api.route_registration._password_reset_enabled", return_value=True
            ),
            patch(
                "core.entrypoints.api.route_registration._password_reset_throttle_check",
                return_value=(False, 0),
            ),
            patch("core.entrypoints.api.route_registration._password_reset_throttle_record"),
            patch(
                "core.entrypoints.api.route_registration._get_user_by_username", return_value=None
            ),
            patch("core.entrypoints.api.route_registration._log_auth_audit_event"),
        ):
            r = auth_client.post(
                "/api/auth/password-reset/request",
                json={
                    "email": "nobody@example.com",
                },
            )
        assert r.status_code == 200
        assert r.json().get("status") == "accepted"

    def test_verify_valid_token(self, auth_client, serve_module):
        bl = _make_mock_bl()
        mock_row = {
            "id": 1,
            "user_id": 1,
            "expires_at": datetime.now(UTC) + timedelta(hours=1),
            "used_at": None,
            "username": "admin@example.com",
            "is_active": True,
        }
        cur = MagicMock()
        cur.fetchone.return_value = mock_row
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        bl.conn.cursor.return_value = cur

        with (
            _patch_app_db(auth_client, bl),
            patch(
                "core.entrypoints.api.route_registration._auth_tables_available", return_value=True
            ),
            patch(
                "core.entrypoints.api.route_registration._auth_password_reset_tables_available",
                return_value=True,
            ),
            patch(
                "core.entrypoints.api.route_registration._password_reset_enabled", return_value=True
            ),
            patch(
                "core.entrypoints.api.route_registration._password_reset_throttle_check",
                return_value=(False, 0),
            ),
            patch("core.entrypoints.api.route_registration._password_reset_throttle_record"),
        ):
            r = auth_client.post(
                "/api/auth/password-reset/verify",
                json={
                    "token": "valid-reset-token",
                },
            )
        assert r.status_code == 200
        data = r.json()
        assert data.get("valid") is True
        # The endpoint is unauthenticated and takes only a token, so it must not
        # reveal which account the token belongs to. Anyone who obtains a reset
        # link — from browser history, a referer header, a forwarded message —
        # would otherwise learn the address behind it.
        assert "username" not in data
        assert "email" not in data
        assert "admin@example.com" not in r.text
        # Named-field assertions only catch the fields someone thought to name.
        # The response is a yes/no about one token, so pin the whole shape: any
        # new key here is a new thing an unauthenticated caller learns.
        assert set(data) == {"valid"}, data

    def test_verify_filters_deleted_users_when_supported(self, auth_client, serve_module):
        bl = _make_mock_bl()
        cur = MagicMock()
        cur.fetchone.return_value = {
            "id": 1,
            "user_id": 1,
            "expires_at": datetime.now(UTC) + timedelta(hours=1),
            "used_at": None,
            "username": "admin@example.com",
            "is_active": True,
        }
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        bl.conn.cursor.return_value = cur

        with (
            _patch_app_db(auth_client, bl),
            patch(
                "core.entrypoints.api.route_registration._auth_tables_available", return_value=True
            ),
            patch(
                "core.entrypoints.api.route_registration._auth_password_reset_tables_available",
                return_value=True,
            ),
            patch(
                "core.entrypoints.api.route_registration._password_reset_enabled", return_value=True
            ),
            patch(
                "core.entrypoints.api.route_registration._password_reset_throttle_check",
                return_value=(False, 0),
            ),
            patch("core.entrypoints.api.route_registration._password_reset_throttle_record"),
            patch(
                "core.entrypoints.api.route_registration._auth_user_column_exists",
                side_effect=lambda _bl, column: column == "deleted_at",
            ),
        ):
            r = auth_client.post(
                "/api/auth/password-reset/verify",
                json={
                    "token": "valid-reset-token",
                },
            )

        assert r.status_code == 200
        verify_sql = composable_to_debug_sql(cur.execute.call_args_list[0].args[0])
        assert "u.deleted_at IS NULL" in verify_sql

    def test_verify_invalid_token(self, auth_client, serve_module):
        bl = _make_mock_bl()
        cur = MagicMock()
        cur.fetchone.return_value = None
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        bl.conn.cursor.return_value = cur

        with (
            _patch_app_db(auth_client, bl),
            patch(
                "core.entrypoints.api.route_registration._auth_tables_available", return_value=True
            ),
            patch(
                "core.entrypoints.api.route_registration._auth_password_reset_tables_available",
                return_value=True,
            ),
            patch(
                "core.entrypoints.api.route_registration._password_reset_enabled", return_value=True
            ),
            patch(
                "core.entrypoints.api.route_registration._password_reset_throttle_check",
                return_value=(False, 0),
            ),
            patch("core.entrypoints.api.route_registration._password_reset_throttle_record"),
        ):
            r = auth_client.post(
                "/api/auth/password-reset/verify",
                json={
                    "token": "bogus-token",
                },
            )
        assert r.status_code == 200
        assert r.json().get("valid") is False

    def test_confirm_resets_password(self, auth_client, serve_module):
        bl = _make_mock_bl()
        mock_row = {
            "id": 1,
            "user_id": 1,
            "username": "admin@example.com",
            "is_active": True,
        }
        cur = MagicMock()
        cur.fetchone.return_value = mock_row
        cur.rowcount = 0
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        bl.conn.cursor.return_value = cur

        with (
            _patch_app_db(auth_client, bl),
            patch(
                "core.entrypoints.api.route_registration._auth_tables_available", return_value=True
            ),
            patch(
                "core.entrypoints.api.route_registration._auth_password_reset_tables_available",
                return_value=True,
            ),
            patch(
                "core.entrypoints.api.route_registration._password_reset_enabled", return_value=True
            ),
            patch(
                "core.entrypoints.api.route_registration._password_reset_throttle_check",
                return_value=(False, 0),
            ),
            patch("core.entrypoints.api.route_registration._password_reset_throttle_record"),
            patch(
                "core.entrypoints.api.route_registration._auth_user_column_exists",
                return_value=True,
            ),
            patch("core.entrypoints.api.route_registration._auth_login_clear_user_failures"),
            patch("core.entrypoints.api.route_registration._log_auth_audit_event"),
        ):
            r = auth_client.post(
                "/api/auth/password-reset/confirm",
                json={
                    "token": "valid-reset-token",
                    "new_password": "N3wStr0ngP@ss!!",
                },
            )
        assert r.status_code == 200
        assert r.json().get("status") == "password_reset_confirmed"

    def test_confirm_canonical_reset_password_rejects_legacy_password_field(
        self, auth_client, serve_module
    ):
        r = auth_client.post(
            "/api/auth/password-reset/confirm",
            json={
                "token": "valid-reset-token",
                "password": "N3wStr0ngP@ss!!",
            },
        )

        assert r.status_code == 422

    def test_confirm_legacy_reset_password_alias_removed(self, auth_client, serve_module):
        r = auth_client.post(
            "/api/auth/reset-password",
            json={
                "token": "valid-reset-token",
                "password": "N3wStr0ngP@ss!!",
            },
        )

        assert r.status_code == 404

    def test_confirm_filters_deleted_users_when_supported(self, auth_client, serve_module):
        bl = _make_mock_bl()
        cur = MagicMock()
        cur.fetchone.return_value = {
            "id": 1,
            "user_id": 1,
            "username": "admin@example.com",
            "is_active": True,
        }
        cur.rowcount = 0
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        bl.conn.cursor.return_value = cur

        with (
            _patch_app_db(auth_client, bl),
            patch(
                "core.entrypoints.api.route_registration._auth_tables_available", return_value=True
            ),
            patch(
                "core.entrypoints.api.route_registration._auth_password_reset_tables_available",
                return_value=True,
            ),
            patch(
                "core.entrypoints.api.route_registration._password_reset_enabled", return_value=True
            ),
            patch(
                "core.entrypoints.api.route_registration._password_reset_throttle_check",
                return_value=(False, 0),
            ),
            patch("core.entrypoints.api.route_registration._password_reset_throttle_record"),
            patch(
                "core.entrypoints.api.route_registration._auth_user_column_exists",
                side_effect=lambda _bl, column: column in {"deleted_at", "must_change_password"},
            ),
            patch("core.entrypoints.api.route_registration._auth_login_clear_user_failures"),
            patch("core.entrypoints.api.route_registration._log_auth_audit_event"),
        ):
            r = auth_client.post(
                "/api/auth/password-reset/confirm",
                json={
                    "token": "valid-reset-token",
                    "new_password": "N3wStr0ngP@ss!!",
                },
            )

        assert r.status_code == 200
        confirm_sql = composable_to_debug_sql(cur.execute.call_args_list[0].args[0])
        assert "u.deleted_at IS NULL" in confirm_sql

    def test_confirm_short_password_rejected(self, auth_client, serve_module):
        bl = _make_mock_bl()
        with (
            _patch_app_db(auth_client, bl),
            patch(
                "core.entrypoints.api.route_registration._auth_tables_available", return_value=True
            ),
            patch(
                "core.entrypoints.api.route_registration._auth_password_reset_tables_available",
                return_value=True,
            ),
            patch(
                "core.entrypoints.api.route_registration._password_reset_enabled", return_value=True
            ),
            patch(
                "core.entrypoints.api.route_registration._password_reset_throttle_check",
                return_value=(False, 0),
            ),
            patch("core.entrypoints.api.route_registration._password_reset_throttle_record"),
        ):
            r = auth_client.post(
                "/api/auth/password-reset/confirm",
                json={
                    "token": "some-token",
                    "new_password": "short",
                },
            )
        assert r.status_code == 400
        assert "password" in r.json().get("error", "").lower()

    def test_confirm_invalid_token_rejected(self, auth_client, serve_module):
        bl = _make_mock_bl()
        cur = MagicMock()
        cur.fetchone.return_value = None  # no matching reset row
        cur.rowcount = 0
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        bl.conn.cursor.return_value = cur

        with (
            _patch_app_db(auth_client, bl),
            patch(
                "core.entrypoints.api.route_registration._auth_tables_available", return_value=True
            ),
            patch(
                "core.entrypoints.api.route_registration._auth_password_reset_tables_available",
                return_value=True,
            ),
            patch(
                "core.entrypoints.api.route_registration._password_reset_enabled", return_value=True
            ),
            patch(
                "core.entrypoints.api.route_registration._password_reset_throttle_check",
                return_value=(False, 0),
            ),
            patch("core.entrypoints.api.route_registration._password_reset_throttle_record"),
            patch("core.entrypoints.api.route_registration._log_auth_audit_event"),
        ):
            r = auth_client.post(
                "/api/auth/password-reset/confirm",
                json={
                    "token": "expired-or-invalid-token",
                    "new_password": "N3wStr0ngP@ss!!",
                },
            )
        assert r.status_code == 400
        assert (
            "token" in r.json().get("error", "").lower()
            or "invalid" in r.json().get("error", "").lower()
        )


# ═══════════════════════════════════════════════════════════════════════════
# TestUserManagement
# ═══════════════════════════════════════════════════════════════════════════


class TestUserManagement:
    def _auth_patches(self, auth_client, bl, user=None):
        """Common patches for admin-authenticated requests."""
        admin = user or _make_admin_user()
        return [
            _patch_app_db(auth_client, bl),
            patch(
                "core.entrypoints.api.route_registration._auth_tables_available", return_value=True
            ),
            patch(
                "core.entrypoints.api.route_registration._get_user_by_session_token",
                return_value=admin,
            ),
            patch("core.entrypoints.api.route_registration._csrf_request_valid", return_value=True),
        ]

    def test_list_users(self, auth_client, serve_module):
        bl = _make_mock_bl()
        rows = [
            {
                "id": 1,
                "username": "admin",
                "display_name": "Admin",
                "is_active": True,
                "must_change_password": False,
                "created_at": None,
                "updated_at": None,
                "roles": ["admin"],
            },
        ]
        cur = MagicMock()
        cur.fetchall.return_value = rows
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        bl.conn.cursor.return_value = cur

        patches = [
            *self._auth_patches(auth_client, bl),
            patch(
                "core.entrypoints.api.route_registration._auth_user_column_exists",
                return_value=True,
            ),
        ]
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            auth_client.cookies.set("fs_session", "fake-token")
            r = auth_client.get("/api/auth/users")
        assert r.status_code == 200
        data = r.json()
        assert "users" in data
        assert len(data["users"]) >= 1
        executed_sql = " ".join(
            str(call.args[0]) for call in cur.execute.call_args_list if call.args
        )
        assert "u.deleted_at IS NULL" in executed_sql

    def test_create_user(self, auth_client, serve_module):
        bl = _make_mock_bl()
        cur = bl.conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (2,)  # new user id

        created_user = {
            "id": 2,
            "username": "newuser@example.com",
            "email": "newuser@example.com",
            "display_name": "New User",
            "is_active": True,
            "must_change_password": False,
            "roles": ["viewer"],
        }
        patches = [
            *self._auth_patches(auth_client, bl),
            patch(
                "core.entrypoints.api.route_registration._get_user_by_username", return_value=None
            ),
            patch("core.entrypoints.api.route_registration._seed_default_roles"),
            patch(
                "core.entrypoints.api.route_registration._auth_user_column_exists",
                return_value=True,
            ),
            patch(
                "core.entrypoints.api.route_registration._normalize_requested_roles",
                return_value=["viewer"],
            ),
            patch(
                "core.entrypoints.api.route_registration._get_user_by_id", return_value=created_user
            ),
            patch("core.entrypoints.api.route_registration._log_auth_audit_event"),
        ]
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            patches[8],
            patches[9],
        ):
            auth_client.cookies.set("fs_session", "fake-token")
            r = auth_client.post(
                "/api/auth/users",
                json={
                    "email": "newuser@example.com",
                    "password": "N3wUserP@ss!!",
                    "display_name": "New User",
                    "roles": ["viewer"],
                },
            )
        assert r.status_code == 200
        data = r.json()
        assert data.get("user", {}).get("username") == "newuser@example.com"

    def test_create_user_sends_signup_notification_email(self, auth_client, serve_module):
        bl = _make_mock_bl()
        cur = bl.conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (3,)  # new user id

        created_user = {
            "id": 3,
            "username": "notify@example.com",
            "email": "notify@example.com",
            "display_name": "Notify User",
            "is_active": True,
            "must_change_password": False,
            "roles": ["viewer"],
        }
        patches = [
            *self._auth_patches(auth_client, bl),
            patch(
                "core.entrypoints.api.route_registration._get_user_by_username", return_value=None
            ),
            patch("core.entrypoints.api.route_registration._seed_default_roles"),
            patch(
                "core.entrypoints.api.route_registration._auth_user_column_exists",
                return_value=True,
            ),
            patch(
                "core.entrypoints.api.route_registration._normalize_requested_roles",
                return_value=["viewer"],
            ),
            patch(
                "core.entrypoints.api.route_registration._get_user_by_id", return_value=created_user
            ),
            patch("core.entrypoints.api.route_registration._log_auth_audit_event"),
        ]
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            patches[8],
            patches[9],
            patch("core.entrypoints.api.auth_users.send_signup_notification_email") as mock_notify,
        ):
            auth_client.cookies.set("fs_session", "fake-token")
            r = auth_client.post(
                "/api/auth/users",
                json={
                    "email": "notify@example.com",
                    "password": "N3wUserP@ss!!",
                    "display_name": "Notify User",
                    "roles": ["viewer"],
                },
            )
        assert r.status_code == 200
        mock_notify.assert_called_once()
        call_kwargs = mock_notify.call_args.kwargs
        assert call_kwargs["recipient"] == "notify@example.com"
        assert "Notify User" in call_kwargs["display_name"]

    def test_create_user_short_password(self, auth_client, serve_module):
        bl = _make_mock_bl()
        patches = self._auth_patches(auth_client, bl)
        with patches[0], patches[1], patches[2], patches[3]:
            auth_client.cookies.set("fs_session", "fake-token")
            r = auth_client.post(
                "/api/auth/users",
                json={
                    "email": "newuser@example.com",
                    "password": "short",
                    "roles": ["viewer"],
                },
            )
        assert r.status_code == 400
        assert "password" in r.json().get("error", "").lower()

    def test_update_user_roles(self, auth_client, serve_module):
        bl = _make_mock_bl()
        existing = _make_admin_user()
        existing["id"] = _USER_2
        existing["username"] = "user2"
        updated = dict(existing, roles=["admin", "operator"])

        patches = [
            *self._auth_patches(auth_client, bl),
            patch(
                "core.entrypoints.api.route_registration._get_user_by_id",
                side_effect=[existing, updated],
            ),
            patch(
                "core.entrypoints.api.route_registration._normalize_requested_roles",
                return_value=["admin", "operator"],
            ),
            patch(
                "core.entrypoints.api.route_registration._auth_user_column_exists",
                return_value=True,
            ),
            patch("core.entrypoints.api.route_registration._log_auth_audit_event"),
        ]
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
        ):
            auth_client.cookies.set("fs_session", "fake-token")
            r = auth_client.put(
                f"/api/auth/users/{_USER_2}",
                json={"roles": ["admin", "operator"]},
            )
        assert r.status_code == 200
        data = r.json()
        assert "user" in data

    def test_unlock_user(self, auth_client, serve_module):
        bl = _make_mock_bl()
        existing = _make_admin_user()
        existing["id"] = _USER_2
        existing["username"] = "lockeduser"

        patches = [
            *self._auth_patches(auth_client, bl),
            patch("core.entrypoints.api.route_registration._get_user_by_id", return_value=existing),
            patch("core.entrypoints.api.route_registration._auth_login_clear_user_failures"),
            patch("core.entrypoints.api.route_registration._log_auth_audit_event"),
        ]
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            auth_client.cookies.set("fs_session", "fake-token")
            r = auth_client.post(f"/api/auth/users/{_USER_2}/unlock")
        assert r.status_code == 200
        data = r.json()
        assert data.get("status") == "unlocked"
        assert data.get("username") == "lockeduser"

    def test_unlock_nonexistent_user_returns_404(self, auth_client, serve_module):
        bl = _make_mock_bl()
        patches = [
            *self._auth_patches(auth_client, bl),
            patch("core.entrypoints.api.route_registration._get_user_by_id", return_value=None),
        ]
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            auth_client.cookies.set("fs_session", "fake-token")
            r = auth_client.post(f"/api/auth/users/{_MISSING_USER}/unlock")
        assert r.status_code == 404

    def test_force_password_reset(self, auth_client, serve_module):
        bl = _make_mock_bl()
        existing = _make_admin_user()
        existing["id"] = _USER_2
        existing["username"] = "user2"

        patches = [
            *self._auth_patches(auth_client, bl),
            patch("core.entrypoints.api.route_registration._get_user_by_id", return_value=existing),
            patch(
                "core.entrypoints.api.route_registration._auth_user_column_exists",
                return_value=True,
            ),
            patch("core.entrypoints.api.route_registration._auth_login_clear_user_failures"),
            patch("core.entrypoints.api.route_registration._log_auth_audit_event"),
        ]
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
        ):
            auth_client.cookies.set("fs_session", "fake-token")
            r = auth_client.post(f"/api/auth/users/{_USER_2}/force-password-reset")
        assert r.status_code == 200
        data = r.json()
        assert data.get("status") == "password_reset_forced" or "revoked_sessions" in data

    def test_user_management_requires_admin_role(self, auth_client, serve_module):
        """Non-admin user should get 403 on user management endpoints."""
        bl = _make_mock_bl()
        viewer_user = {
            "id": 3,
            "username": "viewer",
            "display_name": "Viewer",
            "roles": ["viewer"],
            "is_active": True,
        }
        with (
            _patch_app_db(auth_client, bl),
            patch(
                "core.entrypoints.api.route_registration._auth_tables_available", return_value=True
            ),
            patch(
                "core.entrypoints.api.route_registration._get_user_by_session_token",
                return_value=viewer_user,
            ),
            patch("core.entrypoints.api.route_registration._csrf_request_valid", return_value=True),
        ):
            auth_client.cookies.set("fs_session", "fake-token")
            r = auth_client.get("/api/auth/users")
        assert r.status_code == 403


# ═══════════════════════════════════════════════════════════════════════════
# TestExtensionAPI
# ═══════════════════════════════════════════════════════════════════════════


class TestExtensionAPI:
    def test_list_extensions(self, client, serve_module):
        payload = MagicMock()
        payload.dump.return_value = {
            "extensions": [{"extension_id": "ext-1", "name": "Test Extension", "enabled": True}]
        }
        mock_service = MagicMock()
        mock_service.list_extensions.return_value = payload
        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"extension_query_service": lambda: mock_service},
        ):
            r = client.get("/api/extensions")
        assert r.status_code == 200, r.text
        data = r.json()
        assert "extensions" in data
        assert len(data["extensions"]) >= 1

    def test_list_extensions_no_db(self, client, serve_module):
        def unavailable_service():
            raise RuntimeError("db unavailable")

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"extension_query_service": unavailable_service},
        ):
            r = client.get("/api/extensions")
        assert r.status_code == 500

    def test_scan_extensions(self, client, serve_module):
        bl = MagicMock()
        bl.conn = None
        bl.list_extensions.return_value = [
            {"extension_id": "ext-1", "name": "Scanned"},
        ]
        bl.sync_extension_scan = MagicMock()

        with (
            _patch_app_db(client, bl),
            patch(
                "core.application.extensions.extension_scan_lifecycle."
                "sync_extension_scan_with_llm_lifecycle",
                side_effect=lambda logger, rows: logger.sync_extension_scan(rows),
            ),
            patch(
                "core.entrypoints.api.route_registration.scan_extensions",
                create=True,
                side_effect=None,
            ),
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.default_extensions_root",
                return_value="/tmp/ext",
            ),
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.scan_extensions",
                return_value=[{"id": "ext-1"}],
            ),
        ):
            r = client.post("/api/extensions/scan", json={"root": ""})
        assert r.status_code == 200
        data = r.json()
        assert "extensions" in data
        assert "scanned_count" in data

    def test_enable_extension(self, client, serve_module):
        payload = MagicMock()
        payload.dump.return_value = {"extension": {"extension_id": "ext-1", "enabled": True}}

        with patch(
            "core.application.extensions.command_service.ExtensionCommandService"
        ) as service_cls:
            service_cls.return_value.enable_extension.return_value = payload
            r = client.post("/api/extensions/ext-1/enable", json={"enabled": True})
        assert r.status_code == 200
        data = r.json()
        assert "extension" in data

    def test_enable_extension_not_found(self, client, serve_module):
        from core.application.management.errors import ServiceNotFoundError

        with patch(
            "core.application.extensions.command_service.ExtensionCommandService"
        ) as service_cls:
            service_cls.return_value.enable_extension.side_effect = ServiceNotFoundError(
                "not found"
            )
            r = client.post("/api/extensions/nonexistent/enable", json={"enabled": True})
        assert r.status_code == 404

    def test_health_check(self, client, serve_module):
        payload = MagicMock()
        payload.dump.return_value = {
            "extension_id": "ext-1",
            "health": {"status": "healthy", "detail": None},
        }
        mock_service = MagicMock()
        mock_service.get_health.return_value = payload

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"extension_query_service": lambda: mock_service},
        ):
            r = client.get("/api/extensions/ext-1/health")
        assert r.status_code == 200
        data = r.json()
        assert data.get("extension_id") == "ext-1"
        assert data.get("health", {}).get("status") == "healthy"

    def test_health_check_not_found(self, client, serve_module):
        from core.application.management.errors import ServiceNotFoundError

        mock_service = MagicMock()
        mock_service.get_health.side_effect = ServiceNotFoundError("not found")

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"extension_query_service": lambda: mock_service},
        ):
            r = client.get("/api/extensions/nonexistent/health")
        assert r.status_code == 404

    def test_compiled_manifest_returns_json(self, client, serve_module):
        compiled = {"extension_id": "ext-1", "name": "Test", "version": "1.0"}
        mock_service = MagicMock()
        mock_service.get_compiled_manifest_safe.return_value = compiled

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"extension_query_service": lambda: mock_service},
        ):
            r = client.get("/api/extensions/ext-1/compiled-manifest")
        assert r.status_code == 200
        data = r.json()
        assert data.get("extension_id") == "ext-1"

    def test_compiled_manifest_not_found(self, client, serve_module):
        from core.application.management.errors import ServiceNotFoundError

        mock_service = MagicMock()
        mock_service.get_compiled_manifest_safe.side_effect = ServiceNotFoundError("not found")
        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"extension_query_service": lambda: mock_service},
        ):
            r = client.get("/api/extensions/nonexistent/compiled-manifest")
        assert r.status_code == 404

    def test_invoke_disabled_extension_rejected(self, client, serve_module):
        """Querying a disabled extension returns 409."""
        from core.application.management.errors import ServiceConflictError

        mock_service = MagicMock()
        mock_service.execute_query.side_effect = ServiceConflictError("Extension is disabled")
        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"extension_binding_service": lambda: mock_service},
        ):
            r = client.post(
                "/api/extensions/ext-1/query/some-query",
                json={"page_id": "", "component_id": ""},
            )
        assert r.status_code == 409
        assert "disabled" in r.json().get("error", "").lower()

    def test_preview_extension(self, client, serve_module):
        preview_data = {"extension_id": "ext-1", "preview": "html-content"}
        mock_service = MagicMock()
        mock_service.get_preview.return_value = preview_data

        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"extension_query_service": lambda: mock_service},
        ):
            r = client.get("/api/extensions/ext-1/preview")
        assert r.status_code == 200

    def test_preview_not_found(self, client, serve_module):
        from core.application.management.errors import ServiceNotFoundError

        mock_service = MagicMock()
        mock_service.get_preview.side_effect = ServiceNotFoundError("not found")
        with patch.dict(
            client.app.state.runtime_container.service_factories,
            {"extension_query_service": lambda: mock_service},
        ):
            r = client.get("/api/extensions/nonexistent/preview")
        assert r.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# TestAuthDisabled
# ═══════════════════════════════════════════════════════════════════════════


class TestAuthDisabled:
    """Auth endpoints return errors when local auth is disabled."""

    def test_bootstrap_disabled(self, client, serve_module):
        r = client.post(
            "/api/auth/bootstrap",
            json={
                "email": "admin@example.com",
                "password": "Str0ngP@ssword!",
            },
        )
        assert r.status_code == 400
        assert "disabled" in r.json().get("error", "").lower()

    def test_login_disabled(self, client, serve_module):
        r = client.post(
            "/api/auth/login",
            json={
                "email": "admin@example.com",
                "password": "Str0ngP@ssword!",
            },
        )
        assert r.status_code == 400
        assert "disabled" in r.json().get("error", "").lower()

    def test_logout_when_disabled_still_works(self, client, serve_module):
        r = client.post("/api/auth/logout")
        assert r.status_code == 200
        assert r.json().get("status") == "logged_out"

    def test_me_when_auth_disabled_returns_unauthenticated(self, client, serve_module):
        r = client.get("/api/auth/me")
        assert r.status_code == 200
        data = r.json()
        assert data.get("authenticated") is False


class TestTokenAuthorityIntersection:
    """enforce_token_scope_within_caller_authority subset/intersection rules.

    Covers FS-DEEP-CAND-061 (PAT) and FS-DEEP-CAND-063 (service/machine token)
    at the helper level: a token grant must never exceed the caller's authority.
    """

    @staticmethod
    def _enforce(**kwargs):
        from core.application.auth.principal import (
            enforce_token_scope_within_caller_authority,
        )

        return enforce_token_scope_within_caller_authority(
            kwargs.pop("caller"),
            requested_scopes=kwargs.pop("requested_scopes", None),
            requested_project_ids=kwargs.pop("requested_project_ids", None),
            requested_workflow_ids=kwargs.pop("requested_workflow_ids", None),
        )

    def test_admin_caller_may_grant_anything(self):
        # Privileged carve-out: admin role provisions freely.
        self._enforce(
            caller={"roles": ["admin"]},
            requested_scopes=["admin:all"],
            requested_project_ids=["1"],
            requested_workflow_ids=["wf"],
        )

    def test_restricted_caller_cannot_widen_scopes(self):
        from core.application.auth.principal import TokenAuthorityError

        with pytest.raises(TokenAuthorityError):
            self._enforce(
                caller={"auth_source": "pat", "scopes": ["jobs.read"]},
                requested_scopes=["admin:all"],
            )

    def test_restricted_caller_may_grant_held_scopes(self):
        # Exact held scopes pass; colon-wildcard holders grant within their reach.
        self._enforce(
            caller={"auth_source": "pat", "scopes": ["jobs.read", "jobs.write"]},
            requested_scopes=["jobs.read"],
        )
        self._enforce(
            caller={"auth_source": "pat", "scopes": ["command:*"]},
            requested_scopes=["command:run"],
        )

    def test_session_user_scope_list_uses_role_ceiling(self):
        # Interactive session users are role-gated downstream, so an empty
        # stored scope list does not block role-appropriate grants.
        self._enforce(
            caller={"roles": ["operator"], "scopes": []},
            requested_scopes=["read:*"],
        )
        self._enforce(
            caller={"roles": ["operator"], "scopes": []},
            requested_scopes=["command:*"],
        )

    def test_session_operator_cannot_mint_admin_or_machine_scopes(self):
        from core.application.auth.principal import TokenAuthorityError

        for scope in ("admin:all", "notify:dispatch", "mcp:use", "agent:claim"):
            with pytest.raises(TokenAuthorityError):
                self._enforce(
                    caller={"roles": ["operator"], "scopes": []},
                    requested_scopes=[scope],
                )

    def test_project_restricted_caller_cannot_widen(self):
        from core.application.auth.principal import TokenAuthorityError

        with pytest.raises(TokenAuthorityError):
            self._enforce(
                caller={"roles": ["operator"], "allowed_project_ids": ["1"]},
                requested_project_ids=["1", "2"],
            )

    def test_project_restricted_caller_cannot_mint_unrestricted(self):
        from core.application.auth.principal import TokenAuthorityError

        with pytest.raises(TokenAuthorityError):
            self._enforce(
                caller={"roles": ["operator"], "allowed_project_ids": ["1"]},
                requested_project_ids=[],
            )

    def test_workflow_restricted_caller_may_narrow(self):
        self._enforce(
            caller={"roles": ["operator"], "allowed_workflow_ids": ["wf-a", "wf-b"]},
            requested_workflow_ids=["wf-a"],
        )
