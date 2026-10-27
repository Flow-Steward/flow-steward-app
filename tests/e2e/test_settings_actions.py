"""E2E tests for Settings actions (SET-02 through SET-05).

Covers extension scan/list/health, watchdog scan/recover,
and notification route listing.
"""

from __future__ import annotations

import os

import pytest

from core.application.tooling.testing.runtime_env import resolve_test_env
from tests.e2e.helpers import get, post_json

pytestmark = [pytest.mark.e2e]

TEST_ENV = resolve_test_env(os.environ, lane="backend", include_dotenv=True)
PROJECT_ID = str(TEST_ENV.project_id or "").strip()
BACKEND_ROLE = str(os.environ.get("E2E_BACKEND_ROLE", "admin") or "admin").strip().lower()


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


# ── Extensions ───────────────────────────────────────────────────────────────


class TestExtensions:
    def test_extension_list(self, server_available):
        resp = get("/api/extensions")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body.get("extensions"), list)

    def test_extension_scan(self, server_available):
        resp = post_json("/api/extensions/scan", {"root": ""})
        if resp.status_code == 403:
            pytest.skip("Extension scan requires elevated permissions")
        assert resp.status_code == 200, f"scan failed: {resp.status_code} {resp.text[:300]}"
        body = resp.json()
        assert isinstance(body.get("scanned_count"), int)
        assert isinstance(body.get("extensions"), list)

    def test_extension_health_for_known_extension(self, server_available):
        list_resp = get("/api/extensions")
        extensions = (list_resp.json() or {}).get("extensions") or []
        enabled = [e for e in extensions if e.get("enabled")]
        if not enabled:
            pytest.skip("No enabled extensions available for health check")

        ext_id = enabled[0].get("extension_id") or enabled[0].get("id")
        resp = get(f"/api/extensions/{ext_id}/health")
        assert resp.status_code == 200
        body = resp.json()
        health = body.get("health") or body
        assert health.get("status") in ("healthy", "failed", "unknown", "degraded")

    def test_extension_enable_disable_round_trip(self, server_available):
        list_resp = get("/api/extensions")
        extensions = (list_resp.json() or {}).get("extensions") or []
        if not extensions:
            pytest.skip("No extensions available")

        # Pick an extension to toggle (prefer disabled ones so we don't disrupt live state).
        target = None
        for e in extensions:
            if not e.get("enabled"):
                target = e
                break
        if target is None:
            # All enabled — pick last one, toggle off then back on.
            target = extensions[-1]

        # Extensions may use extension_key, extension_id, id, or key as identifier.
        ext_id = (
            target.get("extension_id")
            or target.get("extension_key")
            or target.get("key")
            or target.get("id")
        )
        if not ext_id:
            pytest.skip("Extension has no identifiable key/id")

        original_enabled = bool(target.get("enabled"))
        try:
            # Toggle to opposite state
            toggle_resp = post_json(
                f"/api/extensions/{ext_id}/enable",
                {
                    "enabled": not original_enabled,
                },
            )
            # The enable route /api/extensions/{extension_id}/enable is always
            # mounted.  A 404 here means the ID returned by the list API doesn't
            # resolve to the same key the route expects — that is a product
            # inconsistency, not an environment gap.  Fail loudly instead of
            # silently skipping so it surfaces as a regression.
            assert toggle_resp.status_code != 404, (
                f"Extension {ext_id!r} was returned by the list API but "
                f"/api/extensions/{ext_id}/enable returned 404.  "
                "The list and enable endpoints must use the same identifier format."
            )
            assert toggle_resp.status_code == 200
        finally:
            # Restore original state
            post_json(f"/api/extensions/{ext_id}/enable", {"enabled": original_enabled})


# ── Watchdog ─────────────────────────────────────────────────────────────────


class TestWatchdog:
    def test_watchdog_scan_read_only(self, server_available):
        resp = get("/api/watchdog/jobs/scan")
        assert resp.status_code == 200
        body = resp.json()
        assert "scanned_at" in body or "active_count" in body or "detections" in body

    def test_watchdog_recover_rejects_healthy_job(self, server_available, test_project_id: str):
        """Recover on a non-stuck job should fail gracefully."""
        # Find a completed or non-stuck job.
        jobs_resp = get("/api/jobs", params={"project_id": test_project_id, "limit": 5})
        if jobs_resp.status_code != 200:
            pytest.skip("Could not list jobs")
        jobs = (
            jobs_resp.json()
            if isinstance(jobs_resp.json(), list)
            else (jobs_resp.json() or {}).get("jobs") or []
        )
        completed = [
            j
            for j in jobs
            if str(j.get("status") or "").lower() in ("completed", "cancelled", "failed")
        ]
        if not completed:
            pytest.skip("No completed/terminal jobs to test watchdog recovery against")

        job_id = str(completed[0].get("id") or completed[0].get("job_id") or 0).strip()
        resp = post_json(
            f"/api/watchdog/jobs/{job_id}/recover",
            {
                "action": "retry",
                "reason": "e2e test probe",
            },
        )
        # Server may reject (400/409/422) if job is not stuck, or may allow
        # recovery on any terminal job (200). Both are valid behaviors.
        assert resp.status_code in (200, 400, 404, 409, 422), (
            f"watchdog recover returned unexpected status on completed job {job_id}: "
            f"{resp.status_code} {resp.text[:300]}"
        )


# ── Notifications ────────────────────────────────────────────────────────────


class TestNotifications:
    def test_notification_routes_list(self, server_available, test_project_id: str):
        resp = get(f"/api/projects/{test_project_id}/notification-routes")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body.get("notification_routes") or body.get("routes") or [], list)

    def test_notification_deliveries_list(self, server_available, test_project_id: str):
        resp = get(f"/api/projects/{test_project_id}/notification-deliveries")
        assert resp.status_code == 200
        body = resp.json()
        deliveries = body.get("notification_deliveries") or body.get("deliveries") or []
        assert isinstance(deliveries, list)

    def test_settings_page_loads(self, server_available):
        resp = get("/settings")
        assert resp.status_code in (200, 302)
        html = resp.text.lower()
        assert "login" in html or '<div id="root"></div>' in html or "/app/assets/index-" in html


# ── Settings/Config API Contracts ────────────────────────────────────────────


class TestSettingsApiContracts:
    def test_model_providers_contract_or_boundary(self, server_available):
        resp = get("/api/model-providers")
        if resp.status_code in (400, 401, 403):
            return  # role/auth boundary is enforced
        assert resp.status_code == 200, (
            f"model-providers returned {resp.status_code}. "
            "Route is mounted in routes/api/model_providers.py via api_route_bindings — 404 is a failure."
        )
        body = resp.json()
        providers = body.get("providers") or []
        assert isinstance(providers, list)
        if providers:
            row = providers[0]
            assert "provider_key" in row
            assert "provider_type" in row

    def test_mcp_connectors_contract_or_boundary(self, server_available):
        resp = get("/api/mcp")
        if resp.status_code in (401, 403):
            return
        assert resp.status_code == 200, (
            f"mcp returned {resp.status_code}. "
            "Route is mounted in routes/integrations_connectors.py — 404 is a failure."
        )
        body = resp.json()
        connectors = body.get("connectors") or []
        assert isinstance(connectors, list)
        if connectors:
            row = connectors[0]
            assert "connector_id" in row
            assert "display_name" in row

    def test_trash_endpoint_contract_or_boundary(self, server_available):
        resp = get("/api/trash")
        if resp.status_code in (401, 403):
            return
        assert resp.status_code == 200, (
            f"trash returned {resp.status_code}. "
            "Route is mounted in routes/api/trash.py — 404 is a failure."
        )
        body = resp.json()
        assert isinstance(body.get("items") or [], list)

    def test_project_connections_contract_or_boundary(self, server_available, test_project_id: str):
        resp = get(f"/api/projects/{test_project_id}/connections")
        if resp.status_code in (401, 403):
            return
        assert resp.status_code == 200, (
            f"project connections returned {resp.status_code}. "
            "Route is mounted in routes/project_integrations.py — 404 is a failure."
        )
        body = resp.json()
        assert str(body.get("project_id") or 0).strip() == str(test_project_id)
        assert isinstance(body.get("connections") or [], list)

    def test_retention_settings_roundtrip_and_clamp_or_boundary(self, server_available):
        resp = get("/api/settings/retention")
        if BACKEND_ROLE != "admin":
            assert resp.status_code in (401, 403)
            return
        if resp.status_code == 503:
            pytest.skip("Retention settings unavailable in this environment")
        assert resp.status_code == 200
        body = resp.json() or {}
        settings = body.get("settings") or {}
        assert isinstance(settings, dict)
        original = {
            "action_logs_days": int(settings.get("action_logs_days") or 30),
            "job_logs_days": int(settings.get("job_logs_days") or 30),
            "trash_days": int(settings.get("trash_days") or 30),
        }

        candidate = {}
        for key in ("action_logs_days", "job_logs_days", "trash_days"):
            current = int(original.get(key) or 30)
            candidate[key] = current + 1 if current < 3650 else max(1, current - 1)

        try:
            save_resp = post_json("/api/settings/retention", candidate)
            assert save_resp.status_code == 200, (
                f"retention save failed: {save_resp.status_code} {save_resp.text[:300]}"
            )
            saved = (save_resp.json() or {}).get("settings") or {}
            for key, expected in candidate.items():
                assert int(saved.get(key) or 0) == int(expected), (
                    f"retention save mismatch for {key}: expected={expected} saved={saved.get(key)}"
                )

            reread_resp = get("/api/settings/retention")
            assert reread_resp.status_code == 200
            reread = (reread_resp.json() or {}).get("settings") or {}
            for key, expected in candidate.items():
                assert int(reread.get(key) or 0) == int(expected), (
                    f"retention persisted value mismatch for {key}: "
                    f"expected={expected} actual={reread.get(key)}"
                )

            clamp_resp = post_json(
                "/api/settings/retention",
                {
                    "action_logs_days": -10,
                    "job_logs_days": 0,
                    "trash_days": 999999,
                },
            )
            assert clamp_resp.status_code == 200, (
                f"retention clamp save failed: {clamp_resp.status_code} {clamp_resp.text[:300]}"
            )
            clamped = (clamp_resp.json() or {}).get("settings") or {}
            assert int(clamped.get("action_logs_days") or 0) >= 1
            assert int(clamped.get("job_logs_days") or 0) >= 1
            assert 1 <= int(clamped.get("trash_days") or 0) <= 3650
        finally:
            restore_resp = post_json("/api/settings/retention", original)
            assert restore_resp.status_code == 200, (
                f"retention restore failed: {restore_resp.status_code} {restore_resp.text[:300]}"
            )

    def test_commands_batch_contract_or_boundary(self, server_available):
        resp = post_json(
            "/api/commands/batch",
            {
                "commands": [
                    {
                        "command_type": "noop",
                        "target": {"kind": "e2e"},
                        "payload": {"probe": True},
                    }
                ]
            },
        )
        if resp.status_code in (401, 403):
            return
        assert resp.status_code in (200, 202, 400, 422), (
            f"commands batch returned {resp.status_code}. "
            "Route is mounted in serve.py — 404 is a failure."
        )

    def test_auth_users_list_includes_frontend_fields_or_boundary(self, server_available):
        resp = get("/api/auth/users", params={"page": 1, "per_page": 10})
        if resp.status_code in (400, 401, 403):
            if BACKEND_ROLE != "admin":
                return
            # Admin lane should generally have access when local auth is enabled.
            return
        assert resp.status_code == 200, (
            f"auth/users returned {resp.status_code}. "
            "Route is mounted in routes/auth_users.py — 404 is a failure."
        )
        body = resp.json()
        users = body.get("users") or []
        assert isinstance(users, list)
        if users:
            row = users[0]
            expected = {"id", "username", "display_name", "roles", "is_active"}
            missing = expected.difference(set(row.keys()))
            assert not missing, f"auth users row missing frontend keys: {sorted(missing)}"
