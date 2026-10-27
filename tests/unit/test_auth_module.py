"""
Unit tests for the auth/ module — pure functions and configuration.

Run:
    docker compose exec fs-app python -m pytest tests/unit/test_auth_module.py -v
"""

from __future__ import annotations

import builtins
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from tests.unit._sql_debug import composable_to_debug_sql

# Auth lookups may request RealDictCursor via psycopg_runtime.extras.
# Keep tests runnable in environments where psycopg_runtime is optional.
try:
    from core.infrastructure.db import psycopg_runtime as psycopg_runtime
except Exception:
    _mock_psycopg_runtime = MagicMock()
    _mock_psycopg_runtime.extras.RealDictCursor = "RealDictCursor"
    sys.modules.setdefault("psycopg_runtime", _mock_psycopg_runtime)
    sys.modules.setdefault("psycopg_runtime.extras", _mock_psycopg_runtime.extras)


# ---------------------------------------------------------------------------
# auth.principal — pure functions
# ---------------------------------------------------------------------------


class TestPrincipalFunctions:
    def test_roles_intersect_true(self):
        from core.application.auth.principal import roles_intersect

        assert roles_intersect(["admin", "viewer"], {"admin"}) is True

    def test_roles_intersect_false(self):
        from core.application.auth.principal import roles_intersect

        assert roles_intersect(["viewer"], {"admin", "operator"}) is False

    def test_roles_intersect_empty(self):
        from core.application.auth.principal import roles_intersect

        assert roles_intersect([], {"admin"}) is False

    def test_principal_account_id_default(self):
        from core.application.auth.principal import principal_account_id

        assert principal_account_id(None) != ""
        assert principal_account_id({}) != ""

    def test_principal_account_id_from_user(self):
        from core.application.auth.principal import principal_account_id

        assert principal_account_id({"account_id": "acc_42"}) == "acc_42"

    def test_principal_scopes_empty(self):
        from core.application.auth.principal import principal_scopes

        assert principal_scopes(None) == set()
        assert principal_scopes({}) == set()

    def test_principal_scopes_from_user(self):
        from core.application.auth.principal import principal_scopes

        assert principal_scopes({"scopes": ["read:*", "mcp:use"]}) == {"read:*", "mcp:use"}

    def test_scope_granted_exact(self):
        from core.application.auth.principal import scope_granted

        assert scope_granted({"admin:all"}, "admin:all") is True
        assert scope_granted({"read:projects"}, "admin:all") is False

    def test_scope_granted_wildcard(self):
        from core.application.auth.principal import scope_granted

        assert scope_granted({"read:*"}, "read:projects") is True
        assert scope_granted({"command:*"}, "command:job.pause") is True
        assert scope_granted({"read:*"}, "command:job.pause") is False
        assert scope_granted({"jobs:*"}, "jobs.read") is True
        assert scope_granted({"jobs:*"}, "projects.read") is False

    def test_storage_scope_subset_accepts_canonical_wildcard_for_catalog_scope(self):
        from core.infrastructure.db.platform_logger_common import _scopes_within

        assert _scopes_within(["jobs:*"], ["jobs.read", "jobs.write"]) is True
        assert _scopes_within(["jobs:*"], ["projects.read"]) is False

    def test_scope_granted_admin_all(self):
        from core.application.auth.principal import scope_granted

        assert scope_granted({"admin:all"}, "read:projects") is True
        assert scope_granted({"admin:all"}, "command:anything") is True

    def test_is_machine_principal(self):
        from core.application.auth.principal import is_machine_principal

        assert is_machine_principal({"principal_type": "service_account"}) is True
        assert is_machine_principal({"principal_type": "user"}) is False
        assert is_machine_principal(None) is False

    def test_principal_has_required_scopes_any(self):
        from core.application.auth.principal import principal_has_required_scopes

        user = {"scopes": ["read:projects", "mcp:use"]}
        assert (
            principal_has_required_scopes(user, {"read:projects", "admin:all"}, require_all=False)
            is True
        )

    def test_principal_has_required_scopes_all(self):
        from core.application.auth.principal import principal_has_required_scopes

        user = {"scopes": ["read:projects", "mcp:use"]}
        assert (
            principal_has_required_scopes(user, {"read:projects", "admin:all"}, require_all=True)
            is False
        )
        assert (
            principal_has_required_scopes(user, {"read:projects", "mcp:use"}, require_all=True)
            is True
        )

    def test_principal_allowed_project_ids(self):
        from core.application.auth.principal import principal_allowed_project_ids

        assert principal_allowed_project_ids(None) == set()
        assert principal_allowed_project_ids(
            {"allowed_project_ids": ["00000000-0000-0000-0000-000000000101"]}
        ) == {"00000000-0000-0000-0000-000000000101"}

    def test_principal_allowed_workflow_ids(self):
        from core.application.auth.principal import principal_allowed_workflow_ids

        assert principal_allowed_workflow_ids(None) == set()
        assert principal_allowed_workflow_ids({"allowed_workflow_ids": ["wf_a"]}) == {"wf_a"}

    def test_coerce_optional_project_id(self):
        from core.application.auth.principal import coerce_optional_project_id

        assert coerce_optional_project_id(None) is None
        assert coerce_optional_project_id("") is None
        assert coerce_optional_project_id("00000000-0000-0000-0000-000000000101") == (
            "00000000-0000-0000-0000-000000000101"
        )

    def test_coerce_optional_workflow_id(self):
        from core.application.auth.principal import coerce_optional_workflow_id

        assert coerce_optional_workflow_id(None) is None
        assert coerce_optional_workflow_id("") is None
        assert coerce_optional_workflow_id("weekly_report") == "weekly_report"


# ---------------------------------------------------------------------------
# auth.password
# ---------------------------------------------------------------------------


class TestPasswordFunctions:
    def test_password_hash_and_verify(self):
        from core.application.auth.password import password_hash, verify_password

        h = password_hash("my-secret-123")
        assert h.startswith("pbkdf2_sha256$")
        assert verify_password("my-secret-123", h) is True
        assert verify_password("wrong-password", h) is False

    def test_password_hash_with_salt(self):
        from core.application.auth.password import password_hash

        h1 = password_hash("test", salt_hex="aabbccdd" * 4)
        h2 = password_hash("test", salt_hex="aabbccdd" * 4)
        assert h1 == h2

    def test_password_hash_empty_raises(self):
        from core.application.auth.password import password_hash

        with pytest.raises(ValueError):
            password_hash("")

    def test_session_token_hash(self):
        from core.application.auth.password import session_token_hash

        h = session_token_hash("my-token")
        assert len(h) == 64  # SHA-256 hex

    def test_password_reset_token_hash(self):
        from core.application.auth.password import password_reset_token_hash

        h = password_reset_token_hash("reset-token")
        assert len(h) == 64


# ---------------------------------------------------------------------------
# auth.email / request contracts
# ---------------------------------------------------------------------------


class TestAuthRequestContracts:
    def test_login_identifier_requires_email_address(self):
        from core.application.auth.email import normalize_login_identifier

        assert normalize_login_identifier("Admin@Example.COM") == "admin@example.com"
        with pytest.raises(ValueError, match="Invalid email address"):
            normalize_login_identifier("admin")

    def test_auth_requests_reject_username_alias_payloads(self):
        from pydantic import ValidationError

        from core.application.management.requests import AuthLoginRequest

        with pytest.raises(ValidationError):
            AuthLoginRequest.model_validate(
                {"username": "admin@example.com", "password": "correct horse battery"}
            )


# ---------------------------------------------------------------------------
# auth.enforce
# ---------------------------------------------------------------------------


class TestEnforceFunctions:
    def test_is_public_auth_path(self):
        from core.application.auth.enforce import is_public_auth_path

        assert is_public_auth_path("/api/auth/login") is True
        assert is_public_auth_path("/api/auth/password-reset/request") is True
        assert is_public_auth_path("/api/auth/password-reset/verify") is True
        assert is_public_auth_path("/api/auth/password-reset/confirm") is True
        assert is_public_auth_path("/api/auth/forgot-password") is False
        assert is_public_auth_path("/api/auth/reset-password") is False
        assert is_public_auth_path("/api/app/bootstrap") is True
        assert is_public_auth_path("/api/health") is True
        assert is_public_auth_path("/api/mcp/oauth/client-metadata/mcp_alpha.json") is True
        assert is_public_auth_path("/verify-email-change") is True
        assert is_public_auth_path("/favicon.ico") is True
        assert is_public_auth_path("/static-v/dev123/shell.css") is True
        assert is_public_auth_path("/app/login") is True
        assert is_public_auth_path("/app/assets/main.js") is True
        assert is_public_auth_path("/api/projects") is False

    def test_is_admin_only_route(self):
        from core.application.auth.enforce import is_admin_only_route

        assert is_admin_only_route("/admin/settings") is True
        assert is_admin_only_route("/api/agents/1") is True
        assert is_admin_only_route("/api/projects") is False

    def test_require_authenticated_none(self):
        from core.application.auth.enforce import require_authenticated

        with pytest.raises(PermissionError):
            require_authenticated(None)

    def test_require_authenticated_valid(self):
        from core.application.auth.enforce import require_authenticated

        user = {"id": 1, "username": "admin", "roles": ["admin"]}
        result = require_authenticated(user)
        assert result["id"] == 1

    def test_require_roles_match(self):
        from core.application.auth.enforce import require_roles

        user = {"id": 1, "username": "admin", "roles": ["admin"]}
        result = require_roles(user, {"admin"})
        assert result["id"] == 1

    def test_require_roles_no_match(self):
        from core.application.auth.enforce import require_roles

        user = {"id": 1, "username": "viewer", "roles": ["viewer"]}
        with pytest.raises(PermissionError):
            require_roles(user, {"admin"})

    def test_normalize_requested_roles(self):
        from core.application.auth.enforce import normalize_requested_roles

        assert normalize_requested_roles(["admin", "operator", "viewer"]) == [
            "admin",
            "operator",
            "viewer",
        ]
        assert normalize_requested_roles(["unknown_role"]) == []
        assert normalize_requested_roles(None) == []


# ---------------------------------------------------------------------------
# auth.config
# ---------------------------------------------------------------------------


class TestConfigFunctions:
    def test_env_flag_defaults(self):
        from core.application.auth.config import env_flag

        assert env_flag("NONEXISTENT_VAR_123", True) is True
        assert env_flag("NONEXISTENT_VAR_123", False) is False

    def test_env_flag_truthy(self):
        from core.application.auth.config import env_flag

        for val in ("1", "true", "yes", "on"):
            with patch.dict(os.environ, {"TEST_FLAG": val}):
                assert env_flag("TEST_FLAG", False) is True

    def test_env_flag_falsy(self):
        from core.application.auth.config import env_flag

        for val in ("0", "false", "no", "off"):
            with patch.dict(os.environ, {"TEST_FLAG": val}):
                assert env_flag("TEST_FLAG", True) is False


# ---------------------------------------------------------------------------
# auth.rate_limit
# ---------------------------------------------------------------------------


class TestRateLimit:
    def test_lock_state_clean(self):
        from core.application.auth.rate_limit import (
            auth_login_clear_failures,
            auth_login_lock_state,
        )

        auth_login_clear_failures("test_user_rl", "127.0.0.1")
        locked, _retry = auth_login_lock_state("test_user_rl", "127.0.0.1")
        assert locked is False

    def test_record_failure_and_lockout(self):
        from core.application.auth.config import AUTH_LOGIN_MAX_ATTEMPTS
        from core.application.auth.rate_limit import (
            auth_login_clear_failures,
            auth_login_lock_state,
            auth_login_record_failure,
        )

        auth_login_clear_failures("lockout_user", "10.0.0.1")
        for _ in range(AUTH_LOGIN_MAX_ATTEMPTS + 1):
            auth_login_record_failure("lockout_user", "10.0.0.1")
        locked, retry = auth_login_lock_state("lockout_user", "10.0.0.1")
        assert locked is True
        assert retry > 0
        # Cleanup
        auth_login_clear_failures("lockout_user", "10.0.0.1")

    def test_ip_lockout_does_not_block_unrelated_user_preflight(self):
        from core.application.auth.config import AUTH_LOGIN_MAX_ATTEMPTS
        from core.application.auth.rate_limit import (
            auth_login_clear_failures,
            auth_login_lock_state,
            auth_login_record_failure,
        )

        ip = "10.0.0.9"
        auth_login_clear_failures("lockout_user_a", ip)
        auth_login_clear_failures("lockout_user_b", ip)
        for _ in range(AUTH_LOGIN_MAX_ATTEMPTS + 1):
            auth_login_record_failure("lockout_user_a", ip)

        locked, retry = auth_login_lock_state("lockout_user_b", ip)

        assert locked is False
        assert retry == 0
        throttled, retry = auth_login_record_failure("lockout_user_b", ip)
        assert throttled is True
        assert retry > 0
        auth_login_clear_failures("lockout_user_a", ip)
        auth_login_clear_failures("lockout_user_b", ip)

    def test_shared_store_lockout_survives_process_memory_clear(self):
        from core.application.auth import rate_limit
        from core.application.auth.config import AUTH_LOGIN_MAX_ATTEMPTS

        class _Cursor:
            def __init__(self, store):
                self.store = store
                self._next = None

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, query, params=()):
                normalized = " ".join(str(query).split())
                if "pg_advisory_xact_lock" in normalized:
                    return
                if "SELECT attempts_jsonb" in normalized:
                    row = self.store.get(str(params[0]))
                    self._next = dict(row) if row else None
                    return
                if "INSERT INTO public.auth_rate_limits" in normalized:
                    key, attempts_jsonb, locked_until, _locked_until_copy = params
                    self.store[str(key)] = {
                        "attempts_jsonb": attempts_jsonb,
                        "locked_until_epoch": locked_until,
                    }
                    return
                if "DELETE FROM public.auth_rate_limits" in normalized:
                    self.store.pop(str(params[0]), None)

            def fetchone(self):
                return self._next

        class _Conn:
            def __init__(self, store):
                self.store = store
                self.commits = 0

            def cursor(self):
                return _Cursor(self.store)

            def commit(self):
                self.commits += 1

            def rollback(self):
                return None

        class _BL:
            def __init__(self):
                self.store = {}
                self.conn = _Conn(self.store)

            def _table_exists(self, table):
                return table == "auth_rate_limits"

        bl = _BL()
        ip = "10.0.0.44"
        rate_limit.auth_login_clear_failures("shared_user_a", ip, bl=bl)
        for _ in range(AUTH_LOGIN_MAX_ATTEMPTS):
            rate_limit.auth_login_record_failure("shared_user_a", ip, bl=bl)

        rate_limit._auth_login_attempts.clear()
        locked, retry = rate_limit.auth_login_lock_state("shared_user_b", ip, bl=bl)

        assert locked is False
        assert retry == 0
        throttled, retry = rate_limit.auth_login_record_failure("shared_user_b", ip, bl=bl)
        assert throttled is True
        assert retry > 0
        assert f"ip:{ip}" in bl.store

    def test_shared_store_first_failure_persists_without_lockout(self, pg_conn):
        from uuid import uuid4

        from core.application.auth import rate_limit

        class _BL:
            conn = pg_conn

            @staticmethod
            def _table_exists(table):
                return table == "auth_rate_limits"

        suffix = uuid4().hex
        username = f"rate_limit_sql_{suffix}"
        ip = f"test-{suffix}"
        keys = [f"user:{username}", f"ip:{ip}"]
        bl = _BL()

        try:
            locked, retry = rate_limit.auth_login_record_failure(username, ip, bl=bl)

            assert locked is False
            assert retry == 0
            with pg_conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT key, jsonb_array_length(attempts_jsonb), locked_until
                    FROM public.auth_rate_limits
                    WHERE key = ANY(%s)
                    ORDER BY key
                    """,
                    (keys,),
                )
                rows = cur.fetchall()
            assert rows == [(keys[1], 1, None), (keys[0], 1, None)]
        finally:
            with pg_conn.cursor() as cur:
                cur.execute("DELETE FROM public.auth_rate_limits WHERE key = ANY(%s)", (keys,))
            pg_conn.commit()


# ---------------------------------------------------------------------------
# auth.resolve — table checks with mock BL
# ---------------------------------------------------------------------------


class TestResolveTableChecks:
    def _make_bl(self, tables: set[str]):
        bl = MagicMock()
        bl._table_exists = lambda t: t in tables
        bl._column_exists = lambda t, c: False
        return bl

    def test_auth_tables_available(self):
        from core.application.auth.resolve import auth_tables_available

        bl = self._make_bl({"users", "roles", "user_roles", "auth_sessions"})
        assert auth_tables_available(bl) is True

    def test_auth_tables_missing(self):
        from core.application.auth.resolve import auth_tables_available

        bl = self._make_bl({"users"})
        assert auth_tables_available(bl) is False

    def test_pat_tables_available(self):
        from core.application.auth.resolve import pat_tables_available

        bl = self._make_bl({"personal_access_tokens", "users"})
        assert pat_tables_available(bl) is True

    def test_none_bl(self):
        from core.application.auth.resolve import auth_tables_available, pat_tables_available

        assert auth_tables_available(None) is False
        assert pat_tables_available(None) is False

    def test_resolve_bearer_principal_none(self):
        from core.application.auth.resolve import resolve_bearer_principal

        assert resolve_bearer_principal(None, "token") is None
        bl = self._make_bl(set())
        assert resolve_bearer_principal(bl, "") is None


class TestResolveUserLookupFilters:
    def _make_bl(
        self,
        *,
        has_deleted_at: bool,
        has_timezone: bool = False,
        has_locale: bool = False,
        has_account_id: bool = False,
    ):
        bl = MagicMock()
        bl._table_exists = lambda _table: True
        bl._column_exists = lambda table, column: (
            (
                table == "users"
                and (
                    (column == "deleted_at" and has_deleted_at)
                    or (column == "timezone" and has_timezone)
                    or (column == "locale" and has_locale)
                    or (column == "account_id" and has_account_id)
                )
            )
            or (table == "auth_sessions" and column == "account_id" and has_account_id)
        )
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        bl.conn.cursor.return_value = cur
        return bl, cur

    def test_get_user_by_username_filters_deleted_users_when_supported(self):
        from core.application.auth.resolve import get_user_by_username

        bl, cur = self._make_bl(has_deleted_at=True)
        cur.fetchone.return_value = {
            "id": 7,
            "username": "admin",
            "display_name": "Admin",
            "password_hash": "hash",
            "is_active": True,
            "created_at": None,
            "updated_at": None,
            "must_change_password": False,
        }

        with patch("core.application.auth.resolve.get_user_roles", return_value=["admin"]):
            user = get_user_by_username(bl, "admin")

        assert user is not None
        sql = composable_to_debug_sql(cur.execute.call_args_list[0].args[0])
        assert "u.deleted_at IS NULL" in sql

    def test_get_user_by_username_skips_deleted_filter_when_column_missing(self):
        from core.application.auth.resolve import get_user_by_username

        bl, cur = self._make_bl(has_deleted_at=False)
        cur.fetchone.return_value = None
        get_user_by_username(bl, "admin")

        sql = composable_to_debug_sql(cur.execute.call_args_list[0].args[0])
        assert "u.deleted_at IS NULL" not in sql

    def test_get_user_by_username_selects_timezone_when_supported(self):
        from core.application.auth.resolve import get_user_by_username

        bl, cur = self._make_bl(has_deleted_at=False, has_timezone=True)
        cur.fetchone.return_value = {
            "id": 7,
            "username": "admin",
            "display_name": "Admin",
            "password_hash": "hash",
            "is_active": True,
            "created_at": None,
            "updated_at": None,
            "must_change_password": False,
            "timezone": "Europe/Vilnius",
        }

        with patch("core.application.auth.resolve.get_user_roles", return_value=["admin"]):
            user = get_user_by_username(bl, "admin")

        assert user is not None
        assert user.get("timezone") == "Europe/Vilnius"
        sql = composable_to_debug_sql(cur.execute.call_args_list[0].args[0])
        assert "u.timezone AS timezone" in sql

    def test_get_user_by_username_selects_locale_when_supported(self):
        from core.application.auth.resolve import get_user_by_username

        bl, cur = self._make_bl(has_deleted_at=False, has_locale=True)
        cur.fetchone.return_value = {
            "id": 7,
            "username": "admin",
            "display_name": "Admin",
            "password_hash": "hash",
            "is_active": True,
            "created_at": None,
            "updated_at": None,
            "must_change_password": False,
            "locale": "ru",
        }

        with patch("core.application.auth.resolve.get_user_roles", return_value=["admin"]):
            user = get_user_by_username(bl, "admin")

        assert user is not None
        assert user.get("locale") == "ru"
        sql = composable_to_debug_sql(cur.execute.call_args_list[0].args[0])
        assert "u.locale AS locale" in sql

    def test_get_user_by_session_token_filters_deleted_users_when_supported(self):
        from core.application.auth.resolve import get_user_by_session_token

        bl, cur = self._make_bl(has_deleted_at=True)
        cur.fetchone.return_value = {
            "id": 7,
            "username": "admin",
            "display_name": "Admin",
            "is_active": True,
            "must_change_password": False,
            "session_id": 99,
            "expires_at": None,
            "account_id": "acc_default",  # required — fail-closed without it
        }

        with patch("core.application.auth.resolve.get_user_roles", return_value=["admin"]):
            user = get_user_by_session_token(bl, "token-123")

        assert user is not None
        first_sql = composable_to_debug_sql(cur.execute.call_args_list[0].args[0])
        assert "u.deleted_at IS NULL" in first_sql

    def test_get_user_by_session_token_casts_account_ids_before_nullif(self):
        from core.application.auth.resolve import get_user_by_session_token

        bl, cur = self._make_bl(has_deleted_at=False, has_account_id=True)
        cur.fetchone.return_value = {
            "id": 7,
            "username": "admin",
            "display_name": "Admin",
            "is_active": True,
            "must_change_password": False,
            "session_id": 99,
            "expires_at": None,
            "account_id": "00000000-0000-0000-0000-000000000002",
        }

        with patch("core.application.auth.resolve.get_user_roles", return_value=["admin"]):
            user = get_user_by_session_token(bl, "token-123")

        assert user is not None
        first_sql = composable_to_debug_sql(cur.execute.call_args_list[0].args[0])
        assert "NULLIF(s.account_id::text, '')" in first_sql
        assert "u.account_id::text" in first_sql

    def test_serve_auth_session_account_id_select_casts_account_ids_before_nullif(self):
        from core.application.auth.serve_auth_runtime import auth_session_account_id_select

        bl, _cur = self._make_bl(has_deleted_at=False, has_account_id=True)

        sql = auth_session_account_id_select(bl)

        assert "NULLIF(s.account_id::text, '')" in sql
        assert "u.account_id::text" in sql

    def test_get_user_by_session_token_retries_without_prepare_on_duplicate_statement(self):
        from core.application.auth.resolve import get_user_by_session_token

        bl, cur = self._make_bl(has_deleted_at=True)
        cur.fetchone.return_value = {
            "id": 7,
            "username": "admin",
            "display_name": "Admin",
            "is_active": True,
            "must_change_password": False,
            "session_id": 99,
            "expires_at": None,
            "account_id": "acc_default",
        }

        execute_attempt = {"count": 0}

        def _execute(*args, **kwargs):
            execute_attempt["count"] += 1
            if execute_attempt["count"] in {1, 3} and kwargs.get("prepare", True):
                raise psycopg.errors.DuplicatePreparedStatement("duplicate prepared statement")

        cur.execute.side_effect = _execute

        with patch("core.application.auth.resolve.get_user_roles", return_value=["admin"]):
            user = get_user_by_session_token(bl, "token-123")

        assert user is not None
        assert cur.execute.call_args_list[1].kwargs.get("prepare") is False
        assert cur.execute.call_args_list[3].kwargs.get("prepare") is False

    def test_get_user_by_username_falls_back_without_psycopg_runtime_extras(self, monkeypatch):
        from core.application.auth.resolve import get_user_by_username

        bl, cur = self._make_bl(has_deleted_at=False)
        cur.fetchone.return_value = None

        real_import = builtins.__import__

        def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "psycopg_runtime.extras":
                raise ImportError("psycopg_runtime.extras unavailable")
            return real_import(name, globals, locals, fromlist, level)

        monkeypatch.setattr(builtins, "__import__", fake_import)

        get_user_by_username(bl, "admin")

        assert bl.conn.cursor.call_args_list[0].kwargs == {}


class TestPrincipalNormalization:
    def test_normalize_principal_rejects_pat_without_account_id(self):
        from core.application.auth.resolve import normalize_principal

        principal = normalize_principal(
            {
                "id": 7,
                "username": "alice",
                "auth_source": "pat",
                "roles": ["operator"],
                "is_active": True,
            }
        )

        assert principal is None

    def test_normalize_principal_rejects_machine_without_account_id(self):
        from core.application.auth.resolve import normalize_principal

        principal = normalize_principal(
            {
                "principal_type": "service_account",
                "service_account_id": 12,
                "service_account_key": "svc-ci",
                "auth_source": "machine",
                "roles": ["operator"],
                "is_active": True,
            }
        )

        assert principal is None

    def test_normalize_principal_keeps_external_default_account(self):
        from core.application.auth.config import DEFAULT_ACCOUNT_ID
        from core.application.auth.resolve import normalize_principal

        principal = normalize_principal(
            {
                "id": 9,
                "username": "external-user",
                "auth_source": "external",
                "roles": ["viewer"],
                "is_active": True,
            }
        )

        assert principal is not None
        assert principal.account_id == DEFAULT_ACCOUNT_ID
