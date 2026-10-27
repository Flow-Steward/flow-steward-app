"""E2E coverage for shared auth-management surfaces.

Covers account page, password changes, service accounts, personal tokens,
and community boundary checks for optional auth integration routes.
"""

from __future__ import annotations

import os

import pytest

from tests.e2e.helpers import E2ECleanup, get, post_json, unique_id

pytestmark = [pytest.mark.e2e]
ACTIVE_ROLE = str(os.getenv("E2E_BACKEND_ROLE") or "admin").strip().lower() or "admin"


def _assert_route_mounted(response, route_label: str) -> None:
    assert response.status_code != 404, f"{route_label} must stay mounted"


def _is_react_shell_html(html: str) -> bool:
    lowered = str(html or "").lower()
    return '<div id="root"></div>' in lowered and "/app/assets/index" in lowered


@pytest.fixture(scope="module")
def server_available():
    try:
        resp = get("/health", timeout=5)
    except Exception as exc:
        pytest.skip(f"Flow Steward server not available: {exc}")
    if resp.status_code >= 500:
        pytest.skip(f"Flow Steward server unhealthy: {resp.status_code}")


class TestOptionalAuthBoundary:
    def test_optional_auth_admin_route_not_mounted(self, server_available):
        resp = get("/api/auth/optional-auth-integrations")
        assert resp.status_code == 404

    def test_optional_auth_policy_route_not_mounted(self, server_available):
        resp = get("/api/auth/optional-auth-policies")
        assert resp.status_code == 404


class TestAccountPage:
    def test_account_page_loads(self, server_available):
        resp = get("/settings/account")
        if resp.status_code in (302, 303):
            pytest.skip("Account page redirects due to auth state")
        assert resp.status_code == 200
        html = resp.text.lower()
        assert "<html" in html
        if _is_react_shell_html(resp.text):
            return
        assert "account" in html or "password" in html or "profile" in html


class TestChangePassword:
    def test_change_password_wrong_current(self, server_available):
        resp = post_json(
            "/api/auth/change-password",
            {
                "current_password": "definitely_wrong_password_12345!",
                "new_password": "NewPassword_123!",
            },
        )
        if resp.status_code == 404:
            pytest.skip("Change-password endpoint is not available in this environment")
        assert resp.status_code in (400, 401, 403, 422)

    def test_change_password_weak_new(self, server_available):
        current_password = (
            str(os.getenv("E2E_ADMIN_PASSWORD", "e2e_admin_password_123!")).strip()
            or "e2e_admin_password_123!"
        )
        resp = post_json(
            "/api/auth/change-password",
            {
                "current_password": current_password,
                "new_password": "123",
            },
        )
        if resp.status_code == 404:
            pytest.skip("Change-password endpoint is not available in this environment")
        assert resp.status_code in (400, 422)


class TestServiceAccounts:
    def test_service_accounts_list_route_mounted(self, server_available):
        resp = get("/api/auth/service-accounts")
        _assert_route_mounted(resp, "GET /api/auth/service-accounts")
        if ACTIVE_ROLE != "admin":
            assert resp.status_code in (401, 403)
            return
        assert resp.status_code == 200
        assert isinstance(resp.json(), (dict, list))

    def test_service_account_token_routes_mounted_and_usable(self, server_available):
        uid = unique_id("svc")
        with E2ECleanup() as cleanup:
            create_resp = post_json(
                "/api/auth/service-accounts",
                {
                    "service_account_key": uid,
                    "display_name": f"Auth scope {uid}",
                },
            )
            _assert_route_mounted(create_resp, "POST /api/auth/service-accounts")
            if ACTIVE_ROLE != "admin":
                assert create_resp.status_code in (401, 403)
                return

            assert create_resp.status_code in (200, 201), (
                f"service-account create failed: {create_resp.status_code} {create_resp.text[:300]}"
            )
            cleanup.add("DELETE", f"/api/auth/service-accounts/{uid}")

            tokens_list_resp = get(f"/api/auth/service-accounts/{uid}/tokens")
            _assert_route_mounted(tokens_list_resp, "GET /api/auth/service-accounts/{key}/tokens")
            assert tokens_list_resp.status_code == 200

            token_create_resp = post_json(
                f"/api/auth/service-accounts/{uid}/tokens",
                {"name": "auth-core-token"},
            )
            _assert_route_mounted(token_create_resp, "POST /api/auth/service-accounts/{key}/tokens")
            assert token_create_resp.status_code in (200, 201), (
                f"service-account token create failed: {token_create_resp.status_code} {token_create_resp.text[:300]}"
            )

            token_payload = token_create_resp.json()
            token_id = token_payload.get("id") or token_payload.get("token_id")
            assert token_id, "service-account token response must include token id"

            revoke_resp = post_json(
                f"/api/auth/service-accounts/{uid}/tokens/{token_id}/revoke",
                {},
            )
            _assert_route_mounted(
                revoke_resp,
                "POST /api/auth/service-accounts/{key}/tokens/{token_id}/revoke",
            )
            assert revoke_resp.status_code in (200, 204)


class TestPersonalTokens:
    def test_personal_tokens_list_route_mounted(self, server_available):
        resp = get("/api/auth/tokens")
        _assert_route_mounted(resp, "GET /api/auth/tokens")
        if resp.status_code in (401, 403):
            return
        assert resp.status_code == 200
        assert isinstance(resp.json(), (dict, list))

    def test_personal_token_create_and_revoke_routes_mounted(self, server_available):
        create_resp = post_json(
            "/api/auth/tokens",
            {"name": f"auth-core-token-{unique_id()}"},
        )
        _assert_route_mounted(create_resp, "POST /api/auth/tokens")
        if create_resp.status_code in (401, 403):
            return

        assert create_resp.status_code in (200, 201), (
            f"personal token create failed: {create_resp.status_code} {create_resp.text[:300]}"
        )
        payload = create_resp.json()
        token_id = payload.get("id") or payload.get("token_id") or payload.get("api_token_id")
        assert token_id, "personal token response must include token id"

        revoke_resp = post_json(f"/api/auth/tokens/{token_id}/revoke", {})
        _assert_route_mounted(revoke_resp, "POST /api/auth/tokens/{token_id}/revoke")
        assert revoke_resp.status_code in (200, 204)
