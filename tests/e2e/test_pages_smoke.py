from __future__ import annotations

import os

import pytest
import requests

from core.application.tooling.testing.runtime_env import resolve_base_url, resolve_test_env
from tests.e2e.conftest import e2e_request

pytestmark = [pytest.mark.e2e]

BASE_URL = resolve_base_url(os.environ, include_dotenv=True)
TEST_ENV = resolve_test_env(os.environ, lane="backend", include_dotenv=True)
PROJECT_ID = str(TEST_ENV.project_id or "").strip()
ACTIVE_ROLE = str(os.getenv("E2E_BACKEND_ROLE", "admin") or "admin").strip().lower() or "admin"
VERIFY_SSL = str(os.getenv("E2E_VERIFY_SSL", "0") or "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def _get(path: str) -> requests.Response:
    return e2e_request("GET", f"{BASE_URL}{path}", timeout=20)


def _assert_html_ok(resp: requests.Response, markers: list[str]) -> None:
    assert resp.status_code == 200, f"expected 200 for {resp.url}, got {resp.status_code}"
    text = resp.text
    lowered = text.lower()
    assert "<html" in lowered
    is_react_shell = '<div id="root"></div>' in lowered and "/app/assets/index" in lowered
    if is_react_shell:
        assert 'type="module"' in lowered
        assert "/app/assets/index" in lowered
    else:
        for marker in markers:
            assert marker.lower() in lowered, f"missing marker {marker!r} in {resp.url}"


def _assert_login_redirect(resp: requests.Response) -> None:
    assert resp.status_code == 200, (
        f"expected 200 for redirected login page, got {resp.status_code}"
    )
    lowered = resp.text.lower()
    assert "<html" in lowered
    assert resp.url.rstrip("/").endswith("/login"), f"expected login redirect, got {resp.url}"
    assert "login" in lowered, f"expected login page markers in {resp.url}"


@pytest.fixture(scope="module")
def server_available():
    try:
        resp = requests.get(f"{BASE_URL}/health", verify=VERIFY_SSL, timeout=5)
    except Exception as e:
        pytest.skip(f"Flow Steward server not available at {BASE_URL}: {e}")
    if resp.status_code >= 500:
        pytest.skip(f"Flow Steward server health endpoint unhealthy: {resp.status_code}")


class TestAuthenticatedPageSmoke:
    def test_health_smoke_includes_build_version(self, server_available):
        resp = _get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert isinstance(body.get("build_version"), str)
        assert body["build_version"].strip()

    @pytest.mark.parametrize(
        ("path", "markers", "allowed_roles"),
        [
            ("/dashboard", ["dashboard", "what needs attention"], {"admin", "operator", "viewer"}),
            ("/projects", ["projects", "new project"], {"admin", "operator", "viewer"}),
            ("/jobs", ["jobs", "refresh"], {"admin", "operator", "viewer"}),
            (
                "/monitoring?tab=processes",
                ["active processes", "worker subprocesses"],
                {"admin", "operator"},
            ),
            ("/settings", ["settings", "providers"], {"admin"}),
            ("/registry", ["registry", "agents"], {"admin", "operator", "viewer"}),
        ],
    )
    def test_core_pages_load(
        self,
        server_available,
        path: str,
        markers: list[str],
        allowed_roles: set[str],
    ):
        resp = _get(path)
        if ACTIVE_ROLE not in allowed_roles:
            _assert_login_redirect(resp)
            return
        _assert_html_ok(resp, markers)

    def test_project_detail_pages_load(self, server_available):
        if not PROJECT_ID:
            pytest.skip("TEST_PROJECT_BACKEND_ID not set")

        project_detail_markers = ["project"]
        if ACTIVE_ROLE in {"admin", "operator"}:
            project_detail_markers.append("submit task")

        checks = [
            (f"/projects/{PROJECT_ID}", project_detail_markers, {"admin", "operator", "viewer"}),
        ]
        for path, markers, allowed_roles in checks:
            resp = _get(path)
            if ACTIVE_ROLE not in allowed_roles:
                _assert_login_redirect(resp)
                continue
            _assert_html_ok(resp, markers)
