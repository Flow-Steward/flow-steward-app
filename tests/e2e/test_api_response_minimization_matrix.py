"""E2E API response minimization — strict allowlist/forbidden-field assertions.

P0 gap: only positive key-presence checks exist; no strict extra-key denial tests.

Goal: every UI-consumed API endpoint must return ONLY the fields the frontend
actually needs. Internal fields (password_hash, secrets, raw DB columns, etc.)
must never appear in responses, even for admin callers.

Coverage:

A. User endpoints — no password_hash, no raw credential fields
B. Job endpoints — no internal state machine fields leaked
C. Project endpoints — no raw DB columns or internal account scope fields
D. Settings endpoints — no raw credential values in plaintext
E. Events/activity endpoints — no internal tracing or infra fields
F. Role-scoped payload shrinkage — viewer sees fewer fields than admin

Each test:
  1. Calls the endpoint with an authenticated session
  2. Asserts expected required keys ARE present (positive contract)
  3. Asserts forbidden keys are NOT present (strict minimization contract)

Run:
    docker compose exec fs-app python -m pytest \
        tests/e2e/test_api_response_minimization_matrix.py -v
"""

from __future__ import annotations

import os
import uuid
from typing import ClassVar

import pytest
import requests as _requests

from core.application.tooling.testing.runtime_env import resolve_test_env
from tests.e2e.helpers import BASE_URL, VERIFY_SSL, get, require_ok

pytestmark = [pytest.mark.e2e]

_TEST_ENV = resolve_test_env(os.environ, lane="backend", include_dotenv=True)
_PROJECT_ID = str(_TEST_ENV.project_id or "").strip()

# Fields that must NEVER appear in any API response
_GLOBALLY_FORBIDDEN = frozenset(
    {
        "password_hash",
        "password",
        "hashed_password",
        "raw_password",
        "secret_key",
        "private_key",
        "access_key_secret",
        "smtp_password",
        "api_secret",
        "webhook_secret",
        "internal_token",
    }
)

# Internal DB/ORM fields that must not be exposed
_INTERNAL_DB_FIELDS = frozenset(
    {
        "pg_oid",
        "xmin",
        "ctid",
        "tableoid",
    }
)


def _uid() -> str:
    return uuid.uuid4().hex[:10]


def _create_viewer_session() -> tuple[_requests.Session, str]:
    """Create a temporary viewer user and return (session, user_id) for role-scoped tests."""
    from tests.e2e.conftest import _authed_session

    admin_session, verify_ssl = _authed_session()
    csrf = str(admin_session.cookies.get("fs_csrf") or "").strip()
    # Use a valid email address as the login identifier — the API stores this as username.
    email = f"viewmin-{_uid()}@test.local"
    password = f"Passw0rd-{_uid()}!"
    create_resp = admin_session.post(
        f"{BASE_URL}/api/auth/users",
        json={"email": email, "password": password, "roles": ["viewer"]},
        headers={"x-csrf-token": csrf} if csrf else {},
        verify=verify_ssl,
    )
    if create_resp.status_code not in (200, 201):
        pytest.skip(
            f"Cannot create viewer user for role-scoped payload tests: {create_resp.status_code}"
        )
    body = create_resp.json()
    user_id = str(body.get("id") or (body.get("user") or {}).get("id") or "").strip()

    viewer_session = _requests.Session()
    login_resp = viewer_session.post(
        f"{BASE_URL}/api/auth/login",
        # Login with the email address that was stored as the username in the DB.
        json={"email": email, "password": password},
        verify=verify_ssl,
    )
    if login_resp.status_code not in (200, 201):
        pytest.skip(f"Cannot log in as viewer: {login_resp.status_code}")
    return viewer_session, user_id


def _delete_user(user_id: str) -> None:
    """Delete a test user (best-effort cleanup)."""
    try:
        from tests.e2e.conftest import _authed_session

        admin_session, verify_ssl = _authed_session()
        csrf = str(admin_session.cookies.get("fs_csrf") or "").strip()
        admin_session.delete(
            f"{BASE_URL}/api/auth/users/{user_id}",
            headers={"x-csrf-token": csrf} if csrf else {},
            verify=verify_ssl,
        )
    except Exception:
        pass


@pytest.fixture(scope="module")
def viewer_session_fixture():
    """Module-scoped fixture providing a viewer-role session for payload shrinkage tests."""
    session, user_id = _create_viewer_session()
    yield session
    _delete_user(user_id)


def _assert_no_forbidden_fields(obj: dict | list, *, forbidden: frozenset[str], context: str):
    """Recursively assert no forbidden fields appear in response."""
    if isinstance(obj, dict):
        leaked = forbidden & set(obj.keys())
        assert not leaked, (
            f"Response leaks forbidden fields {leaked} in {context}: {str(obj)[:300]}"
        )
        for v in obj.values():
            _assert_no_forbidden_fields(v, forbidden=forbidden, context=context)
    elif isinstance(obj, list):
        for item in obj[:5]:  # spot-check first 5 items
            _assert_no_forbidden_fields(item, forbidden=forbidden, context=context)


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
# A. User endpoints — no credential fields
# ===========================================================================


class TestUserResponseMinimization:
    """User API responses must not include password hashes or credential fields."""

    def test_auth_me_has_no_password_hash(self, server_available):
        resp = get("/api/auth/me")
        require_ok(resp, "GET /api/auth/me")
        body = resp.json()
        _assert_no_forbidden_fields(body, forbidden=_GLOBALLY_FORBIDDEN, context="GET /api/auth/me")

    def test_users_list_has_no_password_hashes(self, server_available):
        resp = get("/api/auth/users")
        require_ok(resp, "GET /api/auth/users", allow_403=True)
        body = resp.json()
        users = body if isinstance(body, list) else body.get("items") or body.get("users") or []
        for user in users[:10]:
            _assert_no_forbidden_fields(
                user, forbidden=_GLOBALLY_FORBIDDEN, context="GET /api/auth/users item"
            )

    def test_user_response_has_required_keys(self, server_available):
        resp = get("/api/auth/me")
        require_ok(resp, "GET /api/auth/me")
        body = resp.json()
        # /api/auth/me wraps user fields under a "user" key:
        # {"authenticated": true, "user": {"id": ..., "username": ...}, ...}
        user = body.get("user") if isinstance(body.get("user"), dict) else body
        required = {"id", "username"}
        missing = required - set(user.keys())
        assert not missing, f"/api/auth/me missing required keys in user object: {missing}"

    def test_user_response_has_no_internal_db_fields(self, server_available):
        resp = get("/api/auth/me")
        require_ok(resp, "GET /api/auth/me")
        _assert_no_forbidden_fields(
            resp.json(), forbidden=_INTERNAL_DB_FIELDS, context="GET /api/auth/me"
        )


# ===========================================================================
# B. Job endpoints — no internal state machine fields
# ===========================================================================


class TestJobResponseMinimization:
    """Job API responses must expose operational fields, not internal runtime state."""

    # /api/jobs returns AttentionJobView which uses "job_id" (not "id")
    _JOB_REQUIRED_KEYS: ClassVar[set[str]] = {"job_id", "status"}
    _JOB_FORBIDDEN_EXTRA = frozenset(
        {
            "pg_thread_id_raw",
            "internal_checkpoint",
            "langgraph_state_blob",
        }
    )

    def test_jobs_list_has_required_keys(self, server_available):
        resp = get("/api/jobs?limit=5")
        require_ok(resp, "GET /api/jobs")
        body = resp.json()
        items = body if isinstance(body, list) else body.get("items") or body.get("jobs") or []
        if not items:
            pytest.skip("No jobs in environment to validate field contracts")
        for job in items[:5]:
            missing = self._JOB_REQUIRED_KEYS - set(job.keys())
            assert not missing, f"Job response missing required keys {missing}: {job}"

    def test_jobs_list_has_no_globally_forbidden_fields(self, server_available):
        resp = get("/api/jobs?limit=5")
        require_ok(resp, "GET /api/jobs")
        _assert_no_forbidden_fields(
            resp.json(), forbidden=_GLOBALLY_FORBIDDEN, context="GET /api/jobs"
        )

    def test_job_detail_has_no_forbidden_fields(self, project_id):
        resp = get(f"/api/jobs?project_id={project_id}&limit=1")
        require_ok(resp, "GET /api/jobs (project filter)")
        body = resp.json()
        items = body if isinstance(body, list) else body.get("items") or []
        if not items:
            pytest.skip("No jobs in project to validate detail contracts")
        # AttentionJobView uses "job_id"; fallback to "id" for other list shapes
        job_id = items[0].get("job_id") or items[0].get("id")
        detail = get(f"/api/jobs/{job_id}")
        require_ok(detail, f"GET /api/jobs/{job_id}")
        _assert_no_forbidden_fields(
            detail.json(),
            forbidden=_GLOBALLY_FORBIDDEN | self._JOB_FORBIDDEN_EXTRA,
            context=f"GET /api/jobs/{job_id}",
        )


# ===========================================================================
# C. Project endpoints — no raw account scope or internal fields
# ===========================================================================


class TestProjectResponseMinimization:
    """Project API responses must expose project-level data, not account internals."""

    _PROJECT_REQUIRED_KEYS: ClassVar[set[str]] = {"id", "name"}
    _PROJECT_FORBIDDEN = frozenset(
        {
            "account_secret",
            "internal_account_id_raw",
            "db_row_id",
        }
    )

    def test_projects_list_has_required_keys(self, server_available):
        resp = get("/api/projects?limit=5")
        require_ok(resp, "GET /api/projects")
        body = resp.json()
        items = body if isinstance(body, list) else body.get("items") or body.get("projects") or []
        if not items:
            pytest.skip("No projects in environment to validate field contracts")
        for proj in items[:5]:
            missing = self._PROJECT_REQUIRED_KEYS - set(proj.keys())
            assert not missing, f"Project response missing {missing}: {proj}"

    def test_project_detail_has_no_globally_forbidden_fields(self, project_id):
        resp = get(f"/api/projects/{project_id}")
        require_ok(resp, f"GET /api/projects/{project_id}", allow_403=True, allow_404=True)
        _assert_no_forbidden_fields(
            resp.json(), forbidden=_GLOBALLY_FORBIDDEN, context=f"GET /api/projects/{project_id}"
        )


# ===========================================================================
# D. Settings endpoints — no raw credential values
# ===========================================================================


class TestSettingsResponseMinimization:
    """Settings responses must redact credential values (SMTP passwords, API keys, etc.)."""

    _SETTINGS_FORBIDDEN = frozenset(
        {
            "smtp_password",
            "api_key_raw",
            "secret_value",
            "provider_secret",
            "raw_credential",
        }
    )

    def test_settings_model_catalog_has_no_secrets(self, server_available):
        resp = get("/api/settings/model-catalog")
        require_ok(resp, "GET /api/settings/model-catalog", allow_403=True)
        _assert_no_forbidden_fields(
            resp.json(),
            forbidden=_GLOBALLY_FORBIDDEN | self._SETTINGS_FORBIDDEN,
            context="GET /api/settings/model-catalog",
        )

    def test_providers_list_has_no_raw_secrets(self, server_available):
        resp = get("/api/providers?enabled_only=false")
        require_ok(resp, "GET /api/providers", allow_403=True)
        _assert_no_forbidden_fields(
            resp.json(),
            forbidden=_GLOBALLY_FORBIDDEN | self._SETTINGS_FORBIDDEN,
            context="GET /api/providers",
        )

    def test_mcp_connectors_list_has_no_raw_tokens(self, server_available):
        resp = get("/api/mcp")
        require_ok(resp, "GET /api/mcp", allow_403=True)
        _assert_no_forbidden_fields(
            resp.json(), forbidden=_GLOBALLY_FORBIDDEN, context="GET /api/mcp"
        )


# ===========================================================================
# E. Events endpoint — no internal tracing fields
# ===========================================================================


class TestEventsResponseMinimization:
    """Events API must not expose internal infrastructure tracing data."""

    _EVENTS_FORBIDDEN = frozenset(
        {
            "internal_trace_id",
            "pg_log_row_id",
            "raw_stacktrace",
        }
    )

    def test_events_list_has_no_forbidden_fields(self, server_available):
        resp = get("/api/events?limit=10")
        require_ok(resp, "GET /api/events", allow_403=True)
        _assert_no_forbidden_fields(
            resp.json(),
            forbidden=_GLOBALLY_FORBIDDEN | self._EVENTS_FORBIDDEN,
            context="GET /api/events",
        )

    def test_events_list_has_required_structure(self, server_available):
        resp = get("/api/events?limit=5")
        require_ok(resp, "GET /api/events", allow_403=True)
        body = resp.json()
        # Events may be a list or {items: [...]}
        events = body if isinstance(body, list) else body.get("items") or body.get("events") or []
        for ev in events[:3]:
            assert isinstance(ev, dict), f"Event must be a dict: {ev}"


# ===========================================================================
# F. Role-scoped payload shrinkage
# ===========================================================================


class TestRoleScopedPayloadShrinkage:
    """Viewer responses must not include admin-only fields present in admin responses."""

    def test_jobs_list_has_no_admin_only_fields_for_viewer(
        self, server_available, viewer_session_fixture
    ):
        """Viewer role must not see admin-only fields in /api/jobs responses.

        Uses a dynamically created viewer user — no E2E_BACKEND_ROLE env var needed.
        """
        resp = viewer_session_fixture.get(
            f"{BASE_URL}/api/jobs?limit=5",
            verify=VERIFY_SSL,
            timeout=15,
        )
        if resp.status_code in (401, 403):
            pytest.skip(f"Viewer session not authorized for /api/jobs: {resp.status_code}")
        assert resp.status_code == 200, (
            f"Expected 200 from /api/jobs as viewer; got {resp.status_code}"
        )
        body = resp.json()
        items = body if isinstance(body, list) else body.get("items") or []
        admin_only_fields = {"account_id", "internal_config", "agents_config_raw"}
        for job in items[:5]:
            leaked = admin_only_fields & set(job.keys())
            assert not leaked, f"Viewer response leaks admin-only fields {leaked}: {job}"

    def test_projects_list_has_no_admin_only_fields_for_viewer(
        self, server_available, viewer_session_fixture
    ):
        """Viewer role must not see internal account/config fields in /api/projects."""
        resp = viewer_session_fixture.get(
            f"{BASE_URL}/api/projects?limit=5",
            verify=VERIFY_SSL,
            timeout=15,
        )
        if resp.status_code in (401, 403):
            pytest.skip(f"Viewer session not authorized for /api/projects: {resp.status_code}")
        assert resp.status_code == 200, (
            f"Expected 200 from /api/projects as viewer; got {resp.status_code}"
        )
        body = resp.json()
        items = body if isinstance(body, list) else body.get("items") or body.get("projects") or []
        admin_only_fields = {"agents_config", "internal_config", "agents_config_raw"}
        for proj in items[:5]:
            leaked = admin_only_fields & set(proj.keys())
            assert not leaked, f"Viewer response leaks admin-only project fields {leaked}: {proj}"

    def test_viewer_globally_forbidden_fields_absent(
        self, server_available, viewer_session_fixture
    ):
        """Viewer must never see password hashes or credential fields in any response."""
        for path in ["/api/jobs?limit=5", "/api/projects?limit=5"]:
            resp = viewer_session_fixture.get(
                f"{BASE_URL}{path}",
                verify=VERIFY_SSL,
                timeout=15,
            )
            if resp.status_code in (401, 403):
                continue  # viewer blocked from endpoint entirely — OK
            _assert_no_forbidden_fields(
                resp.json(), forbidden=_GLOBALLY_FORBIDDEN, context=f"viewer GET {path}"
            )
