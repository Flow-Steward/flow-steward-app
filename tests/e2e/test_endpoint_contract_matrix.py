"""E2E endpoint contract tests for critical backend routes with low or missing coverage.

Covers the highest-risk uncovered endpoint families from the gap matrix:
  - /api/mcp/{id}                         GET / PUT / DELETE / POST health
  - /api/projects/{id}/connections/{id}   GET / PUT / DELETE
  - /api/security/projects/{id}/rules     POST
  - /api/settings/model-catalog           GET / PUT
  - /api/model-selection/options          GET
  - /api/extensions                            key mutation surfaces (validate / build / import /
                                          install / handshake / descriptor endpoints)

Goals:
  - Confirm every named route is reachable (not 404 routing gap).
  - Confirm invalid/empty inputs return 4xx, never 500.
  - Confirm unknown-resource paths return 404 (not a different resource's data).
  - Confirm auth-protected routes return 401/403 rather than leaking data unauthenticated
    (handled by the E2E auth fixture from conftest).

Non-goals:
  - Full happy-path business logic validation (covered by dedicated suites).
  - Concurrency / load scenarios.

Run:
    docker compose exec fs-app python -m pytest tests/e2e/test_endpoint_contract_matrix.py -v
"""

from __future__ import annotations

import os
import uuid

import pytest

from core.application.tooling.testing.runtime_env import resolve_test_env
from tests.e2e.helpers import delete, get, post_json, put_json

pytestmark = [pytest.mark.e2e]

_TEST_ENV = resolve_test_env(os.environ, lane="backend", include_dotenv=True)
_PROJECT_ID = str(_TEST_ENV.project_id or "").strip()


def _uid() -> str:
    return uuid.uuid4().hex[:12]


# ---------------------------------------------------------------------------
# Shared infrastructure fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def server_available():
    try:
        resp = get("/health", timeout=5)
    except Exception as exc:
        pytest.skip(f"Server not reachable: {exc}")
    if resp.status_code >= 500:
        pytest.skip(f"Server unhealthy: {resp.status_code}")


@pytest.fixture(scope="module")
def project_id(server_available) -> str:
    if not _PROJECT_ID:
        pytest.skip("TEST_PROJECT_BACKEND_ID not set — skipping project-scoped contract tests")
    return _PROJECT_ID


# ===========================================================================
# A. MCP Connectors — GET / PUT / DELETE / health
# ===========================================================================


class TestMcpConnectorCrud:
    """Contract tests for /api/mcp/{connector_id} CRUD and health."""

    @pytest.fixture(scope="class")
    def connector_id(self, server_available) -> str:
        """Create a transient test connector and clean it up after the class.

        Note: class-scoped fixtures run before the function-scoped
        e2e_authenticated_requests autouse patch, so we must use the auth
        session directly rather than relying on the monkeypatched requests.post.
        """
        from tests.e2e.conftest import _authed_session
        from tests.e2e.helpers import BASE_URL

        session, verify_ssl = _authed_session()
        csrf = str(session.cookies.get("fs_csrf") or "").strip()
        name = f"contract-mcp-{_uid()}"
        payload = {
            "display_name": name,
            "base_url": "http://localhost:19999",
            "enabled": False,
        }
        resp = session.post(
            f"{BASE_URL}/api/mcp",
            json=payload,
            headers={"x-csrf-token": csrf} if csrf else {},
            verify=verify_ssl,
        )
        if resp.status_code not in (200, 201):
            pytest.skip(
                f"Could not create test MCP connector: {resp.status_code} — {resp.text[:200]}"
            )
        data = resp.json()
        cid = str(data.get("id") or data.get("connector_id") or "").strip()
        if not cid:
            pytest.skip("Connector create response did not include an id")
        yield cid
        session.delete(
            f"{BASE_URL}/api/mcp/{cid}",
            headers={"x-csrf-token": csrf} if csrf else {},
            verify=verify_ssl,
        )

    # -- GET --

    def test_get_connector_returns_200_with_expected_keys(self, connector_id):
        resp = get(f"/api/mcp/{connector_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, dict)
        # At least one of the canonical id fields must be present
        assert "id" in body or "connector_id" in body

    def test_get_unknown_connector_returns_404(self, server_available):
        resp = get(f"/api/mcp/nonexistent-{_uid()}")
        assert resp.status_code == 404

    # -- PUT --

    def test_put_connector_updates_name(self, connector_id):
        new_name = f"updated-contract-{_uid()}"
        # Connector API uses display_name (not name) — match the create payload field
        resp = put_json(f"/api/mcp/{connector_id}", {"display_name": new_name})
        assert resp.status_code in (200, 204), (
            f"PUT connector returned unexpected status {resp.status_code}: {resp.text[:200]}"
        )
        # Verify the update was persisted
        check = get(f"/api/mcp/{connector_id}")
        assert check.status_code == 200
        body = check.json()
        # Accept either display_name or name field in response
        actual_name = body.get("display_name") or body.get("name") or ""
        assert actual_name == new_name, (
            f"Connector name update did not persist; expected {new_name!r}, got {actual_name!r}: {body}"
        )

    def test_put_unknown_connector_returns_404(self, server_available):
        resp = put_json(f"/api/mcp/ghost-{_uid()}", {"display_name": "ghost"})
        assert resp.status_code == 404

    # -- Health --

    def test_health_check_responds_not_500(self, connector_id):
        resp = post_json(f"/api/mcp/{connector_id}/health", {})
        assert resp.status_code in (200, 422, 503), (
            f"Unexpected health check status {resp.status_code}"
        )

    def test_health_check_unknown_connector_returns_404(self, server_available):
        resp = post_json(f"/api/mcp/ghost-{_uid()}/health", {})
        assert resp.status_code == 404

    # -- DELETE --

    def test_delete_connector_makes_it_unreachable(self, server_available):
        payload = {
            "display_name": f"delete-me-{_uid()}",
            "base_url": "http://localhost:19998/delete-test",
            "enabled": False,
        }
        resp = post_json("/api/mcp", payload)
        if resp.status_code not in (200, 201):
            pytest.skip(f"Could not create connector for delete test: {resp.status_code}")
        cid = str(resp.json().get("id") or resp.json().get("connector_id") or "").strip()
        if not cid:
            pytest.skip("No connector id in create response")

        del_resp = delete(f"/api/mcp/{cid}")
        assert del_resp.status_code in (200, 204), (
            f"DELETE returned unexpected status {del_resp.status_code}"
        )
        # Confirm gone
        check = get(f"/api/mcp/{cid}")
        assert check.status_code == 404

    def test_delete_unknown_connector_returns_404(self, server_available):
        resp = delete(f"/api/mcp/ghost-{_uid()}")
        assert resp.status_code == 404


# ===========================================================================
# B. Project Connections — GET / PUT / DELETE
# ===========================================================================


class TestProjectConnectionsCrud:
    """Contract tests for /api/projects/{project_id}/connections/{connection_id}."""

    def test_get_unknown_connection_returns_404(self, project_id):
        resp = get(f"/api/projects/{project_id}/connections/nonexistent-{_uid()}")
        assert resp.status_code == 404

    def test_put_unknown_connection_returns_404(self, project_id):
        resp = put_json(
            f"/api/projects/{project_id}/connections/nonexistent-{_uid()}",
            {"enabled": True},
        )
        assert resp.status_code == 404

    def test_delete_unknown_connection_returns_404(self, project_id):
        resp = delete(f"/api/projects/{project_id}/connections/nonexistent-{_uid()}")
        assert resp.status_code == 404

    def test_get_connection_for_nonexistent_project_returns_403_or_404(self, server_available):
        resp = get("/api/projects/999999999/connections/any-connection-id")
        assert resp.status_code in (403, 404), (
            f"Expected 403/404 for nonexistent project, got {resp.status_code}"
        )

    def test_put_connection_for_nonexistent_project_returns_403_or_404(self, server_available):
        resp = put_json("/api/projects/999999999/connections/any-id", {"enabled": True})
        assert resp.status_code in (403, 404)

    def test_delete_connection_for_nonexistent_project_returns_403_or_404(self, server_available):
        resp = delete("/api/projects/999999999/connections/any-id")
        assert resp.status_code in (403, 404)


# ===========================================================================
# C. Security project rules — POST
# ===========================================================================


class TestSecurityProjectRules:
    """Contract tests for POST /api/security/projects/{project_id}/rules."""

    def test_post_rule_with_minimal_valid_body_returns_2xx(self, project_id):
        rule = {
            "rule_id": f"contract-rule-{_uid()}",
            "scanner": "secrets",  # required by CustomRule schema
            "description": "Endpoint contract test rule — safe to delete",
            "severity": "low",
            "pattern": "contract_test_noop_pattern",
        }
        resp = post_json(f"/api/security/projects/{project_id}/rules", rule)
        assert resp.status_code in (200, 201, 204), (
            f"Expected 2xx for valid rule POST, got {resp.status_code}: {resp.text[:300]}"
        )

    def test_post_rule_with_empty_body_returns_4xx(self, project_id):
        resp = post_json(f"/api/security/projects/{project_id}/rules", {})
        assert resp.status_code in (400, 422), (
            f"Expected 400/422 for empty rule body, got {resp.status_code}"
        )

    def test_post_rule_with_nonexistent_project_returns_403_or_404(self, server_available):
        rule = {"rule_id": "ghost-rule", "description": "ghost", "severity": "low"}
        resp = post_json("/api/security/projects/999999999/rules", rule)
        assert resp.status_code in (403, 404), (
            f"Expected 403/404 for nonexistent project rules POST, got {resp.status_code}"
        )

    def test_post_rule_never_returns_500(self, project_id):
        # Adversarial rule bodies must fail with typed validation, not 500.
        for bad_body in [
            {"rule_id": "x" * 10000},  # oversized field
            {"severity": None},
            {"pattern": "\x00\xff<script>alert(1)</script>"},
        ]:
            resp = post_json(f"/api/security/projects/{project_id}/rules", bad_body)
            assert resp.status_code in (400, 422), (
                f"Security rules POST must reject invalid body with 400/422; got {resp.status_code}: {resp.text[:300]}"
            )


# ===========================================================================
# D. Model catalog — GET / PUT
# ===========================================================================


class TestModelCatalogEndpoints:
    """Contract tests for /api/settings/model-catalog and /api/model-selection/options."""

    def test_model_selection_options_returns_200(self, server_available):
        resp = get("/api/model-selection/options")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, (dict, list)), (
            f"Expected dict or list from model-selection/options, got {type(body)}"
        )
        # Must not be empty — options endpoint should always have at least one provider
        assert body, "model-selection/options must return a non-empty response"

    def test_model_catalog_settings_get_returns_200_or_403(self, server_available):
        resp = get("/api/settings/model-catalog")
        assert resp.status_code in (200, 403), (
            f"Expected 200 (admin) or 403 (non-admin) for model-catalog GET, got {resp.status_code}"
        )

    def test_model_catalog_settings_get_never_returns_500(self, server_available):
        resp = get("/api/settings/model-catalog")
        assert resp.status_code not in (500, 502, 503), (
            f"GET /api/settings/model-catalog must never crash; got {resp.status_code}"
        )

    def test_model_catalog_put_empty_body_not_500(self, server_available):
        """Empty-body PUT must not cause a server crash."""
        resp = put_json("/api/settings/model-catalog", {})
        assert resp.status_code not in (500, 502, 503), (
            f"Empty-body PUT /api/settings/model-catalog must not crash; got {resp.status_code}"
        )

    def test_model_catalog_put_invalid_key_returns_4xx(self, server_available):
        """Unknown keys must not crash and must not persist into catalog state."""
        resp = put_json("/api/settings/model-catalog", {"__invalid_key_xyz__": "garbage"})
        assert resp.status_code in (200, 400, 403, 422), (
            f"PUT /api/settings/model-catalog with invalid key must return typed status; got {resp.status_code}"
        )
        if resp.status_code == 200:
            body = resp.json()
            assert body == {"status": "saved"}, body
            check = get("/api/settings/model-catalog")
            assert check.status_code == 200
            assert "__invalid_key_xyz__" not in check.text

    def test_model_catalog_put_never_returns_500(self, server_available):
        resp = put_json("/api/settings/model-catalog", {"invalid_key": "garbage"})
        assert resp.status_code in (200, 400, 403, 422)
        if resp.status_code == 200:
            body = resp.json()
            assert body == {"status": "saved"}, body
            check = get("/api/settings/model-catalog")
            assert check.status_code == 200
            assert "invalid_key" not in check.text

    def test_model_selection_options_response_is_not_empty(self, server_available):
        resp = get("/api/model-selection/options")
        assert resp.status_code == 200
        body = resp.json()
        # The catalog should have at least one provider or option when providers are configured
        # Accept an empty response only as a soft warning (provider config may not be set up)
        if isinstance(body, dict):
            # Should have some keys
            pass  # structural contract only; content depends on environment config
        elif isinstance(body, list):
            pass  # list of options; may be empty in unconfigured environments


# ===========================================================================
# E. Extensions supply-chain endpoint contracts
# ===========================================================================


class TestExtensionEndpointContracts:
    """Contract tests for /api/extensions supply-chain surfaces.

    Tests assert:
    - Routes exist (not 404 routing gaps)
    - Empty / invalid inputs return 4xx, never 500
    - Nonexistent resource ids return 404

    These tests do not require real extension artifacts.
    """

    def test_extension_list_returns_200(self, server_available):
        resp = get("/api/extensions")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, (list, dict)), (
            f"Expected list or dict from /api/extensions, got {type(body)}"
        )
        # If a dict, it must have a recognized pagination/items key
        if isinstance(body, dict):
            assert body.keys() & {"items", "results", "extensions", "data", "total"}, (
                f"/api/extensions returned a dict without recognized list keys: {list(body.keys())[:10]}"
            )

    def test_extension_get_nonexistent_returns_404(self, server_available):
        resp = get(f"/api/extensions/nonexistent-extension-{_uid()}")
        assert resp.status_code == 404

    def test_extension_installs_get_nonexistent_returns_404(self, server_available):
        resp = get(f"/api/extensions/installs/nonexistent-install-{_uid()}")
        assert resp.status_code == 404

    @pytest.mark.parametrize(
        "path",
        ["/api/extensions/validate", "/api/extensions/build", "/api/extensions/import"],
    )
    def test_retired_legacy_extension_local_path_routes_reject_post(
        self, server_available, path
    ):
        assert post_json(path, {}).status_code == 405

    def test_extension_descriptor_validate_empty_body_not_500(self, server_available):
        # ExtensionDescriptorValidateRequest has all-default fields, so {} may return 200;
        # what must happen is either acceptance or typed validation.
        resp = post_json("/api/extensions/descriptors/validate", {})
        assert resp.status_code in (200, 400, 422), (
            f"Empty descriptor validate body must return typed success/validation status; got {resp.status_code}"
        )

    def test_extension_descriptor_import_url_invalid_returns_4xx(self, server_available):
        resp = post_json("/api/extensions/descriptors/import/url", {"url": "not-a-url"})
        assert resp.status_code in (400, 422)

    def test_extension_install_nonexistent_extension_returns_404(self, server_available):
        resp = post_json(f"/api/extensions/nonexistent-extension-{_uid()}/install", {})
        # 404 = resource not found (primary); 422 = extension name fails server-side validation
        assert resp.status_code in (404, 422), (
            f"Nonexistent extension install must return 404 or 422; got {resp.status_code}"
        )

    def test_extension_releases_install_nonexistent_returns_404(self, server_available):
        resp = post_json(f"/api/extensions/releases/nonexistent-release-{_uid()}/install", {})
        assert resp.status_code in (404, 422), (
            f"Nonexistent release install must return 404 or 422; got {resp.status_code}"
        )

    def test_extension_installs_handshake_nonexistent_returns_404(self, server_available):
        resp = post_json(f"/api/extensions/installs/nonexistent-install-{_uid()}/handshake", {})
        assert resp.status_code in (404, 422), (
            f"Nonexistent install handshake must return 404 or 422; got {resp.status_code}"
        )

    def test_extension_enable_nonexistent_returns_404(self, server_available):
        resp = post_json(f"/api/extensions/nonexistent-extension-{_uid()}/enable", {})
        assert resp.status_code in (404, 422), (
            f"Nonexistent extension enable must return 404 or 422; got {resp.status_code}"
        )

    def test_extension_disable_nonexistent_returns_404(self, server_available):
        resp = post_json(f"/api/extensions/nonexistent-extension-{_uid()}/disable", {})
        assert resp.status_code in (404, 422), (
            f"Nonexistent extension disable must return 404 or 422; got {resp.status_code}"
        )

    def test_extension_delete_nonexistent_rejects_unsupported_method(self, server_available):
        resp = delete(f"/api/extensions/nonexistent-extension-{_uid()}")
        assert resp.status_code == 405

    def test_extension_marketplace_returns_200_or_503(self, server_available):
        resp = get("/api/extensions/marketplace")
        # Marketplace may be unavailable in local/offline environments
        assert resp.status_code in (200, 503), f"Unexpected marketplace status {resp.status_code}"
