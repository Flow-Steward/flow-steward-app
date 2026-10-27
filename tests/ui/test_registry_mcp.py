from __future__ import annotations

import os

import pytest
import requests

from core.application.tooling.testing.runtime_env import resolve_base_url

pytestmark = [pytest.mark.ui]

BASE_URL = resolve_base_url(os.environ, include_dotenv=True)


def _is_login_page(content: str) -> bool:
    body = str(content or "").lower()
    return "login" in body and ("password" in body or "sign in" in body or "username" in body)


def _is_spa_shell(content: str) -> bool:
    body = str(content or "").lower()
    return "<div id=\"root\"></div>" in body and "/app/assets/index" in body


class TestRegistryMcpPage:
    def test_registry_mcp_page_loads(self, page, server_reachable, base_url):
        resp = requests.get(f"{base_url}/registry/mcp", verify=False, timeout=10, allow_redirects=False)
        assert resp.status_code in (200, 302, 401)
        if resp.status_code == 302:
            assert "/login" in str(resp.headers.get("location") or "")
        elif resp.status_code == 401:
            assert "authentication required" in resp.text.lower()
        else:
            body = resp.text.lower()
            assert "mcp" in body or _is_login_page(body) or _is_spa_shell(body)

    def test_registry_mcp_page_resolves_in_shell(self, page, server_reachable, base_url):
        page.goto(f"{base_url}/registry/mcp", wait_until="networkidle", timeout=20_000)
        content = page.content().lower()
        if _is_login_page(content):
            assert "login" in content
            return
        assert "this react route is not mapped" not in content
        assert "mcp" in content

    def test_settings_connectors_route_is_not_a_valid_surface(self, page, server_reachable, base_url):
        resp = requests.get(
            f"{base_url}/settings/connectors",
            verify=False,
            timeout=10,
            allow_redirects=False,
        )
        assert resp.status_code in (401, 404)
        if resp.status_code == 401:
            assert "authentication required" in resp.text.lower()
        else:
            assert "not found" in resp.text.lower()
