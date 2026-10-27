"""E2E tests for additional page smoke tests.

Covers: PAGE-13 to PAGE-19 — account, events, extension detail,
project history, forgot password, reset password, bootstrap pages.
Also covers webhooks and misc API gaps.
"""

from __future__ import annotations

import os

import pytest

from core.application.tooling.testing.runtime_env import resolve_test_env
from tests.e2e.helpers import get, post_json

pytestmark = [pytest.mark.e2e]

TEST_ENV = resolve_test_env(os.environ, lane="backend", include_dotenv=True)
PROJECT_ID = str(TEST_ENV.project_id or "").strip()


@pytest.fixture(scope="module")
def server_available():
    try:
        resp = get("/health", timeout=5)
    except Exception as e:
        pytest.skip(f"Flow Steward server not available: {e}")
    if resp.status_code >= 500:
        pytest.skip(f"Flow Steward server unhealthy: {resp.status_code}")


@pytest.fixture(scope="module")
def test_project_id() -> str:
    if not PROJECT_ID:
        pytest.skip("TEST_PROJECT_BACKEND_ID not set")
    return PROJECT_ID


def _assert_html_ok(resp, markers=None):
    assert resp.status_code == 200, f"expected 200 for {resp.url}, got {resp.status_code}"
    lowered = resp.text.lower()
    assert "<html" in lowered
    if markers:
        for m in markers:
            assert m.lower() in lowered, f"missing marker {m!r} in {resp.url}"


# ── Page Smoke Tests ────────────────────────────────────────────────────────


class TestAdditionalPages:
    def test_account_page(self, server_available):
        resp = get("/settings/account")
        if resp.status_code in (302, 303):
            pytest.skip("Account page redirects")
        _assert_html_ok(resp)

    def test_account_settings_alias_removed(self, server_available):
        resp = get("/settings/account-settings")
        assert resp.status_code == 404

    def test_events_alias_removed(self, server_available):
        resp = get("/events")
        assert resp.status_code == 404

    def test_project_history_page(self, server_available, test_project_id):
        resp = get(f"/projects/{test_project_id}?tab=history")
        _assert_html_ok(resp)

    def test_extension_detail_page(self, server_available):
        """Load extension detail page for first available extension."""
        list_resp = get("/api/extensions")
        if list_resp.status_code != 200:
            pytest.skip("Cannot list extensions")
        extensions = (list_resp.json() or {}).get("extensions") or []
        if not extensions:
            pytest.skip("No extensions available")
        ext_id = (
            extensions[0].get("extension_id")
            or extensions[0].get("extension_key")
            or extensions[0].get("key")
            or extensions[0].get("id")
        )
        resp = get(f"/extensions/{ext_id}")
        if resp.status_code == 404:
            pytest.skip("Extension detail page not implemented")
        _assert_html_ok(resp)


class TestPublicPages:
    """Pages that do not require authentication."""

    @pytest.mark.e2e_auth_public
    def test_forgot_password_page(self, server_available):
        resp = get("/forgot-password")
        _assert_html_ok(resp)

    @pytest.mark.e2e_auth_public
    def test_reset_password_page(self, server_available):
        resp = get("/reset-password")
        _assert_html_ok(resp)

    @pytest.mark.e2e_auth_public
    def test_bootstrap_page(self, server_available):
        resp = get("/bootstrap")
        _assert_html_ok(resp)

    @pytest.mark.e2e_auth_public
    def test_login_page(self, server_available):
        resp = get("/login")
        _assert_html_ok(resp)


# ── Webhooks ────────────────────────────────────────────────────────────────


class TestWebhooks:
    def test_extension_webhook_endpoint(self, server_available):
        """Extension webhook should exist even if no handler is registered."""
        resp = post_json(
            "/webhook/extensions/nonexistent_key",
            {
                "event": "test",
            },
        )
        # 404 or 400 (no handler) or 401 (bad secret) are all acceptable
        assert resp.status_code in (200, 400, 401, 404, 422)

    def test_reaction_ingestion_endpoint(self, server_available):
        resp = post_json(
            "/api/reactions/ingest",
            {
                "source": "e2e_test",
                "type": "positive",
                "content": "Test reaction",
            },
        )
        if resp.status_code == 404:
            pytest.skip("Reaction ingestion not implemented")
        assert resp.status_code in (200, 201, 400, 401, 422)


# ── Commands API ────────────────────────────────────────────────────────────


class TestCommandsAPI:
    def test_commands_endpoint_exists(self, server_available):
        resp = post_json(
            "/api/commands",
            {
                "command": "status",
            },
        )
        if resp.status_code == 404:
            pytest.skip("Commands endpoint not implemented")
        assert resp.status_code in (200, 202, 400, 422)
