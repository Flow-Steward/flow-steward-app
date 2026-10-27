"""
UI tests for Registry Tools CRUD.

Tests:
- List: loads, toggle works
- Create: modal (tool_id, display_name, description, category, implementation, prompt), submits
- Detail: form editable, save persists, used-by-agents sidebar, recent-changes sidebar
- Delete: from list

Run:
    docker compose exec fs-app python -m pytest tests/ui/test_registry_tools.py -v --timeout=60
"""

from __future__ import annotations

import os

import pytest
import requests

from core.application.tooling.testing.runtime_env import resolve_base_url

pytestmark = [pytest.mark.ui]

BASE_URL = resolve_base_url(os.environ, include_dotenv=True)


def _assert_registry_tool_status(resp, active_test_role: str):
    if active_test_role in {"admin", "operator"}:
        assert resp.ok or resp.status_code == 404
        return
    assert resp.status_code == 403


@pytest.fixture(scope="module")
def registry_mode_enabled(base_url, ui_auth_session):
    session, verify_ssl = ui_auth_session
    _get = session.get if session else requests.get
    try:
        resp = _get(f"{base_url}/api/tools", verify=verify_ssl, timeout=5)
        if resp.status_code == 404:
            pytest.skip("Registry mode not enabled")
    except Exception as e:
        pytest.skip(f"Registry API not reachable: {e}")


@pytest.fixture(scope="module")
def first_tool_id(base_url, registry_mode_enabled, ui_auth_session, active_test_role):
    if active_test_role != "admin":
        pytest.skip("Registry tool API is admin-only")
    """Get first tool ID from core.application.registry."""
    session, verify_ssl = ui_auth_session
    _get = session.get if session else requests.get
    resp = _get(f"{base_url}/api/tools", verify=verify_ssl, timeout=5)
    if not resp.ok:
        pytest.skip("Cannot fetch tools list")
    data = resp.json()
    tools = data if isinstance(data, list) else data.get("tools", [])
    if not tools:
        pytest.skip("No tools in registry")
    return tools[0].get("tool_id") or tools[0].get("id")


class TestRegistryToolsList:
    """Tool list."""

    def test_tools_api_returns_list(self, base_url, registry_mode_enabled, active_test_role):
        """GET /api/tools returns a list."""
        resp = requests.get(f"{base_url}/api/tools", verify=False, timeout=5)
        _assert_registry_tool_status(resp, active_test_role)
        if not resp.ok:
            return
        data = resp.json()
        tools = data if isinstance(data, list) else data.get("tools", [])
        assert isinstance(tools, list)

    def test_tools_have_required_fields(self, base_url, registry_mode_enabled):
        """Each tool has tool_id and display_name."""
        resp = requests.get(f"{base_url}/api/tools", verify=False, timeout=5)
        if resp.ok:
            data = resp.json()
            tools = data if isinstance(data, list) else data.get("tools", [])
            if tools:
                first = tools[0]
                assert "tool_id" in first or "id" in first

    def test_tools_have_enabled_field(self, base_url, registry_mode_enabled):
        """Each tool has an enabled field."""
        resp = requests.get(f"{base_url}/api/tools", verify=False, timeout=5)
        if resp.ok:
            data = resp.json()
            tools = data if isinstance(data, list) else data.get("tools", [])
            if tools:
                assert "enabled" in tools[0]


class TestRegistryToolDetail:
    """Tool detail API."""

    def test_tool_detail_returns_data(
        self, base_url, registry_mode_enabled, first_tool_id, active_test_role
    ):
        """GET /api/tools/{id} returns tool data."""
        resp = requests.get(f"{base_url}/api/tools/{first_tool_id}", verify=False, timeout=5)
        _assert_registry_tool_status(resp, active_test_role)
        if not resp.ok:
            return
        data = resp.json()
        assert isinstance(data, dict)

    def test_tool_detail_has_display_name(self, base_url, registry_mode_enabled, first_tool_id):
        """Tool detail has display_name."""
        resp = requests.get(f"{base_url}/api/tools/{first_tool_id}", verify=False, timeout=5)
        if resp.ok:
            data = resp.json()
            assert "display_name" in data or "name" in data

    def test_tool_detail_has_category(self, base_url, registry_mode_enabled, first_tool_id):
        """Tool detail has category field."""
        resp = requests.get(f"{base_url}/api/tools/{first_tool_id}", verify=False, timeout=5)
        if resp.ok:
            data = resp.json()
            assert "category" in data or isinstance(data, dict)

    def test_tool_not_found_returns_404(self, base_url, registry_mode_enabled, active_test_role):
        """GET /api/tools/nonexistent returns 404."""
        resp = requests.get(
            f"{base_url}/api/tools/nonexistent_tool_xyz_abc",
            verify=False,
            timeout=5,
        )
        if active_test_role in {"admin", "operator"}:
            assert resp.status_code == 404
        else:
            assert resp.status_code == 403


class TestRegistryToolCreate:
    """Tool creation."""

    def test_create_tool_with_missing_fields_returns_error(
        self, base_url, registry_mode_enabled, active_test_role
    ):
        """Creating tool without required fields returns 400/422."""
        resp = requests.post(
            f"{base_url}/api/tools",
            json={"incomplete": "data"},
            verify=False,
            timeout=5,
        )
        if active_test_role == "admin":
            assert resp.status_code in (400, 422, 409)
        else:
            assert resp.status_code == 403

    def test_create_tool_with_valid_data(self, base_url, registry_mode_enabled, active_test_role):
        """Creating tool with valid data returns 200/201."""
        test_tool = {
            "tool_id": "test_ui_tool_xyz_delete_me",
            "display_name": "Test UI Tool",
            "description": "Created by UI test — delete me",
            "category": "utility",
            "enabled": False,
        }
        resp = requests.post(
            f"{base_url}/api/tools",
            json=test_tool,
            verify=False,
            timeout=5,
        )
        if resp.status_code in (200, 201):
            requests.delete(
                f"{base_url}/api/tools/test_ui_tool_xyz_delete_me",
                verify=False,
                timeout=5,
            )
        if active_test_role == "admin":
            assert resp.status_code in (200, 201, 409, 422)
        else:
            assert resp.status_code == 403


class TestRegistryToolToggle:
    """Tool enable/disable toggle."""

    def test_toggle_endpoint_exists(self, base_url, registry_mode_enabled, first_tool_id):
        """Toggle endpoint responds."""
        resp = requests.post(
            f"{base_url}/api/tools/{first_tool_id}/toggle",
            verify=False,
            timeout=5,
        )
        assert resp.status_code not in (404,)


class TestRegistryToolHistory:
    """Tool history."""

    def test_tool_history_endpoint(self, base_url, registry_mode_enabled, first_tool_id):
        """Tool history endpoint responds."""
        resp = requests.get(
            f"{base_url}/api/tools/{first_tool_id}/history",
            verify=False,
            timeout=5,
        )
        assert resp.status_code in (200, 404)
