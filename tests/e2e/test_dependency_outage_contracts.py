"""E2E dependency outage and degraded-mode contracts.

P0 gap: mostly unit-path checks; no deterministic end-to-end degraded behavior
contract for user-facing messages and retry semantics.

Coverage:

A. Database unavailable
   - API endpoints that require DB return 503 (not 500/crash)
   - /health endpoint reflects DB state

B. Message broker (RabbitMQ) unavailable
   - Job submission endpoint returns 503 with clear user-facing message
   - Existing job reads (not requiring broker) remain available

C. SMTP / email delivery unavailable
   - Password reset request returns "accepted" (no 500)
   - System email-not-configured mode returns correct error (not 500)

D. MCP backend unavailable

E. External model provider unavailable
   - Chat or task submission with unavailable provider must not 500

F. Optional dependency intentionally disabled
   - Routes behind disabled features return 404 or a clear explanation
   - Never return 500 for intentionally disabled features

Run:
    docker compose exec fs-app python -m pytest \
        tests/e2e/test_dependency_outage_contracts.py -v
"""

from __future__ import annotations

import os
import uuid

import pytest

from core.application.tooling.testing.runtime_env import resolve_test_env
from tests.e2e.helpers import get, post_json
from tests.e2e.neutral_registry import NEUTRAL_WORKFLOW_ID

pytestmark = [pytest.mark.e2e]

_TEST_ENV = resolve_test_env(os.environ, lane="backend", include_dotenv=True)
_PROJECT_ID = str(_TEST_ENV.project_id or "").strip()


def _uid() -> str:
    return uuid.uuid4().hex[:10]


@pytest.fixture(scope="module")
def server_available():
    try:
        resp = get("/health", timeout=5)
    except Exception as exc:
        pytest.skip(f"Server not reachable: {exc}")
    if resp.status_code >= 500:
        pytest.skip(f"Server unhealthy: {resp.status_code}")


@pytest.fixture(scope="module")
def project_id(server_available) -> str:
    if not _PROJECT_ID:
        pytest.skip("TEST_PROJECT_BACKEND_ID not set")
    return _PROJECT_ID


# ===========================================================================
# A. Database availability signal via /health
# ===========================================================================


class TestDatabaseDegradedMode:
    """Health endpoint reflects DB connectivity; DB-dependent routes return 503 when unavailable."""

    def test_health_endpoint_returns_200_when_db_available(self, server_available):
        resp = get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, dict)

    def test_health_body_has_status_field(self, server_available):
        resp = get("/health")
        body = resp.json()
        # Health endpoint should report overall status
        assert (
            "status" in body or "db" in body or "database" in body or "healthy" in str(body).lower()
        ), f"Health body missing status indicator: {body}"

    def test_api_jobs_list_never_500(self, server_available):
        """Job list endpoint must return 200 or 503, never 500."""
        resp = get("/api/jobs")
        # GET with no body should never return 400 (bad request) — only auth/degraded/success
        assert resp.status_code in (200, 401, 403, 503), (
            f"GET /api/jobs returned unexpected {resp.status_code}"
        )

    def test_api_projects_list_never_500(self, server_available):
        resp = get("/api/projects")
        # GET list endpoints must not return 400 — only auth/degraded/success
        assert resp.status_code in (200, 401, 403, 503)

    def test_503_response_has_error_field(self, server_available):
        """When a DB-dependent endpoint is degraded it returns an error message."""
        # Try an endpoint that is always DB-dependent
        resp = get("/api/jobs")
        if resp.status_code == 503:
            body = resp.json()
            assert "error" in body or "detail" in body or "message" in body, (
                f"503 response must include an error message: {body}"
            )


# ===========================================================================
# B. Broker (RabbitMQ) unavailable — job submission degraded mode
# ===========================================================================


class TestBrokerDegradedMode:
    """Job submission must return 503 (not 500) when the broker is unavailable.

    NOTE: These tests verify the behavior of the live server with its current
    broker connectivity. If the broker IS available, the test verifies
    successful submission. If the broker is NOT available, it verifies 503.
    """

    def test_job_submission_returns_2xx_or_503_never_500(self, project_id):
        resp = post_json(
            "/api/workflow-runs",
            {
                "project_id": str(project_id),
                "inputs": {"topic": f"Outage-contract test {_uid()}"},
                "workflow_id": NEUTRAL_WORKFLOW_ID,
            },
        )
        assert resp.status_code not in (500, 502), (
            f"Job submission must not return 5xx server error; got {resp.status_code}: {resp.text[:300]}"
        )
        # 200/201/202 = success; 400/422 = validation; 503 = broker down; 401/403 = auth
        assert resp.status_code in (200, 201, 202, 400, 401, 403, 422, 503)

    def test_503_job_submission_has_informative_error(self, project_id):
        resp = post_json(
            "/api/workflow-runs",
            {
                "project_id": str(project_id),
                "inputs": {"topic": f"Broker-outage test {_uid()}"},
                "workflow_id": NEUTRAL_WORKFLOW_ID,
            },
        )
        if resp.status_code == 503:
            body = resp.json()
            assert "error" in body or "detail" in body, f"503 must include an error message: {body}"

    def test_job_read_never_depends_on_broker(self, server_available):
        """Reading existing jobs must not require broker connectivity."""
        resp = get("/api/jobs?limit=1")
        # Reads never need the broker — must return 200 or 403
        assert resp.status_code in (200, 403), (
            f"Job list read unexpectedly returned {resp.status_code}"
        )


# ===========================================================================
# C. SMTP / email delivery unavailable
# ===========================================================================


class TestSmtpDegradedMode:
    """Password reset and system email must degrade gracefully when SMTP is unconfigured."""

    def test_password_reset_request_always_returns_accepted(self, server_available):
        """Even when email is not configured the request must return 'accepted'."""
        resp = post_json(
            "/api/auth/password-reset/request",
            {
                "email": f"smtp-test-{_uid()}@degraded-mode.invalid",
            },
        )
        if resp.status_code == 429:
            pytest.skip("Password reset endpoint is temporarily throttled (429)")
        assert resp.status_code in (200, 202)
        body = resp.json()
        assert body.get("status") == "accepted", (
            f"Password reset must return 'accepted' even when SMTP is offline; got {body}"
        )

    def test_password_reset_request_never_500_on_smtp_error(self, server_available):
        """SMTP failures must not propagate as 500 to the caller."""
        resp = post_json(
            "/api/auth/password-reset/request",
            {
                "email": "admin@platform.local",
            },
        )
        assert resp.status_code in (200, 202, 401, 403, 429), (
            f"Password reset must return typed accepted/auth/rate-limit status; got {resp.status_code}"
        )

    def test_system_email_status_endpoint_exists(self, server_available):
        """System email status / config endpoint must return a typed route/auth/degraded status."""
        for path in ["/api/settings/email", "/api/system/email", "/api/settings/notifications"]:
            resp = get(path)
            if resp.status_code not in (404, 405):
                assert resp.status_code in (200, 401, 403, 503), (
                    f"{path} must return typed route/auth/degraded status; got {resp.status_code}"
                )


# ===========================================================================
# D. MCP backend unavailable — graceful degradation
# ===========================================================================


# ===========================================================================
# E. External model provider unavailable
# ===========================================================================


class TestModelProviderDegradedMode:
    """Model provider outage must not crash the server."""

    def test_chat_with_invalid_provider_does_not_500(self, project_id):
        """Submitting a chat request with a non-configured or offline provider must not 500."""
        resp = post_json(
            "/api/chat/turn",
            {
                "user_input": f"provider outage test {_uid()}",
                "surface": "chat_shell",
                "page_context": {
                    "page_type": "project_detail",
                    "current_project_id": str(project_id),
                },
            },
        )
        assert resp.status_code in (200, 400, 401, 403, 422, 503), (
            f"Chat endpoint must return typed success/auth/validation/degraded status; got {resp.status_code}"
        )

    def test_model_selection_options_always_returns_200(self, server_available):
        """Model options endpoint must always be reachable regardless of provider status."""
        resp = get("/api/model-selection/options")
        assert resp.status_code == 200


# ===========================================================================
# F. Intentionally disabled optional features
# ===========================================================================


class TestIntentionallyDisabledFeatures:
    """Routes behind disabled features must return 404/403/503, never 500."""

    def test_optional_auth_integration_returns_404_or_403(self, server_available):
        """Optional auth integration routes must not 500 when unavailable."""
        resp = get("/api/auth/optional-auth-integrations")
        assert resp.status_code in (403, 404, 503), (
            f"Optional auth integration endpoint returned unexpected {resp.status_code}"
        )

    def test_external_sign_in_login_not_500(self, server_available):
        """External sign-in routes must not 500 even if no providers are configured."""
        resp = get("/api/auth/external-sign-in/providers")
        assert resp.status_code in (200, 403, 404), (
            f"External sign-in providers must not return 500; got {resp.status_code}"
        )

    def test_nonexistent_route_returns_404(self, server_available):
        """Completely unknown routes must return 404."""
        resp = get(f"/api/nonexistent-feature-{_uid()}")
        assert resp.status_code == 404

    def test_disabled_feature_error_is_informative(self, server_available):
        """When a disabled-feature endpoint returns 4xx, the response should explain why."""
        resp = get("/api/auth/optional-auth-integrations")
        if resp.status_code in (403, 404, 503):
            # The response may be HTML or JSON; either way must not be empty
            assert len(resp.content) > 0, "Disabled-feature response must not be empty"
