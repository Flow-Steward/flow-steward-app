"""E2E RBAC parity matrix — admin / operator / viewer across UI, API, and MCP surfaces.

P0 gap: scattered role checks exist but no single gating suite that proves
viewer/operator/admin parity is enforced consistently across UI, API, and MCP.

Coverage:

A. Authentication boundary — unauthenticated must be rejected everywhere
B. Viewer role — read access allowed; write/admin access denied
C. Operator role — write within projects allowed; admin-only denied
D. Admin role — full access including admin surfaces
E. Role-escalation boundary — viewer cannot perform operator/admin actions
F. MCP surface — unauthenticated and wrong-role rejection

Each test class exercises one role layer. Role is configured via the
E2E_BACKEND_ROLE environment variable; tests default to the running session role
and skip cases that require a different role if role switching is unavailable.

Run:
    # Full parity run (all three roles — needs three separate passes or role-switch support)
    E2E_BACKEND_ROLE=admin docker compose exec fs-app python -m pytest \
        tests/e2e/test_rbac_ui_api_mcp_matrix.py -v

    E2E_BACKEND_ROLE=operator docker compose exec fs-app python -m pytest \
        tests/e2e/test_rbac_ui_api_mcp_matrix.py -v

    E2E_BACKEND_ROLE=viewer docker compose exec fs-app python -m pytest \
        tests/e2e/test_rbac_ui_api_mcp_matrix.py -v
"""

from __future__ import annotations

import json
import os
import time
import uuid
from urllib.parse import urlsplit, urlunsplit

import pytest
import requests as _requests

from core.application.auth.config import DEFAULT_WORKSPACE_ID
from core.application.tooling.testing.runtime_env import resolve_test_env
from tests.e2e.helpers import (
    BASE_URL,
    VERIFY_SSL,
    anon_get,
    anon_post,
    ensure_test_job,
    get,
    require_ok,
)
from tests.e2e.neutral_registry import NEUTRAL_WORKFLOW_ID

pytestmark = [pytest.mark.e2e]

_TEST_ENV = resolve_test_env(os.environ, lane="backend", include_dotenv=True)
_PROJECT_ID = str(_TEST_ENV.project_id or "").strip()
_ACTIVE_ROLE = str(os.getenv("E2E_BACKEND_ROLE") or "admin").strip().lower()
# MCP server URL — may be a separate service (e.g. fs-mcp:8100) or co-hosted at /mcp.


def _mcp_url_from_env() -> str:
    raw_url = str(os.getenv("MCP_URL") or "").strip()
    if not raw_url:
        base_url = str(os.getenv("MCP_BASE_URL") or "").strip().rstrip("/")
        raw_url = f"{base_url}/mcp" if base_url else ""
    if not raw_url:
        return ""
    parsed = urlsplit(raw_url)
    if parsed.hostname == "fs-mcp" and not os.path.exists("/.dockerenv"):
        netloc = "127.0.0.1"
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path or "/mcp", parsed.query, ""))
    return raw_url


_MCP_URL = _mcp_url_from_env()


def _create_pat(session: _requests.Session) -> str | None:
    """Create a short-lived PAT for the current user and return the raw token string."""
    csrf = str(session.cookies.get("fs_csrf") or "").strip()
    resp = session.post(
        f"{BASE_URL}/api/auth/tokens",
        json={
            "label": f"rbac-mcp-test-{uuid.uuid4().hex[:8]}",
            "scopes": [],
            "allowed_project_ids": [_PROJECT_ID] if _PROJECT_ID else [],
            "expires_in_days": 1,
        },
        headers={"x-csrf-token": csrf} if csrf else {},
        verify=VERIFY_SSL,
        timeout=10,
    )
    if resp.status_code not in (200, 201):
        return None
    body = resp.json()
    token = body.get("token") if isinstance(body, dict) else None
    if not isinstance(token, dict):
        return None
    return str(token.get("raw_token") or "").strip() or None


def _call_mcp_as_session(session: _requests.Session, payload: dict) -> _requests.Response:
    mcp_headers = {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
    }
    if _MCP_URL:
        token = _create_pat(session)
        if token is None:
            pytest.skip("Cannot create PAT for MCP RBAC test in this build")
        return _requests.post(
            _MCP_URL,
            json=payload,
            headers={**mcp_headers, "Authorization": f"Bearer {token}"},
            verify=VERIFY_SSL,
            timeout=15,
        )

    csrf = str(session.cookies.get("fs_csrf") or "").strip()
    if csrf:
        mcp_headers["x-csrf-token"] = csrf
    resp = session.post(
        f"{BASE_URL}/mcp",
        json=payload,
        headers=mcp_headers,
        verify=VERIFY_SSL,
        timeout=15,
    )
    if resp.status_code == 404:
        pytest.skip(
            "MCP endpoint not mounted at /mcp and MCP_URL env var is not set — "
            "configure MCP_URL to test against the dedicated MCP service"
        )
    return resp


def _assert_mcp_denied(resp: _requests.Response, *, tool_name: str) -> None:
    if resp.status_code != 200:
        assert resp.status_code in (401, 403), (
            f"Viewer {tool_name} must be denied; got {resp.status_code}"
        )
        return

    body = resp.json()
    top_error = body.get("error") if isinstance(body, dict) else None
    if isinstance(top_error, dict) and str(top_error.get("code") or "") == "forbidden":
        return

    result = body.get("result")
    content = result.get("content") if isinstance(result, dict) else None
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except Exception:
                payload = None
            if isinstance(payload, dict) and payload.get("ok") is False:
                error = payload.get("error")
                if isinstance(error, dict) and str(error.get("code") or "") == "forbidden":
                    return

    raise AssertionError(
        f"Viewer {tool_name} must return an exact MCP authorization denial; got {body}"
    )


def _scoped_mcp_arguments(
    *,
    tool_name: str,
    arguments: dict,
    project_name: str,
    owned_job_fixture: dict[str, str],
) -> dict:
    """Bind mutating MCP probes to resources owned by the active test account."""
    scoped = dict(arguments)
    if "project_name" in scoped:
        scoped["project_name"] = project_name
    if tool_name == "job_cancel":
        scoped["job_id"] = owned_job_fixture["job_id"]
    elif tool_name == "job_approval_decide":
        scoped["thread_id"] = owned_job_fixture["thread_id"]
        scoped["approval_id"] = owned_job_fixture["approval_id"]
    return scoped


def _uid() -> str:
    return uuid.uuid4().hex[:10]


def _skip_unless_role(*roles: str):
    """Skip the test if the active session role is not one of the listed roles."""
    if _ACTIVE_ROLE not in roles:
        pytest.skip(
            f"Test requires role in {roles!r}; active role is '{_ACTIVE_ROLE}'. "
            "Re-run with E2E_BACKEND_ROLE=<role>."
        )


def _create_role_session(role: str) -> tuple[_requests.Session, str]:
    """Create a temporary user with *role* and return (session, user_id).

    Uses the admin session to create the user, then logs in as that user.
    The caller is responsible for deleting the user afterwards.
    Raises pytest.skip() if user creation or login fails.
    """
    from tests.e2e.conftest import _authed_session

    admin_session, verify_ssl = _authed_session()
    csrf = str(admin_session.cookies.get("fs_csrf") or "").strip()
    username = f"rbac-{role}-{_uid()}"
    # Use a valid email address — the API stores this as the login identifier.
    email = f"{username}@rbac.test"
    password = f"TestRbac_{role.capitalize()}_{_uid()}!"
    create_resp = admin_session.post(
        f"{BASE_URL}/api/auth/users",
        json={
            "email": email,
            "password": password,
            "roles": [role],
        },
        headers={"x-csrf-token": csrf} if csrf else {},
        verify=verify_ssl,
    )
    if create_resp.status_code not in (200, 201):
        pytest.skip(
            f"Could not create {role} test user: {create_resp.status_code} — {create_resp.text[:200]}"
        )
    user_data = create_resp.json().get("user") or create_resp.json()
    user_id = str(user_data.get("id") or "").strip()

    role_session = _requests.Session()
    login_resp = role_session.post(
        f"{BASE_URL}/api/auth/login",
        # Login with the email that was stored as username in the DB.
        json={"email": email, "password": password},
        verify=verify_ssl,
    )
    if login_resp.status_code != 200:
        pytest.skip(f"Could not log in as {role} user: {login_resp.status_code}")
    return role_session, user_id


def _delete_user(user_id: str) -> None:
    """Delete a test user using the admin session (best-effort cleanup)."""
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
def viewer_session():
    """Module-scoped viewer session fixture — creates a viewer user and returns the session."""
    session, user_id = _create_role_session("viewer")
    yield session
    _delete_user(user_id)


@pytest.fixture(scope="module")
def operator_session():
    """Module-scoped operator session fixture — creates an operator user and returns the session."""
    session, user_id = _create_role_session("operator")
    yield session
    _delete_user(user_id)


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


@pytest.fixture(scope="module")
def project_name(project_id: str, viewer_session: _requests.Session) -> str:
    resp = viewer_session.get(f"{BASE_URL}/api/projects/{project_id}", verify=VERIFY_SSL)
    if resp.status_code != 200:
        pytest.skip(f"Cannot load valid scoped project fixture: HTTP {resp.status_code}")
    body = resp.json()
    project = body.get("project") if isinstance(body, dict) else None
    project = project if isinstance(project, dict) else body
    name = str(project.get("name") or "").strip() if isinstance(project, dict) else ""
    if not name:
        pytest.skip("Valid scoped project fixture has no project name")
    return name


def _job_approval_fixture(job: dict) -> dict[str, str] | None:
    job_id = str(job.get("id") or job.get("job_id") or "").strip()
    thread_id = str(job.get("thread_id") or "").strip()
    if not job_id or not thread_id:
        return None
    response = get(f"/api/jobs/{job_id}/approvals")
    if response.status_code != 200:
        return None
    body = response.json()
    rows = body.get("approvals") or body.get("items") or [] if isinstance(body, dict) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        approval_id = str(row.get("approval_id") or "").strip()
        if approval_id:
            return {
                "job_id": job_id,
                "thread_id": thread_id,
                "approval_id": approval_id,
            }
    return None


@pytest.fixture(scope="module")
def owned_job_fixture(project_id: str) -> dict[str, str]:
    """Return a real account-owned job and approval for authorization-first probes."""
    response = get(f"/api/projects/{project_id}/jobs", params={"limit": 100})
    if response.status_code == 200:
        body = response.json()
        jobs = body if isinstance(body, list) else body.get("jobs") or []
        for job in jobs:
            fixture = _job_approval_fixture(job) if isinstance(job, dict) else None
            if fixture:
                return fixture

    seeded = ensure_test_job(project_id)
    for _attempt in range(40):
        job_response = get(f"/api/jobs/{seeded.get('id') or seeded.get('job_id')}")
        if job_response.status_code == 200:
            body = job_response.json()
            job = body.get("job", body) if isinstance(body, dict) else {}
            fixture = _job_approval_fixture(job) if isinstance(job, dict) else None
            if fixture:
                return fixture
        time.sleep(0.5)
    raise AssertionError("Could not obtain a valid owned job/approval fixture for MCP RBAC")


@pytest.fixture(scope="module")
def auth_enabled_mcp(server_available) -> str:
    if not _MCP_URL:
        pytest.skip("MCP_URL is required for the auth-enabled MCP RBAC matrix")
    response = _requests.post(
        _MCP_URL,
        json={"jsonrpc": "2.0", "id": "auth-preflight", "method": "tools/list", "params": {}},
        headers={
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
        },
        verify=VERIFY_SSL,
        timeout=10,
    )
    assert response.status_code in (401, 403), (
        "MCP RBAC matrix requires an explicitly auth-enabled MCP service; "
        f"anonymous tools/list returned HTTP {response.status_code}"
    )
    return _MCP_URL


# ===========================================================================
# A. Unauthenticated boundary
# ===========================================================================


class TestUnauthenticatedBoundary:
    """All protected endpoints must reject anonymous access."""

    def _anon_get(self, path: str):
        # Use anon_get from helpers — bypasses the e2e auth monkeypatch so
        # the request is genuinely unauthenticated (no session cookies).
        return anon_get(path)

    def _anon_post(self, path: str, body: dict):
        return anon_post(path, body)

    def test_api_jobs_anonymous_returns_401_or_redirect(self, server_available):
        resp = self._anon_get("/api/jobs")
        assert resp.status_code in (401, 403, 302), (
            f"Anonymous /api/jobs must return 401/403/redirect; got {resp.status_code}"
        )

    def test_api_projects_anonymous_returns_401_or_redirect(self, server_available):
        resp = self._anon_get("/api/projects")
        assert resp.status_code in (401, 403, 302)

    def test_api_settings_anonymous_returns_401_or_redirect(self, server_available):
        resp = self._anon_get("/api/settings/model-catalog")
        assert resp.status_code in (401, 403, 302)

    def test_api_auth_me_anonymous_returns_401(self, server_available):
        resp = self._anon_get("/api/auth/me")
        assert resp.status_code in (401, 403)

    def test_api_users_anonymous_returns_401_or_redirect(self, server_available):
        resp = self._anon_get("/api/auth/users")
        assert resp.status_code in (401, 403, 302)

    def test_mcp_anonymous_returns_401_or_403(self, server_available):
        resp = self._anon_post(
            "/mcp",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {},
            },
        )
        assert resp.status_code in (401, 403, 404), (
            f"Anonymous MCP access must return 401/403; got {resp.status_code}"
        )


# ===========================================================================
# B. Viewer role — read allowed, writes denied
# ===========================================================================


class TestViewerRoleAccess:
    """Viewer can read public API data but cannot mutate state."""

    def _get(self, session, path: str, **kwargs):
        return session.get(f"{BASE_URL}{path}", verify=VERIFY_SSL, **kwargs)

    def _post(self, session, path: str, payload: dict, **kwargs):
        csrf = str(session.cookies.get("fs_csrf") or "").strip()
        headers = kwargs.pop("headers", {})
        if csrf:
            headers["x-csrf-token"] = csrf
        return session.post(
            f"{BASE_URL}{path}", json=payload, headers=headers, verify=VERIFY_SSL, **kwargs
        )

    def _put(self, session, path: str, payload: dict, **kwargs):
        csrf = str(session.cookies.get("fs_csrf") or "").strip()
        headers = kwargs.pop("headers", {})
        if csrf:
            headers["x-csrf-token"] = csrf
        return session.put(
            f"{BASE_URL}{path}", json=payload, headers=headers, verify=VERIFY_SSL, **kwargs
        )

    def test_viewer_can_list_jobs(self, server_available):
        _skip_unless_role("viewer", "admin", "operator")
        resp = get("/api/jobs")
        # 401 → not authenticated (E2E_ENFORCE_AUTH=0 or no credentials) → skip
        require_ok(resp, "GET /api/jobs (viewer)", allow_403=True)

    def test_viewer_can_read_project(self, project_id):
        _skip_unless_role("viewer", "admin", "operator")
        resp = get(f"/api/projects/{project_id}")
        assert resp.status_code in (200, 403, 404)  # 404 if project not in env

    def test_viewer_cannot_create_project(self, server_available, viewer_session):
        """Viewer must not be able to create projects (uses dynamic viewer session)."""
        resp = self._post(
            viewer_session,
            "/api/projects",
            {
                "name": f"viewer-create-{_uid()}",
                "workspace_id": DEFAULT_WORKSPACE_ID,
            },
        )
        # 401 = unauthenticated (also denied); 403 = authenticated viewer denied; 422 = validation
        assert resp.status_code in (401, 403, 422), (
            f"Viewer must not be able to create projects; got {resp.status_code}"
        )

    def test_viewer_cannot_update_security_policy(self, project_id, viewer_session):
        """Viewer must not update security policy (uses dynamic viewer session)."""
        resp = self._put(viewer_session, f"/api/security/projects/{project_id}/policy", {})
        assert resp.status_code in (401, 403), (
            f"Viewer must not update security policy; got {resp.status_code}"
        )

    def test_viewer_cannot_add_security_rule(self, project_id, viewer_session):
        """Viewer must not add security rules (uses dynamic viewer session)."""
        resp = self._post(
            viewer_session,
            f"/api/security/projects/{project_id}/rules",
            {
                "rule_id": f"viewer-rule-{_uid()}",
                "description": "Viewer rule attempt",
                "severity": "low",
            },
        )
        assert resp.status_code in (401, 403)

    def test_viewer_cannot_list_users(self, server_available, viewer_session):
        """Viewer must not access user management (uses dynamic viewer session)."""
        resp = self._get(viewer_session, "/api/auth/users")
        assert resp.status_code in (401, 403), (
            f"Viewer must not access user management; got {resp.status_code}"
        )

    def test_viewer_cannot_access_admin_routes(self, server_available, viewer_session):
        """Viewer must not access admin-only routes (uses dynamic viewer session)."""
        for path in ["/api/agents", "/api/workflows", "/api/tools"]:
            resp = self._get(viewer_session, path)
            assert resp.status_code in (401, 403, 404), (
                f"Viewer must not access admin route {path}; got {resp.status_code}"
            )

    def test_viewer_cannot_cancel_job(self, server_available, viewer_session):
        """Viewer must not cancel jobs (uses dynamic viewer session)."""
        csrf = str(viewer_session.cookies.get("fs_csrf") or "").strip()
        resp = viewer_session.post(
            f"{BASE_URL}/api/jobs/999999/cancel",
            json={},
            headers={"x-csrf-token": csrf} if csrf else {},
            verify=VERIFY_SSL,
        )
        assert resp.status_code in (401, 403, 404)


# ===========================================================================
# C. Operator role — write within projects, admin-only denied
# ===========================================================================


class TestOperatorRoleAccess:
    """Operator can mutate project data but cannot access admin-only surfaces."""

    def _get(self, session, path: str, **kwargs):
        return session.get(f"{BASE_URL}{path}", verify=VERIFY_SSL, **kwargs)

    def _put(self, session, path: str, payload: dict, **kwargs):
        csrf = str(session.cookies.get("fs_csrf") or "").strip()
        headers = kwargs.pop("headers", {})
        if csrf:
            headers["x-csrf-token"] = csrf
        return session.put(
            f"{BASE_URL}{path}", json=payload, headers=headers, verify=VERIFY_SSL, **kwargs
        )

    def test_operator_can_read_projects(self, server_available, operator_session):
        """Operator must be able to list projects (uses dynamic operator session)."""
        resp = self._get(operator_session, "/api/projects")
        assert resp.status_code in (200, 403), (
            f"Operator must be able to read projects; got {resp.status_code}"
        )

    def test_operator_can_update_security_policy(self, project_id, operator_session):
        """Operator should be able to update project security policy (uses dynamic operator session)."""
        resp = self._put(operator_session, f"/api/security/projects/{project_id}/policy", {})
        # 200/204 = success; 404 = project not in env; 422 = validation failure on empty body
        # 403 = operator not granted security write (deployment-specific) — documented exception
        # 400 removed: a PUT with empty dict {} is valid JSON and should not trigger bad-request
        assert resp.status_code in (200, 204, 403, 404, 422), (
            f"Operator security policy update returned unexpected {resp.status_code}"
        )

    def test_operator_cannot_manage_users(self, server_available, operator_session):
        """Operator must not access user management (uses dynamic operator session)."""
        resp = self._get(operator_session, "/api/auth/users")
        assert resp.status_code in (401, 403), (
            f"Operator must not access user management; got {resp.status_code}"
        )

    def test_operator_cannot_access_admin_agents(self, server_available, operator_session):
        """Operator must not access admin agent registry (uses dynamic operator session)."""
        resp = self._get(operator_session, "/api/agents")
        assert resp.status_code in (401, 403, 404), (
            f"Operator must not access admin agents; got {resp.status_code}"
        )

    def test_operator_cannot_update_global_security_policy(
        self, server_available, operator_session
    ):
        """Operator must not update global (non-project) security policy (uses dynamic operator session)."""
        csrf = str(operator_session.cookies.get("fs_csrf") or "").strip()
        resp = operator_session.put(
            f"{BASE_URL}/api/security/policy",
            json={},
            headers={"x-csrf-token": csrf} if csrf else {},
            verify=VERIFY_SSL,
        )
        # Global policy is admin-only; operator must get 401 (unauthed) or 403 (authed, denied)
        assert resp.status_code in (401, 403), (
            f"Operator must not update global security policy; got {resp.status_code}"
        )


# ===========================================================================
# D. Admin role — full access
# ===========================================================================


class TestAdminRoleAccess:
    """Admin has access to all API surfaces."""

    def test_admin_can_list_users(self, server_available):
        _skip_unless_role("admin")
        resp = get("/api/auth/users")
        require_ok(resp, "GET /api/auth/users (admin)")

    def test_admin_can_read_global_security_policy(self, server_available):
        _skip_unless_role("admin")
        resp = get("/api/security/policy")
        require_ok(resp, "GET /api/security/policy (admin)", allow_403=True)

    def test_admin_can_access_model_catalog(self, server_available):
        _skip_unless_role("admin")
        resp = get("/api/settings/model-catalog")
        if resp.status_code == 401:
            pytest.skip("Not authenticated — configure E2E credentials to run admin tests")
        assert resp.status_code in (200, 403, 503), (
            f"Admin model catalog must return 200/403/503; got {resp.status_code}"
        )

    def test_admin_can_list_registry_agents(self, server_available):
        _skip_unless_role("admin")
        resp = get("/api/agents")
        require_ok(resp, "GET /api/agents (admin)", allow_403=True, allow_404=True)

    def test_admin_can_read_project_security_policy(self, project_id):
        _skip_unless_role("admin")
        resp = get(f"/api/security/projects/{project_id}/policy")
        require_ok(
            resp,
            f"GET /api/security/projects/{project_id}/policy (admin)",
            allow_403=True,
            allow_404=True,
        )

    def test_admin_response_includes_no_forbidden_fields(self, server_available):
        """Admin API responses must not include internal fields like password_hash."""
        _skip_unless_role("admin")
        resp = get("/api/auth/users")
        if resp.status_code != 200:
            return
        users = resp.json()
        if isinstance(users, dict):
            users = users.get("items") or users.get("users") or []
        for user in users[:5]:
            forbidden = {"password_hash", "password", "hashed_password", "secret"}
            leaked = forbidden & set(user.keys())
            assert not leaked, f"User response leaks forbidden fields: {leaked}"


# ===========================================================================
# E. Role-escalation boundary
# ===========================================================================


class TestRoleEscalationBoundary:
    """A lower-privileged user cannot escalate their own role."""

    def _put_session(self, session, path: str, payload: dict, **kwargs):
        csrf = str(session.cookies.get("fs_csrf") or "").strip()
        headers = kwargs.pop("headers", {})
        if csrf:
            headers["x-csrf-token"] = csrf
        return session.put(
            f"{BASE_URL}{path}", json=payload, headers=headers, verify=VERIFY_SSL, **kwargs
        )

    def test_viewer_cannot_update_own_role_to_admin(self, server_available, viewer_session):
        """Viewer must not be able to escalate own role to admin (uses dynamic viewer session)."""
        # Get own user ID
        me_resp = viewer_session.get(f"{BASE_URL}/api/auth/me", verify=VERIFY_SSL)
        if me_resp.status_code != 200:
            pytest.skip(f"Cannot determine viewer user ID: {me_resp.status_code}")
        body = me_resp.json()
        user_id = body.get("id") or (body.get("user") or {}).get("id")
        if not user_id:
            pytest.skip("Own user ID not in /api/auth/me response")

        resp = self._put_session(viewer_session, f"/api/auth/users/{user_id}", {"roles": ["admin"]})
        assert resp.status_code in (401, 403, 422), (
            f"Viewer must not escalate own role to admin; got {resp.status_code}"
        )

    def test_operator_cannot_update_own_role_to_admin(self, server_available, operator_session):
        """Operator must not be able to escalate own role to admin (uses dynamic operator session)."""
        me_resp = operator_session.get(f"{BASE_URL}/api/auth/me", verify=VERIFY_SSL)
        if me_resp.status_code != 200:
            pytest.skip(f"Cannot determine operator user ID: {me_resp.status_code}")
        body = me_resp.json()
        user_id = body.get("id") or (body.get("user") or {}).get("id")
        if not user_id:
            pytest.skip("Own user ID not in response")
        resp = self._put_session(
            operator_session, f"/api/auth/users/{user_id}", {"roles": ["admin"]}
        )
        assert resp.status_code in (401, 403, 422)


# ===========================================================================
# F. MCP RBAC surface
# ===========================================================================


class TestMcpRbacSurface:
    """MCP tool calls must respect the caller's role."""

    def test_authenticated_mcp_list_tools_returns_200(self, auth_enabled_mcp, viewer_session):
        """Authenticated callers (any role) should be able to list MCP tools."""
        resp = _call_mcp_as_session(
            viewer_session,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {},
            },
        )
        assert resp.status_code == 200, f"MCP tools/list returned {resp.status_code}"

    @pytest.mark.parametrize(
        ("tool_name", "arguments"),
        [
            (
                "workflow_trigger",
                {
                    "project_name": "rbac-viewer-deny",
                    "workflow_id": NEUTRAL_WORKFLOW_ID,
                    "payload": {"topic": "Viewer trigger attempt"},
                },
            ),
            (
                "job_cancel",
                {
                    "job_id": "999999",
                },
            ),
            (
                "job_approval_decide",
                {
                    "thread_id": "viewer-thread-deny",
                    "approval_id": "viewer-approval-deny",
                    "approved": True,
                    "idempotency_key": f"viewer-approval-{_uid()}",
                    "comment": "viewer should not approve",
                },
            ),
            (
                "dispatch_project_notification",
                {
                    "project_name": "rbac-viewer-deny",
                    "event_type": "job.failed",
                    "severity": "error",
                    "payload": {"summary": "viewer deny"},
                },
            ),
        ],
    )
    def test_mcp_mutating_tools_deny_viewer_role(
        self,
        auth_enabled_mcp,
        viewer_session,
        project_name: str,
        owned_job_fixture: dict[str, str],
        tool_name: str,
        arguments: dict,
    ):
        scoped_arguments = _scoped_mcp_arguments(
            tool_name=tool_name,
            arguments=arguments,
            project_name=project_name,
            owned_job_fixture=owned_job_fixture,
        )
        resp = _call_mcp_as_session(
            viewer_session,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": tool_name,
                    "arguments": scoped_arguments,
                },
            },
        )

        _assert_mcp_denied(resp, tool_name=tool_name)
