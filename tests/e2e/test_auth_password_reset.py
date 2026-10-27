"""E2E tests for password-reset flow (AUTH-03).

Covers the 3-step self-service password reset:
  1. POST /api/auth/password-reset/request
  2. POST /api/auth/password-reset/verify
  3. POST /api/auth/password-reset/confirm

Requires FS_AUTH_PASSWORD_RESET_DEBUG_REVEAL_TOKEN=1 in the test
environment for the full lifecycle test (otherwise token is not returned).
"""

from __future__ import annotations

import pytest

from tests.e2e.helpers import (
    E2ECleanup,
    get,
    post_json,
    unique_id,
)

pytestmark = [pytest.mark.e2e]


@pytest.fixture(scope="module")
def server_available():
    try:
        resp = get("/health", timeout=5)
    except Exception as e:
        pytest.skip(f"Flow Steward server not available: {e}")
    if resp.status_code >= 500:
        pytest.skip(f"Flow Steward server unhealthy: {resp.status_code}")


def _password_reset_enabled() -> bool:
    """Check whether the password-reset endpoint is enabled in the target env."""
    resp = post_json("/api/auth/password-reset/request", {"email": "probe@example.com"})
    if resp.status_code == 400:
        err = str((resp.json() or {}).get("error") or "").lower()
        if "disabled" in err:
            return False
    # 200 or other 4xx means the route exists and is enabled.
    return resp.status_code != 404


def _skip_if_reset_throttled(resp) -> None:
    if int(getattr(resp, "status_code", 0) or 0) == 429:
        pytest.skip("Password reset endpoint is temporarily throttled (429)")


def _create_temp_user(cleanup: E2ECleanup) -> tuple[str, str, str]:
    """Create an isolated e2e user and register it for cleanup.

    Returns (user_id, email, password).
    """
    email = f"{unique_id('e2e_pwreset')}@example.com"
    password = "E2eResetPass_1234!"
    resp = post_json(
        "/api/auth/users",
        {
            "email": email,
            "password": password,
            "display_name": f"PW Reset E2E {email}",
            "roles": ["viewer"],
            "is_active": True,
        },
    )
    assert resp.status_code == 200, (
        f"Failed to create temp user: {resp.status_code} {resp.text[:300]}"
    )
    body = resp.json()
    user = body if "id" in body else body.get("user", body)
    user_id = str(user.get("id") or user.get("user_id") or "").strip()
    assert user_id, f"No user id in create response: {body}"
    # Disable user on cleanup to avoid leftover accounts.
    cleanup.add("PUT", f"/api/auth/users/{user_id}", {"is_active": False})
    return user_id, email, password


class TestPasswordResetFlow:
    def test_password_reset_full_lifecycle(self, server_available):
        if not _password_reset_enabled():
            pytest.skip("Password reset is disabled in the target environment")

        cleanup = E2ECleanup()
        try:
            _user_id, email, old_password = _create_temp_user(cleanup)

            # Step 1: request reset token
            req_resp = post_json("/api/auth/password-reset/request", {"email": email})
            _skip_if_reset_throttled(req_resp)
            assert req_resp.status_code == 200, f"reset request failed: {req_resp.text[:300]}"
            req_body = req_resp.json()
            assert req_body.get("status") == "accepted"
            token = req_body.get("reset_token")
            if not token:
                pytest.skip(
                    "Server did not reveal reset_token — set "
                    "FS_AUTH_PASSWORD_RESET_DEBUG_REVEAL_TOKEN=1 in the test environment"
                )

            # Step 2: verify token
            verify_resp = post_json("/api/auth/password-reset/verify", {"token": token})
            assert verify_resp.status_code == 200
            verify_body = verify_resp.json()
            assert verify_body.get("valid") is True
            assert verify_body.get("username") == email

            # Step 3: confirm reset with new password
            new_password = "E2eNewPass_5678!"
            confirm_resp = post_json(
                "/api/auth/password-reset/confirm",
                {
                    "token": token,
                    "new_password": new_password,
                    "revoke_other_sessions": True,
                },
            )
            assert confirm_resp.status_code == 200, (
                f"reset confirm failed: {confirm_resp.text[:300]}"
            )
            confirm_body = confirm_resp.json()
            assert confirm_body.get("status") == "password_reset_confirmed"
            assert isinstance(confirm_body.get("revoked_sessions"), int)

            # Verify new password works (login with fresh session)
            import requests as _requests

            fresh = _requests.Session()
            login_ok = fresh.post(
                f"{get('/health').url.rsplit('/health', 1)[0]}/api/auth/login",
                json={"email": email, "password": new_password},
                verify=False,
                timeout=10,
            )
            assert login_ok.status_code == 200, "Login with new password should succeed"

            # Verify old password fails
            login_old = fresh.post(
                f"{get('/health').url.rsplit('/health', 1)[0]}/api/auth/login",
                json={"email": email, "password": old_password},
                verify=False,
                timeout=10,
            )
            assert login_old.status_code != 200, "Login with old password should fail"
        finally:
            cleanup.run()

    def test_password_reset_request_unknown_user_still_accepted(self, server_available):
        if not _password_reset_enabled():
            pytest.skip("Password reset is disabled in the target environment")

        resp = post_json(
            "/api/auth/password-reset/request",
            {"email": "e2e_nonexistent_user_12345@example.com"},
        )
        _skip_if_reset_throttled(resp)
        assert resp.status_code == 200
        body = resp.json()
        # Must not leak whether user exists.
        assert body.get("status") == "accepted"

    def test_password_reset_verify_invalid_token(self, server_available):
        if not _password_reset_enabled():
            pytest.skip("Password reset is disabled in the target environment")

        resp = post_json("/api/auth/password-reset/verify", {"token": "e2e_bogus_token_00000000"})
        if resp.status_code == 429:
            pytest.skip(
                "Password-reset verify endpoint is temporarily throttled (429); "
                "skip to avoid cross-test lockout contamination"
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body.get("valid") is False

    def test_password_reset_confirm_invalid_token(self, server_available):
        if not _password_reset_enabled():
            pytest.skip("Password reset is disabled in the target environment")

        resp = post_json(
            "/api/auth/password-reset/confirm",
            {
                "token": "e2e_bogus_token_00000000",
                "new_password": "IrrelevantPass_999!",
            },
        )
        if resp.status_code == 429:
            pytest.skip(
                "Password-reset confirm endpoint is temporarily throttled (429); "
                "skip to avoid cross-test lockout contamination"
            )
        assert resp.status_code in (400, 404)
        err = str((resp.json() or {}).get("error") or "").lower()
        assert "invalid" in err or "expired" in err or "token" in err
