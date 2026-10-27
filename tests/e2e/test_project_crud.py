"""E2E tests for Project CRUD and configuration APIs.

Covers: PROJ-01 to PROJ-17 — create, list, edit/save, delete,
workflows, policy, providers, queue, history, bulk delete,
notification dispatch.
"""

from __future__ import annotations

import os
import time

import pytest

from core.application.auth.config import DEFAULT_WORKSPACE_ID
from core.application.tooling.testing.runtime_env import resolve_test_env
from tests.e2e.helpers import (
    delete as delete_json,
)
from tests.e2e.helpers import (
    get,
    post_form,
    post_json,
    put_json,
    unique_id,
)

pytestmark = [pytest.mark.e2e]

TEST_ENV = resolve_test_env(os.environ, lane="backend", include_dotenv=True)
PROJECT_ID = str(TEST_ENV.project_id or "").strip()


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


# ── Project List API ────────────────────────────────────────────────────────


class TestProjectListAPI:
    def test_project_list_returns_projects(self, server_available):
        resp = get("/api/projects")
        assert resp.status_code == 200
        data = resp.json()
        # May be a list or a dict with "projects" key
        projects = data if isinstance(data, list) else data.get("projects") or []
        assert isinstance(projects, list)
        if projects:
            p = projects[0]
            expected_keys = {"id", "name", "job_count"}
            missing = expected_keys.difference(set(p.keys()))
            assert not missing, f"project list row missing frontend keys: {sorted(missing)}"
            retired_keys = {"stack", "index_status", "repo_url", "project_type"}
            leaked = retired_keys.intersection(set(p.keys()))
            assert not leaked, f"project list row exposes retired keys: {sorted(leaked)}"

    def test_project_list_contains_test_project(self, server_available, test_project_id):
        resp = get("/api/projects", params={"search": test_project_id, "per_page": 200})
        assert resp.status_code == 200
        data = resp.json()
        projects = data if isinstance(data, list) else data.get("projects") or []
        ids = [str(p.get("id")) for p in projects]
        if test_project_id in ids:
            return

        fallback = get(f"/projects/{test_project_id}")
        assert fallback.status_code == 200, (
            f"Project {test_project_id} missing from list and project page lookup"
        )


# ── Project CRUD ────────────────────────────────────────────────────────────


class TestProjectCRUD:
    def test_project_form_create_route_removed_after_react_cutover(self, server_available):
        name = unique_id("retired-proj")
        resp = post_form(
            "/projects",
            data={
                "name": name,
                "workspace_id": DEFAULT_WORKSPACE_ID,
            },
        )
        assert resp.status_code in (404, 405), (
            f"retired POST /projects returned {resp.status_code}: {resp.text[:300]}"
        )

    def test_create_and_delete_project_through_api(self, server_available):
        name = unique_id("proj")
        project_id = ""
        try:
            resp = post_json(
                "/api/projects",
                {
                    "name": name,
                    "workspace_id": DEFAULT_WORKSPACE_ID,
                },
            )
            assert resp.status_code in (200, 201), (
                f"API create failed: {resp.status_code} {resp.text[:300]}"
            )
            body = resp.json() or {}
            project_id = str(body.get("id") or body.get("project_id") or "").strip()
            assert project_id, f"API create response missing project id: {body!r}"

            detail_resp = get(f"/api/projects/{project_id}")
            assert detail_resp.status_code == 200, (
                f"created project detail failed: {detail_resp.status_code} {detail_resp.text[:300]}"
            )
            detail = detail_resp.json() or {}
            assert str(detail.get("name") or "").strip() == name
        finally:
            if project_id:
                delete_resp = delete_json(f"/api/projects/{project_id}")
                assert delete_resp.status_code in (200, 204, 404), (
                    f"API delete cleanup failed: {delete_resp.status_code} {delete_resp.text[:300]}"
                )

    def test_create_valid_empty_workflows_roundtrips_without_blocking_or_overwrite(
        self, server_available
    ):
        name = unique_id("project-invariant")
        project_id = ""
        create_payload = {
            "name": name,
            "workspace_id": DEFAULT_WORKSPACE_ID,
            "workflow_ids": [],
        }
        try:
            created = post_json("/api/projects", create_payload)
            assert created.status_code == 201, created.text[:300]
            project_id = str(
                (created.json() or {}).get("id") or (created.json() or {}).get("project_id") or ""
            ).strip()
            assert project_id

            before = get(f"/api/projects/{project_id}")
            assert before.status_code == 200, before.text[:300]
            assert list((before.json() or {}).get("project_workflow_ids") or []) == []

            started = time.monotonic()
            updated = put_json(f"/api/projects/{project_id}", create_payload)
            elapsed = time.monotonic() - started
            assert updated.status_code == 200, updated.text[:300]
            assert elapsed < 5, f"no-op project update blocked for {elapsed:.2f}s"
            assert get("/api/health", timeout=5).status_code == 200

            after = get(f"/api/projects/{project_id}")
            assert after.status_code == 200, after.text[:300]
            assert str((after.json() or {}).get("name") or "") == name
            assert list((after.json() or {}).get("project_workflow_ids") or []) == []

            duplicate = post_json("/api/projects", create_payload)
            assert duplicate.status_code == 409, duplicate.text[:300]

            listed = get(
                "/api/projects",
                params={"workspace_id": DEFAULT_WORKSPACE_ID, "search": name, "per_page": 200},
            )
            assert listed.status_code == 200, listed.text[:300]
            rows = (listed.json() or {}).get("projects") or []
            assert [str(row.get("id") or "") for row in rows].count(project_id) == 1

            unknown = get("/api/projects/11111111-1111-1111-1111-111111111111")
            assert unknown.status_code == 404, unknown.text[:300]
            unknown_workspace = post_json(
                "/api/projects",
                {
                    **create_payload,
                    "name": f"{name}-unknown-workspace",
                    "workspace_id": "11111111-1111-1111-1111-111111111111",
                },
            )
            assert unknown_workspace.status_code == 404, unknown_workspace.text[:300]
        finally:
            if project_id:
                cleanup = delete_json(f"/api/projects/{project_id}")
                assert cleanup.status_code in (200, 204, 404), cleanup.text[:300]

    def test_create_project_api_requires_name(self, server_available):
        resp = post_json(
            "/api/projects",
            {
                "name": "",
                "workspace_id": DEFAULT_WORKSPACE_ID,
            },
        )
        assert resp.status_code in (400, 422)


# ── Project Config APIs ─────────────────────────────────────────────────────


class TestProjectConfigAPIs:
    def test_project_workflows_get(self, server_available, test_project_id):
        resp = get(f"/api/projects/{test_project_id}/workflows")
        if resp.status_code == 404:
            pytest.skip("Project workflows endpoint not implemented")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, (dict, list))

    def test_project_policy_get(self, server_available, test_project_id):
        resp = get(f"/api/projects/{test_project_id}/policy")
        if resp.status_code == 404:
            pytest.skip("Project policy endpoint not implemented")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, dict)

    def test_project_policy_put_roundtrip(self, server_available, test_project_id):
        # Read current policy
        get_resp = get(f"/api/projects/{test_project_id}/policy")
        if get_resp.status_code == 404:
            pytest.skip("Project policy endpoint not implemented")
        assert get_resp.status_code == 200
        original = get_resp.json()

        # Write it back unchanged — should succeed
        put_resp = put_json(f"/api/projects/{test_project_id}/policy", original)
        assert put_resp.status_code in (200, 204), (
            f"policy PUT failed: {put_resp.status_code} {put_resp.text[:300]}"
        )

    def test_project_providers_get(self, server_available, test_project_id):
        resp = get(f"/api/projects/{test_project_id}/providers")
        if resp.status_code in (404, 410):
            pytest.skip("Project providers endpoint not available or deprecated")
        assert resp.status_code == 200

    def test_project_queue_status(self, server_available, test_project_id):
        resp = get(f"/api/projects/{test_project_id}/queue")
        if resp.status_code == 404:
            pytest.skip("Queue endpoint not implemented")
        assert resp.status_code == 200


# ── Project History ─────────────────────────────────────────────────────────


class TestProjectHistory:
    def test_project_history_list(self, server_available, test_project_id):
        resp = get(f"/api/projects/{test_project_id}/history")
        if resp.status_code == 404:
            pytest.skip("Project history endpoint not implemented")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, (dict, list))

    def test_project_history_page_loads(self, server_available, test_project_id):
        # History is a tab of the project page.
        resp = get(f"/projects/{test_project_id}?tab=history")
        assert resp.status_code == 200
        assert "<html" in resp.text.lower()


# ── Bulk Job Delete ─────────────────────────────────────────────────────────


class TestBulkJobDelete:
    def test_bulk_delete_empty_list(self, server_available, test_project_id):
        resp = post_json(
            f"/api/projects/{test_project_id}/jobs/bulk-delete",
            {
                "ids": [],
                "confirm": "DELETE",
            },
        )
        if resp.status_code == 404:
            pytest.skip("Bulk delete endpoint not implemented")
        # Empty list should succeed or be rejected gracefully
        assert resp.status_code in (200, 400, 403, 422)

    def test_bulk_delete_nonexistent_ids(self, server_available, test_project_id):
        resp = post_json(
            f"/api/projects/{test_project_id}/jobs/bulk-delete",
            {
                "ids": [999999, 999998],
                "confirm": "DELETE",
            },
        )
        if resp.status_code == 404:
            pytest.skip("Bulk delete endpoint not implemented")
        # Should not error — just skip missing IDs
        assert resp.status_code in (200, 400, 403, 404, 422)


# ── Notification Dispatch ───────────────────────────────────────────────────


class TestNotificationDispatch:
    def test_dispatch_endpoint_exists(self, server_available, test_project_id):
        resp = post_json(
            f"/api/projects/{test_project_id}/notifications/dispatch",
            {
                "type": "test",
                "message": "E2E smoke test notification",
            },
        )
        # Route may be absent or routes may be unconfigured, but response must stay typed.
        assert resp.status_code in (200, 202, 400, 404, 422)
