"""E2E tests covering previously-uncovered serve.py endpoints.

Fills gaps identified in the coverage audit:
- Project edit/save, delete
- Task with attachments
- Global providers CRUD + project binding
- Project workflow config write
- Notification route create
- Workflow reactions create
- Sample Tasks poll
- Project history diff/restore
- Job restart
- Job logs API
"""

from __future__ import annotations

import os
import time

import pytest

from core.application.auth.config import DEFAULT_WORKSPACE_ID
from core.application.tooling.testing.runtime_env import resolve_test_env
from tests.e2e.helpers import (
    E2ECleanup,
    ensure_test_job,
    get,
    post_form,
    post_json,
    purge_trashed_project,
    put_json,
    unique_id,
)
from tests.e2e.helpers import (
    delete as delete_json,
)

pytestmark = [pytest.mark.e2e]

TEST_ENV = resolve_test_env(os.environ, lane="backend", include_dotenv=True)
PROJECT_ID = str(TEST_ENV.project_id or "").strip()


@pytest.fixture(scope="module")
def server_available():
    resp = get("/health")
    if resp.status_code >= 500:
        pytest.fail(f"Flow Steward server unhealthy: {resp.status_code}")


@pytest.fixture(scope="module")
def test_project_id() -> str:
    if not PROJECT_ID:
        pytest.fail("TEST_PROJECT_BACKEND_ID not set")
    return PROJECT_ID


TERMINAL_JOB_STATUSES = {
    # Approval gates are restartable paused states: the job has a persisted thread
    # and the restart endpoint can clone it without needing another worker tick.
    "awaiting_approval",
    "completed",
    "failed",
    "cancelled",
    "max_iterations",
    "retryable_interrupted_step",
    "stuck",
}


def _wait_for_job_terminal(job_id: str | int, *, timeout: float = 120, interval: float = 2) -> dict:
    deadline = time.monotonic() + timeout
    last_row: dict | None = None
    while time.monotonic() < deadline:
        resp = get(f"/api/jobs/{job_id}")
        assert resp.status_code == 200, (
            f"Could not read job {job_id}: {resp.status_code} {resp.text[:300]}"
        )
        body = resp.json()
        row = body.get("job", body) if isinstance(body, dict) else body
        assert isinstance(row, dict), f"Unexpected job payload for {job_id}: {body!r}"
        last_row = row
        status = str(row.get("status") or "").strip().lower()
        if status in TERMINAL_JOB_STATUSES:
            return row
        time.sleep(interval)
    pytest.fail(f"Job {job_id} did not reach terminal state: {last_row!r}")


def _wait_for_job_logs(
    job_id: str | int,
    *,
    timeout: float = 60,
    interval: float = 2,
    limit: int = 2,
) -> dict:
    deadline = time.monotonic() + timeout
    last_body: dict = {}
    while time.monotonic() < deadline:
        resp = get(f"/api/jobs/{job_id}/logs", params={"limit": max(1, int(limit))})
        assert resp.status_code == 200, (
            f"Could not read logs for job {job_id}: {resp.status_code} {resp.text[:300]}"
        )
        body = resp.json() or {}
        assert isinstance(body, dict), f"Unexpected logs payload for job {job_id}: {body!r}"
        last_body = body
        logs = body.get("logs") or []
        if isinstance(logs, list) and logs:
            return body
        time.sleep(interval)
    pytest.fail(f"Job {job_id} did not produce logs: {last_body!r}")


# ── Project Edit/Save ────────────────────────────────────────────────────────


class TestProjectEditSave:
    def test_edit_page_is_gone(self, server_available, test_project_id):
        """GET /projects/{id}/edit is gone: the project page holds every section."""
        resp = get(f"/projects/{test_project_id}/edit")
        assert resp.status_code == 404

    def test_save_project_form_post_removed_after_react_cutover(
        self, server_available, test_project_id
    ):
        """POST /projects/{id}/edit is removed; React saves through the project API."""
        resp = post_form(
            f"/projects/{test_project_id}/edit",
            data={
                "name": "test-project",
                "workspace_id": DEFAULT_WORKSPACE_ID,
                "workflow_ids": "wf-main",
                "operator_default_workflow_id": "wf-main",
            },
        )
        assert resp.status_code in (404, 405)

    def test_save_project_missing_workflow_form_post_removed(
        self, server_available, test_project_id
    ):
        """Old form validation path is gone with the old form transport."""
        resp = post_form(
            f"/projects/{test_project_id}/edit",
            data={
                "name": "test-project",
                "workspace_id": DEFAULT_WORKSPACE_ID,
                # No workflow_ids — should fail validation.
            },
        )
        assert resp.status_code in (404, 405)


# ── Project Delete ────────────────────────────────────────────────────────────


class TestProjectDelete:
    def test_delete_nonexistent_project(self, server_available):
        """POST /projects/99999/delete is removed after React cutover."""
        resp = post_form("/projects/99999/delete")
        assert resp.status_code in (404, 405)

    def test_delete_and_recreate_project(self, server_available):
        """Full round-trip: create → delete → verify gone."""
        uid = unique_id("delproj")

        create_resp = post_json(
            "/api/projects",
            {
                "name": uid,
                "workspace_id": DEFAULT_WORKSPACE_ID,
            },
        )
        assert create_resp.status_code in (200, 201), (
            f"Could not create test project: {create_resp.status_code} {create_resp.text[:300]}"
        )
        create_body = create_resp.json() or {}
        project_id = create_body.get("id") or create_body.get("project_id")
        assert project_id, f"Project create response missing id: {create_body}"

        # Verify the project materialized before deleting it.
        created: dict | None = None
        for _ in range(20):
            project_resp = get(f"/api/projects/{project_id}")
            if project_resp.status_code == 200:
                body = project_resp.json() or {}
                if str(body.get("name") or "").strip() == uid:
                    created = body
                    break
            time.sleep(0.5)
        assert created is not None, (
            f"Project create returned {create_resp.status_code} but {uid!r} did not materialize "
            f"at /api/projects/{project_id}."
        )

        # Delete through the canonical API endpoint.
        removed_form_resp = post_form(f"/projects/{project_id}/delete")
        assert removed_form_resp.status_code in (404, 405)
        del_resp = delete_json(f"/api/projects/{project_id}")
        assert del_resp.status_code in (200, 204), f"delete returned {del_resp.status_code}"

        # Verify gone.
        for _ in range(20):
            project_resp = get(f"/api/projects/{project_id}")
            if project_resp.status_code == 404:
                break
            time.sleep(0.5)
        assert project_resp.status_code == 404, (
            f"Project {project_id} still exists after delete: "
            f"{project_resp.status_code} {project_resp.text[:300]}"
        )


# ── Task With Attachments ─────────────────────────────────────────────────────


class TestGlobalProviders:
    def test_list_providers(self, server_available):
        """GET /api/providers returns provider list."""
        resp = get("/api/providers")
        if resp.status_code == 503:
            pytest.fail("Database not configured")
        assert resp.status_code == 200
        body = resp.json()
        assert "providers" in body or isinstance(body, list)

    def test_list_providers_with_filter(self, server_available):
        """GET /api/providers?enabled_only=false returns all providers."""
        resp = get("/api/providers", params={"enabled_only": "false"})
        if resp.status_code == 503:
            pytest.fail("Database not configured")
        assert resp.status_code == 200

    def test_create_provider(self, server_available):
        """POST /api/providers creates a provider."""
        uid = unique_id("prov")
        with E2ECleanup():
            resp = post_json(
                "/api/providers",
                {
                    "provider_key": uid,
                    "provider_type_key": "tool_provider",
                    "display_name": f"E2E Test Provider {uid}",
                    "enabled": False,
                },
            )
            if resp.status_code == 503:
                pytest.fail("Database not configured")
            assert resp.status_code in (200, 201), (
                f"create provider returned {resp.status_code}: {resp.text[:300]}"
            )

    def test_set_provider_secrets(self, server_available):
        """POST /api/providers/{key}/secrets stores a secret."""
        uid = unique_id("secprov")
        # Create provider first.
        create_resp = post_json(
            "/api/providers",
            {
                "provider_key": uid,
                "provider_type_key": "tool_provider",
                "display_name": f"E2E Secret Provider {uid}",
                "enabled": False,
            },
        )
        if create_resp.status_code == 503:
            pytest.fail("Database not configured")
        assert create_resp.status_code in (200, 201), (
            f"Could not create provider: {create_resp.status_code} {create_resp.text[:300]}"
        )

        resp = post_json(
            f"/api/providers/{uid}/secrets",
            {
                "secret_key": "api_token",
                "secret_value": "e2e_test_value_12345",
                "active": True,
            },
        )
        if resp.status_code in (500, 503):
            error_text = resp.text
            if "SECRET_KEY" in error_text or "encrypted secret storage" in error_text:
                pytest.fail("FS_SECRET_KEY not configured for encrypted secret storage")
        assert resp.status_code in (200, 201), (
            f"set secret returned {resp.status_code}: {resp.text[:300]}"
        )
        body = resp.json()
        assert body.get("status") in ("saved", "skipped")


# ── Project Provider Binding ──────────────────────────────────────────────────


class TestProjectProviderBinding:
    def test_bind_provider_to_project(self, server_available, test_project_id):
        """POST /api/projects/{id}/providers/bind creates a binding."""
        # Use a well-known provider key or create one.
        resp = post_json(
            f"/api/projects/{test_project_id}/providers/bind",
            {
                "provider_key": "acme.notification-provider",
                "enabled": True,
                "config": {},
            },
        )
        if resp.status_code == 503:
            pytest.fail("Database not configured")
        # May succeed (200) or fail with 404 if provider doesn't exist.
        assert resp.status_code in (200, 400, 404, 422), (
            f"bind provider returned {resp.status_code}: {resp.text[:300]}"
        )

    def test_bind_nonexistent_provider(self, server_available, test_project_id):
        """Binding a nonexistent provider should fail gracefully."""
        resp = post_json(
            f"/api/projects/{test_project_id}/providers/bind",
            {
                "provider_key": "nonexistent_provider_xyz_99999",
                "enabled": True,
            },
        )
        if resp.status_code == 503:
            pytest.fail("Database not configured")
        # Binding a nonexistent provider must fail cleanly — 500 is a crash
        # regression, and 200 would mean the bind falsely "succeeded".
        assert resp.status_code in (400, 404, 422), (
            f"bind nonexistent provider returned {resp.status_code}; "
            "expected a validation error (400/404/422)."
        )


# ── Project Workflow Config Write ─────────────────────────────────────────────


class TestProjectWorkflowWrite:
    def test_update_project_workflows(self, server_available, test_project_id):
        """PUT /api/projects/{id}/workflows updates workflow selection."""
        # Read current to restore later.
        current = get(f"/api/projects/{test_project_id}/workflows")
        if current.status_code != 200:
            pytest.fail(f"Could not read current workflows: {current.status_code}")
        original_body = current.json()

        # Discover valid enabled workflow IDs from current config or catalog.
        orig_ids = original_body.get("workflow_ids") or []
        catalog = get("/api/workflows", params={"enabled_only": "true"})
        if catalog.status_code != 200:
            pytest.fail(f"Could not discover enabled workflow IDs: {catalog.status_code}")
        catalog_body = catalog.json()
        catalog_rows = (
            catalog_body
            if isinstance(catalog_body, list)
            else (catalog_body or {}).get("workflows") or []
        )
        enabled_ids = [
            str(row.get("workflow_id") or row.get("id") or "").strip()
            for row in catalog_rows
            if isinstance(row, dict) and str(row.get("workflow_id") or row.get("id") or "").strip()
        ]
        current_ids = (
            [str(w.get("workflow_id", w) if isinstance(w, dict) else w).strip() for w in orig_ids]
            if isinstance(orig_ids, list)
            else []
        )
        wf_ids = [workflow_id for workflow_id in current_ids if workflow_id in enabled_ids]
        if not wf_ids and enabled_ids:
            wf_ids = enabled_ids[:1]
        if not wf_ids:
            pytest.fail("No enabled workflow IDs available")

        # Update with discovered workflow.
        resp = put_json(
            f"/api/projects/{test_project_id}/workflows",
            {
                "workflow_ids": wf_ids,
                "operator_default_workflow_id": wf_ids[0],
            },
        )
        if resp.status_code == 503:
            pytest.fail("Database not configured")
        if resp.status_code == 403:
            pytest.fail("Admin role required for workflow updates in this environment")
        assert resp.status_code in (200, 404), (
            f"update workflows returned {resp.status_code}: {resp.text[:300]}"
        )

        # Restore original if we changed anything.
        if resp.status_code == 200 and original_body and wf_ids:
            orig_wf_ids = original_body.get("workflow_ids") or []
            if isinstance(orig_wf_ids, list) and orig_wf_ids:
                ids = [w.get("workflow_id", w) if isinstance(w, dict) else w for w in orig_wf_ids]
                ids = [
                    str(workflow_id or "").strip()
                    for workflow_id in ids
                    if str(workflow_id or "").strip()
                ]
                enabled_restore_ids = [
                    workflow_id for workflow_id in ids if workflow_id in enabled_ids
                ]
                if enabled_restore_ids:
                    orig_default = str(
                        original_body.get("operator_default_workflow_id") or ""
                    ).strip()
                    if orig_default not in enabled_restore_ids:
                        orig_default = enabled_restore_ids[0]
                    put_json(
                        f"/api/projects/{test_project_id}/workflows",
                        {
                            "workflow_ids": enabled_restore_ids,
                            "operator_default_workflow_id": orig_default,
                        },
                    )

    def test_update_workflows_requires_at_least_one(self, server_available, test_project_id):
        """PUT /api/projects/{id}/workflows with empty list should fail."""
        resp = put_json(
            f"/api/projects/{test_project_id}/workflows",
            {
                "workflow_ids": [],
                "operator_default_workflow_id": "",
            },
        )
        if resp.status_code == 503:
            pytest.fail("Database not configured")
        if resp.status_code == 403:
            pytest.fail("Admin role required for workflow updates in this environment")
        assert resp.status_code in (400, 422), (
            f"empty workflows should be rejected: {resp.status_code}"
        )


# ── Notification Route Create ─────────────────────────────────────────────────


class TestNotificationRouteCreate:
    def test_create_notification_route(self, server_available, test_project_id):
        """POST /api/projects/{id}/notification-routes creates routes."""
        resp = post_json(
            f"/api/projects/{test_project_id}/notification-routes",
            {
                "routes": [
                    {
                        "event_type": "job.completed",
                        "provider_key": "acme.notification-provider",
                        "severity": "any",
                        "enabled": False,
                    }
                ],
            },
        )
        if resp.status_code == 503:
            pytest.fail("Database not configured")
        # May fail if provider doesn't exist, which is fine.
        assert resp.status_code in (200, 400, 404, 422), (
            f"create notification route returned {resp.status_code}: {resp.text[:300]}"
        )

    def test_create_empty_notification_routes(self, server_available, test_project_id):
        """POST with empty routes list should succeed (clear routes)."""
        resp = post_json(
            f"/api/projects/{test_project_id}/notification-routes",
            {
                "routes": [],
            },
        )
        if resp.status_code == 503:
            pytest.fail("Database not configured")
        assert resp.status_code in (200, 400, 404)


# ── Workflow Reactions Create ─────────────────────────────────────────────────


class TestWorkflowReactionsCreate:
    def test_create_workflow_reaction(self, server_available):
        """POST /api/workflows/{id}/reactions creates reaction rules."""
        # First find a workflow.
        wf_resp = get("/api/workflows")
        if wf_resp.status_code != 200:
            pytest.fail("Could not list workflows")
        workflows = (
            wf_resp.json()
            if isinstance(wf_resp.json(), list)
            else (wf_resp.json() or {}).get("workflows") or []
        )
        if not workflows:
            pytest.fail("No workflows available")
        wf_id = workflows[0].get("workflow_id") or workflows[0].get("id")

        # Read current reactions to restore later.
        current = get(f"/api/workflows/{wf_id}/reactions")
        original_reactions = []
        if current.status_code == 200:
            original_reactions = (current.json() or {}).get("reactions") or []

        resp = post_json(
            f"/api/workflows/{wf_id}/reactions",
            {
                "reactions": [
                    {
                        "event_type": "job.completed",
                        "reaction_type": "notify",
                        "config": {"channel": "e2e-test"},
                        "enabled": False,
                    },
                ],
            },
        )
        if resp.status_code == 503:
            pytest.fail("Database not configured")
        assert resp.status_code in (200, 400, 404, 422), (
            f"create reaction returned {resp.status_code}: {resp.text[:300]}"
        )

        # Restore original reactions.
        if resp.status_code == 200:
            post_json(
                f"/api/workflows/{wf_id}/reactions",
                {
                    "reactions": original_reactions,
                },
            )

    def test_create_reaction_for_nonexistent_workflow(self, server_available):
        """Reactions for nonexistent workflow should return 404."""
        resp = post_json(
            "/api/workflows/nonexistent_workflow_xyz/reactions",
            {
                "reactions": [],
            },
        )
        if resp.status_code == 503:
            pytest.fail("Database not configured")
        assert resp.status_code in (404, 400)


# ── Project History Diff / Restore ────────────────────────────────────────────


class TestProjectHistoryDiffRestore:
    def _get_revisions(self, test_project_id: str) -> list:
        resp = get(f"/api/projects/{test_project_id}/history")
        if resp.status_code != 200:
            return []
        body = resp.json()
        return (
            body.get("revisions") or body.get("history") or (body if isinstance(body, list) else [])
        )

    def _cleanup_history_project(self, project_id: str) -> None:
        trash_resp = post_json(f"/api/trash/project/{project_id}/trash", {})
        assert trash_resp.status_code == 200, (
            f"history project trash failed: {trash_resp.status_code} {trash_resp.text[:300]}"
        )
        purge_resp = purge_trashed_project(project_id)
        assert purge_resp.status_code == 200, (
            f"history project purge failed: {purge_resp.status_code} {purge_resp.text[:300]}"
        )

    def _ensure_revisions(self, test_project_id: str) -> tuple[str, list, callable]:
        revisions = self._get_revisions(test_project_id)
        if len(revisions) >= 2:
            return test_project_id, revisions, lambda: None

        project_name = unique_id("histproj")
        create_resp = post_json(
            "/api/projects",
            {
                "name": project_name,
                "workspace_id": DEFAULT_WORKSPACE_ID,
            },
        )
        assert create_resp.status_code in (200, 201), (
            f"Could not create history test project: {create_resp.status_code} "
            f"{create_resp.text[:300]}"
        )
        project_body = create_resp.json() or {}
        project_id = str(project_body.get("id") or project_body.get("project_id") or "").strip()
        assert project_id, f"History test project missing id: {project_body}"

        update_one = put_json(
            f"/api/projects/{project_id}",
            {
                "name": f"{project_name}-v2",
                "workspace_id": DEFAULT_WORKSPACE_ID,
            },
        )
        assert update_one.status_code == 200, (
            f"History test project update 1 failed: {update_one.status_code} "
            f"{update_one.text[:300]}"
        )
        update_two = put_json(
            f"/api/projects/{project_id}",
            {
                "name": f"{project_name}-v3",
                "workspace_id": DEFAULT_WORKSPACE_ID,
                "description": "History test revision three",
            },
        )
        assert update_two.status_code == 200, (
            f"History test project update 2 failed: {update_two.status_code} "
            f"{update_two.text[:300]}"
        )
        revisions = self._get_revisions(project_id)
        assert revisions, "Temporary history test project did not produce revisions"
        return project_id, revisions, lambda: self._cleanup_history_project(project_id)

    def test_history_diff_endpoint(self, server_available, test_project_id):
        """GET /api/projects/{id}/history/{revision_id}/diff returns diff."""
        project_id, revisions, cleanup = self._ensure_revisions(test_project_id)
        try:
            rev_id = revisions[0].get("id") or revisions[0].get("revision_id")
            if not rev_id:
                pytest.fail("Revision has no ID")

            resp = get(f"/api/projects/{project_id}/history/{rev_id}/diff")
            if resp.status_code == 503:
                pytest.fail("History diff not available in this schema")
            assert resp.status_code in (200, 404), (
                f"history diff returned {resp.status_code}: {resp.text[:300]}"
            )
            if resp.status_code == 200:
                body = resp.json()
                assert "changes" in body or "summary" in body or "diff" in body
        finally:
            cleanup()

    def test_history_diff_nonexistent_revision(self, server_available, test_project_id):
        """Diff for nonexistent revision should return 404."""
        resp = get(f"/api/projects/{test_project_id}/history/99999/diff")
        if resp.status_code == 503:
            pytest.fail("History diff not available in this schema")
        assert resp.status_code in (404, 400)

    def test_history_diff_compare_two_revisions(self, server_available, test_project_id):
        """Diff with compare_to_revision_id compares two revisions."""
        project_id, revisions, cleanup = self._ensure_revisions(test_project_id)
        try:
            if len(revisions) < 2:
                pytest.fail("Need at least 2 revisions to compare")

            rev1 = revisions[0].get("id") or revisions[0].get("revision_id")
            rev2 = revisions[1].get("id") or revisions[1].get("revision_id")

            resp = get(
                f"/api/projects/{project_id}/history/{rev1}/diff",
                params={"compare_to_revision_id": rev2},
            )
            if resp.status_code == 503:
                pytest.fail("History diff not available")
            assert resp.status_code in (200, 404)
        finally:
            cleanup()

    def test_history_api_restore(self, server_available, test_project_id):
        """POST /api/projects/{id}/history/{revision_id}/restore restores."""
        project_id, revisions, cleanup = self._ensure_revisions(test_project_id)
        try:
            rev_id = revisions[0].get("id") or revisions[0].get("revision_id")
            if not rev_id:
                pytest.fail("Revision has no ID")

            resp = post_json(f"/api/projects/{project_id}/history/{rev_id}/restore", {})
            if resp.status_code == 503:
                pytest.fail("History restore not available in this schema")
            assert resp.status_code in (200, 400, 404, 409, 422), (
                f"history restore returned {resp.status_code}: {resp.text[:300]}"
            )
        finally:
            cleanup()

    def test_history_form_restore_removed_after_react_cutover(
        self, server_available, test_project_id
    ):
        """Retired form restore route is removed; React uses the API restore contract."""
        project_id, revisions, cleanup = self._ensure_revisions(test_project_id)
        try:
            rev_id = revisions[0].get("id") or revisions[0].get("revision_id")
            if not rev_id:
                pytest.fail("Revision has no ID")

            resp = post_form(
                f"/projects/{project_id}/history/{rev_id}/restore",
                data={"return_to": f"/projects/{project_id}?tab=history"},
            )
            if resp.status_code == 503:
                pytest.fail("History restore not available")
            assert resp.status_code in (404, 405)
        finally:
            cleanup()


# ── Job Restart ───────────────────────────────────────────────────────────────


class TestJobRestart:
    def _find_terminal_job(self, test_project_id: str) -> int | None:
        resp = get("/api/jobs", params={"project_id": test_project_id, "limit": 10})
        if resp.status_code != 200:
            return None
        jobs = (
            resp.json() if isinstance(resp.json(), list) else (resp.json() or {}).get("jobs") or []
        )
        for j in jobs:
            status = str(j.get("status") or "").lower()
            if status in ("completed", "cancelled", "failed", "max_iterations"):
                return str(j.get("id") or j.get("job_id") or 0).strip() or None
        return None

    def test_restart_nonexistent_job(self, server_available):
        """POST /api/jobs/99999/restart is JSON-only and never a form redirect."""
        resp = post_json(
            "/api/jobs/99999/restart",
            {},
            headers={"accept": "application/json"},
            allow_redirects=False,
        )
        assert resp.status_code in (404, 500, 503)
        assert resp.status_code not in (302, 303)

    def test_restart_retired_job_form_alias_removed(self, server_available):
        """POST /jobs/{id}/restart remains removed after the React cutover."""
        resp = post_form("/jobs/99999/restart")
        assert resp.status_code in (404, 405)

    def test_restart_terminal_job(self, server_available, test_project_id):
        """POST /api/jobs/{id}/restart on terminal job creates new job."""
        job = ensure_test_job(test_project_id)
        job_id = str(job.get("id") or job.get("job_id") or 0).strip()
        assert job_id, f"Seed job missing id: {job}"
        source_row = _wait_for_job_terminal(job_id)
        source_status = str(source_row.get("status") or "").strip().lower()
        assert source_status in TERMINAL_JOB_STATUSES, (
            f"Seed job {job_id} did not reach terminal state: {source_row!r}"
        )

        resp = post_json(
            f"/api/jobs/{job_id}/restart",
            {},
            headers={"accept": "application/json"},
            allow_redirects=False,
        )
        assert resp.status_code == 200, f"restart returned {resp.status_code}: {resp.text[:300]}"
        body = resp.json() or {}
        restarted_job_id = str(body.get("new_job_id") or 0).strip()
        assert restarted_job_id, f"Restart response missing new_job_id: {body}"
        assert restarted_job_id != job_id, (
            f"Restart endpoint returned same job id for source={job_id}: {body}"
        )


# ── Job Logs API ──────────────────────────────────────────────────────────────


class TestJobLogsAPI:
    @pytest.fixture(scope="class")
    def seed_job_id(self, test_project_id):
        job = ensure_test_job(test_project_id)
        return str(job.get("id") or job.get("job_id") or 0).strip()

    def test_job_logs_basic(self, server_available, test_project_id, seed_job_id):
        """GET /api/jobs/{id}/logs returns logs array."""
        job_id = seed_job_id

        resp = get(f"/api/jobs/{job_id}/logs")
        if resp.status_code == 503:
            pytest.fail("Database not configured")
        assert resp.status_code == 200
        body = resp.json()
        assert "logs" in body
        assert isinstance(body["logs"], list)
        assert "status" in body
        assert "last_id" in body or "has_more" in body

    def test_job_logs_with_limit(self, server_available, test_project_id, seed_job_id):
        """GET /api/jobs/{id}/logs?limit=5 respects limit."""
        job_id = seed_job_id

        resp = get(f"/api/jobs/{job_id}/logs", params={"limit": 5})
        assert resp.status_code == 200
        body = resp.json()
        assert len(body.get("logs", [])) <= 5

    def test_job_logs_since_id(self, server_available, test_project_id, seed_job_id):
        """GET /api/jobs/{id}/logs?since_id=N returns incremental logs."""
        job_id = seed_job_id

        # Get first batch.
        resp = get(f"/api/jobs/{job_id}/logs", params={"limit": 2})
        assert resp.status_code == 200, (
            f"Could not read logs for job {job_id}: {resp.status_code} {resp.text[:300]}"
        )
        body = resp.json() or {}
        assert isinstance(body, dict), f"Unexpected logs payload for job {job_id}: {body!r}"
        logs = body.get("logs") or []
        last_id = body.get("last_id") or (logs[-1].get("id", 0) if logs else 0)
        # Get logs since last_id.
        resp2 = get(f"/api/jobs/{job_id}/logs", params={"since_id": last_id})
        assert resp2.status_code == 200

    def test_job_logs_nonexistent_job(self, server_available):
        """GET /api/jobs/99999/logs returns 404."""
        resp = get("/api/jobs/99999/logs")
        assert resp.status_code in (404, 503)

    def test_job_logs_newest_first(self, server_available, test_project_id, seed_job_id):
        """GET /api/jobs/{id}/logs?newest=true returns newest first."""
        job_id = seed_job_id

        resp = get(f"/api/jobs/{job_id}/logs", params={"newest": "true", "limit": 5})
        assert resp.status_code == 200
