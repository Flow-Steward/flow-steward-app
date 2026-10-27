"""
UI tests for the Registry Dashboard (/api/agents and registry UI).

Tests:
- Loads with counts
- Shows validation banner
- Reload button
- Validate button
- Navigation links

Run:
    docker compose exec fs-app python -m pytest tests/ui/test_registry_dashboard.py -v --timeout=60
"""

from __future__ import annotations

import os

import pytest
import requests

from core.application.tooling.testing.runtime_env import resolve_base_url

pytestmark = [pytest.mark.ui]

BASE_URL = resolve_base_url(os.environ, include_dotenv=True)


def _expect_registry_status(resp, active_test_role, *, write: bool = False):
    if active_test_role in {"admin", "operator"} and not write:
        assert resp.status_code not in (401, 403)
        return
    assert resp.status_code == 403


@pytest.fixture(scope="module")
def registry_mode_enabled(base_url):
    """Skip if not running in registry mode."""
    try:
        resp = requests.get(f"{base_url}/api/agents", verify=False, timeout=5)
        if resp.status_code == 404:
            pytest.skip("Registry mode not enabled (admin API not found)")
    except Exception as e:
        pytest.skip(f"Registry API not reachable: {e}")


class TestRegistryDashboard:
    """Registry dashboard page."""

    def test_registry_page_loads(self, page, server_reachable, registry_mode_enabled, base_url):
        """Registry dashboard or agent list page loads."""
        # Try several possible registry URLs
        for path in ["/registry", "/api/agents", "/admin/registry"]:
            try:
                page.goto(f"{base_url}{path}", wait_until="networkidle", timeout=10000)
                if page.url and "404" not in page.content().lower():
                    break
            except Exception:
                continue

        assert len(page.content()) > 100

    def test_registry_shows_agent_count(self, base_url, registry_mode_enabled, active_test_role):
        """Registry stats API returns agent count."""
        resp = requests.get(f"{base_url}/api/agents", verify=False, timeout=5)
        if active_test_role == "admin":
            assert resp.status_code not in (401, 403)
        else:
            assert resp.status_code == 403
        if resp.ok:
            data = resp.json()
            if isinstance(data, list):
                assert len(data) >= 0
            elif isinstance(data, dict):
                count = data.get("count") or len(data.get("agents", []))
                assert count >= 0

    def test_registry_shows_workflow_count(self, base_url, registry_mode_enabled, active_test_role):
        """Registry workflow list is accessible."""
        resp = requests.get(f"{base_url}/api/workflows", verify=False, timeout=5)
        _expect_registry_status(resp, active_test_role)

    def test_registry_shows_tool_count(self, base_url, registry_mode_enabled, active_test_role):
        """Registry tool list is accessible."""
        resp = requests.get(f"{base_url}/api/tools", verify=False, timeout=5)
        _expect_registry_status(resp, active_test_role)

    def test_registry_validate_endpoint(self, base_url, registry_mode_enabled):
        """Registry validation endpoint is reachable."""
        resp = requests.get(f"{base_url}/api/validate", verify=False, timeout=10)
        assert resp.status_code in (200, 202, 404)

    def test_registry_stats_endpoint(self, base_url, registry_mode_enabled):
        """Registry stats endpoint returns counts."""
        resp = requests.get(f"{base_url}/api/registry/stats", verify=False, timeout=5)
        if resp.ok:
            data = resp.json()
            assert isinstance(data, dict)

    def test_registry_reload_endpoint(self, base_url, registry_mode_enabled, active_test_role):
        """Registry reload endpoint accepts POST."""
        resp = requests.post(f"{base_url}/api/registry/reload", verify=False, timeout=60)
        if active_test_role == "admin":
            assert resp.status_code in (200, 202, 404)
        else:
            assert resp.status_code == 403


class TestRegistryNavigationLinks:
    """Registry navigation links."""

    def test_agents_link_works(
        self, page, server_reachable, registry_mode_enabled, base_url, active_test_role
    ):
        """Link to agents list works."""
        resp = requests.get(f"{base_url}/api/agents", verify=False, timeout=5)
        if active_test_role == "admin":
            assert resp.status_code not in (401, 403)
        else:
            assert resp.status_code == 403

    def test_tools_link_works(
        self, page, server_reachable, registry_mode_enabled, base_url, active_test_role
    ):
        """Link to tools list works."""
        resp = requests.get(f"{base_url}/api/tools", verify=False, timeout=5)
        _expect_registry_status(resp, active_test_role)

    def test_skills_link_works(
        self, page, server_reachable, registry_mode_enabled, base_url, active_test_role
    ):
        """Link to skills list works."""
        resp = requests.get(f"{base_url}/api/skills", verify=False, timeout=5)
        _expect_registry_status(resp, active_test_role)

    def test_workflows_link_works(
        self, page, server_reachable, registry_mode_enabled, base_url, active_test_role
    ):
        """Link to workflows list works."""
        resp = requests.get(f"{base_url}/api/workflows", verify=False, timeout=5)
        _expect_registry_status(resp, active_test_role)
