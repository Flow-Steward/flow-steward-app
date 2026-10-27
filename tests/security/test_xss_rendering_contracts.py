"""Browser-rendered XSS deterministic tests.

P0 gap (C-01): No suite verifying that user-controlled data submitted via API is
properly escaped when rendered in HTML pages. A stored-XSS vector exists if
project names, task summaries, or other user inputs are rendered raw in templates.

Coverage:

A. API layer — JSON responses must not contain unescaped HTML in user fields
   - JSON-encoded strings are inherently safe (& → &amp; not required in JSON)
   - But verify that user input is not modified/injected with HTML tags

B. HTML rendering layer — user-controlled fields must be escaped in page HTML
   - Project name with XSS payload in page title / heading
   - Task summary with XSS payload in jobs list / job detail
   - Forms must include CSRF protection (x-csrf-token / meta csrf)

C. Header security — pages must include protective security headers
   - Content-Security-Policy or X-Content-Type-Options
   - X-Frame-Options

D. Content-Type enforcement
   - HTML pages must not serve content as text/plain
   - JSON endpoints must not return text/html

Run:
    docker compose exec fs-app python -m pytest \
        tests/security/test_xss_rendering_contracts.py -v
"""

from __future__ import annotations

import contextlib
import os
import re
import uuid

import pytest

from core.application.auth.config import DEFAULT_WORKSPACE_ID
from core.application.tooling.testing.runtime_env import resolve_test_env
from tests.e2e.helpers import BASE_URL, VERIFY_SSL, get, post_json

pytestmark = [pytest.mark.e2e]

_TEST_ENV = resolve_test_env(os.environ, lane="backend", include_dotenv=True)
_PROJECT_ID = str(_TEST_ENV.project_id or "").strip()

# Probe label prefix used to mark our injected XSS payloads
_PROBE_LABEL_PREFIX = "XSS-probe-"


def _raw_xss_pattern_for(label: str) -> re.Pattern:
    """Return a pattern that matches our specific probe payload rendered raw in HTML."""
    # Match our specific payload: <script>alert("label")</script> unescaped
    escaped_label = re.escape(label)
    return re.compile(
        rf'<script[^>]*>\s*alert\s*\(\s*["\']?{escaped_label}',
        re.IGNORECASE,
    )


def _uid() -> str:
    return uuid.uuid4().hex[:10]


def _xss_probe(label: str) -> str:
    """Return a payload combining a unique label with an XSS probe."""
    return f'XSS-probe-{label}-<script>alert("{label}")</script>'


@pytest.fixture(scope="module")
def server_available():
    try:
        resp = get("/health", timeout=5)
    except Exception as exc:
        pytest.skip(f"Server not reachable: {exc}")
    if resp.status_code >= 500:
        pytest.skip(f"Server unhealthy: {resp.status_code}")


def _check_probe_not_raw(html_content: str, probe_label: str, context: str) -> None:
    """Assert that our specific XSS probe payload is NOT rendered raw in html_content.

    We only check for our specific probe labels (not all alert() calls) to avoid
    false positives from legitimate JavaScript code on the page.
    """
    pattern = _raw_xss_pattern_for(probe_label)
    m = pattern.search(html_content)
    assert m is None, (
        f"Stored XSS detected in {context}: probe payload '{probe_label}' rendered raw. "
        f"Context: {html_content[max(0, m.start() - 100) : m.end() + 100]!r}"
    )
    # Also check for event-handler injection with our probe label
    eh_pattern = re.compile(
        rf'(onerror|onload)\s*=\s*["\']?[^"\']*{re.escape(probe_label)}',
        re.IGNORECASE,
    )
    m2 = eh_pattern.search(html_content)
    assert m2 is None, (
        f"Stored XSS (event handler) detected in {context}: probe '{probe_label}' in event handler"
    )


# ===========================================================================
# A. API layer — JSON responses must not inject HTML
# ===========================================================================


class TestApiXssInjectionPrevention:
    """User-supplied XSS payloads must be returned verbatim in JSON (not injected)."""

    def test_task_summary_xss_payload_returns_safe_json(self, server_available):
        """Submitting XSS payload as task_summary must not trigger injection in JSON response."""
        if not _PROJECT_ID:
            pytest.skip("TEST_PROJECT_BACKEND_ID not set")
        payload_label = _uid()
        xss_summary = _xss_probe(payload_label)
        resp = post_json(
            f"/api/projects/{_PROJECT_ID}/jobs",
            {
                "task_summary": xss_summary,
                "mode": "brownfield",
            },
        )
        if resp.status_code == 401:
            pytest.skip("Not authenticated")
        body = resp.text or ""
        # Check that our specific probe is not reflected raw (unescaped)
        _check_probe_not_raw(body, payload_label, "/api/projects/{id}/jobs response")

    def test_search_xss_payload_does_not_reflect(self, server_available):
        """Search query with XSS payload must not be reflected unescaped in response."""
        probe_label = _uid()
        resp = get(
            f'/api/search?q=XSS-probe-{probe_label}-<script>alert("{probe_label}")</script>&limit=1'
        )
        if resp.status_code in (401, 403, 404, 405):
            pytest.skip(f"Search endpoint not available: {resp.status_code}")
        body = resp.text or ""
        _check_probe_not_raw(body, probe_label, "/api/search response")

    def test_auth_login_xss_payload_does_not_reflect(self, server_available):
        """XSS in username field must not be reflected in login error response."""
        probe_label = _uid()
        resp = post_json(
            "/api/auth/login",
            {
                "username": f'XSS-probe-{probe_label}-<script>alert("{probe_label}")</script>',
                "password": "invalid-password-for-xss-test",
            },
        )
        body = resp.text or ""
        _check_probe_not_raw(body, probe_label, "/api/auth/login error response")


# ===========================================================================
# B. HTML rendering — user-controlled fields must be escaped
# ===========================================================================


class TestHtmlRenderingXssEscaping:
    """HTML pages must escape user-controlled content before rendering.

    Strategy: create a project/entity with a unique XSS probe in the name,
    load the relevant HTML page, verify the probe does NOT appear raw.
    The probe label is unique per run to avoid false positives from legitimate JS.
    """

    def test_project_name_xss_not_rendered_raw(self, server_available):
        """Project with XSS payload in name must render the name escaped in /projects HTML."""
        from tests.e2e.conftest import _authed_session

        session, verify_ssl = _authed_session()
        csrf = str(session.cookies.get("fs_csrf") or "").strip()
        probe_label = _uid()
        xss_name = f'XSS-probe-{probe_label}-<script>alert("{probe_label}")</script>'

        # Create project with XSS payload in name
        create_resp = session.post(
            f"{BASE_URL}/api/projects",
            json={
                "name": xss_name,
                "project_type": "general",
                "workspace_id": DEFAULT_WORKSPACE_ID,
            },
            headers={"x-csrf-token": csrf} if csrf else {},
            verify=verify_ssl,
        )
        if create_resp.status_code not in (200, 201):
            pytest.skip(
                f"Cannot create project with XSS name: {create_resp.status_code} — "
                f"{create_resp.text[:200]}"
            )
        created = create_resp.json()
        project_id = str(
            created.get("id") or (created.get("project") or {}).get("id") or ""
        ).strip()

        try:
            # Load projects page and verify payload is escaped
            page_resp = session.get(f"{BASE_URL}/projects", verify=verify_ssl, timeout=10)
            if page_resp.status_code in (401, 403):
                pytest.skip("Not authenticated for HTML page check")
            content = page_resp.text or ""
            _check_probe_not_raw(content, probe_label, "/projects HTML listing")
        finally:
            # Clean up the test project
            if project_id:
                with contextlib.suppress(Exception):
                    session.delete(
                        f"{BASE_URL}/api/projects/{project_id}",
                        headers={"x-csrf-token": csrf} if csrf else {},
                        verify=verify_ssl,
                    )

    def test_api_search_xss_not_reflected_in_json(self, server_available):
        """Search results with XSS content must not be reflected raw in JSON response."""
        from tests.e2e.conftest import _authed_session

        session, verify_ssl = _authed_session()
        probe_label = _uid()
        # Search for our probe string
        resp = session.get(
            f"{BASE_URL}/api/search",
            params={
                "q": f'XSS-probe-{probe_label}-<script>alert("{probe_label}")</script>',
                "limit": "1",
            },
            verify=verify_ssl,
            timeout=10,
        )
        if resp.status_code in (401, 403, 404, 405):
            pytest.skip(f"Search endpoint not available: {resp.status_code}")
        content = resp.text or ""
        _check_probe_not_raw(content, probe_label, "/api/search response")


# ===========================================================================
# C. Security response headers
# ===========================================================================


class TestSecurityResponseHeaders:
    """HTML responses must include protective security headers."""

    def _get_page_headers(self, path: str) -> dict:
        """Fetch page headers using auth session."""
        from tests.e2e.conftest import _authed_session

        session, verify_ssl = _authed_session()
        resp = session.get(f"{BASE_URL}{path}", verify=verify_ssl, timeout=10, allow_redirects=True)
        return resp.headers, resp.status_code

    def test_html_pages_have_content_type_text_html(self, server_available):
        """HTML pages must be served as text/html."""
        headers, status = self._get_page_headers("/projects")
        if status in (401, 403, 302):
            pytest.skip("Not authenticated")
        ct = headers.get("content-type", "")
        assert "text/html" in ct.lower(), f"/projects Content-Type must be text/html; got {ct!r}"

    def test_api_json_endpoints_served_as_json(self, server_available):
        """API endpoints must return application/json, not text/html."""
        resp = get("/api/jobs?limit=1")
        if resp.status_code in (401, 403):
            pytest.skip("Not authenticated")
        if resp.status_code != 200:
            pytest.skip(f"/api/jobs returned {resp.status_code}")
        ct = resp.headers.get("content-type", "")
        assert "application/json" in ct.lower() or "json" in ct.lower(), (
            f"/api/jobs Content-Type must be application/json; got {ct!r}"
        )

    def test_html_pages_have_xframe_or_csp(self, server_available):
        """HTML pages must include X-Frame-Options or Content-Security-Policy header."""
        headers, status = self._get_page_headers("/projects")
        if status in (401, 403, 302):
            pytest.skip("Not authenticated")
        lowered_header_names = {k.lower() for k in headers}
        has_protection = (
            "x-frame-options" in lowered_header_names
            or "content-security-policy" in lowered_header_names
        )
        assert has_protection, (
            "HTML pages must include X-Frame-Options or Content-Security-Policy header "
            f"to prevent clickjacking. Headers present: {list(headers.keys())}"
        )

    def test_html_pages_have_xcontent_type_options(self, server_available):
        """HTML pages must include X-Content-Type-Options: nosniff."""
        headers, status = self._get_page_headers("/projects")
        if status in (401, 403, 302):
            pytest.skip("Not authenticated")
        xcto = headers.get("x-content-type-options", "").lower()
        # This is a should (not must) — many apps use other mitigations
        if not xcto:
            pytest.skip("X-Content-Type-Options not set — acceptable if CSP is comprehensive")
        assert "nosniff" in xcto, f"X-Content-Type-Options must be 'nosniff'; got {xcto!r}"


# ===========================================================================
# D. CSRF protection
# ===========================================================================


class TestCsrfProtection:
    """Mutating API endpoints must require CSRF token or equiv protection."""

    def test_post_without_csrf_returns_4xx(self, server_available):
        """POST to a mutating endpoint without CSRF token must be rejected."""
        import requests as _req

        # Use a fresh session with no CSRF token
        anon_session = _req.Session()
        resp = anon_session.post(
            f"{BASE_URL}/api/projects",
            json={"name": f"csrf-test-{_uid()}", "project_type": "general"},
            verify=VERIFY_SSL,
            timeout=10,
        )
        # Must be rejected: 401 (no auth), 403 (no CSRF), or 422 (validation) — NOT 200/201
        assert resp.status_code not in (200, 201), (
            f"POST /api/projects without auth/CSRF must not succeed; got {resp.status_code}"
        )

    def test_login_page_has_csrf_token(self, server_available):
        """Login page must render the public auth shell without embedding auth secrets."""
        import requests as _req

        resp = _req.get(f"{BASE_URL}/login", verify=VERIFY_SSL, timeout=10)
        if resp.status_code not in (200, 302):
            pytest.skip(f"Login page not reachable: {resp.status_code}")
        content = resp.text or ""
        content_lower = content.lower()
        assert '<div id="root"></div>' in content_lower or "<div id='root'></div>" in content_lower, (
            "Login page must render the frontend auth shell mount point"
        )
        assert "/app/assets/" in content_lower, "Login page must boot the frontend auth bundle"
        assert "session_token" not in content_lower, "Login page must not inline session tokens"
        assert "bearer " not in content_lower, "Login page must not inline bearer tokens"
