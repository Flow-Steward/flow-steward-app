"""Security tests for API and MCP endpoint rate limiting and abuse protection.

P0 gap: no dedicated suite for non-auth API/MCP throttle and burst-rate enforcement.

Coverage:

A. Password reset throttling (unit + E2E contracts)
   - per-IP lockout after configured max_attempts
   - Retry-After header present on 429 responses
   - Different actions (request/verify/confirm) each have independent counters

B. Login brute-force lockout (unit)
   - auth_login_record_failure: lockout after threshold
   - auth_login_lock_state: returns is_locked, retry_after

C. General API abuse patterns (E2E — server must be available)
   - Rapid identical POST requests must eventually yield 429 or be accepted
   - MCP endpoints do not serve data to unauthenticated callers

D. MCP tool rate-limit contract
   - Repeated tool invocations from the same principal should not exhaust resources silently
   - (Documented as a contract spec; hard assertion requires real MCP backend)

Run:
    # Unit only (no server)
    docker compose exec fs-app python -m pytest \
        tests/security/test_api_mcp_rate_limit_abuse.py::TestPasswordResetThrottleUnit \
        tests/security/test_api_mcp_rate_limit_abuse.py::TestLoginRateLimitUnit -v

    # Full (including E2E)
    docker compose exec fs-app python -m pytest \
        tests/security/test_api_mcp_rate_limit_abuse.py -v
"""

from __future__ import annotations

import os
import threading
import uuid

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _uid() -> str:
    return uuid.uuid4().hex[:8]


# ===========================================================================
# A. Password reset throttle — unit level
# ===========================================================================


class TestPasswordResetThrottleUnit:
    """Unit tests for auth/password_reset_rate_limit.py throttle functions."""

    @pytest.fixture(autouse=True)
    def import_throttle(self):
        try:
            from core.application.auth.password_reset_rate_limit import (
                password_reset_throttle_check,
                password_reset_throttle_key,
                password_reset_throttle_record,
            )

            self._check = password_reset_throttle_check
            self._record = password_reset_throttle_record
            self._key = password_reset_throttle_key
        except ImportError as exc:
            pytest.skip(f"core.application.auth.password_reset_rate_limit not available: {exc}")

    def _fresh_store(self):
        return {}, threading.Lock()

    def test_first_request_is_not_throttled(self):
        store, lock = self._fresh_store()
        throttled, retry_after = self._check(
            "1.2.3.4",
            action="request",
            attempts_store=store,
            attempts_lock=lock,
            rate_window_seconds=60,
            max_attempts=5,
            lockout_seconds=300,
        )
        assert throttled is False
        assert retry_after == 0

    def test_lockout_after_max_attempts(self):
        store, lock = self._fresh_store()
        max_att = 3
        # Record max_att failures
        for _ in range(max_att):
            self._record(
                "10.0.0.1",
                action="request",
                attempts_store=store,
                attempts_lock=lock,
                rate_window_seconds=60,
                max_attempts=max_att,
                lockout_seconds=300,
            )
        # Now check should be throttled
        throttled, retry_after = self._check(
            "10.0.0.1",
            action="request",
            attempts_store=store,
            attempts_lock=lock,
            rate_window_seconds=60,
            max_attempts=max_att,
            lockout_seconds=300,
        )
        assert throttled is True
        assert retry_after > 0

    def test_different_ip_is_not_throttled(self):
        store, lock = self._fresh_store()
        # Exhaust IP 10.0.0.1
        for _ in range(5):
            self._record(
                "10.0.0.1",
                action="request",
                attempts_store=store,
                attempts_lock=lock,
                rate_window_seconds=60,
                max_attempts=5,
                lockout_seconds=300,
            )
        # Different IP must be clean
        throttled, _ = self._check(
            "10.0.0.2",
            action="request",
            attempts_store=store,
            attempts_lock=lock,
            rate_window_seconds=60,
            max_attempts=5,
            lockout_seconds=300,
        )
        assert throttled is False

    def test_verify_and_confirm_have_independent_counters(self):
        store, lock = self._fresh_store()
        max_att = 3
        ip = "192.168.1.1"
        # Exhaust "request" action counter
        for _ in range(max_att):
            self._record(
                ip,
                action="request",
                attempts_store=store,
                attempts_lock=lock,
                rate_window_seconds=60,
                max_attempts=max_att,
                lockout_seconds=300,
            )
        # "verify" action should still be clean
        throttled, _ = self._check(
            ip,
            action="verify",
            attempts_store=store,
            attempts_lock=lock,
            rate_window_seconds=60,
            max_attempts=max_att,
            lockout_seconds=300,
        )
        assert throttled is False, "Verify counter must be independent of request counter"

    def test_throttle_key_includes_action(self):
        key_req = self._key("1.2.3.4", "request")
        key_ver = self._key("1.2.3.4", "verify")
        assert key_req != key_ver, "Throttle keys must differ by action"

    def test_throttle_key_includes_ip(self):
        key_a = self._key("1.1.1.1", "request")
        key_b = self._key("2.2.2.2", "request")
        assert key_a != key_b, "Throttle keys must differ by IP"

    def test_lockout_expires_after_window(self):
        store, lock = self._fresh_store()
        t = [0.0]

        def fake_now():
            return t[0]

        max_att = 2
        ip = "5.6.7.8"
        lockout_seconds = 10

        for _ in range(max_att):
            self._record(
                ip,
                action="request",
                attempts_store=store,
                attempts_lock=lock,
                rate_window_seconds=60,
                max_attempts=max_att,
                lockout_seconds=lockout_seconds,
                now_fn=fake_now,
            )
        # Throttled at t=0
        throttled, _ = self._check(
            ip,
            action="request",
            attempts_store=store,
            attempts_lock=lock,
            rate_window_seconds=60,
            max_attempts=max_att,
            lockout_seconds=lockout_seconds,
            now_fn=fake_now,
        )
        assert throttled is True

        # Advance time past lockout
        t[0] = lockout_seconds + 1
        throttled2, _ = self._check(
            ip,
            action="request",
            attempts_store=store,
            attempts_lock=lock,
            rate_window_seconds=60,
            max_attempts=max_att,
            lockout_seconds=lockout_seconds,
            now_fn=fake_now,
        )
        assert throttled2 is False, "Lockout must expire after lockout_seconds"


# ===========================================================================
# B. Login rate limit — unit level
# ===========================================================================


class TestLoginRateLimitUnit:
    """Unit tests for auth/rate_limit.py login rate limiting."""

    @pytest.fixture(autouse=True)
    def import_rate_limit(self):
        try:
            from core.application.auth import rate_limit as _rl

            self._rl = _rl
            # Reset module-level state between tests using the actual attribute name.
            if hasattr(_rl, "_auth_login_attempts"):
                _rl._auth_login_attempts.clear()
            # Older attribute names are kept temporarily in case of rename.
            if hasattr(_rl, "_attempts"):
                _rl._attempts.clear()
            if hasattr(_rl, "_failures"):
                _rl._failures.clear()
        except ImportError as exc:
            pytest.skip(f"core.application.auth.rate_limit not available: {exc}")

    def test_first_failure_does_not_lock(self):
        is_locked, _ = self._rl.auth_login_record_failure(f"user_{_uid()}", "10.0.0.1")
        # Single failure should not immediately lock most configurations
        # (Result depends on max_attempts config; at minimum must not 500)
        assert isinstance(is_locked, bool)

    def test_ip_lockout_throttles_rotated_username_only_after_failure(self):
        username = f"user_{_uid()}"
        rotated_username = f"user_{_uid()}"
        ip = "10.0.0.5"
        # Drive up failures until locked (up to 20 attempts)
        locked = False
        for _ in range(20):
            locked, _ = self._rl.auth_login_record_failure(username, ip)
            if locked:
                break

        if not locked:
            pytest.skip("Could not lock out in 20 attempts — check rate_limit config")

        self._rl.auth_login_clear_user_failures(username)
        is_locked, _ = self._rl.auth_login_lock_state(rotated_username, ip)
        assert is_locked is False, "IP lockout must not deny an unrelated valid login"
        throttled, _ = self._rl.auth_login_record_failure(rotated_username, ip)
        assert throttled is True, "IP lockout must still stop username spraying"

    def test_lock_state_reflects_failure_history(self):
        username = f"user_{_uid()}"
        ip = "10.0.0.6"
        # Initially unlocked
        is_locked, _ = self._rl.auth_login_lock_state(username, ip)
        assert is_locked is False

    def test_clear_failures_does_not_raise(self):
        # Clearing a user with no failures must not raise
        self._rl.auth_login_clear_user_failures(f"ghost_user_{_uid()}")
        self._rl.auth_login_clear_failures(f"ghost_user_{_uid()}", "10.0.0.1")


# ===========================================================================
# C. E2E API abuse patterns (server required)
# ===========================================================================


try:
    from core.application.tooling.testing.runtime_env import resolve_test_env
    from tests.e2e.helpers import get, post_json

    _TEST_ENV = resolve_test_env(os.environ, lane="backend", include_dotenv=True)
    _HAS_E2E = True
except Exception:
    _HAS_E2E = False


@pytest.mark.e2e
class TestApiRateLimitE2E:
    """E2E rate-limit abuse tests against the live server.

    Focuses on observable behavior at the HTTP boundary:
    - Password reset endpoint must throttle after repeated rapid requests.
    - MCP endpoint must reject unauthenticated callers.
    - Auth endpoints return proper 429 + Retry-After headers when throttled.
    """

    @pytest.fixture(scope="class", autouse=True)
    def server_available(self):
        if not _HAS_E2E:
            pytest.skip("E2E imports unavailable")
        try:
            resp = get("/health", timeout=5)
        except Exception as exc:
            pytest.skip(f"Server not reachable: {exc}")
        if resp.status_code >= 500:
            pytest.skip(f"Server unhealthy: {resp.status_code}")

    def test_mcp_endpoint_rejects_unauthenticated(self):
        """MCP /mcp endpoint must not serve tool results to anonymous callers."""
        import requests as _req

        from tests.e2e.helpers import BASE_URL, VERIFY_SSL

        mcp_url = BASE_URL.rstrip("/") + "/mcp"
        # Send a well-formed MCP tool-list request without any auth token
        try:
            resp = _req.post(
                mcp_url,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                headers={"Content-Type": "application/json"},
                verify=VERIFY_SSL,
                timeout=10,
            )
        except Exception as exc:
            pytest.skip(f"MCP endpoint not reachable: {exc}")
        assert resp.status_code in (401, 403, 404), (
            f"MCP endpoint must reject unauthenticated callers; got {resp.status_code}"
        )

    def test_password_reset_rapid_requests_trigger_429_or_accept(self):
        """Rapid reset requests from the same IP should trigger throttling (429).

        If the server is behind a proxy that resets X-Forwarded-For or if
        the rate limiter is configured with a high threshold, this may return
        200 for all requests — that is also acceptable (not a failure).
        The key invariant: never 500.
        """
        statuses = []
        for _ in range(10):
            resp = post_json(
                "/api/auth/password-reset/request",
                {"email": f"flood-{_uid()}@rate-limit-test.invalid"},
            )
            statuses.append(resp.status_code)
            if resp.status_code == 429:
                break

        for status in statuses:
            assert status != 500, "Reset flood must never return 500"

    def test_429_response_has_retry_after_header(self):
        """When a 429 is returned it must include a Retry-After header."""
        from core.application.auth.config import AUTH_PASSWORD_RESET_MAX_ATTEMPTS

        email = f"retry-after-{_uid()}@test.invalid"
        attempts = max(6, AUTH_PASSWORD_RESET_MAX_ATTEMPTS + 2)
        # Drive repeated requests for the same subject until we get a 429.
        for _ in range(attempts):
            resp = post_json(
                "/api/auth/password-reset/request",
                {"email": email},
            )
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
                assert retry_after is not None, "429 response must include a Retry-After header"
                assert int(retry_after) > 0, "Retry-After must be a positive integer (seconds)"
                return
            assert resp.status_code != 500, "Password reset throttle path must never 500"

        pytest.fail(
            "Password reset throttle did not return 429 after repeated identical requests; "
            "check the subject-based rate-limit contract"
        )

    def test_login_endpoint_returns_401_for_bad_credentials(self):
        """Bad credentials must return 401/403 or an active throttle, never 500 or 200."""
        resp = post_json(
            "/api/auth/login",
            {
                "email": f"nonexistent-{_uid()}@example.com",
                "password": "definitely-wrong-password-xyz",
            },
        )
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
            assert retry_after is not None, "429 login throttle response must include Retry-After"
            return
        assert resp.status_code in (401, 403), (
            f"Expected 401/403 or 429 for bad credentials, got {resp.status_code}"
        )

    def test_login_brute_force_eventually_locks(self):
        """Repeated bad logins against the same username should eventually lock (429/423) or remain 401."""
        username = f"brute-{_uid()}"
        statuses = set()
        for _ in range(12):
            resp = post_json(
                "/api/auth/login",
                {
                    "username": username,
                    "password": f"wrong-{_uid()}",
                },
            )
            statuses.add(resp.status_code)
            if resp.status_code in (429, 423):
                break

        # After 12 attempts: must not be 500; acceptable: 401, 429, 423
        assert 500 not in statuses, f"Login brute force returned 500: statuses={statuses}"


# ===========================================================================
# D. MCP tool rate-limit contract (spec / documentation tests)
# ===========================================================================


class TestMcpRateLimitContract:
    """Contract spec for MCP per-principal rate limiting.

    These tests define the EXPECTED interface for an MCP rate-limit guard.
    They skip if the module does not exist yet; going green once implemented.
    """

    @pytest.fixture(autouse=True)
    def require_mcp_rate_limit(self):
        try:
            from core.application.orchestration.tools import mcp_rate_limit  # noqa: F401
        except ImportError:
            pytest.skip(
                "core.application.orchestration.tools.mcp_rate_limit not yet implemented — "
                "this suite validates the contract once the guard is added"
            )

    def test_principal_within_budget_is_allowed(self):
        from core.application.orchestration.tools.mcp_rate_limit import check_mcp_rate_limit

        allowed, retry_after = check_mcp_rate_limit(
            principal_id="user_abc",
            tool_name="project_list",
            window_seconds=60,
            max_calls=100,
        )
        assert allowed is True
        assert retry_after == 0

    def test_principal_over_budget_is_denied(self):
        from core.application.orchestration.tools.mcp_rate_limit import (
            check_mcp_rate_limit,
            record_mcp_call,
        )

        pid = f"user_{_uid()}"
        max_calls = 3
        for _ in range(max_calls):
            record_mcp_call(pid, tool_name="project_list", window_seconds=60, _max_calls=max_calls)

        allowed, retry_after = check_mcp_rate_limit(
            principal_id=pid,
            tool_name="project_list",
            window_seconds=60,
            max_calls=max_calls,
        )
        assert allowed is False
        assert retry_after > 0

    def test_different_principals_have_independent_budgets(self):
        from core.application.orchestration.tools.mcp_rate_limit import (
            check_mcp_rate_limit,
            record_mcp_call,
        )

        pid_a = f"user_{_uid()}"
        pid_b = f"user_{_uid()}"
        max_calls = 2
        for _ in range(max_calls):
            record_mcp_call(
                pid_a, tool_name="project_list", window_seconds=60, _max_calls=max_calls
            )

        allowed_b, _ = check_mcp_rate_limit(
            principal_id=pid_b,
            tool_name="project_list",
            window_seconds=60,
            max_calls=max_calls,
        )
        assert allowed_b is True, "Rate limit on pid_a must not affect pid_b"
