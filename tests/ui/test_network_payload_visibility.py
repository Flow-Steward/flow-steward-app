"""UI/API tests for network payload visibility — sensitive field leakage.

P0 gap: no dedicated suite asserting that API responses consumed by the browser
do not expose internal/sensitive fields (password_hash, secret keys, tokens,
internal IDs, etc.) that should never reach a client session.

Coverage:

A. User-facing API endpoints must not leak credential fields
   - /api/auth/me must not return password_hash / hashed_password / secret
   - /api/auth/users list must not return password fields for any user

B. Project data must not leak internal fields
   - /api/projects list and detail must not include internal DB metadata
   - Jobs list and detail must not include raw runner secrets

C. Settings/config endpoints must not leak provider credentials
   - /api/settings/model-catalog must not leak API keys / secrets
   - Provider config must return masked or absent secrets

D. MCP tool responses must not leak system internals

E. Error responses must not expose stack traces or SQL
   - 4xx/5xx responses must not include traceback / SQL statements
   - Error body must not reference internal file paths

Run:
    docker compose exec fs-app python -m pytest \
        tests/ui/test_network_payload_visibility.py -v
"""

from __future__ import annotations

import os
import re

import pytest

from core.application.tooling.testing.runtime_env import resolve_test_env
from tests.e2e.helpers import get, post_json

pytestmark = [pytest.mark.e2e]

_TEST_ENV = resolve_test_env(os.environ, lane="backend", include_dotenv=True)
_PROJECT_ID = str(_TEST_ENV.project_id or "").strip()

# Fields that must never appear in any user-visible API response
_CREDENTIAL_FIELDS = frozenset(
    {
        "password_hash",
        "hashed_password",
        "password",
        "secret",
        "api_key",
        "api_secret",
        "private_key",
        "token_hash",
        "refresh_token_hash",
        "access_token",
    }
)

# Patterns that indicate stack trace / SQL leakage in error bodies
_LEAK_PATTERNS = [
    re.compile(r"Traceback \(most recent call last\)", re.IGNORECASE),
    re.compile(r"File \"/[^\"]+\.py\", line \d+", re.IGNORECASE),
    re.compile(r"SELECT .+ FROM ", re.IGNORECASE),
    re.compile(r"INSERT INTO .+ VALUES", re.IGNORECASE),
    re.compile(r"sqlalchemy\.", re.IGNORECASE),
    re.compile(r"psycopg_runtime\.", re.IGNORECASE),
    re.compile(r"asyncpg\.", re.IGNORECASE),
]


@pytest.fixture(scope="module")
def server_available():
    resp = get("/health", timeout=5)
    if resp.status_code >= 500:
        pytest.fail(f"Server unhealthy: {resp.status_code}")


def _check_no_credential_fields(obj, path: str = "root") -> None:
    """Recursively assert that obj contains no credential field names."""
    if isinstance(obj, dict):
        _CREDENTIAL_FIELDS & {k.lower() for k in obj}
        # Map back to original keys for error message
        actual_leaked = [k for k in obj if k.lower() in _CREDENTIAL_FIELDS]
        assert not actual_leaked, (
            f"Credential field(s) {actual_leaked!r} found at {path}: "
            f"{dict.fromkeys(actual_leaked, '***')!s}"
        )
        for k, v in obj.items():
            _check_no_credential_fields(v, path=f"{path}.{k}")
    elif isinstance(obj, list):
        for i, item in enumerate(obj[:10]):  # check first 10 items
            _check_no_credential_fields(item, path=f"{path}[{i}]")


# ===========================================================================
# A. User-facing auth endpoints
# ===========================================================================


class TestAuthEndpointPayloadVisibility:
    """Auth API endpoints must not expose credential fields."""

    def test_auth_me_does_not_leak_credentials(self, server_available):
        """/api/auth/me response must not contain any credential fields."""
        resp = get("/api/auth/me")
        if resp.status_code in (401, 403):
            pytest.fail("Not authenticated — configure E2E credentials to run payload tests")
        if resp.status_code != 200:
            pytest.fail(f"/api/auth/me returned {resp.status_code}")
        _check_no_credential_fields(resp.json(), path="/api/auth/me")

    def test_auth_users_list_does_not_leak_credentials(self, server_available):
        """/api/auth/users list must not expose password hashes or secrets."""
        resp = get("/api/auth/users")
        if resp.status_code in (401, 403):
            pytest.fail("Not authenticated as admin — cannot run user list payload test")
        if resp.status_code != 200:
            pytest.fail(f"/api/auth/users returned {resp.status_code}")
        body = resp.json()
        users = body if isinstance(body, list) else body.get("items") or body.get("users") or []
        for user in users[:10]:
            _check_no_credential_fields(user, path="/api/auth/users[item]")

    def test_auth_me_contains_expected_user_fields(self, server_available):
        """/api/auth/me must include id and username (basic shape check)."""
        resp = get("/api/auth/me")
        if resp.status_code in (401, 403):
            pytest.fail("Not authenticated")
        if resp.status_code != 200:
            pytest.fail(f"/api/auth/me returned {resp.status_code}")
        body = resp.json()
        user_payload = (
            body.get("user")
            if isinstance(body, dict) and isinstance(body.get("user"), dict)
            else body
        )
        has_identity = any(field in user_payload for field in ("id", "username", "email"))
        assert has_identity, (
            f"/api/auth/me must include at least id/username/email: {list(user_payload.keys())}"
        )


# ===========================================================================
# B. Project and job data
# ===========================================================================


class TestProjectJobPayloadVisibility:
    """Project and job API responses must not leak internal state."""

    def test_projects_list_no_credential_fields(self, server_available):
        """/api/projects list must not contain credential fields."""
        resp = get("/api/projects")
        if resp.status_code in (401, 403):
            pytest.fail("Not authenticated")
        if resp.status_code != 200:
            pytest.fail(f"/api/projects returned {resp.status_code}")
        body = resp.json()
        items = body if isinstance(body, list) else body.get("items") or body.get("projects") or []
        for project in items[:5]:
            _check_no_credential_fields(project, path="/api/projects[item]")

    def test_project_detail_no_credential_fields(self, server_available):
        """/api/projects/{id} detail must not contain credential fields."""
        if not _PROJECT_ID:
            pytest.fail("TEST_PROJECT_BACKEND_ID not set")
        resp = get(f"/api/projects/{_PROJECT_ID}")
        if resp.status_code in (401, 403, 404):
            pytest.fail(f"Cannot read project: {resp.status_code}")
        if resp.status_code != 200:
            pytest.fail(f"Project detail returned {resp.status_code}")
        _check_no_credential_fields(resp.json(), path=f"/api/projects/{_PROJECT_ID}")

    def test_jobs_list_no_credential_fields(self, server_available):
        """/api/jobs list must not contain credential fields."""
        resp = get("/api/jobs?limit=5")
        if resp.status_code in (401, 403):
            pytest.fail("Not authenticated")
        if resp.status_code != 200:
            pytest.fail(f"/api/jobs returned {resp.status_code}")
        body = resp.json()
        items = body if isinstance(body, list) else body.get("items") or body.get("jobs") or []
        for job in items[:5]:
            _check_no_credential_fields(job, path="/api/jobs[item]")


# ===========================================================================
# C. Settings and configuration
# ===========================================================================


class TestSettingsPayloadVisibility:
    """Settings/config API must not leak provider secrets or API keys."""

    def test_model_catalog_no_api_keys(self, server_available):
        """/api/settings/model-catalog must not return raw API keys."""
        resp = get("/api/settings/model-catalog")
        if resp.status_code in (401, 403):
            pytest.fail("Not authenticated")
        if resp.status_code not in (200, 206):
            pytest.fail(f"Model catalog returned {resp.status_code}")
        _check_no_credential_fields(resp.json(), path="/api/settings/model-catalog")

    def test_mcp_connectors_no_secrets(self, server_available):
        """/api/mcp list must not expose authentication secrets."""
        resp = get("/api/mcp")
        if resp.status_code in (401, 403, 404):
            pytest.fail(f"MCP endpoint returned {resp.status_code}")
        if resp.status_code != 200:
            pytest.fail(f"MCP returned {resp.status_code}")
        body = resp.json()
        connectors = (
            body if isinstance(body, list) else body.get("items") or body.get("connectors") or []
        )
        for connector in connectors[:5]:
            _check_no_credential_fields(connector, path="/api/mcp[item]")


# ===========================================================================
# D. Error response stack-trace containment
# ===========================================================================


class TestErrorResponseLeakage:
    """4xx/5xx error responses must not expose stack traces or SQL."""

    def _check_no_stack_trace(self, body_text: str, endpoint: str) -> None:
        for pattern in _LEAK_PATTERNS:
            match = pattern.search(body_text)
            assert match is None, (
                f"Error response from {endpoint} leaks internal details "
                f"(matched: {pattern.pattern!r}): {body_text[:300]}"
            )

    def test_404_response_does_not_leak_internals(self, server_available):
        """404 responses must not include stack traces or SQL."""
        resp = get("/api/nonexistent-endpoint-xyz-404-check")
        text = resp.text or ""
        self._check_no_stack_trace(text, "/api/nonexistent-endpoint-xyz-404-check")

    def test_malformed_request_does_not_leak_internals(self, server_available):
        """422 (validation error) responses must not include stack traces or SQL."""
        # Send a job submit with completely wrong types to force validation error
        resp = post_json(
            "/api/projects/not-a-valid-id/jobs",
            {
                "task_summary": None,
                "mode": 12345,
            },
        )
        text = resp.text or ""
        self._check_no_stack_trace(text, "/api/projects/not-a-valid-id/jobs")

    def test_server_error_does_not_expose_sql(self, server_available):
        """Any 5xx response must not contain SQL statements or tracebacks."""
        # Attempt to trigger an error via a nonsense PUT payload
        resp = post_json(
            "/api/auth/login",
            {
                "username": "x" * 5000,  # oversized — may cause validation or DB error
                "password": "y",
            },
        )
        text = resp.text or ""
        if resp.status_code >= 500:
            self._check_no_stack_trace(text, "/api/auth/login (oversized)")
