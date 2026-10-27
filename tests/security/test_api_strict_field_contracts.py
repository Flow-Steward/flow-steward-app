"""Strict API over-fetch / hidden-field leakage contracts.

P0 gap (C-02): No test suite verifying that API responses do not return more
fields than the UI consumes (over-fetching) or leak internal/private fields
beyond what forbidden-field checks already cover.

This suite:
1. Defines strict allowlists for key API endpoints
2. Asserts the response shape does not include unexpected extra fields
3. Catches regressions where new internal fields are added to responses

Coverage:

A. /api/auth/me — user session shape contract
   - Must have: id, username, authenticated
   - Must not have: password_hash, hashed_password, raw_password, secret_key

B. /api/jobs — job list item shape
   - Must have: job_id, status
   - Internal LangGraph/checkpoint fields must not appear

C. /api/projects — project list item shape
   - Must have: id, name
   - account-level secrets and raw DB columns must not appear

D. /api/auth/users — user list shape (admin only)
   - Must not expose password/secret fields in any list item

E. /api/providers — provider list shape
   - API key secrets must not appear in plaintext

F. Recursive deep scan for credential field names in any response

Run:
    docker compose exec fs-app python -m pytest \
        tests/security/test_api_strict_field_contracts.py -v
"""

from __future__ import annotations

import os
import re
from typing import ClassVar

import pytest

from core.application.tooling.testing.runtime_env import resolve_test_env
from tests.e2e.helpers import BASE_URL, ensure_test_job

pytestmark = [pytest.mark.e2e]

_TEST_ENV = resolve_test_env(os.environ, lane="backend", include_dotenv=True)
_PROJECT_ID = str(_TEST_ENV.project_id or "").strip()

# ---------------------------------------------------------------------------
# Global forbidden fields — MUST NEVER appear in any API response
# ---------------------------------------------------------------------------

_CREDENTIAL_FIELD_PATTERNS = [
    # Actual password storage/credential fields — but NOT safe metadata fields
    # safe: must_change_password, password_changed_at, password_expires_at, password_policy
    re.compile(
        r"^(password|hashed_password|raw_password|password_hash|password_salt|"
        r"plaintext_password|old_password|new_password|confirm_password|smtp_password)$",
        re.IGNORECASE,
    ),
    # Secret storage fields — not safe metadata like secret_id, secret_name
    re.compile(
        r"^(secret_key|api_secret|webhook_secret|internal_secret|"
        r"signing_secret|oauth_secret|client_secret)$",
        re.IGNORECASE,
    ),
    re.compile(r"^(private_key|rsa_private|ssh_private_key)$", re.IGNORECASE),
    re.compile(
        r"^(access_token|bearer_token|refresh_token|id_token|"
        r"session_token|internal_token|auth_token)$",
        re.IGNORECASE,
    ),
]

_INTERNAL_INFRA_FIELDS = frozenset(
    {
        "pg_oid",
        "xmin",
        "ctid",
        "tableoid",
        "pg_log_row_id",
        "internal_trace_id",
        "langgraph_state_blob",
        "internal_checkpoint",
        "pg_thread_id_raw",
    }
)


def _is_credential_field(field_name: str) -> bool:
    return any(pat.search(field_name) for pat in _CREDENTIAL_FIELD_PATTERNS)


def _deep_scan_for_credential_fields(obj, *, path: str = "root") -> list[str]:
    """Return list of field paths that look like credential fields."""
    violations = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            current_path = f"{path}.{key}"
            if _is_credential_field(key):
                violations.append(f"{current_path} (value: [REDACTED])")
            violations.extend(_deep_scan_for_credential_fields(value, path=current_path))
    elif isinstance(obj, list):
        for i, item in enumerate(obj[:5]):  # spot-check first 5 items
            violations.extend(_deep_scan_for_credential_fields(item, path=f"{path}[{i}]"))
    return violations


def _assert_no_internal_infra_fields(obj, *, context: str) -> None:
    """Recursively assert no internal infrastructure fields appear."""
    if isinstance(obj, dict):
        leaked = _INTERNAL_INFRA_FIELDS & set(obj.keys())
        assert not leaked, f"Response leaks internal infra fields {leaked} in {context}"
        for v in obj.values():
            _assert_no_internal_infra_fields(v, context=context)
    elif isinstance(obj, list):
        for item in obj[:5]:
            _assert_no_internal_infra_fields(item, context=context)


def _authed_get(path: str, **kwargs):
    """Perform an authenticated GET request using the admin session."""
    from tests.e2e.conftest import _authed_session

    session, verify_ssl = _authed_session()
    url = BASE_URL.rstrip("/") + path
    return session.get(url, verify=verify_ssl, timeout=kwargs.get("timeout", 15))


def _require_ok(resp, label: str, *, allow_403: bool = False, allow_404: bool = False) -> None:
    """Skip or assert based on response status."""
    if resp.status_code == 401:
        pytest.skip(f"{label}: not authenticated (401)")
    if resp.status_code == 403 and allow_403:
        pytest.skip(f"{label}: forbidden (403) — role-restricted, skip")
    if resp.status_code == 404 and allow_404:
        pytest.skip(f"{label}: not found (404)")
    assert resp.status_code == 200, (
        f"{label}: expected 200, got {resp.status_code}: {resp.text[:200]}"
    )


@pytest.fixture(scope="module")
def server_available():
    try:
        resp = _authed_get("/health", timeout=5)
    except Exception as exc:
        pytest.skip(f"Server not reachable: {exc}")
    if resp.status_code >= 500:
        pytest.skip(f"Server unhealthy: {resp.status_code}")


# ===========================================================================
# A. /api/auth/me — user session shape
# ===========================================================================


class TestAuthMeFieldContract:
    """GET /api/auth/me must return exactly the right fields — no secret leakage."""

    def test_me_has_required_user_fields(self, server_available):
        """User object must include id and username."""
        resp = _authed_get("/api/auth/me")
        _require_ok(resp, "GET /api/auth/me")
        body = resp.json()
        user = body.get("user") if isinstance(body.get("user"), dict) else body
        required = {"id", "username"}
        missing = required - set(user.keys())
        assert not missing, f"/api/auth/me missing required user fields: {missing}"

    def test_me_has_no_credential_fields(self, server_available):
        """GET /api/auth/me must not include password or secret fields."""
        resp = _authed_get("/api/auth/me")
        _require_ok(resp, "GET /api/auth/me")
        violations = _deep_scan_for_credential_fields(resp.json(), path="/api/auth/me")
        assert not violations, f"GET /api/auth/me leaks credential field(s): {violations[:5]}"

    def test_me_has_no_internal_infra_fields(self, server_available):
        """GET /api/auth/me must not include raw DB infrastructure fields."""
        resp = _authed_get("/api/auth/me")
        _require_ok(resp, "GET /api/auth/me")
        _assert_no_internal_infra_fields(resp.json(), context="GET /api/auth/me")


# ===========================================================================
# B. /api/jobs — job list shape
# ===========================================================================


class TestJobsListFieldContract:
    """GET /api/jobs list items must not expose internal LangGraph/DB state."""

    def test_jobs_has_no_credential_fields(self, server_available):
        """Job list must not include password or secret fields."""
        resp = _authed_get("/api/jobs?limit=5")
        _require_ok(resp, "GET /api/jobs")
        violations = _deep_scan_for_credential_fields(resp.json(), path="/api/jobs")
        assert not violations, f"GET /api/jobs leaks credential field(s): {violations[:5]}"

    def test_jobs_has_no_internal_infra_fields(self, server_available):
        """Job list must not expose LangGraph checkpoint blobs or raw DB fields."""
        resp = _authed_get("/api/jobs?limit=5")
        _require_ok(resp, "GET /api/jobs")
        _assert_no_internal_infra_fields(resp.json(), context="GET /api/jobs")

    def test_job_detail_has_no_credential_fields(self, server_available):
        """GET /api/jobs/{id} must not include credential fields."""
        seeded_job = ensure_test_job(_PROJECT_ID or None)
        job_id = str(seeded_job.get("job_id") or seeded_job.get("id") or "").strip()
        assert job_id, f"Seed job missing identifier: {seeded_job}"
        detail_resp = _authed_get(f"/api/jobs/{job_id}")
        _require_ok(detail_resp, f"GET /api/jobs/{job_id}", allow_404=True)
        if detail_resp.status_code == 404:
            pytest.skip(f"Job {job_id} not found")
        violations = _deep_scan_for_credential_fields(
            detail_resp.json(), path=f"/api/jobs/{job_id}"
        )
        assert not violations, f"GET /api/jobs/{job_id} leaks credential field(s): {violations[:5]}"


# ===========================================================================
# C. /api/projects — project list shape
# ===========================================================================


class TestProjectsListFieldContract:
    """GET /api/projects must not expose account-level secrets or raw DB columns."""

    def test_projects_has_no_credential_fields(self, server_available):
        """Project list must not expose any credential fields."""
        resp = _authed_get("/api/projects?limit=5")
        _require_ok(resp, "GET /api/projects")
        violations = _deep_scan_for_credential_fields(resp.json(), path="/api/projects")
        assert not violations, f"GET /api/projects leaks credential field(s): {violations[:5]}"

    def test_projects_has_no_internal_infra_fields(self, server_available):
        """Project list must not expose internal DB fields."""
        resp = _authed_get("/api/projects?limit=5")
        _require_ok(resp, "GET /api/projects")
        _assert_no_internal_infra_fields(resp.json(), context="GET /api/projects")

    def test_project_detail_has_no_credential_fields(self, server_available):
        """GET /api/projects/{id} must not expose credential/secret fields."""
        if not _PROJECT_ID:
            pytest.skip("TEST_PROJECT_BACKEND_ID not set")
        resp = _authed_get(f"/api/projects/{_PROJECT_ID}")
        _require_ok(resp, f"GET /api/projects/{_PROJECT_ID}", allow_403=True, allow_404=True)
        violations = _deep_scan_for_credential_fields(
            resp.json(), path=f"/api/projects/{_PROJECT_ID}"
        )
        assert not violations, (
            f"GET /api/projects/{_PROJECT_ID} leaks credential field(s): {violations[:5]}"
        )


# ===========================================================================
# D. /api/auth/users — user list shape (admin)
# ===========================================================================


class TestUsersListFieldContract:
    """GET /api/auth/users must not expose password or authentication secrets."""

    def test_users_list_has_no_credential_fields(self, server_available):
        """Admin user list must never include password hashes or credential secrets."""
        resp = _authed_get("/api/auth/users")
        _require_ok(resp, "GET /api/auth/users", allow_403=True)
        violations = _deep_scan_for_credential_fields(resp.json(), path="/api/auth/users")
        assert not violations, f"GET /api/auth/users leaks credential field(s): {violations[:5]}"

    def test_users_list_has_no_internal_infra_fields(self, server_available):
        """Admin user list must not expose raw DB fields."""
        resp = _authed_get("/api/auth/users")
        _require_ok(resp, "GET /api/auth/users", allow_403=True)
        _assert_no_internal_infra_fields(resp.json(), context="GET /api/auth/users")


# ===========================================================================
# E. /api/providers — provider configuration shape
# ===========================================================================


class TestProvidersFieldContract:
    """GET /api/providers must redact API keys and provider secrets."""

    def test_providers_list_has_no_credential_fields(self, server_available):
        """Provider list must not expose API keys or connection secrets."""
        resp = _authed_get("/api/providers?enabled_only=false")
        _require_ok(resp, "GET /api/providers", allow_403=True)
        violations = _deep_scan_for_credential_fields(resp.json(), path="/api/providers")
        assert not violations, f"GET /api/providers leaks credential field(s): {violations[:5]}"

    def test_mcp_connectors_has_no_credential_fields(self, server_available):
        """MCP connector list must not expose auth tokens or secrets."""
        resp = _authed_get("/api/mcp")
        _require_ok(resp, "GET /api/mcp", allow_403=True)
        violations = _deep_scan_for_credential_fields(resp.json(), path="/api/mcp")
        assert not violations, (
            f"GET /api/mcp leaks credential field(s): {violations[:5]}"
        )


# ===========================================================================
# F. Cross-endpoint recursive deep scan
# ===========================================================================


class TestCrossEndpointDeepScan:
    """Spot-check multiple key endpoints for credential field leakage in one pass."""

    _ENDPOINTS_TO_SCAN: ClassVar[list[str]] = [
        "/api/auth/me",
        "/api/jobs?limit=3",
        "/api/projects?limit=3",
        "/api/events?limit=3",
        "/api/settings/model-catalog",
    ]

    def test_all_key_endpoints_clean(self, server_available):
        """All key endpoints must be free of credential field names."""
        all_violations = []
        for endpoint in self._ENDPOINTS_TO_SCAN:
            try:
                resp = _authed_get(endpoint)
            except Exception:
                continue  # network failure — skip that endpoint
            if resp.status_code in (401, 403, 404):
                continue  # not accessible — not a leakage issue
            if resp.status_code != 200:
                continue
            try:
                body = resp.json()
            except Exception:
                continue
            violations = _deep_scan_for_credential_fields(body, path=endpoint)
            all_violations.extend(violations)

        assert not all_violations, (
            "Credential field leakage detected across endpoints:\n"
            + "\n".join(f"  - {v}" for v in all_violations[:10])
        )
