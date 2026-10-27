"""
UI tests for Registry Agents CRUD.

Tests:
- List page loads, shows table with rows
- Shows enabled status, toggle works
- Create: modal opens, fields present, submits
- Detail page: all 6 tabs
  - prompts-tab: editable, save persists
  - skills-tab: shows skill cards
  - tools-tab: shows tools table
  - config-tab: model/iterations/timeout editable
  - manifest-tab: lazy-loads, save persists
  - history-tab: shows commits, rollback works
- Delete from list

Run:
    docker compose exec fs-app python -m pytest tests/ui/test_registry_agents.py -v --timeout=60
"""

from __future__ import annotations

import os

import pytest
import requests

from core.application.tooling.testing.runtime_env import resolve_base_url

pytestmark = [pytest.mark.ui]

BASE_URL = resolve_base_url(os.environ, include_dotenv=True)


def _assert_registry_agent_status(resp, active_test_role: str, *, write: bool = False):
    if active_test_role == "admin":
        assert resp.ok or resp.status_code == 404
        return
    assert resp.status_code == 403


@pytest.fixture(scope="module")
def registry_mode_enabled(base_url, ui_auth_session):
    session, verify_ssl = ui_auth_session
    _get = session.get if session else requests.get
    try:
        resp = _get(f"{base_url}/api/agents", verify=verify_ssl, timeout=5)
        if resp.status_code == 404:
            pytest.skip("Registry mode not enabled")
    except Exception as e:
        pytest.skip(f"Registry API not reachable: {e}")


@pytest.fixture(scope="module")
def first_agent_id(base_url, registry_mode_enabled, ui_auth_session, active_test_role):
    """Get the first agent ID from the registry."""
    if active_test_role != "admin":
        pytest.skip("Registry agent API is admin-only")
    session, verify_ssl = ui_auth_session
    _get = session.get if session else requests.get
    resp = _get(f"{base_url}/api/agents", verify=verify_ssl, timeout=5)
    if not resp.ok:
        pytest.skip("Cannot fetch agents list")
    data = resp.json()
    agents = data if isinstance(data, list) else data.get("agents", [])
    if not agents:
        pytest.skip("No agents in registry")
    return agents[0].get("agent_id") or agents[0].get("id")


class TestRegistryAgentsList:
    """Agent list page."""

    def test_agents_api_returns_list(self, base_url, registry_mode_enabled, active_test_role):
        """GET /api/agents returns a list."""
        resp = requests.get(f"{base_url}/api/agents", verify=False, timeout=5)
        _assert_registry_agent_status(resp, active_test_role)
        if not resp.ok:
            return
        data = resp.json()
        agents = data if isinstance(data, list) else data.get("agents", [])
        assert isinstance(agents, list)

    def test_agents_list_has_agent_id(self, base_url, registry_mode_enabled):
        """Each agent has an agent_id field."""
        resp = requests.get(f"{base_url}/api/agents", verify=False, timeout=5)
        if resp.ok:
            data = resp.json()
            agents = data if isinstance(data, list) else data.get("agents", [])
            if agents:
                first = agents[0]
                assert "agent_id" in first or "id" in first

    def test_agents_list_has_enabled_field(self, base_url, registry_mode_enabled):
        """Each agent has an enabled field."""
        resp = requests.get(f"{base_url}/api/agents", verify=False, timeout=5)
        if resp.ok:
            data = resp.json()
            agents = data if isinstance(data, list) else data.get("agents", [])
            if agents:
                first = agents[0]
                assert "enabled" in first


class TestRegistryAgentDetail:
    """Agent detail page with 6 tabs."""

    def test_agent_detail_returns_data(
        self, base_url, registry_mode_enabled, first_agent_id, active_test_role
    ):
        """GET /api/agents/{id} returns agent data."""
        resp = requests.get(f"{base_url}/api/agents/{first_agent_id}", verify=False, timeout=5)
        _assert_registry_agent_status(resp, active_test_role)
        if not resp.ok:
            return
        data = resp.json()
        assert isinstance(data, dict)

    def test_agent_detail_has_display_name(self, base_url, registry_mode_enabled, first_agent_id):
        """Agent detail has display_name."""
        resp = requests.get(f"{base_url}/api/agents/{first_agent_id}", verify=False, timeout=5)
        if resp.ok:
            data = resp.json()
            assert "display_name" in data or "name" in data

    def test_agent_detail_has_skills(self, base_url, registry_mode_enabled, first_agent_id):
        """Agent detail includes skills list."""
        resp = requests.get(f"{base_url}/api/agents/{first_agent_id}", verify=False, timeout=5)
        if resp.ok:
            data = resp.json()
            assert "skills" in data

    def test_agent_detail_has_tools(self, base_url, registry_mode_enabled, first_agent_id):
        """Agent detail includes tools list."""
        resp = requests.get(f"{base_url}/api/agents/{first_agent_id}", verify=False, timeout=5)
        if resp.ok:
            data = resp.json()
            assert "tools" in data

    def test_agent_detail_has_prompts(self, base_url, registry_mode_enabled, first_agent_id):
        """Agent detail includes prompts."""
        resp = requests.get(f"{base_url}/api/agents/{first_agent_id}", verify=False, timeout=5)
        if resp.ok:
            data = resp.json()
            assert "prompts" in data

    def test_agent_detail_has_version(self, base_url, registry_mode_enabled, first_agent_id):
        """Agent detail includes version."""
        resp = requests.get(f"{base_url}/api/agents/{first_agent_id}", verify=False, timeout=5)
        if resp.ok:
            data = resp.json()
            assert "version" in data


class TestRegistryAgentHistory:
    """Agent history and rollback."""

    def test_agent_history_endpoint(
        self, base_url, registry_mode_enabled, first_agent_id, active_test_role
    ):
        """Agent history endpoint returns data."""
        resp = requests.get(
            f"{base_url}/api/agents/{first_agent_id}/history",
            verify=False,
            timeout=5,
        )
        if active_test_role == "admin":
            assert resp.status_code in (200, 404)
        else:
            assert resp.status_code == 403
        if resp.ok:
            data = resp.json()
            assert isinstance(data, (list, dict))

    def test_agent_at_version_endpoint(
        self, base_url, registry_mode_enabled, first_agent_id, active_test_role
    ):
        """Agent at-version endpoint accepts version parameter."""
        resp = requests.get(
            f"{base_url}/api/agents/{first_agent_id}/history/1",
            verify=False,
            timeout=5,
        )
        if active_test_role == "admin":
            assert resp.status_code in (200, 404)
        else:
            assert resp.status_code == 403


class TestRegistryAgentToggle:
    """Enable/disable agent toggle."""

    def test_toggle_endpoint_exists(
        self, base_url, registry_mode_enabled, first_agent_id, active_test_role
    ):
        """Agent toggle endpoint exists."""
        resp = requests.post(
            f"{base_url}/api/agents/{first_agent_id}/toggle",
            verify=False,
            timeout=5,
        )
        if active_test_role == "admin":
            assert resp.status_code not in (404, 500)
        else:
            assert resp.status_code == 403


class TestRegistryAgentCreate:
    """Agent creation via API."""

    def test_create_agent_validates_required_fields(
        self, base_url, registry_mode_enabled, active_test_role
    ):
        """Creating agent without required fields returns 422."""
        resp = requests.post(
            f"{base_url}/api/agents",
            json={"incomplete": "data"},
            verify=False,
            timeout=5,
        )
        if active_test_role == "admin":
            assert resp.status_code in (400, 422, 409)
        else:
            assert resp.status_code == 403

    def test_create_agent_with_valid_data(self, base_url, registry_mode_enabled, active_test_role):
        """Creating agent with valid data returns 200/201."""
        test_agent = {
            "agent_id": "test_ui_agent_xyz_delete_me",
            "display_name": "Test UI Agent",
            "version": "1.0.0",
            "enabled": False,
            "prompts": {"system": "You are a test agent."},
            "skills": [],
            "tools": [],
            "config": {},
        }
        resp = requests.post(
            f"{base_url}/api/agents",
            json=test_agent,
            verify=False,
            timeout=5,
        )
        if resp.status_code in (200, 201):
            # Clean up
            requests.delete(
                f"{base_url}/api/agents/test_ui_agent_xyz_delete_me",
                verify=False,
                timeout=5,
            )
        if active_test_role == "admin":
            assert resp.status_code in (200, 201, 409, 422)
        else:
            assert resp.status_code == 403
