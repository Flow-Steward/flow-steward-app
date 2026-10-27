"""E2E tests for admin user-management surface (AUTH-05).

Covers create, list, update, role assignment, enable/disable,
unlock, and force-password-reset using isolated ``e2e_mgmt_*`` users
that are cleaned up after each test.
"""

from __future__ import annotations

import pytest

from tests.e2e.helpers import (
    E2ECleanup,
    get,
    post_json,
    put_json,
    unique_id,
)

pytestmark = [pytest.mark.e2e]


@pytest.fixture(scope="module")
def server_available():
    try:
        resp = get("/health", timeout=5)
    except Exception as e:
        pytest.skip(f"Flow Steward server not available: {e}")
    if resp.status_code >= 500:
        pytest.skip(f"Flow Steward server unhealthy: {resp.status_code}")


def _create_user(cleanup: E2ECleanup, **overrides) -> dict:
    """Create an isolated e2e user and register cleanup."""
    username = overrides.pop("username", f"{unique_id('e2e_mgmt')}@example.com")
    payload = {
        "email": username,
        "password": "E2eMgmtPass_1234!",
        "display_name": f"E2E User {username}",
        "roles": ["viewer"],
        "is_active": True,
        "must_change_password": False,
        **overrides,
    }
    resp = post_json("/api/auth/users", payload)
    assert resp.status_code == 200, f"Create user failed: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    user = body if "id" in body else body.get("user", body)
    user_id = str(user.get("id") or user.get("user_id") or "").strip()
    assert user_id, f"No user id returned: {body}"
    cleanup.add("PUT", f"/api/auth/users/{user_id}", {"is_active": False})
    return {"id": user_id, "username": username, **payload}


class TestAdminUserManagement:
    def test_user_crud_lifecycle(self, server_available):
        with E2ECleanup() as cleanup:
            # Create
            user = _create_user(cleanup)
            user_id = user["id"]

            # List — find our user
            list_resp = get("/api/auth/users", params={"search": user["username"], "per_page": 200})
            assert list_resp.status_code == 200
            users = list_resp.json().get("users") or list_resp.json()
            assert isinstance(users, list)
            found = [u for u in users if str(u.get("id") or "").strip() == user_id]
            assert found, f"Created user {user_id} not found in user list"
            assert found[0]["username"] == user["username"]

            # Update — change display_name & roles
            update_resp = put_json(
                f"/api/auth/users/{user_id}",
                {
                    "display_name": "Updated E2E User",
                    "roles": ["admin"],
                    "is_active": True,
                },
            )
            assert update_resp.status_code == 200
            updated = update_resp.json()
            u = updated if "username" in updated else updated.get("user", updated)
            assert u.get("display_name") == "Updated E2E User"
            assert "admin" in (u.get("roles") or [])

            # Disable
            disable_resp = put_json(f"/api/auth/users/{user_id}", {"is_active": False})
            assert disable_resp.status_code == 200
            d = disable_resp.json()
            du = d if "is_active" in d else d.get("user", d)
            assert du.get("is_active") is False

            # Re-enable
            enable_resp = put_json(f"/api/auth/users/{user_id}", {"is_active": True})
            assert enable_resp.status_code == 200

    def test_user_unlock(self, server_available):
        with E2ECleanup() as cleanup:
            user = _create_user(cleanup)
            resp = post_json(f"/api/auth/users/{user['id']}/unlock", {})
            assert resp.status_code == 200
            body = resp.json()
            assert body.get("status") == "unlocked"
            assert body.get("username") == user["username"]

    def test_user_force_password_reset(self, server_available):
        with E2ECleanup() as cleanup:
            user = _create_user(cleanup)
            resp = post_json(f"/api/auth/users/{user['id']}/force-password-reset", {})
            assert resp.status_code == 200
            body = resp.json()
            assert body.get("status") == "password_reset_required"
            assert isinstance(body.get("revoked_sessions"), int)

    def test_user_create_duplicate_rejected(self, server_available):
        with E2ECleanup() as cleanup:
            user = _create_user(cleanup)
            dup_resp = post_json(
                "/api/auth/users",
                {
                    "email": user["username"],
                    "password": "AnotherPass_1234!",
                },
            )
            assert dup_resp.status_code in (400, 409)
            err = str((dup_resp.json() or {}).get("error") or "").lower()
            assert "exists" in err or "duplicate" in err or "conflict" in err

    def test_user_create_weak_password_rejected(self, server_available):
        resp = post_json(
            "/api/auth/users",
            {
                "email": f"{unique_id('e2e_weak')}@example.com",
                "password": "short",
            },
        )
        assert resp.status_code == 400
        err = str((resp.json() or {}).get("error") or "").lower()
        assert "password" in err or "length" in err or "characters" in err

    def test_users_page_loads(self, server_available):
        resp = get("/settings/users")
        assert resp.status_code == 200
        html = resp.text.lower()
        if '<div id="root"></div>' in html and "/app/assets/index" in html:
            return
        assert "user" in html
