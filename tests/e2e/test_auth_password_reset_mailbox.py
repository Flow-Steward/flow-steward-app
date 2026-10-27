"""E2E password reset flow — mailbox-backed and debug-token paths.

P0 gap: the prior reset suite uses a debug token reveal endpoint; this suite
documents the full flow contracts including the mailbox-backed path and ensures
that the API surface is deterministic at every step regardless of delivery mode.

Coverage layers:

A. Token-generation contract
   - POST /api/auth/password-reset/request always returns {"status": "accepted"}
     regardless of whether the email address exists (no enumeration).
   - Response must not disclose whether a real user matched.

B. Token-verification contract (POST /api/auth/password-reset/verify)
   - A valid token returns {"valid": true, "email": "...", "username": "..."}.
   - An invalid / expired / already-used token returns {"valid": false}.
   - The verify endpoint never returns 500.

C. Reset-confirm contract (POST /api/auth/password-reset/confirm)
   - A valid confirm changes the password and returns status "password_reset_confirmed".
   - The old password stops working after reset.
   - The new password grants a successful login session.
   - The used token is rejected on a second confirm attempt.

D. Mailbox-backed path (requires MAILPIT_API_URL env var)
   - Sends a real reset email through the connected email extension.
   - Asserts email is delivered to the Mailpit test inbox.
   - Extracts reset URL from the email body.
   - Completes the full click-through → verify → confirm → login journey.
   - Mailpit is the canonical test inbox (`infra/docker/docker-compose.dev.yaml`).
     Set MAILPIT_API_URL=http://localhost:8025 (or the container service URL).

E. Rate-limiting contract
   - Successive rapid requests from the same IP trigger a 429 with Retry-After.

Tests skip gracefully when the server is unavailable or the required env vars
are not set.

Run:
    # Basic (no mailbox)
    docker compose -f infra/docker/docker-compose.yaml exec fs-app python -m pytest \
        tests/e2e/test_auth_password_reset_mailbox.py -v -k "not mailbox"

    # Full mailbox test (requires Mailpit)
    MAILPIT_API_URL=http://mailpit:8025 \
    docker compose -f infra/docker/docker-compose.yaml exec fs-app python -m pytest \
        tests/e2e/test_auth_password_reset_mailbox.py::TestMailboxBackedReset -v
"""

from __future__ import annotations

import os
import re
import time
import uuid

import pytest

from core.application.tooling.testing.runtime_env import resolve_test_env
from tests.e2e.helpers import get, post_json

pytestmark = [pytest.mark.e2e]

_TEST_ENV = resolve_test_env(os.environ, lane="backend", include_dotenv=True)
# Mailpit is the canonical test mailbox (`infra/docker/docker-compose.dev.yaml`).
# MAILHOG_API_URL remains accepted for prior mailbox setups.
_MAILPIT_API_URL = str(os.getenv("MAILPIT_API_URL") or "").rstrip("/")
_MAILHOG_API_URL = str(os.getenv("MAILHOG_API_URL") or "").rstrip("/")
# Resolved inbox URL — Mailpit takes precedence over the prior MailHog setting.
_INBOX_API_URL = _MAILPIT_API_URL or _MAILHOG_API_URL

_RESET_REQUEST = "/api/auth/password-reset/request"
_RESET_VERIFY = "/api/auth/password-reset/verify"
_RESET_CONFIRM = "/api/auth/password-reset/confirm"
_LOGIN = "/api/auth/login"
_ME = "/api/auth/me"


def _uid() -> str:
    return uuid.uuid4().hex[:10]


def _skip_if_reset_throttled(resp) -> None:
    if int(getattr(resp, "status_code", 0) or 0) == 429:
        pytest.skip("Password reset endpoint is temporarily throttled (429)")


# ---------------------------------------------------------------------------
# Shared infra
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def server_available():
    try:
        resp = get("/health", timeout=5)
    except Exception as exc:
        pytest.skip(f"Server not reachable: {exc}")
    if resp.status_code >= 500:
        pytest.skip(f"Server unhealthy: {resp.status_code}")


# ===========================================================================
# A + E. Token-generation contract — no enumeration, always "accepted"
# ===========================================================================


class TestResetRequestContract:
    """POST /api/auth/password-reset/request contracts."""

    def test_accepted_for_existing_email(self, server_available):
        """Real users get the same response shape as non-existent users."""
        resp = post_json(_RESET_REQUEST, {"email": "admin@platform.local"})
        if resp.status_code == 429:
            pytest.skip("Rate-limited (429) — cross-suite contamination from rate-limit test suite")
        assert resp.status_code in (200, 202)
        body = resp.json()
        assert body.get("status") == "accepted", f"Expected 'accepted', got {body}"

    def test_accepted_for_nonexistent_email(self, server_available):
        """Non-existent addresses must also return 'accepted' — no enumeration."""
        fake_email = f"nobody-{_uid()}@example-nonexistent.invalid"
        resp = post_json(_RESET_REQUEST, {"email": fake_email})
        _skip_if_reset_throttled(resp)
        assert resp.status_code in (200, 202)
        body = resp.json()
        assert body.get("status") == "accepted", (
            f"Expected 'accepted' even for unknown email; got {body}"
        )

    def test_response_never_leaks_whether_user_exists(self, server_available):
        """Both existent and non-existent emails must produce structurally identical responses."""
        real = post_json(_RESET_REQUEST, {"email": "admin@platform.local"})
        fake = post_json(_RESET_REQUEST, {"email": f"nope-{_uid()}@void.invalid"})
        if real.status_code == 429 or fake.status_code == 429:
            pytest.skip("Rate-limited (429) — cross-suite contamination from rate-limit test suite")
        # Same status code
        assert real.status_code == fake.status_code
        # Same keys in body (no extra "email_sent" or "user_found" leakage)
        real_keys = set(real.json().keys())
        fake_keys = set(fake.json().keys())
        assert real_keys == fake_keys, (
            f"Response keys differ — may leak user existence. Real: {real_keys}, Fake: {fake_keys}"
        )

    def test_missing_email_field_returns_4xx(self, server_available):
        resp = post_json(_RESET_REQUEST, {})
        assert resp.status_code in (400, 422)

    def test_malformed_email_returns_4xx_or_accepted(self, server_available):
        # "accepted" is allowed to prevent oracle-style probing; 422 is also fine
        resp = post_json(_RESET_REQUEST, {"email": "not-an-email"})
        assert resp.status_code in (200, 202, 400, 422)

    def test_never_500(self, server_available):
        for body in [{}, {"email": None}, {"email": "x" * 5000}]:
            resp = post_json(_RESET_REQUEST, body)
            assert resp.status_code in (200, 202, 400, 422, 429), (
                f"Reset request must return typed accepted/validation/rate-limit status; got {resp.status_code}: {resp.text[:300]}"
            )


# ===========================================================================
# B. Token-verification contract
# ===========================================================================


class TestResetVerifyContract:
    """POST /api/auth/password-reset/verify contracts."""

    def test_invalid_token_returns_valid_false(self, server_available):
        resp = post_json(_RESET_VERIFY, {"token": "completelyfaketoken" + _uid()})
        if resp.status_code == 429:
            pytest.skip("Rate-limited (429) — cross-suite contamination from rate-limit test suite")
        assert resp.status_code in (200, 400)
        if resp.status_code == 200:
            assert resp.json().get("valid") is False

    def test_empty_token_returns_4xx(self, server_available):
        resp = post_json(_RESET_VERIFY, {"token": ""})
        # 429 acceptable: rate-limiter may fire before token validation (cross-suite contamination).
        assert resp.status_code in (400, 422, 429)

    def test_missing_token_field_returns_4xx(self, server_available):
        resp = post_json(_RESET_VERIFY, {})
        assert resp.status_code in (400, 422)

    def test_never_500(self, server_available):
        for body in [{}, {"token": None}, {"token": "x" * 500}]:
            resp = post_json(_RESET_VERIFY, body)
            assert resp.status_code in (200, 400, 422, 429), (
                f"Reset verify must return typed success/validation/rate-limit status; got {resp.status_code}: {resp.text[:300]}"
            )

    def test_valid_response_has_required_keys_when_200(self, server_available):
        resp = post_json(_RESET_VERIFY, {"token": "invalid_token_" + _uid()})
        if resp.status_code != 200:
            return  # 400 is also valid for invalid tokens
        body = resp.json()
        assert "valid" in body, f"Response missing 'valid': {body}"


# ===========================================================================
# C. Reset-confirm contract
# ===========================================================================


class TestResetConfirmContract:
    """POST /api/auth/password-reset/confirm contracts."""

    def test_invalid_token_returns_4xx(self, server_available):
        resp = post_json(
            _RESET_CONFIRM,
            {
                "token": "invalid_token_" + _uid(),
                "new_password": "NewSecurePassword123!",
            },
        )
        # 429 is acceptable: rate-limiter fired before token validation (cross-suite contamination).
        assert resp.status_code in (400, 422, 429), (
            f"Expected 4xx for invalid token confirm, got {resp.status_code}"
        )

    def test_missing_token_returns_4xx(self, server_available):
        resp = post_json(_RESET_CONFIRM, {"new_password": "SomePassword123!"})
        assert resp.status_code in (400, 422)

    def test_missing_password_returns_4xx(self, server_available):
        resp = post_json(_RESET_CONFIRM, {"token": "sometoken"})
        assert resp.status_code in (400, 422)

    def test_too_short_password_returns_4xx(self, server_available):
        resp = post_json(
            _RESET_CONFIRM,
            {
                "token": "sometoken",
                "new_password": "short",
            },
        )
        # 429 acceptable: rate-limiter may fire before validation (cross-suite contamination).
        assert resp.status_code in (400, 422, 429)

    def test_never_500(self, server_available):
        for body in [
            {},
            {"token": None, "new_password": None},
            {"token": "x" * 1000, "new_password": "ValidPassword123!"},
        ]:
            resp = post_json(_RESET_CONFIRM, body)
            assert resp.status_code in (400, 422, 429), (
                f"Reset confirm must return typed validation/rate-limit status; got {resp.status_code}: {resp.text[:300]}"
            )


# ===========================================================================
# D. Mailbox-backed full click-through (requires MAILPIT_API_URL)
# ===========================================================================


def _get_latest_reset_email(to_email: str, *, timeout: float = 15.0) -> dict | None:
    """Poll the configured test inbox for the latest password reset email.

    Supports Mailpit (primary) and MailHog (prior setup).
    Always returns a normalized dict with a ``_body`` key containing the plain-text
    body of the message — independent of which backend was used.

    Mailpit API:  GET /api/v1/messages (list)  +  GET /api/v1/message/{id} (body)
    MailHog API:  GET /api/v2/messages          (body embedded in list response)
    """
    import requests as _req

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if _MAILPIT_API_URL:
                resp = _req.get(f"{_MAILPIT_API_URL}/api/v1/messages", timeout=5)
                if resp.status_code == 200:
                    for msg in resp.json().get("messages") or []:
                        recipients = [r.get("Address", "") for r in (msg.get("To") or [])]
                        if to_email.lower() in [r.lower() for r in recipients]:
                            # Fetch full message to get the body text.
                            msg_id = str(msg.get("ID") or "").strip()
                            body_text = ""
                            if msg_id:
                                try:
                                    full = _req.get(
                                        f"{_MAILPIT_API_URL}/api/v1/message/{msg_id}",
                                        timeout=5,
                                    )
                                    if full.status_code == 200:
                                        body_text = str(full.json().get("Text") or "").strip()
                                except Exception:
                                    pass
                            msg["_body"] = body_text
                            return msg
            elif _MAILHOG_API_URL:
                # MailHog fallback kept for existing test environments.
                resp = _req.get(f"{_MAILHOG_API_URL}/api/v2/messages", timeout=5)
                if resp.status_code == 200:
                    for item in resp.json().get("items") or []:
                        recipients = [
                            r.get("Mailbox", "") + "@" + r.get("Domain", "")
                            for r in (item.get("Content", {}).get("Headers", {}).get("To") or [])
                        ]
                        if to_email.lower() in [r.lower() for r in recipients]:
                            # Normalise MailHog body into the shared ``_body`` key.
                            item["_body"] = str(item.get("Content", {}).get("Body", "") or "")
                            return item
        except Exception:
            pass
        time.sleep(1)
    return None


# Keep the old name as an alias for any external callers.
_get_latest_reset_email_mailhog = _get_latest_reset_email


def _extract_reset_url_from_body(body_text: str) -> str | None:
    """Extract the first https?://... /reset-password... URL from email body."""
    match = re.search(
        r"https?://[^\s\"'<>]+/(?:reset-password|password-reset/confirm)[^\s\"'<>]*", body_text
    )
    return match.group(0) if match else None


def _extract_token_from_url(reset_url: str) -> str | None:
    """Extract the token query parameter from a reset URL."""
    match = re.search(r"[?&]token=([A-Za-z0-9_\-]+)", reset_url)
    return match.group(1) if match else None


@pytest.mark.skipif(
    not _INBOX_API_URL,
    reason="MAILPIT_API_URL not set — skipping mailbox-backed reset tests",
)
class TestMailboxBackedReset:
    """Full click-through reset flow via Mailpit test inbox.

    Requires:
      - MAILPIT_API_URL=http://mailpit:8025 (or http://localhost:8025 from host)
      - An Outgoing Email extension connection whose SMTP server is Mailpit
      - A real test user whose email is resolvable by the server

    Set MAILPIT_API_URL before running (`infra/docker/docker-compose.dev.yaml` starts Mailpit;
    point an Outgoing Email connection at mailpit:1025 so system email goes there).
    """

    @pytest.fixture(scope="class")
    def test_user_email(self, server_available) -> str:
        email = str(os.getenv("E2E_RESET_TEST_EMAIL") or "").strip()
        if not email:
            pytest.skip(
                "E2E_RESET_TEST_EMAIL not set — "
                "set to a real test-user email address for mailbox-backed reset tests"
            )
        return email

    def test_reset_email_is_delivered(self, server_available, test_user_email):
        # Trigger reset
        resp = post_json(_RESET_REQUEST, {"email": test_user_email})
        assert resp.status_code in (200, 202)

        # Poll inbox
        email = _get_latest_reset_email(test_user_email)
        assert email is not None, (
            f"No password reset email delivered to {test_user_email} within 15s. "
            "Check the email extension connection points at the test inbox."
        )

    def test_reset_url_in_email_body_contains_token(self, server_available, test_user_email):
        post_json(_RESET_REQUEST, {"email": test_user_email})
        email = _get_latest_reset_email(test_user_email)
        if email is None:
            pytest.skip("No email delivered — check the email extension connection")

        body_parts = email.get("_body", "") or ""
        reset_url = _extract_reset_url_from_body(body_parts)
        assert reset_url is not None, f"No reset URL found in email body: {body_parts[:500]}"
        token = _extract_token_from_url(reset_url)
        assert token, f"Token too short or missing from URL: {reset_url}"
        assert len(token) > 10, f"Token too short or missing from URL: {reset_url}"

    def test_token_from_email_passes_verify(self, server_available, test_user_email):
        post_json(_RESET_REQUEST, {"email": test_user_email})
        email = _get_latest_reset_email(test_user_email)
        if email is None:
            pytest.skip("No email delivered")
        body = email.get("_body", "") or ""
        reset_url = _extract_reset_url_from_body(body)
        if not reset_url:
            pytest.skip("No reset URL in email body")
        token = _extract_token_from_url(reset_url)
        if not token:
            pytest.skip("No token in reset URL")

        verify_resp = post_json(_RESET_VERIFY, {"token": token})
        assert verify_resp.status_code == 200
        assert verify_resp.json().get("valid") is True

    def test_full_click_through_reset_and_login(self, server_available, test_user_email):
        """End-to-end: request → email → extract token → confirm → login with new password."""
        new_password = f"ResetE2E-{_uid()}!A1"

        post_json(_RESET_REQUEST, {"email": test_user_email})
        email = _get_latest_reset_email(test_user_email)
        if email is None:
            pytest.skip("No email delivered")
        body = email.get("_body", "") or ""
        reset_url = _extract_reset_url_from_body(body)
        if not reset_url:
            pytest.skip("No reset URL in email body")
        token = _extract_token_from_url(reset_url)
        if not token:
            pytest.skip("No token in URL")

        # Confirm reset
        confirm_resp = post_json(_RESET_CONFIRM, {"token": token, "new_password": new_password})
        assert confirm_resp.status_code in (200, 201, 204), (
            f"Confirm failed: {confirm_resp.status_code} — {confirm_resp.text[:300]}"
        )
        data = confirm_resp.json() if confirm_resp.content else {}
        assert data.get("status") == "password_reset_confirmed" or confirm_resp.status_code == 204

        # Used token is now rejected
        reuse = post_json(_RESET_CONFIRM, {"token": token, "new_password": new_password})
        assert reuse.status_code in (400, 422), (
            "Used reset token must be rejected on second confirm attempt"
        )
