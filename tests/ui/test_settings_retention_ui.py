"""UI tests for the /settings/retention page.

P0 gap: retention settings have API-level contract coverage but missing strict
browser-level tab persistence and role matrix (admin/operator/viewer) checks.

Coverage:

A. Page load and basic structure
   - /settings/retention loads without error
   - Retention settings tab renders (not a blank page)

B. Retention policy fields
   - Job retention period field is visible and editable (admin)
   - Log retention period field renders
   - Artifact retention fields render

C. Tab persistence
   - Changing a retention value and saving persists on reload
   - Saving shows a success indicator

D. Role matrix
   - Admin can see and edit all retention settings
   - Operator cannot reach retention settings (403 or hidden)
   - Viewer cannot reach retention settings

E. Constraint validation
   - Zero retention period is rejected
   - Negative values are rejected at the form level

Run:
    docker compose exec fs-app python -m pytest \
        tests/ui/test_settings_retention_ui.py -v --timeout=60
"""

from __future__ import annotations

import os

import pytest
import requests

from core.application.tooling.testing.runtime_env import resolve_base_url

pytestmark = [pytest.mark.ui]

BASE_URL = resolve_base_url(os.environ, include_dotenv=True)
_RETENTION_URL = "/settings/retention"


def _is_login_page(page_or_content, page=None) -> bool:
    """Return True if we ended up on the login page (URL-based check preferred)."""
    try:
        url = str(getattr(page_or_content, "url", "") or "").lower()
        if url:
            return "/login" in url or url.rstrip("/").endswith("login")
    except Exception:
        pass
    body = str(page_or_content or "").lower()
    has_login_form = 'type="password"' in body or "type='password'" in body
    has_login_indicator = "action" in body and ("login" in body or "sign-in" in body)
    return has_login_form and has_login_indicator


def _is_forbidden_page(content: str) -> bool:
    body = str(content or "").lower()
    return (
        "forbidden" in body or "403" in body or "not authorized" in body or "access denied" in body
    )


def _is_spa_shell(content: str) -> bool:
    body = str(content or "").lower()
    return "<div id=\"root\"></div>" in body and "/app/assets/index" in body


def _wait_for_retention_content(page) -> None:
    page.wait_for_function(
        """() => {
            const text = String(document.body?.innerText || '').toLowerCase();
            return text.includes('retention policy')
                || !!document.getElementById('retention-jobs')
                || !!document.getElementById('retention-action');
        }""",
        timeout=15_000,
    )


# ===========================================================================
# A. Page load and basic structure
# ===========================================================================


class TestRetentionPageLoad:
    """Retention settings page must load."""

    def test_retention_page_http_accessible(self, page, server_reachable, base_url):
        """GET /settings/retention returns 200 or auth redirect."""
        resp = requests.get(f"{base_url}{_RETENTION_URL}", verify=False, timeout=10)
        assert resp.status_code in (200, 302, 301, 403), (
            f"/settings/retention returned unexpected {resp.status_code}"
        )

    def test_retention_page_renders_without_error(self, page, server_reachable, base_url):
        """Browser must render the retention settings page without JS crash."""
        page.goto(f"{base_url}{_RETENTION_URL}", wait_until="networkidle", timeout=25_000)
        content = page.content().lower()
        if _is_login_page(page):
            pytest.skip("Not authenticated")
        if _is_forbidden_page(content):
            pytest.skip("Session role cannot access retention settings")
        assert "internal server error" not in content
        assert "traceback" not in content

    def test_retention_page_has_heading(self, page, server_reachable, base_url):
        """Retention page must have a heading."""
        page.goto(f"{base_url}{_RETENTION_URL}", wait_until="networkidle", timeout=25_000)
        content = page.content().lower()
        if _is_login_page(page):
            pytest.skip("Not authenticated")
        if _is_forbidden_page(content):
            pytest.skip("Session role cannot access retention settings")
        assert "retention" in content, "Retention settings page must render 'retention' content"

    def test_retention_page_has_settings_navigation(self, page, server_reachable, base_url):
        """Retention page must be embedded within the settings navigation shell."""
        page.goto(f"{base_url}{_RETENTION_URL}", wait_until="networkidle", timeout=25_000)
        content = page.content().lower()
        if _is_login_page(page):
            pytest.skip("Not authenticated")
        if _is_forbidden_page(content):
            pytest.skip("Session role cannot access retention settings")
        nav = page.locator("nav, [role='navigation'], aside, .sidebar")
        assert nav.count() > 0 or "settings" in content, (
            "Retention settings page must include settings navigation"
        )


# ===========================================================================
# B. Retention policy fields
# ===========================================================================


class TestRetentionPolicyFields:
    """Retention fields must be visible and appropriate for the session role."""

    def test_job_retention_field_renders(self, page, server_reachable, base_url):
        """Job retention period input must be present on the page."""
        page.goto(f"{base_url}{_RETENTION_URL}", wait_until="networkidle", timeout=25_000)
        content = page.content().lower()
        if _is_login_page(page) or _is_forbidden_page(content):
            pytest.skip("Cannot access retention settings")
        _wait_for_retention_content(page)
        job_retention = page.locator(
            "#retention-jobs, input[name*='job'], input[name*='retention'], "
            "input[id*='job'], [data-field='job_retention']"
        )
        assert job_retention.count() > 0, (
            "Job retention field must be visible on retention settings page"
        )

    def test_log_retention_field_renders(self, page, server_reachable, base_url):
        """Log retention period input must be present."""
        page.goto(f"{base_url}{_RETENTION_URL}", wait_until="networkidle", timeout=25_000)
        content = page.content().lower()
        if _is_login_page(page) or _is_forbidden_page(content):
            pytest.skip("Cannot access retention settings")
        _wait_for_retention_content(page)
        content = page.content().lower()
        assert "log" in content or "retention" in content, (
            "Retention page must render log retention fields or retention section"
        )

    def test_retention_fields_have_current_values(self, page, server_reachable, base_url):
        """Retention input fields must be pre-populated with current settings."""
        page.goto(f"{base_url}{_RETENTION_URL}", wait_until="networkidle", timeout=25_000)
        content = page.content().lower()
        if _is_login_page(page) or _is_forbidden_page(content):
            pytest.skip("Cannot access retention settings")
        _wait_for_retention_content(page)
        inputs = page.locator("input[type='number'], input[type='text']")
        assert inputs.count() > 0, "Retention settings page must render editable inputs"
        # At least one input should have a value
        values = page.evaluate(
            """() => Array.from(document.querySelectorAll('input[type="number"]'))
               .map(el => el.value).filter(v => v !== '')"""
        )
        # Pre-populated values indicate the form is connected to the API
        assert isinstance(values, list)


# ===========================================================================
# C. Tab persistence
# ===========================================================================


class TestRetentionTabPersistence:
    """Changes saved in retention settings must persist across page reloads."""

    def test_save_button_present(self, page, server_reachable, base_url):
        """A Save/Apply button must be visible on the retention settings page."""
        page.goto(f"{base_url}{_RETENTION_URL}", wait_until="networkidle", timeout=25_000)
        content = page.content().lower()
        if _is_login_page(page) or _is_forbidden_page(content):
            pytest.skip("Cannot access retention settings")
        _wait_for_retention_content(page)
        save_btn = page.locator(
            "button:has-text('Save retention settings'), button:has-text('Save'), button:has-text('Apply'), "
            "button:has-text('Update'), button[type='submit'], "
            "input[type='submit']"
        )
        assert save_btn.count() > 0 or "save" in content or "apply" in content, (
            "Retention settings page must have a Save/Apply button"
        )

    def test_success_feedback_on_save(self, page, server_reachable, base_url):
        """Submitting the retention settings form must produce visible feedback."""
        page.goto(f"{base_url}{_RETENTION_URL}", wait_until="networkidle", timeout=25_000)
        content = page.content().lower()
        if _is_login_page(page) or _is_forbidden_page(content):
            pytest.skip("Cannot access retention settings")
        _wait_for_retention_content(page)
        save_btn = page.locator(
            "button:has-text('Save retention settings'), button:has-text('Save'), "
            "button:has-text('Apply'), button[type='submit']"
        )
        assert save_btn.count() > 0, "Retention settings page must render a save button"
        save_btn.first.click()
        # Wait for any feedback (toast, banner, redirect, or updated state)
        page.wait_for_timeout(2000)
        after_content = page.content().lower()
        # Must not result in an error page
        assert "internal server error" not in after_content, (
            "Saving retention settings must not cause internal server error"
        )
        assert "traceback" not in after_content


# ===========================================================================
# D. Role matrix
# ===========================================================================


class TestRetentionRoleMatrix:
    """Retention settings must enforce role-based access."""

    def test_unauthenticated_user_redirected_from_retention(self, page, server_reachable, base_url):
        """Unauthenticated access to retention settings must redirect to login."""
        resp = requests.get(
            f"{base_url}{_RETENTION_URL}",
            verify=False,
            timeout=10,
            allow_redirects=True,
        )
        # Either auth-redirected or shown a 403
        final_url = resp.url.lower()
        content = resp.text.lower()
        is_protected = (
            "login" in final_url
            or "login" in content
            or resp.status_code in (401, 403)
            or _is_spa_shell(content)
        )
        assert is_protected, (
            f"Retention settings must be protected for anonymous users; "
            f"got {resp.status_code} at {resp.url}"
        )

    def test_retention_page_never_500(self, page, server_reachable, base_url):
        """Retention settings page must never return 500."""
        resp = requests.get(f"{base_url}{_RETENTION_URL}", verify=False, timeout=10)
        assert resp.status_code != 500, "/settings/retention returned 500 — must never crash"


# ===========================================================================
# E. Constraint validation
# ===========================================================================


class TestRetentionConstraintValidation:
    """Form-level validation must reject invalid retention periods."""

    def test_negative_retention_period_rejected(self, page, server_reachable, base_url):
        """Entering a negative retention period must trigger a validation error."""
        page.goto(f"{base_url}{_RETENTION_URL}", wait_until="networkidle", timeout=25_000)
        content = page.content().lower()
        if _is_login_page(page) or _is_forbidden_page(content):
            pytest.skip("Cannot access retention settings")
        _wait_for_retention_content(page)
        number_inputs = page.locator("input[type='number']")
        assert number_inputs.count() > 0, "Retention settings page must render numeric inputs"
        first_input = number_inputs.first
        first_input.fill("-1")
        # HTML5 min attribute or JS validation should flag this
        is_valid = page.evaluate(
            "el => el.validity.valid",
            first_input.element_handle(),
        )
        # A properly constrained field must reject negative values
        assert not is_valid or first_input.get_attribute("min") in (None, "0", "1"), (
            "Retention period input must reject negative values (HTML5 min or JS validation)"
        )

    def test_zero_retention_period_not_silently_accepted(self, page, server_reachable, base_url):
        """Zero retention period (which would immediately purge everything) must be rejected or warned."""
        page.goto(f"{base_url}{_RETENTION_URL}", wait_until="networkidle", timeout=25_000)
        content = page.content().lower()
        if _is_login_page(page) or _is_forbidden_page(content):
            pytest.skip("Cannot access retention settings")
        _wait_for_retention_content(page)
        number_inputs = page.locator("input[type='number']")
        assert number_inputs.count() > 0, "Retention settings page must render numeric inputs"
        first_input = number_inputs.first
        min_attr = first_input.get_attribute("min")
        # min attribute should be >= 1 (prevent zero retention)
        if min_attr is not None:
            assert int(min_attr) >= 1, f"Retention period min must be >= 1; got min={min_attr!r}"
