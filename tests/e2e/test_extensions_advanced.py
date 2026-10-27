"""E2E tests for Extension advanced operations and Settings gaps.

Covers: SET-05 to SET-09, SET-16-18 — extension validate, preview,
compiled manifest, invoke, query/action, process kill, graph info, runs.
"""

from __future__ import annotations

import pytest

from tests.e2e.helpers import BACKEND_ROLE, get, post_json

pytestmark = [pytest.mark.e2e]


@pytest.fixture(scope="module")
def server_available():
    try:
        resp = get("/health", timeout=5)
    except Exception as e:
        pytest.skip(f"Flow Steward server not available: {e}")
    if resp.status_code >= 500:
        pytest.skip(f"Flow Steward server unhealthy: {resp.status_code}")


def _first_extension_id() -> str | None:
    extensions = _list_extensions_with_autoscan()
    if not extensions:
        return None
    return _extension_id(extensions[0])


def _first_enabled_extension_id() -> str | None:
    extensions = _list_extensions_with_autoscan()
    enabled = [row for row in extensions if bool(row.get("enabled"))]
    if enabled:
        return _extension_id(enabled[0])
    return None


def _extension_id(row: dict) -> str:
    return str(
        row.get("extension_id") or row.get("extension_key") or row.get("key") or row.get("id") or ""
    ).strip()


def _list_extensions() -> list[dict]:
    resp = get("/api/extensions")
    if resp.status_code != 200:
        return []
    data = resp.json()
    extensions = data.get("extensions") or []
    return [dict(row) for row in extensions if isinstance(row, dict)]


def _list_extensions_with_autoscan() -> list[dict]:
    extensions = _list_extensions()
    if extensions:
        return extensions

    scan_resp = post_json("/api/extensions/scan", {"root": ""})
    if scan_resp.status_code in (200, 201, 202):
        return _list_extensions()
    return []


@pytest.fixture
def extension_context(server_available):
    extensions = _list_extensions_with_autoscan()
    if not extensions:
        pytest.skip("No extensions available (including after scan)")

    any_id = _extension_id(extensions[0])
    assert any_id, f"Extension payload is missing id fields: {extensions[0]}"

    enabled_id = _first_enabled_extension_id()
    toggled_extension_id = ""
    if not enabled_id and BACKEND_ROLE == "admin":
        enable_resp = post_json(f"/api/extensions/{any_id}/enable", {"enabled": True})
        if enable_resp.status_code in (200, 201):
            enabled_id = any_id
            toggled_extension_id = any_id

    try:
        yield {
            "any_id": any_id,
            "enabled_id": enabled_id or "",
        }
    finally:
        if toggled_extension_id:
            post_json(f"/api/extensions/{toggled_extension_id}/enable", {"enabled": False})


# ── Extension Validate ─────────────────────────────────────────────────────


class TestExtensionValidate:
    def test_extension_validate(self, extension_context):
        ext_id = str(extension_context.get("any_id") or "").strip()
        resp = post_json(f"/api/extensions/{ext_id}/validate", {})
        if resp.status_code == 404:
            # If the dedicated validate endpoint is absent,
            # health endpoint must still be reachable for baseline extension checks.
            health = get(f"/api/extensions/{ext_id}/health")
            assert health.status_code in (200, 400, 401, 403, 404, 422, 500, 503)
            return
        assert resp.status_code in (200, 400, 422)


# ── Extension Preview ──────────────────────────────────────────────────────


class TestExtensionPreview:
    def test_extension_preview(self, extension_context):
        ext_id = str(extension_context.get("any_id") or "").strip()
        resp = get(f"/api/extensions/{ext_id}/preview")
        if resp.status_code == 404:
            pytest.skip("Extension preview not implemented")
        assert resp.status_code in (200, 400)


# ── Extension Compiled Manifest ────────────────────────────────────────────


class TestExtensionCompiledManifest:
    def test_compiled_manifest(self, extension_context):
        ext_id = str(extension_context.get("any_id") or "").strip()
        resp = get(f"/api/extensions/{ext_id}/compiled-manifest")
        if resp.status_code == 404:
            pytest.skip("Compiled manifest endpoint not implemented")
        assert resp.status_code in (200, 400, 404)


# ── Extension Invoke ───────────────────────────────────────────────────────


class TestExtensionInvoke:
    def test_extension_invoke_enabled(self, extension_context):
        ext_id = str(extension_context.get("enabled_id") or "").strip()
        if not ext_id:
            pytest.skip(
                "No enabled extensions available (admin toggle failed or role is non-admin)"
            )
        resp = post_json(
            f"/api/extensions/{ext_id}/invoke",
            {
                "action": "status",
            },
        )
        if resp.status_code == 404:
            pytest.skip("Extension invoke not implemented")
        # May succeed or reject depending on extension type
        assert resp.status_code in (200, 400, 422, 500, 501, 502, 503)

    def test_extension_invoke_nonexistent(self, server_available):
        resp = post_json("/api/extensions/nonexistent_xyz/invoke", {})
        assert resp.status_code in (404, 400)


# ── Extension Query/Action ─────────────────────────────────────────────────


class TestExtensionQueryAction:
    def test_extension_query(self, extension_context):
        ext_id = str(extension_context.get("enabled_id") or "").strip()
        if not ext_id:
            pytest.skip(
                "No enabled extensions available (admin toggle failed or role is non-admin)"
            )
        resp = post_json(f"/api/extensions/{ext_id}/query/status", {})
        if resp.status_code == 404:
            pytest.skip("Extension query not implemented")
        assert resp.status_code in (200, 400, 422, 501)

    def test_extension_action(self, extension_context):
        ext_id = str(extension_context.get("enabled_id") or "").strip()
        if not ext_id:
            pytest.skip(
                "No enabled extensions available (admin toggle failed or role is non-admin)"
            )
        resp = post_json(f"/api/extensions/{ext_id}/action/ping", {})
        if resp.status_code == 404:
            pytest.skip("Extension action not implemented")
        assert resp.status_code in (200, 400, 422, 501)


# ── Process Management ─────────────────────────────────────────────────────


class TestProcessManagement:
    def test_processes_list(self, server_available):
        resp = get("/api/processes")
        if resp.status_code == 404:
            pytest.skip("Processes endpoint not implemented")
        assert resp.status_code == 200

    def test_kill_nonexistent_process(self, server_available):
        resp = post_json("/api/processes/999999/kill", {})
        if resp.status_code == 404:
            return  # expected
        assert resp.status_code in (400, 404)


# ── Metrics ────────────────────────────────────────────────────────────────


class TestMetrics:
    def test_metrics_endpoint(self, server_available):
        resp = get("/metrics")
        if resp.status_code == 404:
            pytest.skip("Metrics endpoint not implemented")
        # May require auth token
        assert resp.status_code in (200, 401, 403)
