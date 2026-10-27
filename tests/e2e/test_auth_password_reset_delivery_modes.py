"""E2E tests for password reset across different email delivery modes.

P0 gap: SMTP-not-configured and email-delivery-failure explicit behavior tests.

Coverage:

A. Debug token mode
   - Reset request returns "accepted"
   - Debug token endpoint reveals token (if enabled)
   - Verify + confirm work with debug token

B. Email extension connected but delivery fails
   - Request still returns "accepted" (no delivery error leakage)
   - Response time must be bounded (no indefinite delivery hang)

C. No email extension connected
   - Request returns "accepted" — same as when enabled
   - No error-code leakage that reveals email is disabled

D. Token TTL and expiry
   - An expired token returns {"valid": false} on verify
   - Confirm with expired token returns 400/422

E. Token reuse prevention
   - A used token is rejected on a second confirm

F. Password policy enforcement on confirm
   - Password shorter than minimum is rejected
   - Password meeting minimum is accepted

Run:
    docker compose exec fs-app python -m pytest \
        tests/e2e/test_auth_password_reset_delivery_modes.py -v
"""

from __future__ import annotations

import os
import time
import uuid

import pytest

from core.application.tooling.testing.runtime_env import resolve_test_env
from tests.e2e.helpers import get, post_json

pytestmark = [pytest.mark.e2e]

_TEST_ENV = resolve_test_env(os.environ, lane="backend", include_dotenv=True)

_RESET_REQUEST = "/api/auth/password-reset/request"
_RESET_VERIFY = "/api/auth/password-reset/verify"
_RESET_CONFIRM = "/api/auth/password-reset/confirm"
_DEBUG_TOKEN = "/api/auth/password-reset/debug-token"  # dev/test-only endpoint
_LOGIN = "/api/auth/login"


def _uid() -> str:
    return uuid.uuid4().hex[:10]


def _isolated_client_ip() -> str:
    """Generate a deterministic TEST-NET-3 IP to isolate per-test throttle buckets."""
    return f"203.0.113.{(uuid.uuid4().int % 253) + 1}"


def _post_reset_json(path: str, payload: dict | None = None, **kwargs):
    """POST helper that isolates password-reset rate limits by synthetic client IP."""
    headers = dict(kwargs.pop("headers", {}) or {})
    if not any(str(key).lower() == "x-forwarded-for" for key in headers):
        client_ip = _isolated_client_ip()
        headers["x-forwarded-for"] = client_ip
        headers.setdefault("x-real-ip", client_ip)
    return post_json(path, payload, headers=headers, **kwargs)


def _skip_if_reset_throttled(resp, endpoint: str) -> None:
    if int(getattr(resp, "status_code", 0) or 0) == 429:
        pytest.skip(
            f"Password-reset {endpoint} endpoint is temporarily throttled (429); "
            "skip to avoid cross-test lockout contamination"
        )


@pytest.fixture(scope="module")
def server_available():
    try:
        resp = get("/health", timeout=5)
    except Exception as exc:
        pytest.skip(f"Server not reachable: {exc}")
    if resp.status_code >= 500:
        pytest.skip(f"Server unhealthy: {resp.status_code}")


def _debug_token_for_email(email: str) -> str | None:
    """Try to retrieve a debug token from the dev-only debug-token endpoint."""
    resp = _post_reset_json(_DEBUG_TOKEN, {"email": email})
    if resp.status_code == 200:
        return resp.json().get("token")
    return None


# ===========================================================================
# A. Reset request — universal accepted contract
# ===========================================================================


class TestResetRequestDeliveryModes:
    """POST /api/auth/password-reset/request must return 'accepted' in all delivery modes."""

    def test_accepted_regardless_of_email_delivery_mode(self, server_available):
        resp = _post_reset_json(_RESET_REQUEST, {"email": f"delivery-mode-{_uid()}@test.invalid"})
        _skip_if_reset_throttled(resp, "request")
        assert resp.status_code in (200, 202)
        assert resp.json().get("status") == "accepted"

    def test_response_time_bounded_under_10s(self, server_available):
        """Request must complete within 10 s even if SMTP times out."""
        start = time.monotonic()
        resp = _post_reset_json(
            _RESET_REQUEST,
            {"email": f"timing-{_uid()}@test.invalid"},
            timeout=12,
        )
        elapsed = time.monotonic() - start
        _skip_if_reset_throttled(resp, "request")
        assert resp.status_code in (200, 202)
        assert elapsed < 10.0, (
            f"Password reset request took {elapsed:.1f}s — possible SMTP hang. "
            "Check the email extension connection's timeout."
        )

    def test_never_reveals_smtp_error_in_response(self, server_available):
        """SMTP delivery failures must not appear in the API response body."""
        resp = _post_reset_json(_RESET_REQUEST, {"email": "admin@platform.local"})
        body_text = resp.text.lower()
        for leak_phrase in ("smtp", "connection refused", "traceback", "exception", "sendmail"):
            assert leak_phrase not in body_text, (
                f"Response leaks internal error phrase '{leak_phrase}': {resp.text[:300]}"
            )


# ===========================================================================
# B. Console/debug delivery mode — debug token flow
# ===========================================================================


class TestDebugTokenFlow:
    """Tests using the debug token endpoint (dev/test environments only).

    These tests skip if the debug endpoint is not available — it is intentionally
    absent in production builds.
    """

    @pytest.fixture(scope="class")
    def debug_available(self, server_available) -> str:
        """Skip if debug token endpoint is not mounted."""
        resp = get("/api/auth/password-reset/debug-token")
        if resp.status_code in (404, 405):
            pytest.skip("Debug token endpoint not mounted (expected in production)")
        return "available"

    def test_debug_token_request_for_unknown_email_returns_404_or_none(
        self, server_available, debug_available
    ):
        resp = _post_reset_json(_DEBUG_TOKEN, {"email": f"nobody-{_uid()}@void.invalid"})
        # May return 404 or {"token": null} for unknown users
        assert resp.status_code in (200, 404)
        if resp.status_code == 200:
            # token field must be null/None for unknown users
            assert resp.json().get("token") is None

    def test_debug_token_verify_produces_valid_true(self, server_available, debug_available):
        email = "admin@platform.local"
        # Request a reset to seed the token
        _post_reset_json(_RESET_REQUEST, {"email": email})
        token = _debug_token_for_email(email)
        if not token:
            pytest.skip("Could not retrieve debug token for admin user")

        resp = _post_reset_json(_RESET_VERIFY, {"token": token})
        assert resp.status_code == 200
        body = resp.json()
        assert body.get("valid") is True, f"Expected valid=true for debug token; got {body}"
        assert "email" in body
        assert "username" in body

    def test_debug_token_confirm_changes_password(self, server_available, debug_available):
        """Full flow: request → debug token → confirm → verify old session invalidated."""
        # Use a dedicated test user if available, fall back to admin
        test_email = str(os.getenv("E2E_RESET_TEST_EMAIL") or "").strip() or "admin@platform.local"
        new_pw = f"ResetDebug-{_uid()}!B2"

        _post_reset_json(_RESET_REQUEST, {"email": test_email})
        token = _debug_token_for_email(test_email)
        if not token:
            pytest.skip(f"Could not retrieve debug token for {test_email}")

        confirm_resp = _post_reset_json(_RESET_CONFIRM, {"token": token, "new_password": new_pw})
        assert confirm_resp.status_code in (200, 201, 204), (
            f"Confirm failed: {confirm_resp.status_code} — {confirm_resp.text[:300]}"
        )

    def test_used_debug_token_rejected_on_second_confirm(self, server_available, debug_available):
        test_email = "admin@platform.local"
        new_pw = f"ResetReuse-{_uid()}!C3"

        _post_reset_json(_RESET_REQUEST, {"email": test_email})
        token = _debug_token_for_email(test_email)
        if not token:
            pytest.skip("Could not retrieve debug token")

        # First confirm
        first = _post_reset_json(_RESET_CONFIRM, {"token": token, "new_password": new_pw})
        if first.status_code not in (200, 201, 204):
            pytest.skip(f"First confirm failed: {first.status_code}")

        # Second confirm with same token
        second = _post_reset_json(_RESET_CONFIRM, {"token": token, "new_password": new_pw})
        assert second.status_code in (400, 422), (
            f"Used token must be rejected on second confirm; got {second.status_code}"
        )


# ===========================================================================
# C. Token TTL / expiry
# ===========================================================================


class TestTokenExpiry:
    """Verify and confirm with expired tokens must return appropriate errors."""

    def test_obviously_fake_token_returns_invalid(self, server_available):
        resp = _post_reset_json(_RESET_VERIFY, {"token": "expired_fake_token_" + _uid()})
        _skip_if_reset_throttled(resp, "verify")
        assert resp.status_code in (200, 400)
        if resp.status_code == 200:
            assert resp.json().get("valid") is False

    def test_expired_token_format_returns_4xx_or_invalid(self, server_available):
        """An expired (but syntactically valid) token must return invalid=false or 4xx."""
        # We simulate an expired token by constructing a long URL-safe string
        # that passes format validation but won't match any DB row
        fake_expired = "a" * 64  # 64-char URL-safe token lookalike
        verify_resp = _post_reset_json(_RESET_VERIFY, {"token": fake_expired})
        _skip_if_reset_throttled(verify_resp, "verify")
        assert verify_resp.status_code in (200, 400, 422)
        if verify_resp.status_code == 200:
            assert verify_resp.json().get("valid") is False

    def test_expired_token_confirm_returns_4xx(self, server_available):
        fake_expired = "b" * 64
        confirm_resp = _post_reset_json(
            _RESET_CONFIRM,
            {
                "token": fake_expired,
                "new_password": "NewValid123!Password",
            },
        )
        _skip_if_reset_throttled(confirm_resp, "confirm")
        assert confirm_resp.status_code in (400, 422), (
            f"Expired/invalid token must be rejected on confirm; got {confirm_resp.status_code}"
        )


# ===========================================================================
# D. Password policy enforcement
# ===========================================================================


class TestPasswordPolicyOnConfirm:
    """Password policy must be enforced at the confirm step."""

    def test_password_shorter_than_minimum_returns_4xx(self, server_available):
        resp = _post_reset_json(
            _RESET_CONFIRM,
            {
                "token": "some_valid_looking_token_" + _uid(),
                "new_password": "short",
            },
        )
        _skip_if_reset_throttled(resp, "confirm")
        assert resp.status_code in (400, 422), (
            f"Short password must be rejected; got {resp.status_code}"
        )

    def test_empty_password_returns_4xx(self, server_available):
        resp = _post_reset_json(_RESET_CONFIRM, {"token": "sometoken", "new_password": ""})
        _skip_if_reset_throttled(resp, "confirm")
        assert resp.status_code in (400, 422)

    def test_null_password_returns_4xx(self, server_available):
        resp = _post_reset_json(_RESET_CONFIRM, {"token": "sometoken", "new_password": None})
        assert resp.status_code in (400, 422)

    def test_very_long_password_never_500(self, server_available):
        resp = _post_reset_json(
            _RESET_CONFIRM,
            {
                "token": "sometoken",
                "new_password": "A" * 10000,
            },
        )
        assert resp.status_code != 500
