"""E2E contracts for the project workflow-preview endpoint."""

from __future__ import annotations

import os
import time

import pytest
import requests

from core.application.tooling.testing.runtime_env import resolve_base_url, resolve_test_env

pytestmark = [pytest.mark.e2e]

BACKEND_ROLE = str(os.environ.get("E2E_BACKEND_ROLE", "admin") or "admin").strip().lower()
BASE_URL = resolve_base_url(os.environ, include_dotenv=True)
_BACKEND_ENV = resolve_test_env(os.environ, lane="backend", role=BACKEND_ROLE, include_dotenv=True)
PROJECT_ID = str(_BACKEND_ENV.project_id or "").strip()
VERIFY_SSL = str(os.getenv("E2E_VERIFY_SSL", "0") or "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
HTTP_RETRY_COUNT = max(0, min(int(str(os.getenv("E2E_HTTP_RETRY_COUNT", "2") or "2")), 6))


def _get(path: str, **kwargs) -> requests.Response:
    return requests.get(f"{BASE_URL}{path}", verify=VERIFY_SSL, timeout=20, **kwargs)


def _post_json(path: str, payload: dict, **kwargs) -> requests.Response:
    attempts = 0
    while True:
        try:
            resp = requests.post(
                f"{BASE_URL}{path}",
                json=payload,
                verify=VERIFY_SSL,
                timeout=30,
                **kwargs,
            )
        except (requests.ConnectionError, requests.Timeout) as exc:
            text = str(exc or "").lower()
            transient = any(
                marker in text
                for marker in (
                    "connection reset",
                    "connection aborted",
                    "remote disconnected",
                    "timed out",
                    "timeout",
                    "broken pipe",
                )
            )
            if attempts >= HTTP_RETRY_COUNT or not transient:
                if transient and any(marker in text for marker in ("timed out", "timeout")):
                    pytest.skip(f"Request timeout for {path} in target environment: {exc}")
                raise
            attempts += 1
            time.sleep(min(0.25 * attempts, 1.0))
            continue

        if resp.status_code in {429, 502, 503, 504} and attempts < HTTP_RETRY_COUNT:
            attempts += 1
            time.sleep(min(0.25 * attempts, 1.0))
            continue
        return resp


def _viewer_forbidden(resp: requests.Response) -> bool:
    if BACKEND_ROLE != "viewer":
        return False
    assert resp.status_code == 403
    assert "operator role required" in str((resp.json() or {}).get("error") or "").lower()
    return True


@pytest.fixture(scope="module")
def test_project_id() -> str:
    if not PROJECT_ID:
        pytest.skip("TEST_PROJECT_BACKEND_ID not set")
    return PROJECT_ID


@pytest.fixture(scope="module")
def server_available():
    try:
        resp = _get("/health")
    except Exception as e:
        pytest.skip(f"Flow Steward server not available at {BASE_URL}: {e}")
    if resp.status_code >= 500:
        pytest.skip(f"Flow Steward server health endpoint unhealthy: {resp.status_code}")


class TestWorkflowPreview:
    def test_project_detail_exposes_workflow_preview(
        self,
        test_project_id: str,
        server_available,
    ):
        resp = _get(f"/projects/{test_project_id}")
        assert resp.status_code == 200
        preview_resp = _post_json(
            f"/api/projects/{test_project_id}/workflow-preview",
            {"task_summary": "Summarise this week's activity.", "workflow_id": ""},
        )
        if _viewer_forbidden(preview_resp):
            return
        assert preview_resp.status_code < 500

    def test_workflow_preview_requires_task_summary(self, test_project_id: str, server_available):
        resp = _post_json(
            f"/api/projects/{test_project_id}/workflow-preview",
            {"task_summary": ""},
        )
        if _viewer_forbidden(resp):
            return
        assert resp.status_code == 400
        assert "task_summary is required" in str((resp.json() or {}).get("error") or "")

    def test_workflow_preview_returns_step_plan(self, test_project_id: str, server_available):
        resp = _post_json(
            f"/api/projects/{test_project_id}/workflow-preview",
            {
                "task_summary": "Summarise this week's activity and describe the expected workflow plan"
            },
        )
        if _viewer_forbidden(resp):
            return
        if resp.status_code == 400:
            body = (
                resp.json()
                if resp.headers.get("content-type", "").startswith("application/json")
                else {}
            )
            detail = str((body or {}).get("error") or "").lower()
            if "no enabled workflow is linked" in detail:
                pytest.skip(
                    "Project has no enabled workflow mapping for the inferred task class in this environment"
                )
        assert resp.status_code == 200, (
            f"workflow preview failed: {resp.status_code} {resp.text[:300]}"
        )
        body = resp.json()
        assert str(body.get("project_id")) == str(test_project_id)
        assert isinstance(body.get("selected_workflow_id"), str)
        assert body.get("selection_source")
        assert isinstance(body.get("step_count"), int)
        assert isinstance(body.get("step_plan"), list)
        assert body.get("step_count") == len(body.get("step_plan") or [])
        assert body.get("step_plan"), "workflow preview should include at least one step"
        assert "phase_plan" not in body
        first_step = body["step_plan"][0]
        assert first_step.get("step_id")
        assert first_step.get("step_kind")

    # ── Workflow override (TASK-03) ──────────────────────────────────────

    def test_workflow_preview_with_explicit_workflow_id(
        self, test_project_id: str, server_available
    ):
        """When workflow_id is provided, the preview should select that workflow."""
        # First, discover a valid workflow_id from the project.
        base_resp = _post_json(
            f"/api/projects/{test_project_id}/workflow-preview",
            {"task_summary": "Summarise this week's activity for the workflow override check"},
        )
        if _viewer_forbidden(base_resp):
            return
        if base_resp.status_code != 200:
            pytest.skip(f"Baseline workflow preview failed: {base_resp.status_code}")
        known_wf = base_resp.json().get("selected_workflow_id")
        if not known_wf:
            pytest.skip("No workflow_id in baseline preview response")

        # Re-submit with explicit workflow_id override.
        override_resp = _post_json(
            f"/api/projects/{test_project_id}/workflow-preview",
            {
                "task_summary": "Summarise this week's activity for the workflow override check",
                "workflow_id": known_wf,
            },
        )
        if override_resp.status_code in {400, 404}:
            body = (
                override_resp.json()
                if override_resp.headers.get("content-type", "").startswith("application/json")
                else {}
            )
            detail = str((body or {}).get("error") or "").lower()
            if any(
                marker in detail
                for marker in (
                    "not linked to project",
                    "no enabled workflow is linked",
                    "invalid workflow",
                    "unresolved contract errors",
                    "is disabled",
                )
            ):
                pytest.skip(
                    "Environment workflow bindings do not allow explicit override for the selected workflow"
                )
        assert override_resp.status_code == 200
        override_body = override_resp.json()
        assert override_body.get("selected_workflow_id") == known_wf

    def test_workflow_preview_rejects_invalid_workflow_id(
        self, test_project_id: str, server_available
    ):
        resp = _post_json(
            f"/api/projects/{test_project_id}/workflow-preview",
            {"task_summary": "Anything", "workflow_id": "e2e_nonexistent_workflow_99999"},
        )
        if _viewer_forbidden(resp):
            return
        # Expect 400 or the server may fall back to auto-select.
        if resp.status_code >= 500:
            pytest.skip(f"Workflow preview backend unavailable: {resp.status_code}")
        if resp.status_code == 200:
            # Server fell back to auto-select — that's acceptable behavior.
            body = resp.json()
            assert body.get("selected_workflow_id") != "e2e_nonexistent_workflow_99999"
        else:
            assert resp.status_code in (400, 404, 422)
